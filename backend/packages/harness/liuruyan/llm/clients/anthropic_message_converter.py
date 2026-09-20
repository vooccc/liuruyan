"""OpenAI ChatCompletion 标准消息 → Anthropic Messages API 格式。

P1-B-2 搬迁说明：
    搬自 `backend/llm_adapters/anthropic_native.py`（2026-05-02）。
    原文件改为 thin shim 转发本模块，保持 backend 调用方
    （agent.py / llm_adapters/__init__.py / 测试套）import 路径不变。

    搬来的原因：harness/llm/clients/anthropic.py（AnthropicApiClient）
    需要 `to_anthropic_messages` 把 CoreRequest.messages 翻译成
    Anthropic SDK params。按"harness 永不 import backend.*"硬约束，
    converter 真身必须在 harness。

设计参考 `_research/hermes-agent-main/agent/anthropic_adapter.py:1195-1430
convert_messages_to_anthropic`，简化版只处理文本 + tool_use/tool_result + thinking
（多模态留待后续切片）。

输入：OpenAI std messages（list of dict），通常是 history_to_openai_std.history_entry_to_openai_std 的输出
输出：(system_prompt, anthropic_messages)
- system_prompt: str | None（Anthropic API 要求 system 单独传，不在 messages 里）
- anthropic_messages: list of {role, content}，content 是 list of content blocks

核心保障：
1. tool_use_id 经 _sanitize_tool_id 清洗（Anthropic 只接受 [a-zA-Z0-9_-]）
2. 孤儿 tool_use（无对应 tool_result）剥离 — 防 Anthropic 400
3. 孤儿 tool_result（无对应 tool_use）剥离 — 防 Anthropic 400
4. 连续 user 合并、空 assistant 兜底，强制角色交替
5. tool_result 合并到下一条 user 消息（Anthropic 协议要求）
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..tool_id_contract import synthesize_tool_id


_TOOL_ID_PATTERN = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_tool_id(tool_id: Any, *, origin: str = "anthropic_adapter") -> str:
    """Anthropic IDs 只允许 [a-zA-Z0-9_-]，其它替换为 _。

    P0 修复：空值不再返回硬编码 "tool_0"——两次调用会碰撞到同一占位，
    让 assistant.tool_use 与 user.tool_result 的 id 配对失真，Anthropic 400。
    改为 synthesize 一个全局唯一 id 并打 [TOOL-ID-SYNTHESIZED] 日志（说明
    上游漏了 id，是 bug 信号）。
    """
    if not tool_id:
        return synthesize_tool_id(origin)
    s = str(tool_id).strip()
    if not s:
        return synthesize_tool_id(origin)
    sanitized = _TOOL_ID_PATTERN.sub("_", s)
    return sanitized or synthesize_tool_id(origin)


def to_anthropic_messages(messages: List[Dict[str, Any]]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """OpenAI std messages → (system, anthropic_messages)."""
    system: Optional[str] = None
    result: List[Dict[str, Any]] = []

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content = m.get("content", "")

        if role == "system":
            # Anthropic 把 system 单独传出去，多条 system 用换行拼
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            else:
                text = str(content)
            system = text if system is None else f"{system}\n{text}"
            continue

        if role == "assistant":
            blocks: List[Dict[str, Any]] = []

            # thinking blocks 必须放在 text/tool_use 之前（Anthropic 协议）
            reasoning = m.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                blocks.append({"type": "thinking", "thinking": reasoning})

            # text content
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                # 幂等：识别所有已是 Anthropic content block 的 part（thinking / text /
                # tool_use / image / image_ref），不只 text。否则
                # AnthropicApiClient._build_sdk_params 在每次 send_stream 时会二次调本
                # 函数，把上一轮 assistant_msg 里的 thinking / tool_use block 全吞掉，
                # 导致模型下一轮看不到自己刚调的工具→重复调同一个工具直至 budget 满。
                # 事故复现：2026-05-12 read_file 读图场景，claude 重复调 read_file 3 次。
                for part in content:
                    if isinstance(part, dict):
                        ptype = part.get("type")
                        if ptype == "text":
                            text_val = part.get("text", "")
                            if text_val:
                                blocks.append({"type": "text", "text": text_val})
                        elif ptype in ("thinking", "tool_use", "image", "image_ref"):
                            blocks.append(part)

            # tool_calls → tool_use blocks
            for tc in m.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                args = fn.get("arguments", "{}")
                try:
                    parsed_args = json.loads(args) if isinstance(args, str) else args
                except (json.JSONDecodeError, ValueError, TypeError):
                    parsed_args = {}
                if not isinstance(parsed_args, dict):
                    parsed_args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": _sanitize_tool_id(tc.get("id", ""), origin="anthropic_adapter_tool_use"),
                    "name": str(fn.get("name", "")),
                    "input": parsed_args,
                })

            # 空 assistant 兜底（Anthropic 拒绝完全空的 content）
            if not blocks:
                blocks = [{"type": "text", "text": "(empty)"}]

            result.append({"role": "assistant", "content": blocks})
            continue

        if role == "tool":
            # OpenAI std 的 role="tool" → Anthropic 协议下要变成下一条 user 消息里的
            # tool_result block。多个 tool_result 可以合并到同一条 user 消息。
            # 当 content 已是 Anthropic content blocks list（含 image/text block，
            # 由 read_file 图片路径产出）时**保留 list 形态**直接透传，不要 json.dumps
            # 拍平 —— 那会把 base64 当字符串塞进 tool_result，模型看不到图片。
            tool_content: Any
            if isinstance(content, list) and content and all(
                isinstance(b, dict) and isinstance(b.get("type"), str) for b in content
            ):
                tool_content = content
            else:
                content_str = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                if not content_str:
                    content_str = "(no output)"
                tool_content = content_str
            tool_result_block = {
                "type": "tool_result",
                "tool_use_id": _sanitize_tool_id(m.get("tool_call_id", ""), origin="anthropic_adapter_tool_result"),
                "content": tool_content,
            }
            if (
                result
                and result[-1]["role"] == "user"
                and isinstance(result[-1]["content"], list)
                and result[-1]["content"]
                and isinstance(result[-1]["content"][0], dict)
                and result[-1]["content"][0].get("type") == "tool_result"
            ):
                result[-1]["content"].append(tool_result_block)
            else:
                result.append({"role": "user", "content": [tool_result_block]})
            continue

        # 普通 user 消息
        if isinstance(content, str):
            if not content.strip():
                content = "(empty message)"
            result.append({"role": "user", "content": content})
        elif isinstance(content, list):
            # 幂等：识别所有已是 Anthropic content block 的 part（text / tool_result /
            # image / image_ref），不只 text。否则二次 normalize 时 user content=
            # list[tool_result block] 的消息会被误识别为“空 user 消息”（因为未包含
            # type=text）→ 被替换为 `[{type:text,text:"(empty message)"}]` → image base64 全丢
            # → 模型看到“空消息”反复调 read_file 。
            # 事故复现：2026-05-12 read_file 读图场景。
            preserved_blocks: List[Dict[str, Any]] = []
            for part in content:
                if isinstance(part, dict):
                    ptype = part.get("type")
                    if ptype == "text":
                        txt = part.get("text", "")
                        if txt:
                            preserved_blocks.append({"type": "text", "text": txt})
                    elif ptype in ("tool_result", "image", "image_ref"):
                        preserved_blocks.append(part)
                elif isinstance(part, str) and part:
                    preserved_blocks.append({"type": "text", "text": part})
            if not preserved_blocks:
                preserved_blocks = [{"type": "text", "text": "(empty message)"}]
            result.append({"role": "user", "content": preserved_blocks})
        else:
            result.append({"role": "user", "content": str(content) or "(empty message)"})

    # 孤儿 tool_use 剥离（无后续 tool_result 配对）
    result = _strip_orphan_tool_uses(result)
    # 孤儿 tool_result 剥离（无前置 tool_use 配对）
    result = _strip_orphan_tool_results(result)
    # 强制角色交替（Anthropic 拒绝连续同 role）
    result = _enforce_role_alternation(result)
    # Anthropic 协议：首条消息必须 role=user。长会话压缩/裁剪后保留窗口可能以
    # assistant（含 tool_use）起头（首个 user 被切掉）→ 这里丢弃开头的非 user
    # 消息，避免 "first message must use the user role" 400。丢掉 assistant 后其
    # tool_result 可能变孤儿，故紧接着再剥一次孤儿 tool_result。
    result = _ensure_user_leading(result)
    result = _strip_orphan_tool_results(result)

    return system, result


def _strip_orphan_tool_uses(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """剥离没有对应 tool_result 的 tool_use blocks。压缩或截断后可能出现孤儿。"""
    tool_result_ids = set()
    for m in messages:
        if m["role"] == "user" and isinstance(m.get("content"), list):
            for block in m["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_result_ids.add(block.get("tool_use_id"))
    for m in messages:
        if m["role"] == "assistant" and isinstance(m.get("content"), list):
            m["content"] = [
                b for b in m["content"]
                if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in tool_result_ids)
            ]
            if not m["content"]:
                m["content"] = [{"type": "text", "text": "(tool call removed)"}]
    return messages


def _strip_orphan_tool_results(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """剥离没有对应 tool_use 的 tool_result blocks。"""
    tool_use_ids = set()
    for m in messages:
        if m["role"] == "assistant" and isinstance(m.get("content"), list):
            for block in m["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_ids.add(block.get("id"))
    for m in messages:
        if m["role"] == "user" and isinstance(m.get("content"), list):
            new_content = [
                b for b in m["content"]
                if not (isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") not in tool_use_ids)
            ]
            m["content"] = new_content if new_content else [{"type": "text", "text": "(tool result removed)"}]
    return messages


def _ensure_user_leading(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """保证首条消息 role=user（Anthropic 硬性要求）。

    丢弃开头连续的非 user 消息（通常是压缩/裁剪后窗口以 assistant 起头）。
    全部非 user 时返回空列表——build_messages 后续会 append 当前轮的 user 消息兜底。
    不注入伪造 user 消息，避免污染语义。
    """
    idx = 0
    n = len(messages)
    while idx < n and messages[idx].get("role") != "user":
        idx += 1
    return messages[idx:] if idx else messages


def _enforce_role_alternation(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """连续同 role 合并。Anthropic 拒绝连续 user 或连续 assistant。"""
    fixed: List[Dict[str, Any]] = []
    for m in messages:
        if fixed and fixed[-1]["role"] == m["role"]:
            prev = fixed[-1]["content"]
            curr = m["content"]
            if isinstance(prev, str) and isinstance(curr, str):
                fixed[-1]["content"] = prev + "\n" + curr
            elif isinstance(prev, list) and isinstance(curr, list):
                fixed[-1]["content"] = prev + curr
            elif isinstance(prev, str) and isinstance(curr, list):
                fixed[-1]["content"] = [{"type": "text", "text": prev}] + curr
            elif isinstance(prev, list) and isinstance(curr, str):
                fixed[-1]["content"] = prev + [{"type": "text", "text": curr}]
            else:
                fixed.append(m)
        else:
            fixed.append(m)
    return fixed


def convert_tools_to_anthropic(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """OpenAI 风格 tool 定义 → Anthropic 风格。

    透传规则：
    - 顶层 `cache_control`（AnthropicToolsCachePlugin 可能写入）直通到
      Anthropic 风格 tool dict 的同名字段。Anthropic SDK 识别它以做
      tools 块 prompt cache。OpenAI 路径不会调本函数，不用担心脏字段。
    """
    if not tools:
        return []
    result: List[Dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # 兼容两种输入：
        # - OpenAI 格式 {"type":"function","function":{"name":...,"parameters":...}}
        # - Anthropic 格式 {"name":...,"input_schema":...}（harness 上游已是这种）
        # 顶层字段优先；回退到 function.xxx。避免回归：P1-B2 后 anthropic_client 直接
        # 传 Anthropic 风格 tools 进来，converter 如果只认 OpenAI 格式会把 name 全清空，
        # 讯飞等严格校验的代理会返 "invalid function name" 400。
        fn = t.get("function") or {}
        name = t.get("name") or fn.get("name", "")
        description = t.get("description") or fn.get("description", "")
        input_schema = (
            t.get("input_schema")
            or fn.get("parameters")
            or {"type": "object", "properties": {}}
        )
        converted: Dict[str, Any] = {
            "name": name,
            "description": description,
            "input_schema": input_schema,
        }
        # 透传 Anthropic 特有的 cache_control（由 AnthropicToolsCachePlugin 注入）
        cache_control = t.get("cache_control")
        if cache_control is not None:
            converted["cache_control"] = cache_control
        result.append(converted)
    return result


__all__ = [
    "to_anthropic_messages",
    "convert_tools_to_anthropic",
    "_sanitize_tool_id",
    "_strip_orphan_tool_uses",
    "_strip_orphan_tool_results",
    "_enforce_role_alternation",
    "_ensure_user_leading",
]
