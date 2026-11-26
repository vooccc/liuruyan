from datetime import datetime
from typing import Any, Dict, Optional

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
    elif etype == "tool_result_update":
        # ask_user_question 阻塞等待的终态回写：同 tool_use_id 就地替换先前的
        # awaiting tool_result（前端卡片刷新 + 持久化 segment 改写）。
        payload = {
            "type": "tool_result_update",
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
