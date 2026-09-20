"""
SSE (Server-Sent Events) 事件总线
用于向前端推送实时事件（子代理进度、工具调用等）

设计原则：
1. 独立模块，不依赖 worker，各自演化
2. 借鉴 worker.py 验证过的 pub/sub 模式：backlog 回放、终态清理、bounded queue、多订阅者
3. 核心健壮性：
   - 队列不存在时预创建缓冲（消除 race condition）
   - Ring buffer 保留最近事件（断线续传）
   - 单调 event_id（前端去重/排序/Last-Event-ID 续传）
   - Bounded queue（防内存爆炸）
   - 终态后 5 分钟自动清理
"""
import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Deque, Dict, List, Optional


# ─────────────────────────── 常量 ───────────────────────────

RING_BUFFER_SIZE = 200           # 每个 session 保留最近 N 条事件（断线续传用）
EVENT_SEQUENCE_RESERVATION_SIZE = 256
SUBSCRIBER_QUEUE_SIZE = 1024     # 单个订阅者 queue 上限（防慢消费者拖垮）
TERMINAL_CLEANUP_DELAY = 300.0   # 终态 session 5 分钟后清理（对齐 worker）
INACTIVE_TIMEOUT = 3600.0        # 1 小时不活跃自动清理


# ─────────────────────────── 数据结构 ───────────────────────────

@dataclass
class _SessionChannel:
    """单个 session 的事件频道"""
    session_id: str
    ring_buffer: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=RING_BUFFER_SIZE))
    subscribers: List[asyncio.Queue] = field(default_factory=list)
    next_event_id: int = 1
    reserved_until: int = 1
    reservation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    terminal: bool = False           # 终态标记（engine 任务全部结束）
    terminal_at: Optional[float] = None
    last_activity: float = field(default_factory=time.time)


# ─────────────────────────── 核心管理器 ───────────────────────────

