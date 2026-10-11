"""v2 · C-1 · MessageBus — IM ↔ Agent 消息总线（骨架）

设计：docs/architecture_v2/02_runtime_core.md § 三

骨架阶段：
- Inbound：bounded queue（max_inbound 默认 1000）；channel adapter put → agent consumer get
- Outbound：listener fan-out；agent publish → 各 channel adapter 发到对应平台

骨架完整支持 publish/subscribe/get/drain，但当前 phase 没有真消费者
（v1 主路径走 HTTP /chat → SSE，不走 MessageBus）。Phase E 接 IM 时
ChannelManager 会真用上。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

log = logging.getLogger(__name__)

OutboundCallback = Callable[["OutboundMessage"], Awaitable[Any]]


@dataclass(slots=True)
class InboundMessage:
    """IM 平台 → Agent。"""

    channel: str            # "web" | "dingtalk" | "feishu" | ...
    chat_id: str            # 平台原生会话 ID
    user_id: str            # 平台用户 ID（不是柳如烟 user_id）
    text: str
    msg_type: Literal["chat", "command", "system"] = "chat"
    thread_ts: str | None = None
    topic_id: str | None = None
    files: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class OutboundMessage:
    """Agent → IM 平台。"""

    channel: str
    chat_id: str
    text: str
    is_final: bool = True              # 流式更新时 False，最后 True
    artifacts: list[str] = field(default_factory=list)
    attachments: list[Any] = field(default_factory=list)
    thread_ts: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class MessageBus:
    """bounded inbound queue + outbound fan-out listener 列表。"""

    def __init__(self, max_inbound: int = 1000) -> None:
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=max_inbound)
        self._outbound_listeners: list[OutboundCallback] = []
        self._closed = False

    # ── inbound（channel → agent）────────────────────────────────────

    async def publish_inbound(self, msg: InboundMessage) -> bool:
        """非阻塞 put。返回 False 表示队列满 → channel 应做 NACK / retry。"""
        if self._closed:
            log.warning("MessageBus.publish_inbound on closed bus, channel=%s", msg.channel)
            return False
        try:
            self._inbound.put_nowait(msg)
            return True
        except asyncio.QueueFull:
            log.warning(
                "MessageBus inbound full (size=%d), dropping channel=%s chat=%s",
                self._inbound.maxsize, msg.channel, msg.chat_id,
            )
            return False

    async def get_inbound(self) -> InboundMessage:
        """阻塞拿一条。channel manager 的 consumer loop 用。"""
        return await self._inbound.get()

    def inbound_pending(self) -> int:
        return self._inbound.qsize()

    # ── outbound（agent → channels）──────────────────────────────────

    def subscribe_outbound(self, cb: OutboundCallback) -> Callable[[], None]:
        """注册 outbound listener；返回 unsubscribe 函数。"""
        self._outbound_listeners.append(cb)

        def _un() -> None:
            try:
                self._outbound_listeners.remove(cb)
            except ValueError:
                pass

        return _un

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """fan-out 到所有 listener。异常隔离 + 不阻塞其它。"""
        if self._closed:
            log.warning("MessageBus.publish_outbound on closed bus, channel=%s", msg.channel)
            return
        results = await asyncio.gather(
            *(cb(msg) for cb in self._outbound_listeners),
            return_exceptions=True,
        )
        for cb, res in zip(self._outbound_listeners, results):
            if isinstance(res, BaseException):
                log.exception(
                    "MessageBus outbound listener failed channel=%s cb=%s",
                    msg.channel, getattr(cb, "__name__", repr(cb)),
                    exc_info=res,
                )

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def drain(self, timeout: float = 10.0) -> int:
        """优雅关闭：等 inbound queue 被消费完。返回剩余条数（0=完全 drain）。"""
        deadline = time.monotonic() + timeout
        while not self._inbound.empty():
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        return self._inbound.qsize()

    def close(self) -> None:
        self._closed = True


__all__ = [
    "InboundMessage",
    "OutboundMessage",
    "MessageBus",
    "OutboundCallback",
]
