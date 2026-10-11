"""Computer Use 安全栏：总开关 + 速率限制 + 坐标边界 + action 白名单。

四道闸（按检查顺序）：
1. 全局总开关：settings 里 computer_use_enabled = false 默认（用户首次开必须显式同意）
2. action 白名单：settings 可禁某些 action（例如只允许 screenshot，不允许 click/type）
3. 坐标边界：click/move/drag 的 [x, y] 必须落在已知显示器范围内（防 LLM 出 [-1, 99999] 这种）
4. 速率限制：每 session 每秒 ≤ MAX_PER_SECOND，每分钟 ≤ MAX_PER_MINUTE（令牌桶）

不在这里做的事：
- 单实例锁（在 lock.py）
- 进程隔离（在 sidecar.py）
- 审计（在 audit.py）

这一层失败抛 SafetyDenied，executor 转 ComputerUseDenied，工具层转 'denied' code。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ============================================================================
# 总开关 + 全局策略
# ============================================================================
@dataclass
class ComputerUsePolicy:
    """全局策略 —— M4 先用进程内单例，M5 之后从 settings 持久化。"""

    enabled: bool = False
    allowed_actions: Optional[frozenset] = None  # None = 全部允许；非空 set = 白名单
    blocked_actions: frozenset = field(default_factory=lambda: frozenset())
    max_per_second: int = 5
    max_per_minute: int = 60
    # M5 视觉闭环：mutating action（click/type/key/scroll/...）执行后自动再截一张，
    # 算 before/after diff 塞回 LLM 上下文，让模型自我校正点没点中。
    # 默认 ON——这是相对原生 anthropic computer_use 的最大壁垒。
    auto_screenshot_after_mutate: bool = True


_policy_lock = threading.Lock()
_policy = ComputerUsePolicy()


def get_policy() -> ComputerUsePolicy:
    with _policy_lock:
        return ComputerUsePolicy(
            enabled=_policy.enabled,
            allowed_actions=_policy.allowed_actions,
            blocked_actions=_policy.blocked_actions,
            max_per_second=_policy.max_per_second,
            max_per_minute=_policy.max_per_minute,
            auto_screenshot_after_mutate=_policy.auto_screenshot_after_mutate,
        )


def set_policy(**kwargs) -> None:
    """更新策略（部分字段）。供 settings API / 测试用。"""
    with _policy_lock:
        for k, v in kwargs.items():
            if hasattr(_policy, k):
                setattr(_policy, k, v)
            else:
                raise ValueError(f"unknown policy field: {k!r}")


def policy_to_dict(p: Optional[ComputerUsePolicy] = None) -> Dict[str, Any]:
    """策略 → JSON-safe dict。frozenset 落成 sorted list，None 保留。"""
    p = p or get_policy()
    return {
        "enabled": p.enabled,
        "allowed_actions": (
            None if p.allowed_actions is None else sorted(p.allowed_actions)
        ),
        "blocked_actions": sorted(p.blocked_actions),
        "max_per_second": p.max_per_second,
        "max_per_minute": p.max_per_minute,
        "auto_screenshot_after_mutate": p.auto_screenshot_after_mutate,
    }


def policy_from_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """dict → set_policy 接受的 kwargs。允许部分字段缺失。"""
    out: Dict[str, Any] = {}
    if "enabled" in d:
        out["enabled"] = bool(d["enabled"])
    if "allowed_actions" in d:
        v = d["allowed_actions"]
        out["allowed_actions"] = None if v is None else frozenset(v)
    if "blocked_actions" in d:
        out["blocked_actions"] = frozenset(d.get("blocked_actions") or [])
    if "max_per_second" in d:
        out["max_per_second"] = max(1, min(100, int(d["max_per_second"])))
    if "max_per_minute" in d:
        out["max_per_minute"] = max(1, min(10000, int(d["max_per_minute"])))
    if "auto_screenshot_after_mutate" in d:
        out["auto_screenshot_after_mutate"] = bool(d["auto_screenshot_after_mutate"])
    return out


def load_from_disk() -> bool:
    """从 paths.computer_use_policy_path() 加载持久化策略；不存在/坏档时保持默认。

    返回 True 表示成功加载并应用。
    """
    try:
        from backend import paths
    except ImportError:
        return False
    p = paths.computer_use_policy_path()
    if not p.exists():
        return False
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[computer_use] failed to load policy file: %s", exc)
        return False
    if not isinstance(data, dict):
        return False
    try:
        kwargs = policy_from_dict(data)
        set_policy(**kwargs)
        return True
    except (ValueError, TypeError) as exc:
        logger.warning("[computer_use] invalid policy on disk: %s", exc)
        return False


def save_to_disk() -> None:
    """把当前策略原子落盘。"""
    try:
        from backend import paths
    except ImportError:
        return
    p = paths.computer_use_policy_path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    payload = json.dumps(policy_to_dict(), ensure_ascii=False, indent=2)
    try:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        logger.warning("[computer_use] failed to save policy: %s", exc)


def reset_policy_for_testing() -> None:
    global _policy
    with _policy_lock:
        _policy = ComputerUsePolicy()
        _rate.clear()


# ============================================================================
# Errors
# ============================================================================
class SafetyDenied(RuntimeError):
    """安全栏拒绝某个 action。"""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


# ============================================================================
# 速率限制（令牌桶 / 滑动窗口）
# ============================================================================
@dataclass
class _RateState:
    """每 session 一份。两个窗口：1s / 60s 滑动队列。"""
    sec_window: deque = field(default_factory=deque)  # 1 秒内的 action 时间戳
    min_window: deque = field(default_factory=deque)  # 60 秒内的 action 时间戳


_rate: Dict[str, _RateState] = {}
_rate_lock = threading.Lock()


def _record_rate(session_id: str, *, max_per_second: int, max_per_minute: int) -> None:
    """登记一次成功调用；超限抛 SafetyDenied。"""
    now = time.monotonic()
    with _rate_lock:
        state = _rate.setdefault(session_id, _RateState())
        # 滑出窗口外的
        while state.sec_window and now - state.sec_window[0] > 1.0:
            state.sec_window.popleft()
        while state.min_window and now - state.min_window[0] > 60.0:
            state.min_window.popleft()

        if len(state.sec_window) >= max_per_second:
            raise SafetyDenied(
                f"rate limit exceeded: more than {max_per_second} actions/sec",
                code="rate_limit_per_second",
            )
        if len(state.min_window) >= max_per_minute:
            raise SafetyDenied(
                f"rate limit exceeded: more than {max_per_minute} actions/min",
                code="rate_limit_per_minute",
            )

        state.sec_window.append(now)
        state.min_window.append(now)


# ============================================================================
# 坐标边界
# ============================================================================
def _coord_within_displays(x: int, y: int, displays: list[Dict]) -> bool:
    """坐标必须落在某个 display 的物理像素矩形里。"""
    for d in displays or []:
        left = d.get("left", 0) or 0
        top = d.get("top", 0) or 0
        w = d.get("width", 0) or 0
        h = d.get("height", 0) or 0
        if left <= x < left + w and top <= y < top + h:
            return True
    return False


# ============================================================================
# 入口：check_and_record
# ============================================================================
# 哪些 action 必须做坐标边界检查
_COORDINATE_ACTIONS = frozenset({
    "left_click", "right_click", "middle_click",
    "double_click", "triple_click",
    "mouse_move", "left_click_drag",
    "left_mouse_down", "left_mouse_up",
})


def check_and_record(
    *,
    session_id: str,
    action: str,
    coordinate: Optional[list] = None,
    cached_displays: Optional[list[Dict]] = None,
) -> None:
    """统一安全栏入口；通过则记录速率，违规则抛 SafetyDenied。"""
    policy = get_policy()

    if not policy.enabled:
        raise SafetyDenied(
            "Computer Use is disabled. Enable it in Settings → Computer Use first.",
            code="disabled",
        )

    if action in policy.blocked_actions:
        raise SafetyDenied(
            f"action {action!r} is blocked by user policy.",
            code="action_blocked",
        )

    if policy.allowed_actions is not None and action not in policy.allowed_actions:
        raise SafetyDenied(
            f"action {action!r} not in allow-list. Currently allowed: "
            f"{sorted(policy.allowed_actions)}",
            code="action_not_allowed",
        )

    # 坐标边界
    if action in _COORDINATE_ACTIONS and coordinate is not None and cached_displays:
        if isinstance(coordinate, list) and len(coordinate) == 2:
            try:
                x, y = int(coordinate[0]), int(coordinate[1])
            except (TypeError, ValueError):
                raise SafetyDenied(
                    f"coordinate has non-integer values: {coordinate!r}",
                    code="bad_coordinate",
                ) from None
            if not _coord_within_displays(x, y, cached_displays):
                raise SafetyDenied(
                    f"coordinate ({x}, {y}) is outside any known display. "
                    f"Run list_displays first to see valid ranges.",
                    code="coordinate_out_of_bounds",
                )

    # 速率限制（最后一道，通过即记录）
    _record_rate(
        session_id,
        max_per_second=policy.max_per_second,
        max_per_minute=policy.max_per_minute,
    )
