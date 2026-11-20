"""History entry (v3 Anthropic content blocks 写盘格式) → OpenAI ChatCompletion 标准消息序列。

历史命名背景（2026-05-20 Phase E 清理）：
本模块原名 ``legacy_segments.normalize_legacy_assistant_entry``。
最初是为兼容 v1 (segments JSON string) + v2 (OpenAI std 顶层 tool_calls 数组) + v3
(Anthropic content blocks) 三种盘上格式而生。

Phase D 完成 v3 写盘统一后，磁盘上 v1/v2 残留已清零（131 个 transcript.jsonl
扫描 0 条 v1/v2 entry，见 ``scripts/scan_legacy_segments.py``）。Phase E 砍掉
v1/v2 分支 + 改名为 ``history_entry_to_openai_std``。

2026-11-20 根治多轮折叠历史畸形（``normalize_history_to_openai_std``）：
v3 写盘会把同一次用户请求内的多轮工具调用（round_1..round_k + 最终总结）折叠存成
1 条 ``assistant`` entry（blocks 内 thinking/text/tool_use 交错）+ 1 条 ``user`` entry
（全部 tool_result）。直接按单条折叠喂给 LLM 会造成：
1. 所有轮次的文字与最终总结被提前倒装到所有 tool_calls 之前；
2. 数十轮工具调用挤在同一个 model turn 内，在 Google Antigravity 签名收敛时
   第 2~N 轮的真实 thought_signature 被迫丢弃，且超长畸形 turn 极易诱发模型跳过
   工具调用直接幻觉输出。

因此提供整段历史归一化入口 ``normalize_history_to_openai_std(history)``：
在不修改盘上 transcript 格式、也不打破单条函数 ``history_entry_to_openai_std``
契约的前提下，将折叠的 ``assistant + user(tool_result)`` 配对还原为标准多轮序列：
``assistant(round_1) -> tool(res_1) -> ... -> assistant(round_k) -> tool(res_k) -> assistant(final_text)``。
"""
from __future__ import annotations

from collections import defaultdict, deque
import json
from typing import Any, Deque, Dict, List, Optional, Tuple


_TOOL_RESULT_IMAGE_ANCHOR = "[attachment(s) shown in following user message]"


def _build_openai_tool_call_from_block(block: Dict[str, Any]) -> Dict[str, Any]:
    """把单个 v3 Anthropic tool_use block 转为 OpenAI std tool_call dict。"""
    inp = block.get("input", {})
    if not isinstance(inp, str):
        inp_str = json.dumps(inp, ensure_ascii=False) if inp else "{}"
    else:
        inp_str = inp
    tc_item: Dict[str, Any] = {
        "id": str(block.get("id", "")),
        "type": "function",
        "function": {
            "name": str(block.get("name", "")),
            "arguments": inp_str,
        },
    }
    tsig = block.get("thought_signature") or block.get("thoughtSignature")
    if tsig:
        tc_item["thought_signature"] = tsig
    return tc_item


def _convert_tool_result_block(block: Dict[str, Any]) -> Dict[str, Any]:
    """把单个 v3 tool_result block 转为 OpenAI std role=tool dict。"""
    tr_content = block.get("content", "") or ""
    if isinstance(tr_content, list):
        # Stage 1b: vision 跨轮持久化—— content 是 list[block]，
        # 含 image / image_ref 时保留 list 形态透传，让 agent.py
        # reinflate + to_anthropic_messages 透传链路看得到图。
        has_non_text = any(
            isinstance(b, dict) and b.get("type") not in ("text", None)
            for b in tr_content
        )
        if not has_non_text:
            tr_content = "\n".join(
                b.get("text", "") for b in tr_content
                if isinstance(b, dict) and b.get("type") == "text"
            )
    if not isinstance(tr_content, (str, list)):
        tr_content = json.dumps(tr_content, ensure_ascii=False)
    return {
        "role": "tool",
        "tool_call_id": block.get("tool_use_id") or "",
        "content": tr_content,
    }


