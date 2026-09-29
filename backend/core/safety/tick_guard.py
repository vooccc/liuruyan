"""TickGuard：心跳安全阀（handover §5.3 安全阀 1）

职责：
1. 每日 tick 配额检查（防 LLM 烧钱失控）
2. tick_interval 合法性检查（5min~24h，超出强制 fallback）
3. 跨日窗口自动重置

不依赖时钟外部，所有时间通过参数 `now` 注入，方便测试。
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# ─────── 常量（handover §5.3）─────────

TICK_HARD_MIN_SEC = 300       # 5min
TICK_HARD_MAX_SEC = 86400     # 24h
DAILY_QUOTA_DEFAULT = 100     # 单 agent 单日 tick 配额
TICK_INTERVAL_FALLBACK = 3600 # 非法时强制重置为 1h

DAY_SECONDS = 86400.0


@dataclass
class TickGuardConfig:
    """每个 agent 的 guard 配置（一般从 AgentConfig 读取）"""

    tick_interval_sec: int = TICK_INTERVAL_FALLBACK
    tick_min_sec: int = TICK_HARD_MIN_SEC
    tick_max_sec: int = TICK_HARD_MAX_SEC
    daily_tick_quota: int = DAILY_QUOTA_DEFAULT


@dataclass
class _AgentState:
    daily_count: int = 0
    daily_window_start: float = 0.0


class TickGuard:
    """全局 tick 安全阀（process 单例）

    用法：
        guard = TickGuard()
        if guard.check_can_tick(agent_id, config):
            # do tick
            guard.record_tick(agent_id)
    """

    def __init__(self) -> None:
        self._states: Dict[str, _AgentState] = defaultdict(_AgentState)

    # ─────── 间隔合法性（纯函数，无副作用）─────────

    @staticmethod
    def normalize_interval(interval_sec: int) -> int:
        """把不合法的 interval 强制 fallback 为默认值

        合法范围：[TICK_HARD_MIN_SEC, TICK_HARD_MAX_SEC]
        非法（含 0/负数/过大）→ TICK_INTERVAL_FALLBACK
        """
        if not isinstance(interval_sec, (int, float)):
            return TICK_INTERVAL_FALLBACK
        v = int(interval_sec)
        if v < TICK_HARD_MIN_SEC or v > TICK_HARD_MAX_SEC:
            return TICK_INTERVAL_FALLBACK
        return v

    @staticmethod
    def is_interval_legal(interval_sec: int) -> bool:
        return (
            isinstance(interval_sec, (int, float))
            and TICK_HARD_MIN_SEC <= int(interval_sec) <= TICK_HARD_MAX_SEC
        )

    # ─────── 配额 ─────────

    def _reset_if_new_day(self, agent_id: str, now: float) -> None:
        st = self._states[agent_id]
        if st.daily_window_start == 0.0:
            st.daily_window_start = now
            return
        if now - st.daily_window_start >= DAY_SECONDS:
            st.daily_count = 0
            st.daily_window_start = now

    def check_can_tick(
        self,
        agent_id: str,
        config: TickGuardConfig,
        *,
        now: Optional[float] = None,
    ) -> bool:
        """检查 agent 此时能否 tick

        返回 False 的两种情况：
        1. 当日 tick 次数已达 daily_tick_quota
        2. （仅日志）interval 非法 — 不阻断 tick，但记录 warning

        间隔非法不直接 False 是因为：调度层应该用 normalize_interval 拿到合法值，
        guard 在这里只负责记录异常 + 不让它 false alarm。
        """
        ts = now if now is not None else time.time()
        self._reset_if_new_day(agent_id, ts)

        if not self.is_interval_legal(config.tick_interval_sec):
            logger.error(
                "TickGuard: agent=%s has illegal tick_interval_sec=%s, "
                "调度层应使用 normalize_interval 获取合法值",
                agent_id,
                config.tick_interval_sec,
            )

        st = self._states[agent_id]
        if st.daily_count >= max(0, config.daily_tick_quota):
            logger.warning(
                "TickGuard: agent=%s hit daily quota (%d/%d), tick skipped",
                agent_id,
                st.daily_count,
                config.daily_tick_quota,
            )
            return False

        return True

    def record_tick(self, agent_id: str, *, now: Optional[float] = None) -> None:
        ts = now if now is not None else time.time()
        self._reset_if_new_day(agent_id, ts)
        self._states[agent_id].daily_count += 1

    def get_daily_count(self, agent_id: str) -> int:
        return self._states[agent_id].daily_count

    def reset_for_testing(self) -> None:
        self._states.clear()


# ─────── 全局单例 ─────────

_singleton: Optional[TickGuard] = None


def get_tick_guard() -> TickGuard:
    global _singleton
    if _singleton is None:
        _singleton = TickGuard()
    return _singleton


def reset_for_testing() -> None:
    global _singleton
    _singleton = None
