"""Gateway 健康状态缓存（M3）。

进程内存级。30 秒（默认）黑屋让 fallback 链快速跳过最近失败的 provider。

设计：docs/GATEWAY.md §4
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Literal, Optional

FailureKind = Literal["auth", "rate_limit", "server_error", "timeout", "network"]

# 每种 kind 的 cooldown 秒数（auth 最长，因为认证不会快速恢复）
_DEFAULT_COOLDOWN: Dict[FailureKind, int] = {
    "auth": 300,         # 5 min
    "rate_limit": 60,    # 1 min
    "server_error": 30,
    "timeout": 30,
    "network": 30,
}


@dataclass
class HealthState:
    failure_kind: FailureKind
    failed_at: float
    cooldown_secs: int
    error_message: str = ""

    def is_in_cooldown(self, now: Optional[float] = None) -> bool:
        if now is None:
            now = time.time()
        return (now - self.failed_at) < self.cooldown_secs

    def remaining_secs(self, now: Optional[float] = None) -> int:
        if now is None:
            now = time.time()
        return max(0, int(self.cooldown_secs - (now - self.failed_at)))


class HealthCache:
    """全局单例，存 provider_id → HealthState。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: Dict[str, HealthState] = {}

    def should_try(self, provider_id: str, *, now: Optional[float] = None) -> bool:
        """provider 是否应该被 try？黑屋内 → False。"""
        with self._lock:
            state = self._cache.get(provider_id)
            if state is None:
                return True
            if state.is_in_cooldown(now=now):
                return False
            # 过期了清掉
            del self._cache[provider_id]
            return True

    def record_success(self, provider_id: str) -> None:
        with self._lock:
            self._cache.pop(provider_id, None)

    def record_failure(
        self,
        provider_id: str,
        kind: FailureKind,
        *,
        cooldown_secs: Optional[int] = None,
        error_message: str = "",
    ) -> None:
        secs = cooldown_secs if cooldown_secs is not None else _DEFAULT_COOLDOWN.get(kind, 30)
        with self._lock:
            self._cache[provider_id] = HealthState(
                failure_kind=kind,
                failed_at=time.time(),
                cooldown_secs=secs,
                error_message=error_message,
            )

    def snapshot(self) -> Dict[str, Dict]:
        """调试用：当前所有黑屋记录。"""
        out = {}
        now = time.time()
        with self._lock:
            for pid, state in list(self._cache.items()):
                if not state.is_in_cooldown(now=now):
                    continue
                out[pid] = {
                    "failure_kind": state.failure_kind,
                    "failed_at": state.failed_at,
                    "cooldown_secs": state.cooldown_secs,
                    "remaining_secs": state.remaining_secs(now=now),
                    "error_message": state.error_message,
                }
        return out

    def clear(self) -> None:
        """测试用 / admin 强制重置。"""
        with self._lock:
            self._cache.clear()


# 全局单例
_global_cache = HealthCache()


def get_cache() -> HealthCache:
    return _global_cache


def classify_status(status: int) -> FailureKind:
    """把 HTTP status 映射成 FailureKind。"""
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status == 504:
        return "timeout"
    if status >= 500:
        return "server_error"
    # 默认按 server_error 处理（保守）
    return "server_error"


def is_retryable_status(status: int) -> bool:
    """这个 status 是否值得切到下一个 candidate？

    - 401/403/429/5xx → 切（健康原因）
    - 4xx other (400/404/422) → 不切（请求自身有问题，换 provider 也没用）
    """
    return status in (401, 403, 429) or status >= 500


__all__ = [
    "FailureKind",
    "HealthState",
    "HealthCache",
    "get_cache",
    "classify_status",
    "is_retryable_status",
]
