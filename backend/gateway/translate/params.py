"""请求方向双向参数转换（OpenAI Chat ⇄ Anthropic Messages）。

入口：
- openai_request_to_anthropic(openai_body) -> anthropic_body
- anthropic_request_to_openai(anthropic_body) -> openai_body

设计：`docs/GATEWAY.md` §4.1–§4.4
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from backend.packages.harness.liuruyan.llm.clients.anthropic_message_converter import (
    convert_tools_to_anthropic,
    to_anthropic_messages,
)


# ─────────────────────────── OpenAI → Anthropic ───────────────────────────


def openai_request_to_anthropic(body: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI Chat Completions 请求体 → Anthropic Messages 请求体。

    输入是 client 发的 OpenAI body（含 model/messages/tools/...），返回供 Anthropic 上游
    的 body（model 由上层填 vendor_model 覆盖，这里保留请求里原 model 字段不动）。
    """
    out: Dict[str, Any] = {}

    # ── messages + system（system 抽到顶层）
    messages_in = body.get("messages") or []
    if not isinstance(messages_in, list):
        messages_in = []
    system, anthropic_msgs = to_anthropic_messages(messages_in)
    if system:
        out["system"] = system
    out["messages"] = anthropic_msgs

    # ── model
    if body.get("model"):
        out["model"] = body["model"]

    # ── max_tokens（Anthropic 必填，OpenAI 缺省时给 4096）
    mt = body.get("max_tokens")
    out["max_tokens"] = int(mt) if isinstance(mt, (int, float)) and mt > 0 else 4096

    # ── 直传的标量参数
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"), ("stream", "stream")):
        if src in body and body[src] is not None:
            out[dst] = body[src]

    # ── stop → stop_sequences（OpenAI str|list[str] → Anthropic list[str]）
    stop = body.get("stop")
    if isinstance(stop, str) and stop:
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        out["stop_sequences"] = [str(s) for s in stop if s]

    # ── tools
    tools = body.get("tools") or []
    if isinstance(tools, list) and tools:
        out["tools"] = convert_tools_to_anthropic(tools)

    # ── tool_choice
    tc = body.get("tool_choice")
    if tc is not None:
        translated = _openai_tool_choice_to_anthropic(tc)
        if translated is not None:
            out["tool_choice"] = translated
        elif tc == "none":
            # Anthropic 端只能通过不发 tools 字段表达"不用工具"
            out.pop("tools", None)

    # ── n>1 不支持
    if isinstance(body.get("n"), int) and body["n"] > 1:
        logger.warning("[GATEWAY-TRANSLATE-WARN] OpenAI n>1 not supported by Anthropic; forcing n=1")

    return out


def _openai_tool_choice_to_anthropic(tc: Any) -> Optional[Dict[str, Any]]:
    """OpenAI tool_choice → Anthropic tool_choice。返回 None 代表调用方按"丢 tools"处理。"""
    if tc == "auto":
        return {"type": "auto"}
    if tc == "required":
        return {"type": "any"}
    if tc == "none":
        return None  # 上层移除 tools
    if isinstance(tc, dict):
        if tc.get("type") == "function":
            fn = tc.get("function") or {}
            name = fn.get("name")
            if name:
                return {"type": "tool", "name": name}
    return {"type": "auto"}  # 兜底 auto


# ─────────────────────────── Anthropic → OpenAI ───────────────────────────


