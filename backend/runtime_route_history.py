"""
Route history ledger for runtime scheduling decisions.

This module persists structured route decisions on the parent session so
policy, APIs, and UI can share one source of truth instead of reverse
parsing assistant notices.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional

from .runtime_budget import (
    AGGREGATE_BUDGET_WARNING_KIND,
    is_runtime_budget_exhaustion_kind,
    is_runtime_budget_notice_kind,
    is_runtime_budget_warning_kind,
)
from .runtime_route_contract import (
    normalize_route_budget_state,
    normalize_route_diagnostics,
    normalize_route_runtime_snapshot,
)
from .session import Session

logger = logging.getLogger(__name__)


ROUTE_HISTORY_METADATA_KEY = "runtime_route_history"
ROUTE_HISTORY_LIMIT = 120
ROUTE_EVENT_LIMIT = 24

_FINAL_OUTCOME_PRIORITY = {
    "worker_result": 1,
    "worker_failed": 1,
    "worker_cancelled": 1,
    "fallback_to_inline": 1,
    "worker_plan_completed": 2,
    "worker_plan_failed": 2,
}


def format_route_strategy_label(strategy: str) -> str:
    normalized = str(strategy or "").strip()
    if normalized == "worker_plan":
        return "Worker Plan"
    if normalized == "worker":
        return "单 Worker"
    return "Inline"


def format_route_confidence_label(confidence: str) -> str:
    return {
        "high": "高",
        "medium": "中",
        "low": "低",
    }.get(str(confidence or "").strip().lower(), str(confidence or "").strip() or "未知")


def format_route_source_label(source: str) -> str:
    return {
        "policy": "策略",
        "heuristic": "启发式",
        "model": "模型",
        "fallback": "回退",
        "startup_fallback": "启动回退",
    }.get(str(source or "").strip(), str(source or "").strip() or "未知")


def _resolve_route_summary_target(title: str, task: str) -> str:
    normalized_title = str(title or "").strip()
    normalized_task = str(task or "").strip()
    return normalized_title or normalized_task or "未命名任务"


def build_route_submission_notice_content(route: Dict[str, Any]) -> str:
    strategy = str(route.get("strategy") or "inline").strip() or "inline"
    title = str(route.get("title") or "").strip()
    task = str(route.get("task") or "").strip()
    preview = route.get("plan_preview") or {}
    submitted_worker = route.get("worker") or {}
    submitted_plan = route.get("plan") or {}
    summary_target = _resolve_route_summary_target(title, task)

    if strategy == "worker_plan":
        return (
            f"已调度到 Worker Plan：{summary_target}\n"
            f"步骤数：{submitted_plan.get('worker_count') or preview.get('step_count') or 0} | "
            f"阶段数：{submitted_plan.get('stage_count') or 0}"
        )

    if strategy == "worker":
        return (
            f"已调度到单 Worker：{summary_target}\n"
            f"Worker：{submitted_worker.get('task_id') or 'unknown'} | "
            f"状态：{submitted_worker.get('status') or 'submitted'}"
        )

    return f"保持 Inline 处理：{summary_target}"


def build_route_history_submission_summary(entry: Dict[str, Any]) -> str:
    summary_target = _resolve_route_summary_target(entry.get("title") or "", entry.get("task") or "")
    return f"任务已路由到{format_route_strategy_label(str(entry.get('strategy') or 'inline'))}：{summary_target}"


def _now_iso() -> str:
    return datetime.now().isoformat()


def _ensure_session_metadata(session: Session) -> Dict[str, Any]:
    if not isinstance(session.metadata, dict):
        session.metadata = {}
    return session.metadata


def _normalize_history(session: Session) -> List[Dict[str, Any]]:
    metadata = _ensure_session_metadata(session)
    raw_history = metadata.get(ROUTE_HISTORY_METADATA_KEY)
    if not isinstance(raw_history, list):
        return []
    return [deepcopy(item) for item in raw_history if isinstance(item, dict)]


def _persist_history(session: Session, history: List[Dict[str, Any]]) -> None:
    metadata = _ensure_session_metadata(session)
    metadata[ROUTE_HISTORY_METADATA_KEY] = history[-ROUTE_HISTORY_LIMIT:]
    session.updated_at = _now_iso()
    session.last_active_at = session.updated_at
    session.save_meta()


def _find_entry_index(
    history: List[Dict[str, Any]],
    *,
    route_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    plan_id: Optional[str] = None,
) -> Optional[int]:
    for index in range(len(history) - 1, -1, -1):
        entry = history[index]
        if route_id and entry.get("route_id") == route_id:
            return index
        if worker_id and entry.get("worker_id") == worker_id:
            return index
        if plan_id and entry.get("plan_id") == plan_id:
            return index
    return None


def _append_route_event(
    entry: Dict[str, Any],
    *,
    kind: str,
    status: Optional[str],
    summary: str,
    payload: Optional[Dict[str, Any]] = None,
    recorded_at: Optional[str] = None,
) -> None:
    events = entry.setdefault("events", [])
    event = {
        "kind": kind,
        "status": status,
        "summary": summary,
        "recorded_at": recorded_at or _now_iso(),
    }
    if payload:
        event["payload"] = deepcopy(payload)
    events.append(event)
    if len(events) > ROUTE_EVENT_LIMIT:
        del events[:-ROUTE_EVENT_LIMIT]


def _update_budget_state(
    entry: Dict[str, Any],
    *,
    kind: str,
    payload: Dict[str, Any],
    recorded_at: str,
) -> None:
    if not is_runtime_budget_notice_kind(kind):
        return

    budget_state = normalize_route_budget_state(entry.get("budget_state"))
    budget_state["last_kind"] = kind
    budget_state["last_recorded_at"] = recorded_at
    if is_runtime_budget_warning_kind(kind):
        budget_state["warning_count"] += 1
    if is_runtime_budget_exhaustion_kind(kind):
        budget_state["exhausted_count"] += 1

    snapshot = payload.get("snapshot")
    if isinstance(snapshot, dict):
        budget_state["last_snapshot"] = deepcopy(snapshot)

    pressure_axes = payload.get("pressure_axes")
    if isinstance(pressure_axes, list):
        budget_state["pressure_axes"] = [str(item) for item in pressure_axes if str(item).strip()]

    limit_type = str(payload.get("limit_type") or "").strip()
    if limit_type:
        budget_state["limit_type"] = limit_type
    entry["budget_state"] = normalize_route_budget_state(budget_state)


def _should_replace_final_outcome(
    existing: Optional[Dict[str, Any]],
    incoming: Dict[str, Any],
) -> bool:
    if not existing:
        return True

    existing_rank = _FINAL_OUTCOME_PRIORITY.get(str(existing.get("kind") or ""), 0)
    incoming_rank = _FINAL_OUTCOME_PRIORITY.get(str(incoming.get("kind") or ""), 0)
    return incoming_rank >= existing_rank


def list_session_route_history(
    session: Session,
    *,
    limit: Optional[int] = None,
    newest_first: bool = True,
) -> List[Dict[str, Any]]:
    history = _normalize_history(session)
    history.sort(key=lambda item: item.get("created_at") or "", reverse=newest_first)
    if limit is not None and limit >= 0:
        history = history[:limit]
    return deepcopy(history)


def record_route_submission(session: Session, route: Dict[str, Any]) -> Dict[str, Any]:
    history = _normalize_history(session)
    route_id = str(route.get("route_id") or "").strip()
    if not route_id:
        raise ValueError("route_id is required to record route submission")

    now = _now_iso()
    worker = route.get("worker") or {}
    plan = route.get("plan") or {}
    execution_status = (
        str(worker.get("status") or "").strip()
        or str(plan.get("status") or "").strip()
        or "submitted"
    )

    entry = {
        "route_id": route_id,
        "task": str(route.get("task") or "").strip(),
        "title": str(route.get("title") or "").strip(),
        "strategy": str(route.get("strategy") or "inline").strip() or "inline",
        "confidence": str(route.get("confidence") or "unknown").strip() or "unknown",
        "source": str(route.get("source") or "unknown").strip() or "unknown",
        "task_class": str(route.get("task_class") or "bounded_execution").strip() or "bounded_execution",
        "priority": route.get("priority", 0),
        "timeout": route.get("timeout", 300),
        "max_steps": route.get("max_steps"),
        "rationale": str(route.get("rationale") or "").strip(),
        "runtime_snapshot": normalize_route_runtime_snapshot(route.get("runtime_snapshot")),
        "route_diagnostics": normalize_route_diagnostics(route.get("route_diagnostics")),
        "worker_id": worker.get("task_id"),
        "plan_id": plan.get("plan_id"),
        "execution_status": execution_status,
        "created_at": now,
        "updated_at": now,
        "final_outcome": None,
        "budget_state": normalize_route_budget_state(route.get("budget_state")),
        "events": [],
    }
    _append_route_event(
        entry,
        kind="task_routed",
        status=execution_status,
        summary=build_route_history_submission_summary(entry),
        payload={
            "worker_id": entry["worker_id"],
            "plan_id": entry["plan_id"],
            "source": entry["source"],
            "confidence": entry["confidence"],
        },
        recorded_at=now,
    )

    existing_index = _find_entry_index(history, route_id=route_id)
    if existing_index is None:
        history.append(entry)
    else:
        history[existing_index] = entry

    _persist_history(session, history)
    return deepcopy(entry)


def record_route_execution_event(
    session: Session,
    *,
    kind: str,
    execution_status: Optional[str] = None,
    summary: str = "",
    route_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    terminal: bool = False,
    outcome_status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    history = _normalize_history(session)
    entry_index = _find_entry_index(
        history,
        route_id=(route_id or "").strip() or None,
        worker_id=(worker_id or "").strip() or None,
        plan_id=(plan_id or "").strip() or None,
    )
    if entry_index is None:
        return None

    now = _now_iso()
    entry = history[entry_index]
    if route_id and not entry.get("route_id"):
        entry["route_id"] = route_id
    if worker_id and not entry.get("worker_id"):
        entry["worker_id"] = worker_id
    if plan_id and not entry.get("plan_id"):
        entry["plan_id"] = plan_id
    if execution_status:
        entry["execution_status"] = execution_status
    entry["updated_at"] = now

    event_payload = deepcopy(payload or {})
    if worker_id and "worker_id" not in event_payload:
        event_payload["worker_id"] = worker_id
    if plan_id and "plan_id" not in event_payload:
        event_payload["plan_id"] = plan_id
    _append_route_event(
        entry,
        kind=kind,
        status=execution_status or entry.get("execution_status"),
        summary=summary,
        payload=event_payload or None,
        recorded_at=now,
    )
    _update_budget_state(
        entry,
        kind=kind,
        payload=event_payload,
        recorded_at=now,
    )

    if terminal:
        final_outcome = {
            "kind": kind,
            "status": outcome_status or execution_status or entry.get("execution_status") or "completed",
            "summary": summary,
            "recorded_at": now,
        }
        if event_payload:
            final_outcome["payload"] = event_payload
        if _should_replace_final_outcome(entry.get("final_outcome"), final_outcome):
            entry["final_outcome"] = final_outcome
            entry["execution_status"] = final_outcome["status"]

    history[entry_index] = entry
    _persist_history(session, history)
    return deepcopy(entry)


def summarize_route_history_outcomes(
    session: Session,
    *,
    limit: int = 24,
) -> Dict[str, int]:
    counts = {
        "task_routed": 0,
        "worker_result": 0,
        "worker_failed": 0,
        "worker_cancelled": 0,
        "fallback_to_inline": 0,
        "worker_plan_completed": 0,
        "worker_plan_failed": 0,
        "startup_fallback_total": 0,
        "dependency_failed_total": 0,
        "timeout_total": 0,
        "runtime_error_total": 0,
        AGGREGATE_BUDGET_WARNING_KIND: 0,
        "aggregate_wall_clock_exhausted": 0,
        "aggregate_tool_calls_exhausted": 0,
        "aggregate_tool_output_exhausted": 0,
    }

    history = list_session_route_history(session, limit=limit, newest_first=True)
    for entry in history:
        counts["task_routed"] += 1
        final_outcome = entry.get("final_outcome") or {}
        kind = str(final_outcome.get("kind") or "").strip()
        if kind in counts:
            counts[kind] += 1
        payload = final_outcome.get("payload") or {}
        failure_reason_kind = str(payload.get("failure_reason_kind") or "").strip()
        if failure_reason_kind in counts:
            counts[failure_reason_kind] += 1
        elif kind == "fallback_to_inline":
            counts["startup_fallback_total"] += 1
        for event in entry.get("events") or []:
            event_kind = str(event.get("kind") or "").strip()
            if is_runtime_budget_notice_kind(event_kind):
                counts[event_kind] += 1

    counts["worker_success_total"] = counts["worker_result"] + counts["worker_plan_completed"]
    counts["worker_failure_total"] = (
        counts["worker_failed"]
        + counts["worker_plan_failed"]
        + counts["worker_cancelled"]
    )
    counts["budget_warning_total"] = counts[AGGREGATE_BUDGET_WARNING_KIND]
    counts["budget_exhausted_total"] = (
        counts["aggregate_wall_clock_exhausted"]
        + counts["aggregate_tool_calls_exhausted"]
        + counts["aggregate_tool_output_exhausted"]
    )
    return counts


def summarize_route_history_budget_pressure(
    session: Session,
    *,
    limit: int = 24,
) -> Dict[str, Any]:
    history = list_session_route_history(session, limit=limit, newest_first=True)
    axis_counts: Dict[str, int] = {}
    limit_type_counts: Dict[str, int] = {}
    summary: Dict[str, Any] = {
        "tight_route_count": 0,
        "warning_routes": 0,
        "exhausted_routes": 0,
        "warning_events": 0,
        "exhausted_events": 0,
        "pressure_axes": axis_counts,
        "limit_type_counts": limit_type_counts,
        "dominant_axis": "",
        "last_kind": "",
        "last_recorded_at": "",
        "last_snapshot": {},
        "peak_highest_usage_ratio": 0.0,
    }

    for entry in history:
        budget_state = normalize_route_budget_state(entry.get("budget_state"))
        if (
            budget_state["warning_count"] <= 0
            and budget_state["exhausted_count"] <= 0
            and not budget_state["pressure_axes"]
            and not budget_state["limit_type"]
            and not budget_state["last_snapshot"]
            and not budget_state.get("last_kind")
            and not budget_state.get("last_recorded_at")
        ):
            continue

        warning_count = int(budget_state.get("warning_count", 0) or 0)
        exhausted_count = int(budget_state.get("exhausted_count", 0) or 0)
        if warning_count > 0 or exhausted_count > 0:
            summary["tight_route_count"] += 1
        if warning_count > 0:
            summary["warning_routes"] += 1
        if exhausted_count > 0:
            summary["exhausted_routes"] += 1

        summary["warning_events"] += warning_count
        summary["exhausted_events"] += exhausted_count

        for axis in budget_state.get("pressure_axes") or []:
            normalized_axis = str(axis or "").strip()
            if not normalized_axis:
                continue
            axis_counts[normalized_axis] = axis_counts.get(normalized_axis, 0) + 1

        limit_type = str(budget_state.get("limit_type") or "").strip()
        if limit_type:
            limit_type_counts[limit_type] = limit_type_counts.get(limit_type, 0) + 1

        snapshot = budget_state.get("last_snapshot")
        if isinstance(snapshot, dict):
            try:
                highest_ratio = float(snapshot.get("highest_usage_ratio", 0.0) or 0.0)
            except (TypeError, ValueError):
                highest_ratio = 0.0
            summary["peak_highest_usage_ratio"] = max(
                float(summary["peak_highest_usage_ratio"]),
                highest_ratio,
            )
            if not summary["last_snapshot"]:
                summary["last_snapshot"] = deepcopy(snapshot)

        if not summary["last_recorded_at"]:
            summary["last_recorded_at"] = str(budget_state.get("last_recorded_at") or "")
        if not summary["last_kind"]:
            summary["last_kind"] = str(budget_state.get("last_kind") or "")

    if axis_counts:
        summary["dominant_axis"] = max(
            sorted(axis_counts.items()),
            key=lambda item: item[1],
        )[0]

    return summary


# ─── plan 完成召回（plan recall）───────────────────────────────────────────
#
# 2026-05-16 引入。背景：worker.py:_deliver_plan_final_result 已砍，plan 合并交付
# 不再 append assistant message 进 transcript（"AI 回了两次"事故）。
# main agent 下一轮就完全失忆 worker 跑出了什么——这里扫 route_history 找
# worker_plan_completed 终态 entry，拼成 system 提示，由 main.py /chat 入口
# 注入到下一轮 agent.chat 的 system_extra 参数。
#
# 详见 .lry/rules/06-worker-channel-separation.md "已知 trade-off" 段。
#
# 放这里而不是 main.py 的原因：语义上属于"读 route_history → 派生 hint"，
# 跟模块职责对齐；同时独立可测（main.py 拉 fastapi 太重）。

CONSUMED_PLAN_KEYS_METADATA = "consumed_plan_route_ids"
_CONSUMED_PLAN_KEYS_RETAIN = 100


def consume_pending_plan_results(session: Optional[Session]) -> Optional[str]:
    """召回上一轮 spawn 出去的 plan 的合并交付。

    Returns:
        拼好的 system_extra hint 文本；session 为空 / 无未消化 entry 时返回 None。
        消化标记在调用时立即写盘，即便后续 agent 失败也不再重投——
        "一次性 hint"语义。
    """
    if not session:
        return None

    history = list_session_route_history(session, limit=20)
    if not history:
        return None

    metadata = session.metadata if isinstance(session.metadata, dict) else {}
    consumed_raw = metadata.get(CONSUMED_PLAN_KEYS_METADATA, [])
    consumed: set[str] = set(consumed_raw) if isinstance(consumed_raw, list) else set()

    new_entries: List[Dict[str, Any]] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        final_outcome = entry.get("final_outcome")
        if not isinstance(final_outcome, dict):
            continue
        if str(final_outcome.get("status") or "").strip() != "completed":
            continue
        payload = (
            final_outcome.get("payload")
            if isinstance(final_outcome.get("payload"), dict) else {}
        )
        result_text = str(payload.get("result_text") or "").strip()
        if not result_text:
            continue
        # 只关心 plan 级；单 worker 的 worker_result 已落在对应 route-history entry，
        # 不在 plan 结果召回里重复注入。
        plan_id = str(payload.get("plan_id") or entry.get("plan_id") or "").strip()
        if not plan_id:
            continue
        route_id = str(entry.get("route_id") or "").strip()
        dedupe_key = route_id or f"plan:{plan_id}"
        if dedupe_key in consumed:
            continue
        new_entries.append({
            "title": str(entry.get("title") or "").strip() or "（未命名 plan）",
            "result_text": result_text,
            "_dedupe_key": dedupe_key,
        })

    if not new_entries:
        return None

    blocks: List[str] = [
        "## 上一轮你派出的 plan 已完成",
        "下面是 worker 们的合并交付。请基于此内容回应用户最新消息——",
        "用户在主对话流里看不到下方完整文本（只能通过派发卡/任务面板查看），",
        "如果用户问 plan 结果，请直接转述要点；如果用户接着抛新问题，请把这些交付当作上下文。",
        "（每条交付只会注入一次，下一轮就不再出现，不必重复完整复述。）",
        "",
    ]
    for entry in new_entries:
        blocks.append(f"### {entry['title']}")
        blocks.append(entry["result_text"])
        blocks.append("")

    # 写盘消化标记（幂等核心）。失败不阻塞——hint 还是会注入，下次可能重投，
    # 但"重投一次"远小于"main agent 永久失忆"的代价。
    try:
        if not isinstance(session.metadata, dict):
            session.metadata = {}
        consumed.update(entry["_dedupe_key"] for entry in new_entries)
        session.metadata[CONSUMED_PLAN_KEYS_METADATA] = list(consumed)[-_CONSUMED_PLAN_KEYS_RETAIN:]
        save_meta = getattr(session, "save_meta", None)
        if callable(save_meta):
            save_meta()
    except Exception as exc:
        logger.warning("[CHAT-PLAN-HINT] save consumed marker failed (non-fatal): %s", exc)

    return "\n".join(blocks).strip()
