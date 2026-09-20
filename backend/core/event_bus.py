"""EventBus：进程内的发布订阅总线

设计要点（参考 sse_manager.py 的成熟模式）：
1. **API 与 sse_manager 同构**：publish/subscribe/mark_terminal/cleanup，
   下个窗口可平滑合并升级。
2. **Channel 按前缀语义化**：
   - `session:{session_key}` — 1v1 会话流
   - `room:{room_id}` — 协作房间流
   - `agent:{agent_id}:inbox` — agent 私人邮箱（不暴露给前端）
   - `panorama` — 全局活动聚合
3. **Ring buffer 按前缀差异化**：session/room 较大（500），panorama 较小（200），
   inbox 较小（100，因为 actor 一般立刻消费）。
4. **抽象基类隔离实现**：InMemoryEventBus 是 Phase 1 实现，
   未来可加 RedisEventBus 应对多实例横向扩容。

不变量：
- event_id 在每个 channel 内单调递增（前端可用于去重 + Last-Event-ID 续传）
- 慢消费者（queue 满）会被自动踢断，不阻塞总线
- 终态后 5 分钟自动清理
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Deque,
    Dict,
    List,
    Optional,
)

from backend.core.inbox_message import BusEvent

logger = logging.getLogger(__name__)


# ─────────────────────── 常量 ───────────────────────

DEFAULT_RING_SIZE_SESSION = 500
DEFAULT_RING_SIZE_ROOM = 500
DEFAULT_RING_SIZE_PANORAMA = 200
DEFAULT_RING_SIZE_AGENT_INBOX = 100
DEFAULT_RING_SIZE_FALLBACK = 200

SUBSCRIBER_QUEUE_SIZE = 1024
TERMINAL_CLEANUP_DELAY = 300.0   # 5min
INACTIVE_TIMEOUT = 3600.0        # 1h


# ─────────────────────── 通道命名 ───────────────────────

PANORAMA_CHANNEL = "panorama"


def channel_session(session_key: str) -> str:
    return f"session:{session_key}"


def channel_room(room_id: str) -> str:
    return f"room:{room_id}"


def channel_agent_inbox(agent_id: str) -> str:
    return f"agent:{agent_id}:inbox"


def _ring_size_for(channel: str) -> int:
    if channel.startswith("session:"):
        return DEFAULT_RING_SIZE_SESSION
    if channel.startswith("room:"):
        return DEFAULT_RING_SIZE_ROOM
    if channel.startswith("agent:"):
        return DEFAULT_RING_SIZE_AGENT_INBOX
    if channel == PANORAMA_CHANNEL:
        return DEFAULT_RING_SIZE_PANORAMA
    return DEFAULT_RING_SIZE_FALLBACK


# ─────────────────────── 数据结构 ───────────────────────

@dataclass
class _Channel:
    """单条 channel 的运行时状态（仿 sse_manager._SessionChannel）"""

    name: str
    ring_size: int
    ring_buffer: Deque[BusEvent] = field(default_factory=deque)
    subscribers: List[asyncio.Queue] = field(default_factory=list)
    next_event_id: int = 1
    terminal: bool = False
    terminal_at: Optional[float] = None
    last_activity: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.ring_buffer.maxlen != self.ring_size:
            self.ring_buffer = deque(self.ring_buffer, maxlen=self.ring_size)


# ─────────────────────── 抽象基类 ───────────────────────

class EventBus(abc.ABC):
    """事件总线抽象。Phase 1 用 InMemoryEventBus，未来可换 RedisEventBus。"""

    @abc.abstractmethod
    async def publish(self, channel: str, event: BusEvent) -> int:
        """发布事件，返回分配的 event_id"""

    @abc.abstractmethod
    def subscribe(
        self,
        channel: str,
        last_event_id: Optional[int] = None,
    ) -> AsyncIterator[BusEvent]:
        """订阅事件流（含断线续传）"""

    @abc.abstractmethod
    def has_subscriber(self, channel: str) -> bool:
        ...

    @abc.abstractmethod
    def mark_terminal(self, channel: str) -> None:
        ...

    @abc.abstractmethod
    def cleanup(self, now: Optional[float] = None) -> int:
        ...

    @abc.abstractmethod
    def stats(self) -> Dict[str, Any]:
        ...


# ─────────────────────── 内存实现 ───────────────────────

class InMemoryEventBus(EventBus):
    """单进程内存版 EventBus

    线程模型：单 event loop，所有方法都假定在同一 loop 中调用。
    不加锁：Python GIL + 单 loop 已保证 dict/list 操作原子性。
    """

    def __init__(
        self,
        *,
        terminal_cleanup_delay: float = TERMINAL_CLEANUP_DELAY,
        inactive_timeout: float = INACTIVE_TIMEOUT,
        subscriber_queue_size: int = SUBSCRIBER_QUEUE_SIZE,
    ) -> None:
        self._channels: Dict[str, _Channel] = {}
        self._terminal_cleanup_delay = terminal_cleanup_delay
        self._inactive_timeout = inactive_timeout
        self._subscriber_queue_size = subscriber_queue_size

    # ───── channel 生命周期 ─────

    def _ensure_channel(self, name: str) -> _Channel:
        ch = self._channels.get(name)
        if ch is None:
            ch = _Channel(name=name, ring_size=_ring_size_for(name))
            self._channels[name] = ch
        return ch

    def has_subscriber(self, channel: str) -> bool:
        ch = self._channels.get(channel)
        return bool(ch and ch.subscribers)

    def channel_count(self) -> int:
        return len(self._channels)

    def mark_terminal(self, channel: str) -> None:
        ch = self._channels.get(channel)
        if ch is None or ch.terminal:
            return
        ch.terminal = True
        ch.terminal_at = time.time()
        for q in list(ch.subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # ───── 发布 ─────

    async def publish(self, channel: str, event: BusEvent) -> int:
        ch = self._ensure_channel(channel)
        ch.last_activity = time.time()

        # 分配单调 event_id
        event.event_id = ch.next_event_id
        ch.next_event_id += 1
        if event.timestamp is None:
            event.timestamp = ch.last_activity

        # 1) ring buffer
        ch.ring_buffer.append(event)

        # 2) fan-out
        dead: List[asyncio.Queue] = []
        for q in ch.subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)

        for q in dead:
            if q in ch.subscribers:
                ch.subscribers.remove(q)
            # 队列满才进 dead，先清空给 sentinel 让位（被踢的订阅者反正要走 ring buffer 续传）
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
            logger.warning(
                "EventBus: kicked slow subscriber on channel=%s (queue full)",
                channel,
            )

        return event.event_id

    # ───── 订阅 ─────

    async def subscribe(
        self,
        channel: str,
        last_event_id: Optional[int] = None,
    ) -> AsyncIterator[BusEvent]:
        ch = self._ensure_channel(channel)
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._subscriber_queue_size)
        ch.subscribers.append(queue)
        ch.last_activity = time.time()

        try:
            # 1) backlog 回放（如果 last_event_id 不在窗口，从 ring buffer 头开始）
            if last_event_id is not None:
                for ev in list(ch.ring_buffer):
                    if ev.event_id is not None and ev.event_id > last_event_id:
                        yield ev

            # 终态且 backlog 已放完 → 立即退出
            if ch.terminal:
                return

            # 2) 实时
            while True:
                ev = await queue.get()
                if ev is None:
                    return
                yield ev
        finally:
            if queue in ch.subscribers:
                ch.subscribers.remove(queue)

    # ───── 清理 + 观测 ─────

    def cleanup(self, now: Optional[float] = None) -> int:
        ts = now if now is not None else time.time()
        to_remove: List[str] = []
        for name, ch in self._channels.items():
            if (
                ch.terminal
                and ch.terminal_at is not None
                and (ts - ch.terminal_at) > self._terminal_cleanup_delay
            ):
                to_remove.append(name)
            elif (
                not ch.terminal
                and not ch.subscribers
                and (ts - ch.last_activity) > self._inactive_timeout
            ):
                to_remove.append(name)

        for name in to_remove:
            ch = self._channels.pop(name, None)
            if ch:
                for q in ch.subscribers:
                    try:
                        q.put_nowait(None)
                    except asyncio.QueueFull:
                        pass
        return len(to_remove)

    def stats(self) -> Dict[str, Any]:
        total_subs = sum(len(c.subscribers) for c in self._channels.values())
        total_buffered = sum(len(c.ring_buffer) for c in self._channels.values())
        terminal = sum(1 for c in self._channels.values() if c.terminal)
        return {
            "channels": len(self._channels),
            "terminal_channels": terminal,
            "total_subscribers": total_subs,
            "total_buffered_events": total_buffered,
        }


# ─────────────────────── 全局单例 ───────────────────────
#
# 我们选择"单实例 + 多前缀"而不是 handover 中的"三个独立 bus"，
# 因为后者会让跨类型监控/清理/未来 Redis 切换更复杂。
# 三个 helper 仍然保留，是同一实例的语义入口。

_bus_singleton: Optional[InMemoryEventBus] = None


def _get_bus_singleton() -> InMemoryEventBus:
    global _bus_singleton
    if _bus_singleton is None:
        _bus_singleton = InMemoryEventBus()
    return _bus_singleton


def reset_bus_singleton_for_testing() -> None:
    """仅供测试使用：重置单例"""
    global _bus_singleton
    _bus_singleton = None


def get_session_bus() -> EventBus:
    """获取 session 维度的 EventBus（语义入口）"""
    return _get_bus_singleton()


def get_room_bus() -> EventBus:
    """获取 room 维度的 EventBus（语义入口）"""
    return _get_bus_singleton()


def get_panorama_bus() -> EventBus:
    """获取 panorama 维度的 EventBus（语义入口）"""
    return _get_bus_singleton()


def get_default_bus() -> EventBus:
    """获取默认 EventBus（任何 channel 都可用）"""
    return _get_bus_singleton()
