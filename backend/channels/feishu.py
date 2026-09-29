"""v2 · G-feishu · FeishuChannel — 飞书 Lark IM 接入（lark-oapi 长连接）

设计：plan `Phase G Feishu IM 接入 · 实施 plan（2026-05-01）`

跟钉钉 stream mode 同思路（云端长连接 + 不需要公网 IP），但飞书 SDK 模型
跟钉钉**有两个根本差异**：

1. ``lark.ws.Client.start()`` 是 **sync 阻塞**——必须 ``loop.run_in_executor``
   到 thread pool 跑，不能 ``asyncio.create_task``（钉钉那条路只对钉钉有效）
2. handler 是 **sync function**——SDK 内部 thread 调过来；adapter 内用
   ``asyncio.run_coroutine_threadsafe`` bridge 回主 asyncio loop

config schema (``ChannelConfig.config``)：

- ``app_id``: str       飞书应用 AppID
- ``app_secret``: str   飞书应用 AppSecret（secret，前端 PUT 走 mask 脱敏）
- ``default_agent``: str  该 channel 默认路由到哪个 agent_id
- ``domain``: "feishu" | "lark"  默认 "feishu"（中国版）；"lark" 走 INTL

平台行为约束（来自 lark-oapi）：

- ``ws_client.start()`` sync 阻塞 + 自动重连
- handler 收到事件 3 秒内必须 ack（不返回视为失败 → 飞书重投 5s/5min/1h/6h
  四次后丢弃）。我们 handler 用 ``run_coroutine_threadsafe`` 立即返回（fire-
  and-forget），handler 函数本身瞬间 done；后续 LLM 处理时间长不影响 ack
- 发消息走独立 OpenAPI client：``api_client.im.v1.message.create(req)``
  也是 sync——用 ``run_in_executor`` 包成 awaitable
- 消息 content 是 JSON 字符串 ``'{"text":"hi","mentions":[...]}'``，要解 JSON

session_mapper：``InboundRouter`` 默认用 ``(channel, chat_id)`` → session_id；
飞书 chat_id 是稳定 ID（``oc_xxx`` 群 / ``ou_xxx`` 私聊），同 bot 在多个群 =
多个 session 自然隔离。

异常路径：

- ``start()`` AppID/AppSecret 错 / WSS 连不上 → ``ChannelStartError``
- ``send()`` ``message.create`` 抛 / response.success()=False → ``ChannelSendError``
- ``_on_inbound_async`` 异常 → log.exception 但不抛，避免单条坏消息让 SDK
  worker thread 整体停（飞书 SDK handler 异常会自动 ack 失败 → 触发重投，
  消息丢失风险）

可插拔：

- 不装 lark-oapi 也能 import 本模块（``import lark_oapi`` 只在 ``start()``）
- 删本文件 backend 启动正常（``_lazy_feishu`` lazy import 失败被 swallow）
"""

from __future__ import annotations

import asyncio
import json
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


