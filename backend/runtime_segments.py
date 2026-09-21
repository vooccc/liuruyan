import json
from typing import Any, Dict, List, Optional

from .tool_id_contract import ensure_tool_id


def normalize_session_segments(
    segments: Optional[List[Dict]],
    fallback_text: str = "",
    agent_id: Optional[str] = None,
) -> List[Dict]:
    normalized: List[Dict] = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        copied = dict(segment)
        copied["id"] = len(normalized)
        if agent_id and not copied.get("agent_id"):
            copied["agent_id"] = agent_id
        normalized.append(copied)

    if not normalized and fallback_text:
        text_segment: Dict[str, Any] = {
            "id": 0,
            "type": "text",
            "content": fallback_text,
        }
        if agent_id:
            text_segment["agent_id"] = agent_id
        normalized.append(text_segment)

    return normalized


def append_stream_event_segment(
    segments: List[Dict],
    event: Dict,
    agent_id: Optional[str] = None,
) -> None:
    etype = event.get("type", "content")

    if etype == "thinking":
        chunk = event.get("text", "") or ""
        if not chunk:
            return
        if segments and segments[-1].get("type") == "thinking":
            segments[-1]["content"] = (segments[-1].get("content", "") or "") + chunk
            return
        segments.append({
            "id": len(segments),
            "type": "thinking",
            "content": chunk,
            "agent_id": agent_id,
        })
        return

    if etype == "content":
        chunk = event.get("text", "") or ""
        if not chunk:
            return
        if segments and segments[-1].get("type") == "text":
            segments[-1]["content"] = (segments[-1].get("content", "") or "") + chunk
            return
        segments.append({
            "id": len(segments),
            "type": "text",
            "content": chunk,
            "agent_id": agent_id,
        })
        return

    if etype == "system_notice":
        notice_content = event.get("content", event.get("text", "")) or ""
        if not notice_content.strip():
            return
        segment = {
            "id": len(segments),
            "type": "system_notice",
            "kind": event.get("kind"),
            "content": notice_content,
            "agent_id": agent_id,
        }
        if event.get("payload") is not None:
            segment["payload"] = event.get("payload")
        segments.append(segment)
        return

    if etype == "tool_call":
        # P0 契约：tool_use_id 入盘前保证非空。上游（anthropic_client / openai_client）
        # 理应已 ensure_tool_id；此处再兜一层防御，漏传时 synthesize + 打日志。
        raw_tool_id = event.get("tool_use_id") or event.get("id")
        segments.append({
            "id": len(segments),
            "type": "tool_call",
            "tool_name": event.get("name") or "",
            "name": event.get("name") or "",
            "arguments": event.get("input"),
            "input": event.get("input"),
            "tool_use_id": ensure_tool_id(raw_tool_id, origin="runtime_segment_tool_call"),
            "agent_id": agent_id,
        })
        return

    if etype == "tool_result":
        # P0 契约同上：tool_use_id 必须与对应 tool_call 配对，空值 → synthesize + 日志
        result = event.get("result")
        seg: Dict[str, Any] = {
            "id": len(segments),
            "type": "tool_result",
            "name": event.get("name") or "",
            "tool_use_id": ensure_tool_id(event.get("tool_use_id"), origin="runtime_segment_tool_result"),
            "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
            "result": result,
            "agent_id": agent_id,
        }
        segments.append(seg)
        return

    if etype == "tool_image_attachment":
        # cline 流派 2（2026-05-12 流派切换）：本轮工具产出的 image 集中收集
        # 拼成一条独立 user message，紧跟 tool_results 之后。runtime_segments
        # 落到 tool_image_attachment segment，main._segments_to_anthropic_blocks
        # 在 split 输出里给 image_user_messages 字段，session 写盘时分两条 user
        # message（先 tool_results，后 image attachment）。模型路径见
        # backend/llm_client.py:_split_text_and_image_blocks。
        cb = event.get("content_blocks")
        if not (isinstance(cb, list) and cb):
            return
        segments.append({
            "id": len(segments),
            "type": "tool_image_attachment",
            "tool_use_ids": list(event.get("tool_use_ids") or []),
            "tool_names": list(event.get("tool_names") or []),
            "content_blocks": cb,
            "agent_id": agent_id,
        })
        return


def finalize_stream_segments(
    segments: List[Dict],
    response_text: str,
    agent_id: Optional[str] = None,
) -> List[Dict]:
    has_text = any(segment.get("type") == "text" for segment in segments if isinstance(segment, dict))
    if response_text and not has_text:
        segments.append({
            "id": len(segments),
            "type": "text",
            "content": response_text,
            "agent_id": agent_id,
        })
    return normalize_session_segments(segments, response_text, agent_id)


def build_session_payload_from_segments(
    segments: Optional[List[Dict]],
    response_text: str,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    finalized_segments = finalize_stream_segments(list(segments or []), response_text, agent_id)
    return {
        "content": response_text,
        "segments": finalized_segments,
    }


def collect_runtime_event(
    collected_segments: List[Dict[str, Any]],
    event: Dict[str, Any],
    *,
    agent_id: Optional[str] = None,
) -> str:
    """Append one runtime event into session segments and return any text delta."""
    if not isinstance(event, dict):
        return ""

    text_delta = event.get("text", "") if event.get("type", "content") == "content" else ""
    append_stream_event_segment(collected_segments, event, agent_id)
    return text_delta or ""


def encode_sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def extract_assistant_text(content: str) -> str:
    """从存储的 assistant 内容中提取可读文本。"""
    if not content:
        return ""
    try:
        parsed = json.loads(content)
        depth = 0
        while depth < 3 and isinstance(parsed, dict) and isinstance(parsed.get("content"), str) and parsed["content"].strip().startswith("{"):
            try:
                inner = json.loads(parsed["content"])
            except Exception:
                break
            if isinstance(inner, dict):
                parsed = inner
                depth += 1
            else:
                break
        if isinstance(parsed, dict):
            segments = parsed.get("segments")
            if isinstance(segments, list) and segments:
                text_parts = [seg.get("content", "") for seg in segments if isinstance(seg, dict) and seg.get("type") == "text"]
                extracted = "".join(text_parts).strip()
                if extracted:
                    return extracted
            if isinstance(parsed.get("content"), str):
                return parsed["content"]
    except Exception:
        pass
    return content