def _build_leftover_user_message(blocks: List[Any]) -> Optional[Dict[str, Any]]:
    """把 user list content 中非 tool_result 的块聚合成 OpenAI std user 消息。

    若全部为纯文本块（或字符串），拍成 str 保持向后兼容；
    若含 image / image_ref / document 等多模态块，保留 list 形态透传给后续 reinflate。
    """
    if not blocks:
        return None
    has_non_text = any(
        isinstance(b, dict) and b.get("type") not in ("text", None)
        for b in blocks
    )
    if not has_non_text:
        texts: List[str] = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text", "") or ""
                if t:
                    texts.append(t)
            elif isinstance(b, str) and b:
                texts.append(b)
        if not texts:
            return None
        return {"role": "user", "content": "\n".join(texts)}

    preserved: List[Any] = []
    for b in blocks:
        if isinstance(b, dict):
            if b.get("type") == "text" and not b.get("text"):
                continue
            preserved.append(dict(b))
        elif isinstance(b, str) and b:
            preserved.append({"type": "text", "text": b})
    if not preserved:
        return None
    return {"role": "user", "content": preserved}


def history_entry_to_openai_std(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把单条 v3 history entry 转成 OpenAI std 消息序列。

    支持的盘上格式（**v3 only**）：
    - role=assistant, content=list[ContentBlock]  (thinking/text/tool_use 交错)
    - role=user, content=list[ContentBlock]       (含 tool_result blocks 或多模态附件)
    - role=user / role=assistant, content=str     (纯文本，无 tool 的轮次)
    - role=system, content=str                    (罕见，原样透传)

    返回：1+N 条 OpenAI std 消息（assistant + 配套 tool messages）

    输入异常：v1/v2 老格式不在本函数职责，由 transcript 读路径 fail-fast 拦截。
    """
    role = entry.get("role")
    raw_content = entry.get("content", "")

    # ─── v3: user 消息 content 是 list[ContentBlock] ───────────────────
    # 1) 含 tool_result 时：拆成多条 role=tool，非 tool_result 块留在尾部 user 消息；
    # 2) 不含 tool_result（如 多模态延迟图像投递机制 的独立 image_ref user 消息）：保留 list 形态透传。
    if role == "user" and isinstance(raw_content, list):
        out_messages: List[Dict[str, Any]] = []
        leftover_blocks: List[Any] = []
        for block in raw_content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                out_messages.append(_convert_tool_result_block(block))
            else:
                leftover_blocks.append(block)
        leftover_msg = _build_leftover_user_message(leftover_blocks)
        if leftover_msg is not None:
            out_messages.append(leftover_msg)
        return out_messages

    if role != "assistant":
        # user / system / 其它（user 通常是纯字符串）
        return [{"role": role, "content": raw_content if isinstance(raw_content, str) else str(raw_content)}]

    # ─── v3: assistant content 是 list[ContentBlock] ────────────────────
    # 单条接口无上下文配对信息，保持原有单条拍平契约（多轮还原请走 normalize_history_to_openai_std）。
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
                tool_calls_arr.append(_build_openai_tool_call_from_block(block))
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


def _is_user_tool_result_entry(entry: Dict[str, Any]) -> bool:
    if not isinstance(entry, dict) or entry.get("role") != "user":
        return False
    content = entry.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _is_deferred_tool_image_user_entry(entry: Dict[str, Any]) -> bool:
    """识别 多模态延迟图像投递机制 落盘的独立工具图片 user 消息（由 _build_tool_image_user_message 生成）。"""
    if not isinstance(entry, dict) or entry.get("role") != "user":
        return False
    content = entry.get("content")
    if not isinstance(content, list) or not content:
        return False
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        return False
    leading = str(first.get("text") or "")
    if not (leading.startswith("Attached ") and "tool result(s) above:" in leading):
        return False
    return any(
        isinstance(b, dict) and b.get("type") in ("image", "image_ref", "document")
        for b in content[1:]
    )


def _split_assistant_blocks_into_rounds(raw_content: List[Any]) -> List[Dict[str, Any]]:
    """将一条折叠的 v3 assistant content blocks 按真实调用轮次切分。

    切轮依据（同时兼容新会话、带扩散签名的老会话与无签名老会话）：
    1. 遇到非空 thinking / text 块且当前轮已经累计了 tool_use → 上一轮已结束，开启新一轮；
    2. 遇到 tool_use 块且当前轮已有 tool_use：
       - 若当前 tool_use 自带 thought_signature，且该签名不同于当前轮已有签名
         （或当前轮之前已出现过“带签名 + 无签名并行”组合），说明是静默连续新一轮 → 开启新一轮；
       - 若当前 tool_use 与当前轮签名完全相同（修复前接收端扩散的老数据）或无签名（新数据并行调用后续工具）
         → 归入同一轮。
    """
    rounds: List[Dict[str, Any]] = []

    def _new_round() -> Dict[str, Any]:
        return {
            "thinking": [],
            "text": [],
            "tool_calls": [],
            "sigs": set(),
            "saw_unsigned_after_sig": False,
        }

    cur = _new_round()

    def _flush() -> None:
        nonlocal cur
        if cur["thinking"] or cur["text"] or cur["tool_calls"]:
            rounds.append(cur)
            cur = _new_round()

    for block in raw_content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking":
            t = block.get("thinking", "") or ""
            if t:
                if cur["tool_calls"]:
                    _flush()
                cur["thinking"].append(t)
        elif btype == "text":
            t = block.get("text", "") or ""
            if t:
                if cur["tool_calls"]:
                    _flush()
                cur["text"].append(t)
        elif btype == "tool_use":
            tsig = block.get("thought_signature") or block.get("thoughtSignature")
            if cur["tool_calls"] and tsig:
                if (tsig not in cur["sigs"]) or cur["saw_unsigned_after_sig"]:
                    _flush()
            tc_item = _build_openai_tool_call_from_block(block)
            cur["tool_calls"].append(tc_item)
            if tsig:
                cur["sigs"].add(tsig)
            elif cur["sigs"]:
                cur["saw_unsigned_after_sig"] = True

    _flush()

    # 防御：若极罕见出现相邻两个都没有 tool_calls 的纯文本/思考轮，合并避免产生连续 assistant 消息
    merged: List[Dict[str, Any]] = []
    for r in rounds:
        if merged and not merged[-1]["tool_calls"] and not r["tool_calls"]:
            merged[-1]["thinking"].extend(r["thinking"])
            merged[-1]["text"].extend(r["text"])
        else:
            merged.append(r)
    return merged


def _tool_msg_has_image_anchor(tool_msg: Dict[str, Any]) -> bool:
    c = tool_msg.get("content")
    if isinstance(c, str):
        return _TOOL_RESULT_IMAGE_ANCHOR in c
    return False


def normalize_history_to_openai_std(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将整段 v3 历史消息列表归一化为 OpenAI std 消息序列，并按真实轮次拆解折叠的多轮工具调用。

    设计原则：
    - 不原地修改传入的 ``history`` 或内部 dict/block（只读构造新对象）；
    - 当 ``assistant`` entry 仅含 1 轮工具调用，或后面没有配套 ``tool_result`` 时，
      产出与逐条调用 ``history_entry_to_openai_std`` 完全等价；
    - 当 ``assistant`` entry 含多轮工具调用且后面有配套 ``tool_result`` 时，
      按轮次还原为 ``assistant(r1) -> tool(r1) -> assistant(r2) -> tool(r2) -> ... -> assistant(final)``；
    - 若中间轮次出现无法配对的损坏数据，安全降级回逐条 ``history_entry_to_openai_std``，
      绝不制造角色错位或丢失消息。
    """
    if not history:
        return []

    out: List[Dict[str, Any]] = []
    n = len(history)
    i = 0

    while i < n:
        entry = history[i]
        if not isinstance(entry, dict):
            i += 1
            continue

        raw_content = entry.get("content")
        has_tool_use = (
            entry.get("role") == "assistant"
            and isinstance(raw_content, list)
            and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in raw_content)
        )
        if not has_tool_use:
            out.extend(history_entry_to_openai_std(entry))
            i += 1
            continue

        # 收集紧随其后的配套 tool_result 承载消息（user(tool_result) 或兼容老 tool entry）
        j = i + 1
        collected_tool_msgs: List[Dict[str, Any]] = []
        leftover_user_msgs: List[Dict[str, Any]] = []
        while j < n:
            nxt = history[j]
            if not isinstance(nxt, dict):
                j += 1
                continue
            if nxt.get("role") == "tool":
                collected_tool_msgs.append(dict(nxt))
                j += 1
            elif _is_user_tool_result_entry(nxt):
                converted = history_entry_to_openai_std(nxt)
                for cm in converted:
                    if cm.get("role") == "tool":
                        collected_tool_msgs.append(cm)
                    else:
                        leftover_user_msgs.append(cm)
                j += 1
            else:
                break

        # 收集紧跟在 tool_result 之后的 多模态延迟图像投递机制 工具图片 user 消息
        deferred_image_msgs: List[Dict[str, Any]] = []
        while j < n and _is_deferred_tool_image_user_entry(history[j]):
            deferred_image_msgs.extend(history_entry_to_openai_std(history[j]))
            j += 1

        if not collected_tool_msgs:
            # 没有配套 tool_result（例如被中断的末尾 assistant）→ 保持单条不拆，原样走旧路径
            for k in range(i, j):
                if isinstance(history[k], dict):
                    out.extend(history_entry_to_openai_std(history[k]))
            i = j
            continue

        rounds = _split_assistant_blocks_into_rounds(raw_content)
        tool_round_indices = [idx for idx, r in enumerate(rounds) if r["tool_calls"]]
        if len(rounds) <= 1 or not tool_round_indices:
            # 只有 1 轮时不需要拆轮，但若同时存在 deferred_image_msgs，顺序本身就是 assistant -> tools -> image_user -> leftover_user
            out.extend(history_entry_to_openai_std(entry))
            out.extend(collected_tool_msgs)
            out.extend(deferred_image_msgs)
            out.extend(leftover_user_msgs)
            i = j
            continue

        # 按 tool_call_id 建队列，支持同 id 极罕见重复时 FIFO 消费
        by_id: Dict[str, Deque[Tuple[int, Dict[str, Any]]]] = defaultdict(deque)
        for pos, tm in enumerate(collected_tool_msgs):
            tid = str(tm.get("tool_call_id") or "")
            by_id[tid].append((pos, tm))

        # 预演各轮配对，确认中间轮次不会出现完全无 tool_result 导致的连续 assistant 错位
        consumed_positions: set[int] = set()
        round_pairings: List[Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]] = []
        last_tool_round_idx = tool_round_indices[-1]
        unsafe_split = False

        for idx, r in enumerate(rounds):
            if not r["tool_calls"]:
                round_pairings.append((r, [], []))
                continue
            matched_tcs: List[Dict[str, Any]] = []
            unmatched_tcs: List[Dict[str, Any]] = []
            matched_tools: List[Dict[str, Any]] = []
            for tc in r["tool_calls"]:
                tid = tc["id"]
                if tid and by_id.get(tid):
                    pos, tm = by_id[tid].popleft()
                    consumed_positions.add(pos)
                    matched_tcs.append(tc)
                    matched_tools.append(tm)
                else:
                    unmatched_tcs.append(tc)

            if idx < last_tool_round_idx and not matched_tools:
                unsafe_split = True
                break

            # 中间轮次只保留真正有 tool_result 配对的 tool_calls，保证与紧随的 tool 消息严格对齐；
            # 最后一个带工具轮次保留全部（若有孤儿交由下游 _strip_orphan_tool_calls 统一处理）。
            effective_tcs = matched_tcs if idx < last_tool_round_idx else (matched_tcs + unmatched_tcs)
            round_pairings.append((r, effective_tcs, matched_tools))

        if unsafe_split:
            for k in range(i, j):
                if isinstance(history[k], dict):
                    out.extend(history_entry_to_openai_std(history[k]))
            i = j
            continue

        unmatched_tools = [
            tm for pos, tm in enumerate(collected_tool_msgs)
            if pos not in consumed_positions
        ]

        # 组装多轮消息序列，并把 多模态延迟图像投递机制 图片消息放回含 anchor 的那一轮 tool 消息紧后面
        group_msgs: List[Dict[str, Any]] = []
        last_tool_insert_pos: Optional[int] = None
        last_anchor_insert_pos: Optional[int] = None

        for idx, (r, effective_tcs, matched_tools) in enumerate(round_pairings):
            a_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(r["text"]),
            }
            if r["thinking"]:
                a_msg["reasoning_content"] = "\n".join(r["thinking"])
            if effective_tcs:
                a_msg["tool_calls"] = effective_tcs
            group_msgs.append(a_msg)

            if matched_tools:
                group_msgs.extend(matched_tools)
                last_tool_insert_pos = len(group_msgs)
                if any(_tool_msg_has_image_anchor(tm) for tm in matched_tools):
                    last_anchor_insert_pos = len(group_msgs)

            if idx == last_tool_round_idx and unmatched_tools:
                group_msgs.extend(unmatched_tools)
                last_tool_insert_pos = len(group_msgs)

        if deferred_image_msgs:
            insert_at = (
                last_anchor_insert_pos
                if last_anchor_insert_pos is not None
                else (last_tool_insert_pos if last_tool_insert_pos is not None else len(group_msgs))
            )
            group_msgs[insert_at:insert_at] = deferred_image_msgs

        out.extend(group_msgs)
        out.extend(leftover_user_msgs)
        i = j

    return out
