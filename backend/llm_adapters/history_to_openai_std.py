"""History entry (v3 Anthropic content blocks 写盘格式) → OpenAI ChatCompletion 标准消息列表。

历史命名背景（2026-05-20 Phase E 清理）：
本模块原名 ``legacy_segments.normalize_legacy_assistant_entry``。
最初是为兼容 v1 (segments JSON string) + v2 (OpenAI std 顶层 tool_calls 数组) + v3
(Anthropic content blocks) 三种盘上格式而生。

Phase D 完成 v3 写盘统一后，磁盘上 v1/v2 残留已清零（131 个 transcript.jsonl
扫描 0 条 v1/v2 entry，见 ``scripts/scan_legacy_segments.py``）。Phase E 砍掉
v1/v2 分支 + 改名为 ``history_entry_to_openai_std`` —— 这才是本函数当前职责的
诚实描述：**把 v3 盘上格式（Anthropic content blocks）转成 OpenAI std 消息序列**，
供 ``backend/llm_adapters/openai_compat.py:to_openai_messages`` 消费。

兜底：盘上若再出现 v1/v2 entry（不应该），由 ``transcript.rebuild_conversation_history``
fail-fast 提前 raise，本函数不再做容错。
"""
import json
from typing import Any, Dict, List


def history_entry_to_openai_std(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把一条 v3 history entry 转成 OpenAI std 消息序列。

    支持的盘上格式（**v3 only**）：
    - role=assistant, content=list[ContentBlock]  (thinking/text/tool_use 交错)
    - role=user, content=list[ContentBlock]       (含 tool_result blocks)
    - role=user / role=assistant, content=str     (纯文本，无 tool 的轮次)
    - role=system, content=str                    (罕见，原样透传)

    返回：1+N 条 OpenAI std 消息（assistant + 配套 tool messages）

    输入异常：v1/v2 老格式不在本函数职责，由 transcript 读路径 fail-fast 拦截。
    """
    role = entry.get("role")
    raw_content = entry.get("content", "")

    # ─── v3: user 消息 content 是 list[tool_result block] ───────────────
    # Anthropic 协议把 tool_result 包在下一条 user 消息里。喂给 OpenAI 时要拍成
    # 多条 role=tool messages（每个 tool_use_id 一条）。
    if role == "user" and isinstance(raw_content, list):
        out_messages: List[Dict[str, Any]] = []
        leftover_user_text_parts: List[str] = []
        for block in raw_content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                tr_content = block.get("content", "") or ""
                if isinstance(tr_content, list):
                    # Stage 1b: vision 跨轮持久化—— content 是 list[block]，
                    # 含 image / image_ref 时**保留 list 形态**透传，让 agent.py
                    # reinflate + to_anthropic_messages 透传链路看得到图。
                    # 只有清一色 text block 才拍成字符串（向后兼容旧路径）。
                    has_non_text = any(
                        isinstance(b, dict) and b.get("type") not in ("text", None)
                        for b in tr_content
                    )
                    if not has_non_text:
                        tr_content = "\n".join(
                            b.get("text", "") for b in tr_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    # has_non_text=True 时 tr_content 保持 list 形态
                if not isinstance(tr_content, (str, list)):
                    tr_content = json.dumps(tr_content, ensure_ascii=False)
                out_messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or "",
                    "content": tr_content,
                })
            elif btype == "text":
                # 同一条 user 消息里同时有 tool_result 和 text 时（罕见，但 Anthropic 允许）
                t = block.get("text", "") or ""
                if t:
                    leftover_user_text_parts.append(t)
        if leftover_user_text_parts:
            out_messages.append({
                "role": "user",
                "content": "\n".join(leftover_user_text_parts),
            })
        return out_messages

    if role != "assistant":
        # user / system / 其它（user 通常是纯字符串）
        return [{"role": role, "content": raw_content if isinstance(raw_content, str) else str(raw_content)}]

    # ─── v3: assistant content 是 list[ContentBlock] ────────────────────
    # 把 thinking/text/tool_use blocks 拍成 OpenAI std（content + tool_calls）。
    # text/tool_use 交错顺序在 LLM 协议层无法保留（OpenAI ChatCompletion 限制——
    # content 是单段，tool_calls 永远在末尾），所以这里把所有 text 拼起来 + 所有
    # tool_use 拼成数组。**前端展示路径不走这里**（走 _anthropic_blocks_to_segments，
    # 直接读盘 content 数组顺序）。
    if isinstance(raw_content, list):
        text_parts: List[str] = []
        thinking_parts: List[str] = []
        tool_calls_arr: List[Dict[str, Any]] = []
        for block in raw_content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text", "") or ""
                if t:
                    text_parts.append(t)
            elif btype == "thinking":
                t = block.get("thinking", "") or ""
                if t:
                    thinking_parts.append(t)
            elif btype == "tool_use":
                inp = block.get("input", {})
                if not isinstance(inp, str):
                    inp_str = json.dumps(inp, ensure_ascii=False) if inp else "{}"
                else:
                    inp_str = inp
                tool_calls_arr.append({
                    "id": str(block.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name", "")),
                        "arguments": inp_str,
                    },
                })
        out_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": "\n".join(text_parts),
        }
        if thinking_parts:
            out_msg["reasoning_content"] = "\n".join(thinking_parts)
        if tool_calls_arr:
            out_msg["tool_calls"] = tool_calls_arr
        return [out_msg]

    # ─── 纯文本 assistant（无 tool 的轮次）──────────────────────────────
    # v3 写路径也会产出 string content 的轮次（assistant 只产 text 不调 tool 时）。
    return [{"role": role, "content": raw_content if isinstance(raw_content, str) else str(raw_content)}]
