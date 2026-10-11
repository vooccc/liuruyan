"""v2 · E-4'a · WebChannel — 内置 web channel adapter（HTTP 入站 + SSE 出站）

设计：docs/architecture_v2/01_master_plan.md Phase E

WebChannel 是 ChannelAdapter 的参考实现——不依赖任何外部 IM SDK，纯 HTTP +
SSE，让 v2 channels 全链路能用 curl 端到端测：

```
[client]
  ├─ POST /v2/channels/web/inbound  → WebChannel.publish_inbound(msg)
  │                                   → bus.publish_inbound  → InboundRouter
  │                                   → dispatch  → bus.publish_outbound
  │                                   → WebChannel.send(msg)
  │                                   → per-chat queue
  └─ GET /v2/channels/web/outbound/{chat_id}  → WebChannel.stream(chat_id)
                                                → SSE 流式拿 OutboundMessage
```

routes 层（gateway）调 WebChannel 的两个入口：
- ``publish_inbound(msg)``：上行（用户消息进来）
- ``stream(chat_id)``：下行（SSE 拿当前 chat 的 outbound 流）

跟其它 ChannelAdapter（dingtalk / feishu）的区别：
- 它们是"主动连"模式（start 时建 WSS 连接，被动收 webhook）
- WebChannel 是"被动接"模式（gateway routes 把 HTTP 请求转给 adapter）

设计要点：
- per-chat_id ``asyncio.Queue`` 隔离消息流（不同浏览器 tab 不串）
- subscribe_outbound 注册 ``self.send`` 让 bus.publish_outbound 自然路由进来
- send() 按 ``msg.channel == self.channel`` 过滤——只接 web 频道的 outbound
- stream(chat_id) async iterator：客户端断开 / cancel 时自然结束（asyncio.Queue.get
  抛 CancelledError）
- 没活跃订阅的 chat_id 上 send 直接 drop（不缓存，避免内存泄漏）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from liuruyan.buses import OutboundMessage
from liuruyan.channels import ChannelAdapter, ChannelStatus

log = logging.getLogger(__name__)


# 单 chat_id 队列默认 maxsize；超过 → 老消息 drop（避免慢客户端把内存吃爆）
_DEFAULT_QUEUE_MAXSIZE = 256


class WebChannel(ChannelAdapter):
    """HTTP + SSE 风格的 channel adapter。

    config schema（从 ChannelStore 来）：
    - ``default_agent``: str  路由到的 agent_id
    - ``queue_maxsize``: int  per-chat queue maxsize（可选，默认 256）
    """

    channel = "web"

    def __init__(self, bus: Any, config: dict[str, Any]) -> None:
        super().__init__(bus, config)
        self._queue_maxsize: int = int(
            config.get("queue_maxsize") or _DEFAULT_QUEUE_MAXSIZE
        )
        # chat_id → asyncio.Queue（仅活跃订阅时存在）
        self._chat_queues: dict[str, asyncio.Queue[OutboundMessage]] = {}
        # subscribe_outbound 返回的 unsubscribe（stop 时 unsub）
        self._unsub: Any | None = None

    # ── ChannelAdapter 钩子 ──────────────────────────────────────────────

    async def start(self) -> None:
        """注册 outbound listener；不建立平台连接（HTTP 是被动的）。"""
        self._unsub = self.bus.subscribe_outbound(self.send)
        self.status = ChannelStatus.RUNNING
        log.info(
            "[WebChannel] started; queue_maxsize=%d", self._queue_maxsize,
        )

    async def stop(self) -> None:
        """取消 listener + 唤醒所有 stream 让它们结束。idempotent。"""
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:
                log.exception("[WebChannel] unsubscribe failed")
            self._unsub = None

        # 唤醒所有 stream：put 一个 sentinel（None）让它们跳出循环
        # （sentinel 在 stream 内部被识别 + 退出）
        for chat_id, q in list(self._chat_queues.items()):
            try:
                q.put_nowait(_STREAM_END_SENTINEL)
            except asyncio.QueueFull:
                # queue 满 → 直接清空让 stream 自然挂
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                try:
                    q.put_nowait(_STREAM_END_SENTINEL)
                except asyncio.QueueFull:
                    pass

        self._chat_queues.clear()
        self.status = ChannelStatus.STOPPED

    async def send(self, msg: OutboundMessage) -> None:
        """OutboundCallback：bus.publish_outbound fan-out 时调过来。

        - 跨平台过滤：只处理 ``msg.channel == 'web'``
        - 没订阅的 chat_id：drop（不缓存——客户端没接 SSE 流就丢消息，正常 IM
          语义都这样：没在线就丢）
        - queue 满：drop 老消息（避免慢客户端拖死）
        """
        if msg.channel != self.channel:
            return  # 不是 web 频道的 outbound，跳过

        q = self._chat_queues.get(msg.chat_id)
        if q is None:
            # 没活跃 stream → 静默 drop
            log.debug(
                "[WebChannel] no active stream for chat=%s; drop outbound",
                msg.chat_id,
            )
            return

        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            # 慢客户端：drop 最老的，put 新的（保证最新消息能流到）
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning(
                    "[WebChannel] queue full and drop-then-put failed chat=%s",
                    msg.chat_id,
                )

    # ── routes 层调的入口 ───────────────────────────────────────────────

    async def publish_inbound(self, msg) -> bool:
        """让 routes 层把 HTTP POST 收到的 InboundMessage 进 MessageBus。

        薄壳——其实就是 ``bus.publish_inbound(msg)`` 的 alias，只是放在
        adapter 上更符合"channel 是消息的双向门户"语义（adapter 负责双向）。

        额外 enrich：补上 ``config_id`` 让 InboundRouter 精确路由到本配置
        （多账号场景关键）。routes 层不需要知道 config_id；adapter 自注入。
        """
        if self.config_key and not msg.metadata.get("config_id"):
            msg.metadata["config_id"] = self.config_key
        return await self.bus.publish_inbound(msg)

    async def stream(
        self, chat_id: str, *, queue_maxsize: int | None = None,
    ) -> AsyncIterator[OutboundMessage]:
        """SSE 流：取该 chat_id 的 OutboundMessage 流，直到 stop / 客户端断开。

        async iterator 用法：
        ```python
        async for msg in adapter.stream(chat_id):
            yield serialize_sse(msg)
        ```

        chat_id 不存在时**新建** queue（被动订阅）；调用方 cancel 时清掉 queue。
        重复订阅同 chat_id 会**复用**已有 queue（多 SSE 客户端共享流——但通常
        一个 chat 只有一个客户端订阅）。
        """
        q = self._chat_queues.get(chat_id)
        if q is None:
            maxsize = queue_maxsize if queue_maxsize is not None else self._queue_maxsize
            q = asyncio.Queue(maxsize=maxsize)
            self._chat_queues[chat_id] = q
            log.debug("[WebChannel] new stream chat=%s maxsize=%d", chat_id, maxsize)

        try:
            while True:
                try:
                    msg = await q.get()
                except asyncio.CancelledError:
                    # 客户端断开 / FastAPI cancel
                    raise
                if msg is _STREAM_END_SENTINEL:
                    # adapter.stop() 触发的清场
                    return
                yield msg
        finally:
            # 客户端断开 → 清掉 queue（避免泄漏）；adapter.stop 已清就 no-op
            self._chat_queues.pop(chat_id, None)


# stream 终止哨兵——adapter.stop() 时 put 进 queue 让 stream 跳出
class _StreamEndSentinel:
    """单例 sentinel（is 比较）。"""


_STREAM_END_SENTINEL = _StreamEndSentinel()


__all__ = ["WebChannel"]
