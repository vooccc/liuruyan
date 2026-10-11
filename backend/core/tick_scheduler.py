"""TickScheduler：心跳调度（handover §5.1 反向自适应间隔）

核心算法：
- 活动量 ≥ 10/h    → 间隔 ×4（最长 24h）
- 活动量 ≥ 3/h     → 默认间隔
- 活动量 ≥ 1/h     → 间隔 ÷2（最短 5min）
- 活动量 < 1/h     → 间隔 ÷4（最短 5min）

设计要点：
- ActivityCounter 是滑动窗口（默认 1h），按调用方注入的 now 计数，纯逻辑无副作用
- compute_next_delay 是纯函数，便于测试
- 实际"等到下次 tick"由 AgentActor 自己用 asyncio.wait_for 实现，
  TickScheduler 不持有 task。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

from backend.core.safety.tick_guard import (
    TICK_HARD_MAX_SEC,
    TICK_HARD_MIN_SEC,
)


DEFAULT_ACTIVITY_WINDOW_SEC = 3600.0  # 1h


# ─────── 活动量计数器（滑动窗口）─────────

@dataclass
class ActivityCounter:
    window_sec: float = DEFAULT_ACTIVITY_WINDOW_SEC
    _events: Deque[float] = field(default_factory=deque)

    def record(self, now: Optional[float] = None) -> None:
        ts = now if now is not None else time.time()
        self._events.append(ts)
        self._evict(ts)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def count(self, now: Optional[float] = None) -> int:
        ts = now if now is not None else time.time()
        self._evict(ts)
        return len(self._events)

    def reset(self) -> None:
        self._events.clear()


# ─────── 反向自适应间隔（纯函数）─────────

def compute_next_delay(
    *,
    activity_count: int,
    base_interval_sec: int,
    min_sec: int = TICK_HARD_MIN_SEC,
    max_sec: int = TICK_HARD_MAX_SEC,
) -> float:
    """根据活动量决定下次 tick 等待秒数

    返回浮点数（asyncio.wait_for 接受 float timeout）。
    最终结果会 clamp 到 [min_sec, max_sec]，保证不会越界。
    """
    base = max(1, int(base_interval_sec))

    if activity_count >= 10:
        delay = base * 4
    elif activity_count >= 3:
        delay = base
    elif activity_count >= 1:
        delay = base // 2
    else:
        delay = base // 4

    delay = max(min_sec, min(delay, max_sec))
    return float(delay)
