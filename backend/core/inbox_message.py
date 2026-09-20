"""内部消息类型（不暴露给外部）

包含三种核心载荷：
- BusEvent：流过 EventBus 的事件（前后端协议体）
- InboxMessage：流入 AgentActor inbox 的内部消息
- MessageLineage：跨 agent 传播时携带的祖源信息（LoopGuard 依据）
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


InboxKind = Literal[
    "session_message",   # 1v1 会话来的消息
    "room_message",      # 协作房间来的消息
    "tick",              # 心跳触发
    "system",            # 系统指令（重载配置、shutdown 等）
    "delegation",        # 来自其他 agent 的委派
]


@dataclass
class MessageLineage:
    """跨 agent 传播时携带的祖源信息（LoopGuard 依据）"""

    origin_user_msg_id: str
    hop_depth: int = 0
    chain: List[str] = field(default_factory=list)

    def extend(self, agent_id: str) -> "MessageLineage":
        """生成新一跳"""
        return MessageLineage(
            origin_user_msg_id=self.origin_user_msg_id,
            hop_depth=self.hop_depth + 1,
            chain=[*self.chain, agent_id],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "origin_user_msg_id": self.origin_user_msg_id,
            "hop_depth": self.hop_depth,
            "chain": list(self.chain),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["MessageLineage"]:
        if not data:
            return None
        return cls(
            origin_user_msg_id=data["origin_user_msg_id"],
            hop_depth=int(data.get("hop_depth", 0)),
            chain=list(data.get("chain", [])),
        )


@dataclass
class InboxMessage:
    """流入 AgentActor inbox 的内部消息

    注意：context_id + context_type 共同决定上下文（哪个 session/room）。
    payload 是任意业务体，actor 自己负责解释。
    """

    kind: InboxKind
    context_id: str                      # session_key 字符串 / room_id / agent_id
    context_type: Literal["session", "room", "agent"]
    payload: Any = None
    lineage: Optional[MessageLineage] = None
    enqueued_at: float = field(default_factory=time.time)
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass
class BusEvent:
    """流过 EventBus 的事件（前后端协议体）

    设计与 sse_manager 事件 dict 同构：必有 event_id / type / timestamp。
    payload 是业务字段，序列化后整个对象 JSON 化即可前端消费。
    """

    type: str                            # 事件类型，如 "message" / "tool_call" / "tick" / "system_notice"
    payload: Dict[str, Any] = field(default_factory=dict)
    event_id: Optional[int] = None       # 由 EventBus 在 publish 时分配（单调递增）
    timestamp: Optional[float] = None    # 由 EventBus 在 publish 时填充

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            **self.payload,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BusEvent":
        meta_keys = {"type", "event_id", "timestamp"}
        return cls(
            type=str(data.get("type") or "message"),
            event_id=data.get("event_id"),
            timestamp=data.get("timestamp"),
            payload={k: v for k, v in data.items() if k not in meta_keys},
        )
