from datetime import datetime
from typing import Any, Dict, Optional

from .runtime_route_contract import (
    normalize_route_budget_state,
    normalize_route_diagnostics,
    normalize_route_runtime_snapshot,
)


RUNTIME_NOTICE_PAYLOAD_VERSION = 1

RUNTIME_EVENT_TYPES = (
    "content",
    "thinking",
    "tool_call",
    "tool_result",
    "system_notice",
    "error",
)

SESSION_SEGMENT_TYPES = (
    "thinking",
    "text",
    "tool_call",
    "tool_result",
    "system_notice",
)

COLLAB_RUNTIME_EVENT_TYPES = (
    "agent_content",
    "thinking",
    "tool_call",
    "tool_result",
    "system_notice",
    "error",
)

WORKER_TRACE_EVENT_TYPES = (
    "worker_status",
)


def _assign_if_present(target: Dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return
        target[key] = normalized
        return
    target[key] = value


def build_runtime_notice_payload(
    *,
    route_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    plan_step_id: Optional[str] = None,
    plan_stage: Optional[int] = None,
    strategy: Optional[str] = None,
    confidence: Optional[str] = None,
    source: Optional[str] = None,
    task_class: Optional[str] = None,
    priority: Optional[int] = None,
    timeout: Optional[int] = None,
    execution_status: Optional[str] = None,
    task: Optional[str] = None,
    title: Optional[str] = None,
    max_steps: Optional[int] = None,
    runtime_snapshot: Optional[Dict[str, Any]] = None,
    route_diagnostics: Optional[Dict[str, Any]] = None,
    budget_state: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    refs: Dict[str, Any] = {}
    _assign_if_present(refs, "route_id", route_id)
    _assign_if_present(refs, "worker_id", worker_id)
    _assign_if_present(refs, "plan_id", plan_id)
    _assign_if_present(refs, "plan_step_id", plan_step_id)
    _assign_if_present(refs, "plan_stage", plan_stage)

    payload: Dict[str, Any] = {
        "contract_version": RUNTIME_NOTICE_PAYLOAD_VERSION,
        "refs": refs,
    }
    _assign_if_present(payload, "strategy", strategy)
    _assign_if_present(payload, "confidence", confidence)
    _assign_if_present(payload, "source", source)
    _assign_if_present(payload, "task_class", task_class)
    _assign_if_present(payload, "priority", priority)
    _assign_if_present(payload, "timeout", timeout)
    _assign_if_present(payload, "execution_status", execution_status)
    _assign_if_present(payload, "task", task)
    _assign_if_present(payload, "title", title)
    _assign_if_present(payload, "max_steps", max_steps)
    if runtime_snapshot:
        payload["runtime_snapshot"] = normalize_route_runtime_snapshot(runtime_snapshot)
    if route_diagnostics:
        payload["route_diagnostics"] = normalize_route_diagnostics(route_diagnostics)
    if budget_state:
        payload["budget_state"] = normalize_route_budget_state(budget_state)
    if extra:
        for key, value in extra.items():
            _assign_if_present(payload, str(key), value)
    return payload


def build_runtime_trace_event(
    event: Dict[str, Any],
    *,
    seq: int,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    etype = event.get("type", "content")
    record: Dict[str, Any] = {
        "seq": seq,
        "type": etype,
        "timestamp": timestamp or datetime.now().isoformat(),
    }

    if etype in ("content", "thinking"):
        record["text"] = event.get("text", "")
    elif etype == "system_notice":
        record["kind"] = event.get("kind")
        record["content"] = event.get("content", event.get("text", ""))
        if event.get("payload") is not None:
            record["payload"] = event.get("payload")
    elif etype == "tool_call":
        record["name"] = event.get("name")
        record["input"] = event.get("input")
        record["id"] = event.get("id")
        record["tool_use_id"] = event.get("tool_use_id", event.get("id"))
    elif etype == "tool_result":
        record["name"] = event.get("name")
        record["result"] = event.get("result")
        record["tool_use_id"] = event.get("tool_use_id")
    elif etype == "error":
        record["error"] = event.get("error")
    else:
        record["payload"] = {k: v for k, v in event.items() if k != "type"}

    return record


def build_runtime_transport_event(
    event: Dict[str, Any],
    *,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    content_type: str = "content",
) -> Optional[Dict[str, Any]]:
    etype = event.get("type", "content")

    if etype == "thinking":
        content = event.get("text", "")
        if not content:
            return None
        payload: Dict[str, Any] = {"type": "thinking", "content": content}
    elif etype == "content":
        content = event.get("text", "")
        if not content:
            return None
        payload = {"type": content_type, "content": content}
    elif etype == "system_notice":
        content = event.get("content", event.get("text", ""))
        if not content:
            return None
        payload = {
            "type": "system_notice",
            "kind": event.get("kind"),
            "content": content,
        }
        if event.get("payload") is not None:
            payload["payload"] = event.get("payload")
    elif etype == "tool_call":
        payload = {
            "type": "tool_call",
            "name": event.get("name"),
            "input": event.get("input"),
            "id": event.get("id"),
            "tool_use_id": event.get("tool_use_id"),
        }
    elif etype == "tool_result":
        payload = {
            "type": "tool_result",
            "name": event.get("name"),
            "result": event.get("result"),
            "tool_use_id": event.get("tool_use_id"),
        }
    elif etype == "error":
        payload = {
            "type": "error",
            "error": event.get("error"),
        }
    else:
        return None

    if agent_id:
        payload["agent_id"] = agent_id
    if session_id:
        payload["session_id"] = session_id

    # 传递 task/plan/route refs（用于前端 route progress 归并）
    refs = {}
    if event.get("worker_id"):
        refs["worker_id"] = event["worker_id"]
    if event.get("route_id"):
        refs["route_id"] = event["route_id"]
    if event.get("plan_id"):
        refs["plan_id"] = event["plan_id"]
    if event.get("plan_step_id"):
        refs["plan_step_id"] = event["plan_step_id"]
    if isinstance(event.get("plan_stage"), int):
        refs["plan_stage"] = event["plan_stage"]

    if refs:
        payload["refs"] = refs

    return payload


def build_runtime_event_from_segment(segment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    segment_type = segment.get("type")
    if segment_type == "thinking":
        return {"type": "thinking", "text": segment.get("content", "")}
    if segment_type == "text":
        return {"type": "content", "text": segment.get("content", "")}
    if segment_type == "system_notice":
        return {
            "type": "system_notice",
            "kind": segment.get("kind"),
            "content": segment.get("content", ""),
            "payload": segment.get("payload"),
        }
    if segment_type == "tool_call":
        return {
            "type": "tool_call",
            "name": segment.get("tool_name") or segment.get("name"),
            "input": segment.get("input", segment.get("arguments")),
            "tool_use_id": segment.get("tool_use_id"),
            "id": segment.get("tool_use_id"),
        }
    if segment_type == "tool_result":
        return {
            "type": "tool_result",
            "name": segment.get("name"),
            "result": segment.get("result", segment.get("content")),
            "tool_use_id": segment.get("tool_use_id"),
        }
    return None


def iter_runtime_transport_events_from_payload(
    payload: Dict[str, Any],
    *,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    content_type: str = "content",
):
    for segment in payload.get("segments", []) or []:
        if not isinstance(segment, dict):
            continue
        runtime_event = build_runtime_event_from_segment(segment)
        if not runtime_event:
            continue
        transport = build_runtime_transport_event(
            runtime_event,
            agent_id=agent_id or segment.get("agent_id"),
            session_id=session_id,
            content_type=content_type,
        )
        if transport:
            yield transport
