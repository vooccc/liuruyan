"""v2 · G'-2 · MatrixChannel — Matrix 协议 IM 接入（matrix-nio 客户端）

设计：docs/architecture_v2/phase_g_matrix_self_hosted.md § 4 Slice 2

跟自建 Synapse server 配对：bot 账号通过 access_token 登录，订阅自己加入
的所有房间，把 ``RoomMessageText`` 转成 ``InboundMessage`` 进 MessageBus；
``OutboundMessage`` 通过 ``room_send`` 直发回房间。

config schema (``ChannelConfig.config``)：

- ``homeserver_url``: str  e.g. "https://matrix.yourdomain.cn"
- ``user_id``: str         bot Matrix ID, e.g. "@liuruyan-bot:yourdomain.cn"
- ``access_token``: str    bot 账号的 access_token（secret，前端 PUT 走脱敏）
- ``default_agent``: str   该 channel 实例默认路由到哪个 agent_id
- ``e2ee``: bool = False   第一刀关；Slice 6 再启用

平台行为约束（来自 matrix-nio）：

- ``AsyncClient.sync_forever(timeout=...)`` 是 async + 阻塞 + 自动重连
- ``add_event_callback(handler, RoomMessageText)`` 注册同步流回调；handler
  可以是 sync 也可以是 async（nio 自动适配 async）
- bot 自己发的消息**也会**进 sync 流——必须用 ``event.sender == self._user_id``
  自己跳过，不然 echo 死循环
- 不同于钉钉：matrix 一条 ``room_send`` 直接发完整内容；不需要 chunks 累积。
  因此 ``send`` 直接逐 chunk 发——同一轮 LLM 输出会变成多条房间消息，但
  Matrix 客户端通常合并展示，UX 可接受；Slice 5 真机验收时如不行再合并

session_mapper：``InboundRouter`` 默认会用 ``(channel, chat_id)`` → session_id
的映射，``chat_id`` 在 matrix 里就是 room_id。同一个 bot 在多个房间 = 多个
session，自然隔离。

异常路径：

- ``start()`` access_token 无效 / homeserver 连不上 → ``ChannelStartError``；
  ChannelManager 标 ERROR
- ``send()`` ``room_send`` 抛 → ``ChannelSendError`` 上抛
- ``_on_message`` 内部异常 → log.exception 但**不抛**，避免单条坏消息让
  ``sync_forever`` 整体崩

可插拔：

- 不装 matrix-nio 也能 import 本模块（``from nio import ...`` 只在 ``start()``
  内执行）
- 工程视角看：删本文件 backend 启动正常；删 store 里 matrix 配置 adapter
  不启动；其它 channel 互不影响
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from liuruyan.buses import InboundMessage, OutboundMessage
from liuruyan.channels import (
    ChannelAdapter,
    ChannelSendError,
    ChannelStartError,
    ChannelStatus,
)

log = logging.getLogger(__name__)


class MatrixChannel(ChannelAdapter):
    """Matrix protocol adapter（基于 matrix-nio AsyncClient）。"""

    channel = "matrix"

    def __init__(self, bus: Any, config: dict[str, Any]) -> None:
        super().__init__(bus, config)

        homeserver_url = (config.get("homeserver_url") or "").strip()
        user_id = (config.get("user_id") or "").strip()
        access_token = (config.get("access_token") or "").strip()
        if not homeserver_url or not user_id or not access_token:
            raise ChannelStartError(
                "MatrixChannel requires homeserver_url + user_id + access_token "
                "in config"
            )
        self._homeserver_url = homeserver_url
        self._user_id = user_id
        self._access_token = access_token
        self._default_agent = str(config.get("default_agent") or "default")

        # SDK 状态
        self._client: Any | None = None
        self._sync_task: asyncio.Task | None = None
        self._unsub: Any | None = None

    # ── ChannelAdapter 钩子 ────────────────────────────────────────────

    async def start(self) -> None:
        try:
            from nio import AsyncClient, RoomMessageText
        except ImportError as e:
            raise ChannelStartError(
                f"matrix-nio SDK not installed: {e}; "
                "run: pip install matrix-nio"
            ) from e

        try:
            self._client = AsyncClient(self._homeserver_url, self._user_id)
            self._client.access_token = self._access_token

            self._client.add_event_callback(self._on_message, RoomMessageText)

            # outbound listener（bus.publish_outbound fan-out 进来）
            self._unsub = self.bus.subscribe_outbound(self.send)

            # spawn sync_forever task（30s long-poll；连接断了 nio 自动重连）
            self._sync_task = asyncio.create_task(
                self._client.sync_forever(timeout=30000),
                name="matrix_sync",
            )
            self.status = ChannelStatus.RUNNING
            log.info(
                "[MatrixChannel] started; user=%s homeserver=%s default_agent=%s",
                self._user_id,
                self._homeserver_url,
                self._default_agent,
            )
        except Exception as e:
            self.last_error = str(e)
            raise ChannelStartError(f"MatrixChannel.start failed: {e}") from e

    async def stop(self) -> None:
        # 1. unsubscribe outbound（停止收新消息）
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:
                log.exception("[MatrixChannel] unsubscribe failed")
            self._unsub = None

        # 2. cancel sync task
        if self._sync_task is not None:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sync_task = None

        # 3. close nio client（释放 httpx session）
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                log.exception("[MatrixChannel] client.close failed")
            self._client = None

        self.status = ChannelStatus.STOPPED
        log.info("[MatrixChannel] stopped")

    async def send(self, msg: OutboundMessage) -> None:
        if msg.channel != self.channel:
            return  # 跨平台过滤

        if self._client is None:
            log.debug("[MatrixChannel] send: client is None; skip")
            return

        text = msg.text or ""
        if not text:
            # 仅终结帧（is_final=True 但无文字）→ 不发空消息
            return

        room_id = msg.chat_id
        try:
            await self._client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": text},
            )
        except Exception as e:
            raise ChannelSendError(
                f"MatrixChannel.send room_send failed: {e}"
            ) from e

    # ── 内部：nio sync 流回调 ──────────────────────────────────────────

    async def _on_message(self, room: Any, event: Any) -> None:
        """matrix-nio sync_forever 拿到 RoomMessageText 时调过来。

        转 InboundMessage 进 bus；自己发的消息（self-echo）跳过。
        """
        try:
            sender = str(getattr(event, "sender", "") or "")

            # self-echo skip（bot 自己发的消息也会进 sync 流）
            if sender == self._user_id:
                return

            room_id = str(getattr(room, "room_id", "") or "")
            body = str(getattr(event, "body", "") or "")
            event_id = str(getattr(event, "event_id", "") or "")
            room_name = str(getattr(room, "display_name", "") or "")

            if not room_id or not sender:
                log.debug(
                    "[MatrixChannel] _on_message: missing room_id/sender; drop"
                )
                return

            inbound = InboundMessage(
                channel=self.channel,
                chat_id=room_id,
                user_id=sender,
                text=body,
                metadata={
                    "config_id": self.config_key,
                    "room_name": room_name,
                    "event_id": event_id,
                },
            )

            queued = await self.bus.publish_inbound(inbound)
            if not queued:
                log.warning(
                    "[MatrixChannel] bus.publish_inbound dropped (queue full); "
                    "room=%s user=%s",
                    room_id, sender,
                )
        except Exception:
            log.exception("[MatrixChannel] _on_message failed (swallow)")


__all__ = ["MatrixChannel"]
