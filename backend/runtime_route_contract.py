"""
Structured route/runtime contract helpers.

Keep route decisions, diagnostics, and runtime snapshot payloads on one
stable schema so backend policy, persistence, and frontend displays do
not drift into parallel dict shapes.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional


def normalize_route_runtime_snapshot(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    normalized = snapshot or {}
    session_load = normalized.get("session_load")
    recent_outcomes = normalized.get("recent_outcomes")
    budget_pressure = normalized.get("budget_pressure")
    return {
        "session_load": deepcopy(session_load) if isinstance(session_load, dict) else {},
        "recent_outcomes": deepcopy(recent_outcomes) if isinstance(recent_outcomes, dict) else {},
        "budget_pressure": deepcopy(budget_pressure) if isinstance(budget_pressure, dict) else {},
    }


def build_route_runtime_snapshot(
    *,
    session_load: Optional[Dict[str, Any]] = None,
    recent_outcomes: Optional[Dict[str, Any]] = None,
    budget_pressure: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return normalize_route_runtime_snapshot(
        {
            "session_load": session_load or {},
            "recent_outcomes": recent_outcomes or {},
            "budget_pressure": budget_pressure or {},
        }
    )


def normalize_route_budget_state(budget_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    normalized = budget_state or {}
    pressure_axes = normalized.get("pressure_axes")
    last_snapshot = normalized.get("last_snapshot")
    result = {
        "warning_count": int(normalized.get("warning_count", 0) or 0),
        "exhausted_count": int(normalized.get("exhausted_count", 0) or 0),
        "limit_type": str(normalized.get("limit_type") or "").strip(),
        "pressure_axes": [
            str(item).strip()
            for item in (pressure_axes or [])
            if str(item).strip()
        ] if isinstance(pressure_axes, list) else [],
        "last_snapshot": deepcopy(last_snapshot) if isinstance(last_snapshot, dict) else {},
    }
    last_kind = str(normalized.get("last_kind") or "").strip()
    last_recorded_at = str(normalized.get("last_recorded_at") or "").strip()
    if last_kind:
        result["last_kind"] = last_kind
    if last_recorded_at:
        result["last_recorded_at"] = last_recorded_at
    return result


def format_route_runtime_snapshot_text(snapshot: Optional[Dict[str, Any]]) -> str:
    normalized = normalize_route_runtime_snapshot(snapshot)
    load = normalized.get("session_load") or {}
    workers = load.get("workers") or {}
    plans = load.get("plans") or {}
    capacity = load.get("capacity") or {}
    recent = normalized.get("recent_outcomes") or {}
    budget_pressure = normalized.get("budget_pressure") or {}

    return (
        "runtime_state:\n"
        f"- workers active={workers.get('active', 0)} "
        f"(running={workers.get('running', 0)}, queued={workers.get('queued', 0)}, pending={workers.get('pending', 0)})\n"
        f"- plans active={plans.get('active', 0)} "
        f"(running={plans.get('running', 0)}, queued={plans.get('queued', 0)})\n"
        f"- capacity global_remaining={capacity.get('global_remaining_slots', 0)} "
        f"session_remaining_running={capacity.get('session_remaining_running_slots', 0)} "
        f"session_remaining_enqueued={capacity.get('session_remaining_enqueued_slots', 0)}\n"
        f"- recent routed={recent.get('task_routed', 0)} "
        f"successes={recent.get('worker_success_total', 0)} "
        f"failures={recent.get('worker_failure_total', 0)} "
        f"plan_failures={recent.get('worker_plan_failed', 0)}\n"
        f"- recent recoverability startup_fallbacks={recent.get('startup_fallback_total', 0)} "
        f"dependency_failures={recent.get('dependency_failed_total', 0)} "
        f"timeouts={recent.get('timeout_total', 0)}\n"
        f"- recent pressure budget_warnings={recent.get('budget_warning_total', 0)} "
        f"budget_exhausted={recent.get('budget_exhausted_total', 0)} "
        f"fallback_inline={recent.get('fallback_to_inline', 0)}\n"
        f"- budget dominant_axis={budget_pressure.get('dominant_axis') or 'none'} "
        f"tight_routes={budget_pressure.get('tight_route_count', 0)} "
        f"peak_ratio={budget_pressure.get('peak_highest_usage_ratio', 0.0)}"
    )


def normalize_route_diagnostics(diagnostics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    normalized = diagnostics or {}
    matched_conditions = normalized.get("matched_conditions")
    observed = normalized.get("observed")

    result = {
        "kind": str(normalized.get("kind") or "").strip(),
        "rule_id": str(normalized.get("rule_id") or "").strip(),
        "title": str(normalized.get("title") or "").strip(),
        "summary": str(normalized.get("summary") or "").strip(),
        "matched_conditions": [
            str(item).strip()
            for item in (matched_conditions or [])
            if str(item).strip()
        ] if isinstance(matched_conditions, list) else [],
        "observed": deepcopy(observed) if isinstance(observed, dict) else {},
    }
    suggested_action = str(normalized.get("suggested_action") or "").strip()
    if suggested_action:
        result["suggested_action"] = suggested_action
    return result


def build_route_diagnostics(
    *,
    kind: str,
    rule_id: str,
    title: str,
    summary: str,
    matched_conditions: Optional[List[str]] = None,
    observed: Optional[Dict[str, Any]] = None,
    suggested_action: Optional[str] = None,
) -> Dict[str, Any]:
    return normalize_route_diagnostics(
        {
            "kind": kind,
            "rule_id": rule_id,
            "title": title,
            "summary": summary,
            "matched_conditions": matched_conditions or [],
            "observed": observed or {},
            "suggested_action": suggested_action,
        }
    )


def build_route_decision(
    *,
    strategy: str,
    confidence: str,
    rationale: str,
    priority: int,
    timeout: int,
    source: str,
    task: str,
    title: str,
    max_steps: int,
    task_class: Optional[str] = None,
    runtime_snapshot: Optional[Dict[str, Any]] = None,
    route_diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    decision = {
        "strategy": str(strategy or "").strip() or "inline",
        "confidence": str(confidence or "").strip() or "unknown",
        "rationale": str(rationale or "").strip(),
        "priority": int(priority),
        "timeout": int(timeout),
        "source": str(source or "").strip() or "unknown",
        "task": str(task or "").strip(),
        "title": (str(title or "").strip() or str(task or "").strip()[:48]),
        "max_steps": max(1, min(int(max_steps), 8)),
        "task_class": (str(task_class or "bounded_execution").strip() or "bounded_execution"),
    }
    if runtime_snapshot:
        decision["runtime_snapshot"] = normalize_route_runtime_snapshot(runtime_snapshot)
    if route_diagnostics:
        decision["route_diagnostics"] = normalize_route_diagnostics(route_diagnostics)
    return decision
