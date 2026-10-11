"""非流式响应双向转换（Anthropic Messages JSON ⇄ OpenAI Chat Completions JSON）。

入口：
- anthropic_response_to_openai(anthropic_json, *, model) -> openai_json
- openai_response_to_anthropic(openai_json, *, model) -> anthropic_json

设计：`docs/GATEWAY.md` §4.5–§4.6
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional


# ─────────────────────────── stop_reason ⇄ finish_reason ───────────────────────────

_ANTHROPIC_TO_OPENAI_STOP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "stop",
}
_OPENAI_TO_ANTHROPIC_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


def _anthropic_stop_to_openai(stop_reason: Optional[str]) -> Optional[str]:
    if stop_reason is None:
        return None
    return _ANTHROPIC_TO_OPENAI_STOP.get(stop_reason, "stop")


def _openai_finish_to_anthropic(finish: Optional[str]) -> Optional[str]:
    if finish is None:
        return None
    return _OPENAI_TO_ANTHROPIC_STOP.get(finish, "end_turn")


# ─────────────────────────── Anthropic → OpenAI（非流式） ───────────────────────────


def anthropic_response_to_openai(resp: Dict[str, Any], *, model: str = "") -> Dict[str, Any]:
    """Anthropic Messages 响应 JSON → OpenAI Chat Completions 响应 JSON。"""
    content_blocks = resp.get("content") or []
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    reasoning_parts: List[str] = []

    for b in content_blocks:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            t = b.get("text", "")
            if t:
                text_parts.append(t)
        elif btype == "thinking":
            t = b.get("thinking", "")
            if t:
                reasoning_parts.append(t)
        elif btype == "tool_use":
            args = b.get("input") or {}
            if not isinstance(args, (dict, list)):
                args = {}
            tool_calls.append({
                "id": str(b.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(b.get("name") or ""),
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            })

    message: Dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_parts:
        message["reasoning_content"] = "\n".join(reasoning_parts)

    finish_reason = _anthropic_stop_to_openai(resp.get("stop_reason"))
    # 如果有 tool_use 且 finish 还是 stop，矫正为 tool_calls
    if tool_calls and finish_reason == "stop":
        finish_reason = "tool_calls"

    usage = _anthropic_usage_to_openai(resp.get("usage") or {})

    return {
        "id": resp.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resp.get("model") or model or "",
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": usage,
    }


def _anthropic_usage_to_openai(u: Dict[str, Any]) -> Dict[str, Any]:
    prompt = int(u.get("input_tokens") or 0)
    completion = int(u.get("output_tokens") or 0)
    out: Dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    cache_read = u.get("cache_read_input_tokens")
    if isinstance(cache_read, int) and cache_read > 0:
        out["prompt_tokens_details"] = {"cached_tokens": cache_read}
    return out


# ─────────────────────────── OpenAI → Anthropic（非流式） ───────────────────────────


def openai_response_to_anthropic(resp: Dict[str, Any], *, model: str = "") -> Dict[str, Any]:
    """OpenAI Chat Completions 响应 JSON → Anthropic Messages 响应 JSON。"""
    choices = resp.get("choices") or []
    first = choices[0] if choices else {}
    msg = first.get("message") if isinstance(first, dict) else {} or {}
    msg = msg if isinstance(msg, dict) else {}

    content_blocks: List[Dict[str, Any]] = []

    # reasoning_content → thinking block（放最前，Anthropic 协议要求）
    reasoning = msg.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning})

    # content → text block
    content = msg.get("content")
    if isinstance(content, str) and content:
        content_blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        # 极少见：OpenAI 用 list 形态返 content（vision 输出？标准上不会）
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                t = p.get("text", "")
                if t:
                    content_blocks.append({"type": "text", "text": t})

    # tool_calls → tool_use blocks
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args_raw = fn.get("arguments", "{}")
        try:
            parsed = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            if not isinstance(parsed, (dict, list)):
                parsed = {}
        except (json.JSONDecodeError, ValueError, TypeError):
            parsed = {}
        content_blocks.append({
            "type": "tool_use",
            "id": str(tc.get("id") or ""),
            "name": str(fn.get("name") or ""),
            "input": parsed,
        })

    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]

    stop_reason = _openai_finish_to_anthropic(first.get("finish_reason") if isinstance(first, dict) else None)

    usage = _openai_usage_to_anthropic(resp.get("usage") or {})

    return {
        "id": resp.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": resp.get("model") or model or "",
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


def _openai_usage_to_anthropic(u: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "input_tokens": int(u.get("prompt_tokens") or 0),
        "output_tokens": int(u.get("completion_tokens") or 0),
    }
    details = u.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens")
        if isinstance(cached, int) and cached > 0:
            out["cache_read_input_tokens"] = cached
    return out


__all__ = [
    "anthropic_response_to_openai",
    "openai_response_to_anthropic",
]