class SSEManager:
    """SSE 事件总线（会话级别）

    线程模型：单事件循环，所有操作在同一 event loop 中。
    不加锁：Python GIL + 单 loop 已保证 dict/list 操作原子性。
    """

    def __init__(self) -> None:
        self._channels: Dict[str, _SessionChannel] = {}

    # ─────── 频道生命周期 ───────

    def _ensure_channel(self, session_id: str) -> _SessionChannel:
        """获取或创建频道（内部使用）"""
        ch = self._channels.get(session_id)
        if ch is None:
            ch = _SessionChannel(session_id=session_id)
            self._channels[session_id] = ch
        return ch

    def mark_terminal(self, session_id: str) -> None:
        """标记频道为终态（所有 engine 任务结束后调用）

        终态后订阅者仍可回放 backlog，但新 send_event 会记录 last_activity。
        5 分钟后由 cleanup 清理。
        """
        ch = self._channels.get(session_id)
        if ch is None or ch.terminal:
            return
        ch.terminal = True
        ch.terminal_at = time.time()
        # 向所有活跃订阅者推 None sentinel，优雅终止
        for q in list(ch.subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # ─────── 事件发送 ───────

    async def _ensure_event_sequence_reservation(
        self,
        session_id: str,
        ch: _SessionChannel,
    ) -> None:
        if ch.next_event_id < ch.reserved_until:
            return
        async with ch.reservation_lock:
            if ch.next_event_id < ch.reserved_until:
                return
            from .session import get_session_manager
            reservation = await get_session_manager().reserve_event_sequences(
                session_id,
                EVENT_SEQUENCE_RESERVATION_SIZE,
            )
            if reservation is not None:
                ch.next_event_id, ch.reserved_until = reservation
                return
            ch.reserved_until = ch.next_event_id + EVENT_SEQUENCE_RESERVATION_SIZE

    async def send_event(
        self,
        session_id: str,
        event: Dict[str, Any],
        event_type: Optional[str] = None,
    ) -> int:
        """发送事件到指定会话

        核心改进：频道不存在时**预创建并缓冲**，消除 race condition。

        Returns:
            分配的 event_id
        """
        ch = self._ensure_channel(session_id)
        await self._ensure_event_sequence_reservation(session_id, ch)
        ch.last_activity = time.time()
        stamped_event = dict(event)

        # 补齐 type 与时间戳
        if event_type and "type" not in stamped_event:
            stamped_event["type"] = event_type
        if "timestamp" not in stamped_event:
            stamped_event["timestamp"] = ch.last_activity

        # 分配单调 event_id
        event_id = ch.next_event_id
        ch.next_event_id += 1
        stamped_event["event_id"] = event_id
        stamped_event["event_seq"] = event_id

        # 1) 写入 ring buffer（断线续传源）
        ch.ring_buffer.append(stamped_event)

        # 2) fan-out 到所有活跃订阅者（慢消费者保护：满了就丢给那个订阅者，不阻塞总线）
        dead_queues: List[asyncio.Queue] = []
        for q in ch.subscribers:
            try:
                q.put_nowait(stamped_event)
            except asyncio.QueueFull:
                # 慢消费者：断开订阅，让其重连后走 ring buffer 续传
                dead_queues.append(q)

        for q in dead_queues:
            if q in ch.subscribers:
                ch.subscribers.remove(q)
            try:
                q.put_nowait(None)  # 通知订阅协程退出
            except asyncio.QueueFull:
                pass

        return event_id

    # ─────── 订阅 ───────

    def get_replay_plan(
        self,
        session_id: str,
        last_event_id: Optional[int],
    ) -> Dict[str, Any]:
        ch = self._ensure_channel(session_id)
        latest_event_id = ch.next_event_id - 1
        oldest_event_id = (
            ch.ring_buffer[0].get("event_id")
            if ch.ring_buffer else None
        )
        if last_event_id is None:
            mode = "live"
        elif last_event_id > latest_event_id:
            mode = "full_snapshot"
        elif oldest_event_id is None and last_event_id < latest_event_id:
            mode = "full_snapshot"
        elif oldest_event_id is not None and last_event_id < oldest_event_id - 1:
            mode = "full_snapshot"
        else:
            mode = "tail"
        return {
            "mode": mode,
            "cursor": last_event_id,
            "oldest_event_id": oldest_event_id,
            "latest_event_id": latest_event_id,
        }

    async def subscribe(
        self,
        session_id: str,
        last_event_id: Optional[int] = None,
        include_replay_control: bool = False,
    ) -> AsyncIterator[Dict[str, Any]]:
        """订阅会话事件流

        Args:
            session_id: 会话 ID
            last_event_id: 客户端已收到的最大 event_id。若仍在 ring window 内，
                          回放 event_id > last_event_id 的 tail；超窗、领先 high-water
                          或重启后 ring 缺失时通过 replay_control 请求 full snapshot。

        Yields:
            事件字典（含 event_id / type / timestamp / 业务字段）
        """
        ch = self._ensure_channel(session_id)
        await self._ensure_event_sequence_reservation(session_id, ch)
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        ch.subscribers.append(queue)
        ch.last_activity = time.time()

        # 1) Backlog 回放
        backlog: List[Dict[str, Any]] = []
        replay_plan = self.get_replay_plan(session_id, last_event_id)
        if last_event_id is not None and replay_plan["mode"] == "tail":
            backlog = [e for e in ch.ring_buffer if e.get("event_id", 0) > last_event_id]

        try:
            if include_replay_control:
                yield {
                    "type": "replay_control",
                    "resume_mode": replay_plan["mode"],
                    "cursor": replay_plan["cursor"],
                    "oldest_event_id": replay_plan["oldest_event_id"],
                    "latest_event_id": replay_plan["latest_event_id"],
                }
            for event in backlog:
                yield event

            # 终态且 backlog 已放完 → 立即退出，不等新事件
            if ch.terminal:
                return

            # 2) 实时事件
            while True:
                event = await queue.get()
                if event is None:  # sentinel: 频道终态或慢消费者被踢
                    return
                yield event
        finally:
            if queue in ch.subscribers:
                ch.subscribers.remove(queue)

    # ─────── 清理 ───────

    def cleanup(self, now: Optional[float] = None) -> int:
        """清理过期频道

        规则：
        - 终态 >5 分钟 → 清理
        - 非终态但 >1 小时无活动 → 清理

        Returns:
            清理的频道数
        """
        now = now if now is not None else time.time()
        to_remove: List[str] = []
        for sid, ch in self._channels.items():
            if ch.terminal and ch.terminal_at and (now - ch.terminal_at) > TERMINAL_CLEANUP_DELAY:
                to_remove.append(sid)
            elif not ch.terminal and (now - ch.last_activity) > INACTIVE_TIMEOUT:
                to_remove.append(sid)

        for sid in to_remove:
            ch = self._channels.pop(sid, None)
            if ch:
                # 通知仍在的订阅者退出
                for q in ch.subscribers:
                    try:
                        q.put_nowait(None)
                    except asyncio.QueueFull:
                        pass
        return len(to_remove)

    # ─────── 观测 ───────

    def stats(self) -> Dict[str, Any]:
        """调试/监控用"""
        return {
            "channels": len(self._channels),
            "terminal_channels": sum(1 for c in self._channels.values() if c.terminal),
            "total_subscribers": sum(len(c.subscribers) for c in self._channels.values()),
            "total_buffered_events": sum(len(c.ring_buffer) for c in self._channels.values()),
        }


# ─────────────────────────── SSE 编码辅助 ───────────────────────────

def parse_last_event_id(header_value: Optional[str], query_value: Optional[str]) -> Optional[int]:
    """解析客户端断线续传游标。

    两个来源：
    - header `Last-Event-ID`：浏览器原生 EventSource 自动重连时携带（spec 行为）
    - query `last_event_id`：客户端主动 `new EventSource(...)` 时只能走 URL（无法设自定义 header）

    header 优先（spec-compliant），query 是兜底。任何非法值（非整数）都视为 None。
    """
    for raw in (header_value, query_value):
        if not raw:
            continue
        try:
            cursor = int(raw)
            if cursor >= 0:
                return cursor
        except ValueError:
            continue
    return None


def encode_sse_event(event: Dict[str, Any]) -> str:
    """将事件字典编码为 SSE 格式字符串

    格式：
        id: <event_id>
        event: <type>
        data: <json>

    其中 id 行会被浏览器原生 EventSource 记录，重连时自动带 Last-Event-ID 头。
    """
    event_id = event.get("event_id")
    event_type = event.get("type", "message")
    data = json.dumps(event, ensure_ascii=False)

    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


# ─────────────────────────── 全局单例 ───────────────────────────

_sse_manager: Optional[SSEManager] = None


def get_sse_manager() -> SSEManager:
    """获取 SSE 管理器单例"""
    global _sse_manager
    if _sse_manager is None:
        _sse_manager = SSEManager()
    return _sse_manager