def anthropic_request_to_openai(body: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic Messages 请求体 → OpenAI Chat Completions 请求体。

    输入是 client 发的 Anthropic body（含 model/messages/system/tools/...），返回供 OpenAI
    上游的 body。
    """
    out: Dict[str, Any] = {}

    # ── messages（含把 top-level system 拼到首条）
    messages: List[Dict[str, Any]] = []
    system = body.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        # Anthropic system 也支持 content blocks 形式
        text = "\n".join(
            p.get("text", "") for p in system if isinstance(p, dict) and p.get("type") == "text"
        )
        if text:
            messages.append({"role": "system", "content": text})

    for m in body.get("messages") or []:
        translated = _anthropic_message_to_openai(m)
        messages.extend(translated)

    # 净化（孤儿剥离 / 角色合并）—— 复用 harness 的 to_openai_messages
    from backend.packages.harness.liuruyan.llm.clients.openai_message_converter import (
        to_openai_messages,
    )
    out["messages"] = to_openai_messages(messages)

    # ── model
    if body.get("model"):
        out["model"] = body["model"]

    # ── 直传的标量参数
    for src, dst in (
        ("max_tokens", "max_tokens"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stream", "stream"),
    ):
        if src in body and body[src] is not None:
            out[dst] = body[src]

    # ── stop_sequences → stop
    ss = body.get("stop_sequences")
    if isinstance(ss, list) and ss:
        out["stop"] = list(ss) if len(ss) > 1 else ss[0]

    # ── tools
    tools = body.get("tools") or []
    if isinstance(tools, list) and tools:
        out["tools"] = _convert_tools_anthropic_to_openai(tools)

    # ── tool_choice
    tc = body.get("tool_choice")
    if tc is not None:
        translated = _anthropic_tool_choice_to_openai(tc)
        if translated is not None:
            out["tool_choice"] = translated

    return out


def _anthropic_message_to_openai(m: Dict[str, Any]) -> List[Dict[str, Any]]:
    """单条 Anthropic message → 0~N 条 OpenAI std messages。

    Anthropic user message 含 tool_result blocks 时，每个 tool_result 拆为一条独立的
    OpenAI role="tool" 消息（其余 user 内容拼成一条 user 消息）。
    """
    if not isinstance(m, dict):
        return []
    role = m.get("role")
    content = m.get("content", "")

    # 简单字符串 content
    if isinstance(content, str):
        return [{"role": role or "user", "content": content}]

    if not isinstance(content, list):
        return [{"role": role or "user", "content": str(content)}]

    # ── assistant：text + thinking + tool_use
    if role == "assistant":
        entry: Dict[str, Any] = {"role": "assistant", "content": ""}
        text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        reasoning_parts: List[str] = []

        for b in content:
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

        entry["content"] = "\n".join(text_parts)
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if reasoning_parts:
            entry["reasoning_content"] = "\n".join(reasoning_parts)
        return [entry]

    # ── user：text + image + tool_result（tool_result 各自成 role=tool）
    user_blocks: List[Dict[str, Any]] = []
    tool_msgs: List[Dict[str, Any]] = []
    has_image = False
    for b in content:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            t = b.get("text", "")
            if t:
                user_blocks.append({"type": "text", "text": t})
        elif btype == "image":
            # 保留 Anthropic `image` block 原貌，让 to_openai_messages 走它的 vision 转换
            # 路径自动转 image_url（避免重复实现+多走一遍净化）。
            has_image = True
            user_blocks.append(b)
        elif btype == "tool_result":
            tc_id = str(b.get("tool_use_id") or "")
            tc_content = b.get("content", "")
            if isinstance(tc_content, list):
                # content blocks → 拼字符串（image 块降级为占位）
                parts: List[str] = []
                for sub in tc_content:
                    if not isinstance(sub, dict):
                        continue
                    if sub.get("type") == "text":
                        parts.append(sub.get("text", ""))
                    elif sub.get("type") == "image":
                        parts.append("[image attachment present in result; vision unsupported on this provider]")
                tc_content = "\n".join(p for p in parts if p)
            elif not isinstance(tc_content, str):
                tc_content = json.dumps(tc_content, ensure_ascii=False)
            if not tc_content:
                tc_content = "(no output)"
            tool_msgs.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tc_content,
            })

    out: List[Dict[str, Any]] = list(tool_msgs)
    if user_blocks:
        # 没图片就拼成纯字符串（OpenAI 不强制 list 形态）
        if not has_image:
            text = "\n".join(b.get("text", "") for b in user_blocks if b.get("type") == "text")
            if text:
                out.append({"role": "user", "content": text})
        else:
            out.append({"role": "user", "content": user_blocks})
    return out


def _convert_tools_anthropic_to_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Anthropic tool 定义 → OpenAI 风格。"""
    result: List[Dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or ""
        if not name:
            continue
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description") or "",
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return result


def _anthropic_tool_choice_to_openai(tc: Any) -> Optional[Any]:
    """Anthropic tool_choice → OpenAI tool_choice。"""
    if not isinstance(tc, dict):
        return None
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "tool":
        name = tc.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    if t == "none":
        return "none"
    return None


__all__ = [
    "openai_request_to_anthropic",
    "anthropic_request_to_openai",
]
