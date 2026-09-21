"""协作房间（handover §4.4 + §6 最简版）

设计要点：
1. 不持久化 Actor：房间只持有 participant 引用，actor 由 AgentPool 全局管
2. 串行通知 speaker_targets，不抢话不主持人（最简）
3. 后续扩展（free / round_robin / host_decides）只改 RoomDispatcher，房间数据结构不动

本窗口范围：数据结构 + 串行通知逻辑 + 单测，不接 HTTP 端点。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, List, Literal, Optional, Sequence

from backend.core.agent_actor import AgentPool, get_agent_pool
from backend.core.inbox_message import InboxMessage, MessageLineage
from backend.core.safety.loop_guard import LoopGuard, get_loop_guard

logger = logging.getLogger(__name__)


# ─────── 数据结构 ─────────

ParticipantType = Literal["user", "agent"]
SpeakerPolicy = Literal["sequential"]   # Phase 1 仅 sequential，Phase 3 扩展


@dataclass
class Participant:
    type: ParticipantType
    id: str                       # user_id 或 agent_id
    platform: str = "internal"    # web / wechat / feishu / internal
    display_name: Optional[str] = None


@dataclass
class CollabRoom:
    id: str
    title: str
    participants: List[Participant] = field(default_factory=list)
    speaker_targets: Optional[List[str]] = None
    speaker_policy: SpeakerPolicy = "sequential"
    paused: bool = False
    paused_reason: Optional[str] = None
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    @classmethod
    def create(
        cls,
        title: str,
        participants: Iterable[Participant],
        *,
        speaker_targets: Optional[Sequence[str]] = None,
    ) -> "CollabRoom":
        # ID 是裸 UUID，channel 命名空间由 channel_room() 加前缀
        return cls(
            id=uuid.uuid4().hex[:12],
            title=title,
            participants=list(participants),
            speaker_targets=list(speaker_targets) if speaker_targets else None,
        )

    def agent_ids(self) -> List[str]:
        return [p.id for p in self.participants if p.type == "agent"]

    def has_user(self) -> bool:
        return any(p.type == "user" for p in self.participants)

    def pause(self, reason: str) -> None:
        self.paused = True
        self.paused_reason = reason

    def resume(self) -> None:
        self.paused = False
        self.paused_reason = None


@dataclass
class RoomMessage:
    """协作房间内的一条消息（用于触发调度）"""

    id: str
    sender_type: ParticipantType
    sender_id: str
    content: str
    speaker_targets: Optional[List[str]] = None
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


# ─────── 调度器 ─────────

class RoomDispatcher:
    """串行通知 speaker_targets 中的 agent

    本窗口实现：
    - on_user_message：把 user 消息派发给 targets agents
    - 串行（顺序 await），保证不并行抢话
    - 集成 LoopGuard：检查房间是否应该暂停（连续 N 条无 user）
    - speaker_targets=None 时默认通知所有 agent
    """

    def __init__(
        self,
        *,
        agent_pool: Optional[AgentPool] = None,
        loop_guard: Optional[LoopGuard] = None,
        per_agent_timeout_sec: float = 30.0,
    ) -> None:
        self._pool = agent_pool or get_agent_pool()
        self._guard = loop_guard or get_loop_guard()
        self._per_agent_timeout_sec = per_agent_timeout_sec

    async def on_user_message(
        self,
        room: CollabRoom,
        msg: RoomMessage,
        recent_messages: Sequence[RoomMessage] = (),
    ) -> List[str]:
        """处理用户消息，返回**实际通知到的 agent_id 列表**

        recent_messages：最近 N 条房间消息，用于 LoopGuard 房间无 user 检查。
        """
        if room.paused:
            logger.info(
                "RoomDispatcher: room=%s already paused (%s), skip",
                room.id,
                room.paused_reason,
            )
            return []

        # 安全阀：连续无 user 检查（注意 — 此处接收的 msg 还没入参 recent，
        # 但本检查是"如果 recent 已满 N 条无 user，则暂停"，不依赖当前 msg）
        if self._guard.check_room_should_pause(recent_messages):
            room.pause("room_no_user_messages")
            return []

        # 决定 targets
        targets = list(msg.speaker_targets) if msg.speaker_targets else room.agent_ids()
        if not targets:
            return []

        notified: List[str] = []
        lineage = MessageLineage(origin_user_msg_id=msg.id)

        for agent_id in targets:
            actor = self._pool.get(agent_id)
            if actor is None:
                logger.warning(
                    "RoomDispatcher: agent=%s not in pool, skip", agent_id
                )
                continue

            inbox_msg = InboxMessage(
                kind="room_message",
                context_id=room.id,
                context_type="room",
                payload={
                    "room_id": room.id,
                    "message": {
                        "id": msg.id,
                        "sender_type": msg.sender_type,
                        "sender_id": msg.sender_id,
                        "content": msg.content,
                    },
                },
                lineage=lineage,
            )

            try:
                await asyncio.wait_for(
                    actor.inbox.put(inbox_msg),
                    timeout=self._per_agent_timeout_sec,
                )
                notified.append(agent_id)
            except asyncio.TimeoutError:
                logger.warning(
                    "RoomDispatcher: enqueue to agent=%s timeout", agent_id
                )

        return notified

    async def on_agent_message(
        self,
        room: CollabRoom,
        msg: RoomMessage,
        lineage: Optional[MessageLineage],
    ) -> List[str]:
        """处理 agent 在房间内的发言（可能触发 agent-to-agent 传播）

        speaker_targets 通常由 agent 在 LLM 响应中决定。本方法应用 LoopGuard
        防止环路 / 跳数失控。
        """
        if room.paused:
            return []

        if not msg.speaker_targets:
            return []

        notified: List[str] = []
        for agent_id in msg.speaker_targets:
            if not self._guard.check_hop_allowed(lineage, agent_id):
                continue

            actor = self._pool.get(agent_id)
            if actor is None:
                continue

            next_lineage = (
                lineage.extend(msg.sender_id)
                if lineage is not None
                else MessageLineage(
                    origin_user_msg_id=msg.id,
                    hop_depth=1,
                    chain=[msg.sender_id],
                )
            )

            inbox_msg = InboxMessage(
                kind="room_message",
                context_id=room.id,
                context_type="room",
                payload={
                    "room_id": room.id,
                    "message": {
                        "id": msg.id,
                        "sender_type": msg.sender_type,
                        "sender_id": msg.sender_id,
                        "content": msg.content,
                    },
                },
                lineage=next_lineage,
            )

            try:
                await asyncio.wait_for(
                    actor.inbox.put(inbox_msg),
                    timeout=self._per_agent_timeout_sec,
                )
                notified.append(agent_id)
            except asyncio.TimeoutError:
                logger.warning(
                    "RoomDispatcher: enqueue to agent=%s timeout (agent->agent)",
                    agent_id,
                )

        return notified
