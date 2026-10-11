"""自进化系统地基（Phase 1A）

新架构核心模块。与现有 sse_manager / subagent runtime 并行存在，
后续按通道身份逐步合并统一。

设计原则（handover §3）：
- AgentActor 不知道平台、不知道传输层
- server 是消息的唯一真值
- 路由按通道身份（channel name = 业务身份），不按 payload 字段过滤
"""

from backend.core.session_key import SessionKey, SessionKeyError
from backend.core.inbox_message import (
    BusEvent,
    InboxMessage,
    MessageLineage,
    InboxKind,
)
from backend.core.event_bus import (
    EventBus,
    InMemoryEventBus,
    get_session_bus,
    get_room_bus,
    get_panorama_bus,
    get_default_bus,
    channel_session,
    channel_room,
    channel_agent_inbox,
    PANORAMA_CHANNEL,
    reset_bus_singleton_for_testing,
)
from backend.core.tick_scheduler import (
    ActivityCounter,
    compute_next_delay,
    DEFAULT_ACTIVITY_WINDOW_SEC,
)
from backend.core.safety import (
    TickGuard,
    TickGuardConfig,
    TICK_HARD_MIN_SEC,
    TICK_HARD_MAX_SEC,
    DAILY_QUOTA_DEFAULT,
    get_tick_guard,
    LoopGuard,
    LoopGuardConfig,
    MAX_AGENT_HOP_DEPTH,
    ROOM_NO_USER_PAUSE_THRESHOLD,
    get_loop_guard,
)
from backend.core.agent_actor import (
    AgentActor,
    AgentActorConfig,
    AgentPool,
    get_agent_pool,
    reset_pool_for_testing,
)
from backend.core.collab_room import (
    CollabRoom,
    Participant,
    RoomDispatcher,
    RoomMessage,
)
from backend.core.adapters import (
    DeliveryTarget,
    InboundAdapter,
    OutboundAdapter,
)
from backend.core.adapters.ws import (
    WSConnection,
    WebSocketOutboundAdapter,
    serve_websocket,
)

__all__ = [
    # session
    "SessionKey",
    "SessionKeyError",
    # messages
    "BusEvent",
    "InboxMessage",
    "MessageLineage",
    "InboxKind",
    # bus
    "EventBus",
    "InMemoryEventBus",
    "get_session_bus",
    "get_room_bus",
    "get_panorama_bus",
    "get_default_bus",
    "channel_session",
    "channel_room",
    "channel_agent_inbox",
    "PANORAMA_CHANNEL",
    "reset_bus_singleton_for_testing",
    # tick scheduler
    "ActivityCounter",
    "compute_next_delay",
    "DEFAULT_ACTIVITY_WINDOW_SEC",
    # safety
    "TickGuard",
    "TickGuardConfig",
    "TICK_HARD_MIN_SEC",
    "TICK_HARD_MAX_SEC",
    "DAILY_QUOTA_DEFAULT",
    "get_tick_guard",
    "LoopGuard",
    "LoopGuardConfig",
    "MAX_AGENT_HOP_DEPTH",
    "ROOM_NO_USER_PAUSE_THRESHOLD",
    "get_loop_guard",
    # agent actor
    "AgentActor",
    "AgentActorConfig",
    "AgentPool",
    "get_agent_pool",
    "reset_pool_for_testing",
    # collab
    "CollabRoom",
    "Participant",
    "RoomDispatcher",
    "RoomMessage",
    # adapters
    "DeliveryTarget",
    "InboundAdapter",
    "OutboundAdapter",
    "WSConnection",
    "WebSocketOutboundAdapter",
    "serve_websocket",
]
