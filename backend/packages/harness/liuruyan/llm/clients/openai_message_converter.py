"""OpenAI ChatCompletion 兼容路径（讯飞 OpenAI 协议 / GPT 等）的消息适配器。

P1-C 搬迁说明：
    搬自 `backend/llm_adapters/openai_compat.py`（2026-05-02）。
    原文件改为 thin shim 转发本模块，保持 backend 调用方
    （agent.py / llm_adapters/__init__.py）import 路径不变。

    搬来的原因：harness/llm/clients/openai_compat.py
    （OpenAICompatibleApiClient）在 harness 里要做 messages → SDK params
    转换。按"harness 永不 import backend.*"硬约束，converter 真身必须在
    harness。

由于上游已经是 OpenAI std 格式（来自 history_to_openai_std.history_entry_to_openai_std），这层主要做：
1. 字段校验 + 默认值兜底（讯飞兼容路径对空 content 严格，会 400）
2. arguments 字段强制字符串（OpenAI 标准要求）
3. 孤儿 tool 消息剥离（讯飞同样会 400）
4. 角色交替合规（连续 user 合并，连续 assistant 合并）

本模块的 `to_openai_messages` 是 pass-through 加固版——大多数情况下输入即输出，
但兜底确保不会因为细节差异让讯飞 / OpenAI 兼容侧报错。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def to_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """OpenAI std messages → OpenAI std messages（净化版）。

    输入：history_entry_to_openai_std 输出，可能含轻微脏字段
    输出：可以直接喂给 openai SDK / 讯飞 OpenAI 兼容 endpoint 的纯净版本
    """
    cleaned: List[Dict[str, Any]] = []

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            continue

        if role == "tool":
            tool_call_id = m.get("tool_call_id")
            if not tool_call_id:
                # 没 tool_call_id 的 tool 消息没法配对，丢掉
                continue
            content = m.get("content", "")
            # Stage 1b: 防 image block 误导—— OpenAI 兼容协议（含讯飞 / deepseek）
            # 不支持 Anthropic image block，要拍成字符串。但**绝不能**把 base64 直接
            # json.dumps 进去：那会让模型读到 "Shown inline to the model" 一类的
            # 提示话术 + 一坨 base64 文本，误以为自己看到了图，反而自信瞎编。
            # 解法：含 image block 的 list 整体降级为明确的文字 placeholder。
            if isinstance(content, list):
                has_image = any(
                    isinstance(b, dict) and b.get("type") in ("image", "image_ref")
                    for b in content
                )
                if has_image:
                    text_parts = [
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    text_summary = "\n".join(t for t in text_parts if t)
                    content = (
                        (text_summary + "\n" if text_summary else "")
                        + "[image attachment present in result, but vision is unsupported on this provider; pixels not available]"
                    )
                else:
                    # 清一色 text block 才走拍字符串路径
                    content = "\n".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
            elif not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False) if content is not None else ""
            if not content:
                content = "(no output)"
            cleaned.append({
                "role": "tool",
                "tool_call_id": str(tool_call_id),
                "content": content,
            })
            continue

        if role == "assistant":
            entry: Dict[str, Any] = {"role": "assistant"}
            content = m.get("content", "")
            tool_calls = m.get("tool_calls") or []

            # content 必须是字符串（OpenAI 标准）；空字符串允许，但不能是 None
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            elif content is None:
                content = ""
            elif not isinstance(content, str):
                content = str(content)

            # 讯飞 OpenAI 兼容：assistant 必须有 content 或 tool_calls 至少其一
            # 都没有时塞占位文本（已知坑，agent.py:320-322 当前补丁就是哄这个）
            if not content and not tool_calls:
                content = "[已处理]"

            entry["content"] = content

            if tool_calls:
                cleaned_tool_calls = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    fn_name = fn.get("name") or ""
                    if not fn_name:
                        continue
                    fn_args = fn.get("arguments", "{}")
                    # OpenAI 标准要求 arguments 是字符串
                    if not isinstance(fn_args, str):
                        fn_args = json.dumps(fn_args, ensure_ascii=False)
                    cleaned_tool_calls.append({
                        "id": str(tc.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": fn_name,
                            "arguments": fn_args,
                        },
                    })
                if cleaned_tool_calls:
                    entry["tool_calls"] = cleaned_tool_calls

            # reasoning_content 透传（DeepSeek thinking 模式协议要求：
            # 上一轮 assistant 必须带 reasoning_content 回传，否则 400
            # "The reasoning_content in the thinking mode must be passed back"）。
            # 其他 OpenAI 兼容 endpoint（OpenAI 官方 / 讯飞 / Kimi 等）对未知字段
            # 一般忽略；少数严格校验的 endpoint 如未来出问题可改成按 model/base_url
            # 路由开关。源字段由 history_entry_to_openai_std 写入。
            reasoning = m.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                entry["reasoning_content"] = reasoning

            cleaned.append(entry)
            continue

        # system / user
        content = m.get("content", "")

        # cline 流派 2（2026-05-12 流派切换）：user message.content 含 image / image_ref
        # 时保留 OpenAI vision 协议的 list[block] 形态——image block 转 image_url，
        # text block 透传。事故根因 + 切派理由见
        # backend/llm_client.py:_split_text_and_image_blocks docstring。
        # 注意 system 不走这条路径（OpenAI system content 必须是字符串）。
        if role == "user" and isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") in ("image", "image_ref")
            for b in content
        ):
            new_content: List[Dict[str, Any]] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text":
                    txt = b.get("text", "")
                    if txt:
                        new_content.append({"type": "text", "text": txt})
                elif btype == "image":
                    # Anthropic image base64 block → OpenAI image_url block（data URL）
                    src = b.get("source") or {}
                    if src.get("type") == "base64":
                        media = src.get("media_type") or "image/png"
                        data = src.get("data") or ""
                        if data:
                            new_content.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{media};base64,{data}",
                                },
                            })
                elif btype == "image_url":
                    # 已是 OpenAI 格式 → 直接透传
                    new_content.append(b)
                elif btype == "document":
                    # 2026-05-12 P3 防御：document block（Anthropic 原生 PDF）
                    # 理论上不应该在 OpenAI 路径出现——file_tools._read_pdf 检测
                    # ContextVar=False 时直接走 _read_pdf_text 抽文本，不返 document
                    # block。但万一意外漏到这一层（编程 bug / 跨 provider 切换边
                    # 界），降级为文本占位避免 OpenAI 协议 400。
                    src = b.get("source") or {}
                    mt = src.get("media_type") or "application/pdf"
                    new_content.append({
                        "type": "text",
                        "text": f"[document attachment: {mt} (vision/document unsupported on this provider; use pypdf text path)]",
                    })
                elif btype == "image_ref":
                    # 防御：到 converter 这一层 image_ref 应该已经被 agent.build_messages
                    # 的 reinflate 链路换成 image base64 block 了。残留 image_ref 时
                    # 退化为文字占位避免 OpenAI 协议 400。
                    sha = (b.get("sha256") or "?")[:8]
                    new_content.append({
                        "type": "text",
                        "text": f"[image attachment: sha256={sha}... reinflate missed]",
                    })
            if not new_content:
                new_content = [{"type": "text", "text": "(empty image message)"}]
            cleaned.append({"role": "user", "content": new_content})
            continue

        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        elif not isinstance(content, str):
            content = str(content) if content is not None else ""

        cleaned.append({"role": role, "content": content})

    # DeepSeek thinking 模式兼容：tool_calls 必须配对 reasoning_content（官方
    # 文档明确：assistant 调过工具时 reasoning_content 必须回传；没调工具时可不传）。
    # 老 transcript 持久化前的代码没存 reasoning，这步退化掉历史里"有 tool_calls
    # 但缺 reasoning"的 assistant，避免下一轮被 DeepSeek 400 拒。详见函数 docstring。
    cleaned = _sanitize_thinking_tool_pairs(cleaned)

    # 相邻重复 assistant.tool_calls 去重（2026-05-10 user dump 复现）：
    # 上游某条路径把同一个 assistant_msg append 两次进 messages，导致
    # [N] assistant{tool_calls=[a,b,c]} + [N+1] assistant{同样 tool_calls} 两条
    # 紧挨。DeepSeek 严格校验"assistant.tool_calls 必须紧跟匹配 tool messages"，
    # 看到 [N+1] 又是 assistant 就报 "insufficient tool messages"。
    # 协议合规去重：相邻两条 assistant 的 tool_calls.id 集合完全相同时，丢前留后。
    cleaned = _dedup_adjacent_duplicate_assistant_tool_calls(cleaned)

    # 孤儿剥离：tool_calls 没有对应 tool 消息 / tool 消息没有对应 tool_calls
    cleaned = _strip_orphan_tool_calls(cleaned)
    cleaned = _strip_orphan_tool_messages(cleaned)
    # 最后防线（2026-05-10 user 报 DeepSeek "insufficient tool messages" 后加）：
    # 任何漏过 _strip_orphan_tool_calls 的 mismatch 强制再剥 + 打 [OPENAI-CONVERTER-
    # PAIR-FAIL] 诊断日志，避免 400 直接打到上游 endpoint。
    cleaned = _assert_tool_call_pairing(cleaned)
    # 角色交替合规
    cleaned = _enforce_role_alternation(cleaned)

    return cleaned


def _dedup_adjacent_duplicate_assistant_tool_calls(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """相邻两条 assistant 有完全相同 tool_calls.id 集合 → 丢前留后 + 打日志。

    场景（2026-05-10 user 实测 DeepSeek 400）：
    上游某条 _chat_with_tools 分支双 append 了同一个 assistant_msg，导致 messages
    出现 [N] [N+1] 两条相同 tool_calls 的 assistant 紧挨。这违反 OpenAI 协议
    "assistant.tool_calls 后必须紧跟匹配 tool messages"。

    去重策略：丢前留后（保留更晚的那条；如果用户后续又改了 reasoning/content，
    晚的那条应该是更新版本）。不动跨 tool message 的 assistant，避免影响合法的
    "多轮 tool_calls"流。

    打 `[OPENAI-CONVERTER-DEDUP]` 日志暴露真因，方便后续定位上游双 append 的具体
    分支并修源头。本去重是协议合规层面的防御，不依赖上游修复。
    """
    if len(messages) < 2:
        return messages

    result: List[Dict[str, Any]] = []
    dedup_count = 0
    for m in messages:
        if (
            result
            and result[-1].get("role") == "assistant"
            and m.get("role") == "assistant"
            and result[-1].get("tool_calls")
            and m.get("tool_calls")
        ):
            prev_ids = {
                str(tc.get("id")) for tc in result[-1]["tool_calls"]
                if isinstance(tc, dict) and tc.get("id")
            }
            curr_ids = {
                str(tc.get("id")) for tc in m["tool_calls"]
                if isinstance(tc, dict) and tc.get("id")
            }
            if prev_ids and prev_ids == curr_ids:
                # 丢前留后
                result[-1] = m
                dedup_count += 1
                continue
        result.append(m)

    if dedup_count > 0:
        dedup_msg = (
            f"[OPENAI-CONVERTER-DEDUP] 检测到 {dedup_count} 条相邻重复 assistant.tool_calls "
            "已合并（保留较晚版本）。上游某分支重复 append 了 assistant_msg，请查 "
            "_chat_with_tools / fallback 路径"
        )
        logger.warning(dedup_msg)
        print(dedup_msg)
    return result


_REASONING_PLACEHOLDER = "[历史轮次思考已省略]"


def _sanitize_thinking_tool_pairs(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """老 transcript 兼容：assistant.tool_calls 缺 reasoning_content 时注入占位字符串。

    背景（DeepSeek 官方文档 https://api-docs.deepseek.com/guides/thinking_mode）：
    - 没工具调用的 assistant：reasoning_content 可不回传（传了会被忽略）
    - **有工具调用的 assistant：reasoning_content 必须回传**，否则 400
      "The reasoning_content in the thinking mode must be passed back to the API"

    柳如烟 reasoning_content 持久化是阶段 3 后才补齐的（2026-05-09）。早期写盘的
    transcript 里 assistant 有 tool_calls 但**没有 reasoning_content**，这种历史
    记录被回放给 DeepSeek thinking 模型时必 400。

    策略：注入占位 reasoning_content（不动 tool_calls）。
    - DeepSeek thinking：字段存在即满足"必须回传"硬性要求；DeepSeek 不验证内容真实性
    - OpenAI / 讯飞 / Kimi 等：忽略未知字段（已由上游 reasoning_content 透传契约
      `TestReasoningContentPassthrough` 验证）
    - 保持 tool_calls 与 tool messages 配对完整，跟非 thinking 模型行为一致

    只补 `reasoning_content` 字段缺失或非字符串/空字符串的 case；当前轮
    _chat_one_round 刚生成的 assistant_msg 在 thinking 模式下必有非空
    reasoning_content（参 llm_client.py:1377 注入），不会被这步误覆盖。
    """
    for m in messages:
        if m.get("role") != "assistant":
            continue
        if not m.get("tool_calls"):
            continue
        reasoning = m.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            continue  # 已有真实 reasoning，保留
        m["reasoning_content"] = _REASONING_PLACEHOLDER

    return messages


def _strip_orphan_tool_calls(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """assistant.tool_calls 中 id 没有后续 tool 消息回应的，剥掉。

    2026-05-10 强化：归一化 id 类型（str()）+ 过滤 None/空 id，避免 ID 类型/边界
    差异让"看似配对"的 tool_call 漏过净化（DeepSeek 严格校验：tool_calls.id 必
    须严格匹配后续 tool_call_id）。
    """
    tool_msg_ids = {
        str(m["tool_call_id"]) for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            kept = []
            for tc in m["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                if tc_id and str(tc_id) in tool_msg_ids:
                    kept.append(tc)
            if kept:
                m["tool_calls"] = kept
            else:
                m.pop("tool_calls", None)
                if not m.get("content"):
                    m["content"] = "[已处理]"
    return messages


def _assert_tool_call_pairing(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """最后防线：发请求前断言 assistant.tool_calls 全部有匹配 tool message。

    DeepSeek 严格校验："An assistant message with 'tool_calls' must be followed
    by tool messages responding to each 'tool_call_id'"。若上游净化漏了任何
    case（unicode / id 类型 / 顺序问题），这层兜底强制再剥一次并 print 诊断。
    """
    tool_msg_ids = {
        str(m["tool_call_id"]) for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }
    needs_log = False
    for m in messages:
        if m.get("role") != "assistant":
            continue
        tool_calls = m.get("tool_calls")
        if not tool_calls:
            continue
        bad_ids = [
            tc.get("id") for tc in tool_calls
            if isinstance(tc, dict) and (
                not tc.get("id") or str(tc["id"]) not in tool_msg_ids
            )
        ]
        if not bad_ids:
            continue
        needs_log = True
        # 强制剥光所有 tool_calls，content 占位让 LLM 知道这条历史已无 tool 数据
        m.pop("tool_calls", None)
        if not m.get("content"):
            m["content"] = "[历史轮次：工具调用记录已不完整]"

    if needs_log:
        all_assistant_ids = []
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                all_assistant_ids.extend(
                    str(tc.get("id")) for tc in m["tool_calls"] if isinstance(tc, dict)
                )
        logger.warning(
            "[OPENAI-CONVERTER-PAIR-FAIL] tool_call/tool_msg 配对失败被强制剥离 "
            "assistant_tool_call_ids=%r tool_msg_ids=%r",
            all_assistant_ids, sorted(tool_msg_ids),
        )
    return messages


def _strip_orphan_tool_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """role=tool 但没有前置 assistant.tool_calls 配对的，丢掉。"""
    tool_call_ids = set()
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    tool_call_ids.add(tc["id"])
    return [
        m for m in messages
        if not (m.get("role") == "tool" and m.get("tool_call_id") not in tool_call_ids)
    ]


def _enforce_role_alternation(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """连续 user / 连续 assistant（无 tool_calls）合并。tool 消息保持独立。"""
    fixed: List[Dict[str, Any]] = []
    for m in messages:
        if (
            fixed
            and fixed[-1].get("role") == m.get("role")
            and m.get("role") in ("user", "system")
        ):
            prev = fixed[-1].get("content", "")
            curr = m.get("content", "")
            fixed[-1]["content"] = (prev + "\n" + curr) if prev and curr else (prev or curr)
        else:
            fixed.append(m)
    return fixed


__all__ = [
    "to_openai_messages",
    "_sanitize_thinking_tool_pairs",
    "_strip_orphan_tool_calls",
    "_assert_tool_call_pairing",
    "_strip_orphan_tool_messages",
    "_enforce_role_alternation",
]
