"""Computer Use 录制回放：从 audit 日志重新执行一段 action 序列。

用途：
1. 调试：用户报 bug 时让他们点"回放"复现问题（不用问步骤）
2. 宏录制：人工示范一次，AI 学习后用 replay 重复
3. 演示：复盘 AI 的操作给用户看（dry_run）

设计原则：
- replay 不绕开 safety：每步仍走 executor._guarded_call → 黑名单/速率限制/急停一律生效
- replay 默认 dry_run=True：先 preview，确认无误再 run
- 速率因子：speed_multiplier 控制步骤间 delay，原速复现还是快进
- 失败行为：默认 abort_on_error（更安全）；continue 模式记录错误继续
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.computer_use import audit as _audit

logger = logging.getLogger(__name__)


# Replay 不允许重放的 action（即使 audit 里有也跳过）：
# - screenshot / list_displays / list_windows / list_running_apps / cursor_position：
#   纯读，回放没意义且白白消耗
# - read_clipboard：同理
REPLAY_SKIP_ACTIONS = frozenset({
    "screenshot", "list_displays", "list_windows", "list_running_apps",
    "cursor_position", "read_clipboard", "wait",  # wait 自己有 sleep 逻辑，replay 时合并到 step delay
})


@dataclass
class ReplayStep:
    """单步回放计划。"""
    index: int
    action: str
    payload: Dict[str, Any]
    original_ts: float
    delay_before_ms: float = 0.0  # 距离上一步的间隔（按 audit 里的真实时间差算）


@dataclass
class ReplayResult:
    """单步执行结果。"""
    index: int
    action: str
    ok: bool
    error: Optional[str] = None
    duration_ms: float = 0.0
    skipped_reason: Optional[str] = None


@dataclass
class ReplayPlan:
    """完整回放计划（preview 用）。"""
    session_id: str  # 原录制的 session_id（不是当前 replay 用的 session）
    steps: List[ReplayStep] = field(default_factory=list)
    skipped_count: int = 0
    total_audit_entries: int = 0
    earliest_ts: Optional[float] = None
    latest_ts: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "step_count": len(self.steps),
            "skipped_count": self.skipped_count,
            "total_audit_entries": self.total_audit_entries,
            "earliest_ts": self.earliest_ts,
            "latest_ts": self.latest_ts,
            "duration_estimate_sec": (
                (self.latest_ts - self.earliest_ts)
                if (self.earliest_ts and self.latest_ts) else 0.0
            ),
            "steps": [
                {
                    "index": s.index,
                    "action": s.action,
                    "payload": s.payload,
                    "original_ts": s.original_ts,
                    "delay_before_ms": s.delay_before_ms,
                }
                for s in self.steps
            ],
        }


# ===========================================================================
# 构建 replay 计划（从 audit）
# ===========================================================================
def build_plan(
    *,
    session_id: Optional[str] = None,
    last_n: int = 50,
    max_steps: int = 100,
) -> ReplayPlan:
    """从 audit 末尾抽 action 序列。

    Args:
        session_id: 仅取该 session 的 audit 条；None = 不过滤 session（取全局尾部）
        last_n: 从 audit 末尾扫多少条作为候选
        max_steps: replay 计划最多包含多少步（防爆）

    返回：ReplayPlan，按时间升序。
    """
    if last_n < 1:
        raise ValueError("last_n must be >= 1")
    if max_steps < 1:
        raise ValueError("max_steps must be >= 1")

    recent = _audit.read_recent(limit=max(last_n * 2, 200))
    total = len(recent)

    # 按 session_id 过滤 + 只取成功的 + 非读类
    candidates: List[Dict[str, Any]] = []
    for entry in recent:
        if not entry.get("ok"):
            continue
        if session_id and entry.get("session_id") != session_id:
            continue
        action = entry.get("action")
        if not isinstance(action, str):
            continue
        candidates.append(entry)

    # 取末尾 last_n 条（按 ts 升序）
    candidates = candidates[-last_n:]
    skipped = 0
    steps: List[ReplayStep] = []
    last_ts: Optional[float] = None

    for entry in candidates:
        action = entry["action"]
        if action in REPLAY_SKIP_ACTIONS:
            skipped += 1
            continue
        ts = float(entry.get("ts") or 0.0)
        payload = entry.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        delay_ms = 0.0
        if last_ts is not None and ts > last_ts:
            delay_ms = min((ts - last_ts) * 1000.0, 5000.0)  # 单步等待 cap 5s
        steps.append(ReplayStep(
            index=len(steps),
            action=action,
            payload=payload,
            original_ts=ts,
            delay_before_ms=round(delay_ms, 1),
        ))
        last_ts = ts
        if len(steps) >= max_steps:
            break

    plan_sid = session_id or (steps[0].payload.get("__session__") if steps else "") or "any"
    return ReplayPlan(
        session_id=plan_sid,
        steps=steps,
        skipped_count=skipped,
        total_audit_entries=total,
        earliest_ts=steps[0].original_ts if steps else None,
        latest_ts=steps[-1].original_ts if steps else None,
    )


# ===========================================================================
# 执行 replay
# ===========================================================================
async def run_plan(
    plan: ReplayPlan,
    *,
    executor,  # ComputerUseExecutor 实例
    speed_multiplier: float = 1.0,
    dry_run: bool = False,
    abort_on_error: bool = True,
    step_max_delay_ms: float = 2000.0,
) -> Dict[str, Any]:
    """执行 replay 计划。

    Args:
        plan: build_plan() 产物
        executor: 当前 session 的 ComputerUseExecutor
        speed_multiplier: 1.0=原速；2.0=两倍快进；0.5=半速。<=0 抛 ValueError
        dry_run: 只走流程不真执行 sidecar（仍调 safety check 验证规则）
        abort_on_error: True=单步失败立刻停；False=记录并继续
        step_max_delay_ms: 单步间隔上限（用户 cap，超出直接截断）

    Returns:
        { "results": [...], "completed": bool, "aborted_at": int | None }
    """
    if speed_multiplier <= 0:
        raise ValueError("speed_multiplier must be > 0")

    results: List[ReplayResult] = []
    aborted_at: Optional[int] = None

    for step in plan.steps:
        # 步间 delay（按 speed 缩放）
        if step.delay_before_ms > 0:
            wait_ms = min(step.delay_before_ms / speed_multiplier, step_max_delay_ms)
            await asyncio.sleep(wait_ms / 1000.0)

        started = time.monotonic()
        if dry_run:
            results.append(ReplayResult(
                index=step.index, action=step.action,
                ok=True, duration_ms=0.0,
                skipped_reason="dry_run",
            ))
            continue

        # 真执行：走 executor.run_action → safety/audit 都生效
        try:
            await executor.run_action(step.action, step.payload, timeout_sec=8.0)
            results.append(ReplayResult(
                index=step.index, action=step.action,
                ok=True,
                duration_ms=(time.monotonic() - started) * 1000.0,
            ))
        except Exception as exc:  # noqa: BLE001 — 任何错都让 caller 决定要不要 abort
            results.append(ReplayResult(
                index=step.index, action=step.action,
                ok=False, error=str(exc),
                duration_ms=(time.monotonic() - started) * 1000.0,
            ))
            if abort_on_error:
                aborted_at = step.index
                break

    return {
        "results": [
            {
                "index": r.index,
                "action": r.action,
                "ok": r.ok,
                "error": r.error,
                "duration_ms": round(r.duration_ms, 2),
                "skipped_reason": r.skipped_reason,
            }
            for r in results
        ],
        "completed": aborted_at is None and len(results) == len(plan.steps),
        "aborted_at": aborted_at,
        "step_count": len(plan.steps),
        "executed_count": sum(1 for r in results if r.skipped_reason is None),
    }
