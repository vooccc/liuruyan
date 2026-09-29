"""安全阀（handover §5.3-§5.4）

Phase 1 必交付：
- TickGuard：tick 配额 + 间隔合法性
- LoopGuard：跨 agent 跳数 + 环路 + 房间无 user 暂停
"""

from backend.core.safety.tick_guard import (
    TickGuard,
    TickGuardConfig,
    TICK_HARD_MIN_SEC,
    TICK_HARD_MAX_SEC,
    DAILY_QUOTA_DEFAULT,
    get_tick_guard,
)
from backend.core.safety.loop_guard import (
    LoopGuard,
    LoopGuardConfig,
    MAX_AGENT_HOP_DEPTH,
    ROOM_NO_USER_PAUSE_THRESHOLD,
    get_loop_guard,
)

__all__ = [
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
]