class FeishuChannel(ChannelAdapter):
    """飞书 / Lark protocol adapter（基于 lark-oapi WebSocket 长连接）。"""

    channel = "feishu"

    def __init__(self, bus: Any, config: dict[str, Any]) -> None:
        super().__init__(bus, config)

        app_id = (config.get("app_id") or "").strip()
        app_secret = (config.get("app_secret") or "").strip()
        if not app_id or not app_secret:
            raise ChannelStartError(
                "FeishuChannel requires app_id + app_secret in config"
            )
        self._app_id = app_id
        self._app_secret = app_secret
        self._default_agent = str(config.get("default_agent") or "default")
        # "feishu" 中国版（默认） / "lark" 国际版
        self._domain = str(config.get("domain") or "feishu").lower()

        # SDK 状态
        self._ws_client: Any | None = None       # lark.ws.Client（接消息）
        self._api_client: Any | None = None      # lark.Client（发消息）
        self._ws_future: asyncio.Future | None = None  # run_in_executor handle
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsub: Any | None = None

        # chat_id → 最近一条 incoming message_id（备未来 reply mode）
        self._last_message_id: dict[str, str] = {}

    # ── ChannelAdapter 钩子 ────────────────────────────────────────────

    async def start(self) -> None:
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import P2ImMessageReceiveV1  # noqa: F401
        except ImportError as e:
            raise ChannelStartError(
                f"lark-oapi SDK not installed: {e}; "
                "run: pip install lark-oapi"
            ) from e

        try:
            self._loop = asyncio.get_running_loop()

            # 1. OpenAPI client（发消息）—— domain 决定中国版/国际版
            api_builder = (
                lark.Client.builder()
                    .app_id(self._app_id)
                    .app_secret(self._app_secret)
            )
            if self._domain == "lark":
                # 国际版用 LARK_DOMAIN_INTL；中国版默认不调 .domain()
                intl = getattr(lark, "LARK_DOMAIN_INTL", None)
                if intl is not None:
                    api_builder = api_builder.domain(intl)
            self._api_client = api_builder.build()

            # 2. handler bridge：sync handler → 主 loop async _on_inbound
            #    SDK worker thread 调过来；run_coroutine_threadsafe 立即返回
            #    让 handler 内部 fast-ack（飞书 3s 超时窗口）
            adapter_self = self

            def _sync_bridge(data: Any) -> None:
                loop = adapter_self._loop
                if loop is not None and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        adapter_self._on_inbound_async(data), loop,
                    )

            event_handler = (
                lark.EventDispatcherHandler.builder("", "")
                    .register_p2_im_message_receive_v1(_sync_bridge)
                    .build()
            )

            # 3. WSS client + spawn 到 thread pool（sync 阻塞，不能 create_task）
            self._ws_client = lark.ws.Client(
                self._app_id,
                self._app_secret,
                event_handler=event_handler,
            )
            self._unsub = self.bus.subscribe_outbound(self.send)
            self._ws_future = self._loop.run_in_executor(
                None, self._ws_client.start,
            )

            self.status = ChannelStatus.RUNNING
            log.info(
                "[FeishuChannel] started; app_id=%s domain=%s default_agent=%s",
                self._app_id[:8] + "***",
                self._domain,
                self._default_agent,
            )
        except Exception as e:
            self.last_error = str(e)
            raise ChannelStartError(f"FeishuChannel.start failed: {e}") from e

    async def stop(self) -> None:
        # 1. unsubscribe outbound（停接新 outbound）
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:
                log.exception("[FeishuChannel] unsubscribe failed")
            self._unsub = None

        # 2. cancel WSS future
        # 注意：lark-oapi 当前版本没暴露公开 stop()/close() —— 只能 cancel
        # future（让 thread 抛 CancelledError）+ 等内部资源 GC。如未来 SDK
        # 提供公开 close 加一刀。
        if self._ws_future is not None and not self._ws_future.done():
            self._ws_future.cancel()
            try:
                await self._ws_future
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_future = None

        self._ws_client = None
        self._api_client = None
        self._last_message_id.clear()
        self.status = ChannelStatus.STOPPED
        log.info("[FeishuChannel] stopped")

    async def send(self, msg: OutboundMessage) -> None:
        if msg.channel != self.channel:
            return  # 跨平台过滤
        if self._api_client is None or self._loop is None:
            log.debug("[FeishuChannel] send: api_client/loop is None; skip")
            return

        text = (msg.text or "").strip()
        if not text:
            return

        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            request = (
                CreateMessageRequest.builder()
                    .receive_id_type("chat_id")
                    .request_body(
                        CreateMessageRequestBody.builder()
                            .receive_id(msg.chat_id)
                            .msg_type("text")
                            .content(lark.JSON.marshal({"text": text}))
                            .build()
                    )
                    .build()
            )

            # OpenAPI sync → run_in_executor 包成 awaitable
            response = await self._loop.run_in_executor(
                None, self._api_client.im.v1.message.create, request,
            )
            if not response.success():
                code = getattr(response, "code", "?")
                err_msg = getattr(response, "msg", "?")
                raise ChannelSendError(
                    f"FeishuChannel.send failed: code={code} msg={err_msg}"
                )
        except ChannelSendError:
            raise
        except Exception as e:
            raise ChannelSendError(
                f"FeishuChannel.send raised: {e}"
            ) from e

    # ── 内部：SDK worker thread 通过 run_coroutine_threadsafe 调过来 ───

    async def _on_inbound_async(self, data: Any) -> None:
        """飞书 P2ImMessageReceiveV1 事件 → 转 InboundMessage 进 bus。

        Worker thread 通过 ``run_coroutine_threadsafe(_, self._loop)`` 调度；
        本 coroutine 在主 asyncio loop 跑。异常 swallow 不外抛——单条坏消息
        不应让 SDK worker thread 整体崩。
        """
        try:
            event = data.event
            message = event.message

            chat_id = str(getattr(message, "chat_id", "") or "")
            message_id = str(getattr(message, "message_id", "") or "")
            content_raw = getattr(message, "content", "") or "{}"

            # sender_id 在 event.sender.sender_id（嵌套）；优先 open_id（最稳）
            sender_id = ""
            try:
                sender = getattr(event, "sender", None)
                if sender is not None:
                    sid = getattr(sender, "sender_id", None)
                    if sid is not None:
                        sender_id = str(getattr(sid, "open_id", "") or "")
            except Exception:
                pass

            if not chat_id or not sender_id:
                log.debug(
                    "[FeishuChannel] _on_inbound_async: missing chat_id/sender_id; drop"
                )
                return

            # parse content：'{"text":"hi"}'
            text = ""
            try:
                content_obj = json.loads(content_raw)
                if isinstance(content_obj, dict):
                    text = str(content_obj.get("text", "") or "")
            except Exception:
                # 非 JSON content（图片/文件/post/...）— text 留空，metadata 标 type
                pass

            # 缓存 message_id 备未来 reply mode
            self._last_message_id[chat_id] = message_id

            inbound = InboundMessage(
                channel=self.channel,
                chat_id=chat_id,
                user_id=sender_id,
                text=text,
                metadata={
                    "config_id": self.config_key,
                    "message_id": message_id,
                    "chat_type": str(getattr(message, "chat_type", "") or ""),
                    "message_type": str(getattr(message, "message_type", "") or ""),
                },
            )

            queued = await self.bus.publish_inbound(inbound)
            if not queued:
                log.warning(
                    "[FeishuChannel] bus.publish_inbound dropped (queue full); "
                    "chat=%s user=%s",
                    chat_id, sender_id,
                )
        except Exception:
            log.exception("[FeishuChannel] _on_inbound_async failed (swallow)")


__all__ = ["FeishuChannel"]
