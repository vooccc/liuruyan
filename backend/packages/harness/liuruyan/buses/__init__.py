"""liuruyan.buses — 横切总线层。

EventBus: Agent 主路径 → 多消费者 fan-out（SSE / IM / TraceJournal / Metrics）
MessageBus: IM 平台 ↔ Agent 双向消息（骨架，待 Phase E 接 channel adapter）

详见 _docs/architecture_v2/02_runtime_core.md § 三 / § 四
"""

from .event_bus import EventBus, EventCallback
from .event_schema import AgentEvent, EVENT_SCHEMA_VERSION, EVENT_TYPES
from .message_bus import (
    InboundMessage,
    MessageBus,
    OutboundCallback,
    OutboundMessage,
)

# ── process-wide singleton（lifespan 装配；不是 per-task context）───────
# bus 一个进程一份，由 lifespan 创建并 set；agent / channels / mw 用时 get。
# 这跟 ContextVar 的 per-task 隔离原则不冲突——bus 本身就是 process-wide
# 共享设施，参考 02_runtime_core.md § 十一。

_event_bus: EventBus | None = None
_message_bus: MessageBus | None = None


def set_event_bus(bus: EventBus | None) -> None:
    global _event_bus
    _event_bus = bus


def get_event_bus() -> EventBus | None:
    return _event_bus


def set_message_bus(bus: MessageBus | None) -> None:
    global _message_bus
    _message_bus = bus


def get_message_bus() -> MessageBus | None:
    return _message_bus


__all__ = [
    "AgentEvent",
    "EVENT_SCHEMA_VERSION",
    "EVENT_TYPES",
    "EventBus",
    "EventCallback",
    "InboundMessage",
    "MessageBus",
    "OutboundCallback",
    "OutboundMessage",
    "get_event_bus",
    "get_message_bus",
    "set_event_bus",
    "set_message_bus",
]

