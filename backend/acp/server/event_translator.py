"""把柳如烟 transport_event → ACP session/update payload。

Phase 2 范围：content / thinking 流式翻译。
Phase 3 加：tool_call / tool_result（映射到 ACP start_tool_call / update_tool_call）。

设计参考：
- @_docs/acp_integration_design.md §5.3.3 事件翻译表
- backend/runtime_protocol.py:build_runtime_transport_event 是上游产生 transport_event 的地方
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import acp

logger = logging.getLogger(__name__)


# 柳如烟工具名 → ACP ToolKind 推断（用前缀匹配，简化维护）
# ACP ToolKind 枚举：read / edit / delete / move / search / execute / think / fetch / switch_mode / other
_TOOL_KIND_RULES: list[tuple[tuple[str, ...], str]] = [
    (("read_file", "list_directory", "list_files"), "read"),
    (("grep", "grep_search", "grep_content", "glob"), "search"),
    (("write_file", "edit_file", "ide_apply_edit", "apply_patch"), "edit"),
    (("delete_file", "rm", "remove_file"), "delete"),
    (("move_file", "rename_file", "mv"), "move"),
    (("execute_command", "run_command", "bash"), "execute"),
    (("web_search", "fetch_url", "web_extract", "browser"), "fetch"),
    (("ask_user_question",), "other"),
]


def _infer_tool_kind(tool_name: str) -> str:
    """根据工具名推断 ACP ToolKind。未知工具 → other。"""
    if not tool_name:
        return "other"
    name = tool_name.lower()
    for prefixes, kind in _TOOL_KIND_RULES:
        for prefix in prefixes:
            if name == prefix or name.startswith(prefix + "_") or name.startswith(prefix):
                return kind
    return "other"


def _stringify_tool_result(result: Any) -> str:
    """工具结果归一化成字符串（ACP raw_output 字段虽然 Any，但客户端通常按字符串展示）。"""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            return str(result)
    return str(result)


def _is_error_result(result: Any) -> bool:
    """判断工具结果是不是错误。柳如烟没有强约定，启发式：dict 含 is_error 字段，或字符串含 'error/fail' 前缀。"""
    if isinstance(result, dict):
        if result.get("is_error") is True:
            return True
        # 部分工具会把错误塞 result 字段下面
        if isinstance(result.get("status"), str) and result["status"].lower() in ("error", "failed"):
            return True
    if isinstance(result, str):
        lowered = result.lstrip().lower()
        if lowered.startswith(("error:", "failed:", "exception:", "[error]", "❌")):
            return True
    return False


def translate_event(event: dict) -> Optional[Any]:
    """把柳如烟 transport_event / chat event 翻成 ACP session/update payload。

    返 None 表示这条事件不映射到 ACP（暂时忽略）。

    输入事件格式（来自 LLMClient.chat / Agent.chat）：
        {"type": "content", "text": "..."}      普通输出
        {"type": "thinking", "text": "..."}     思考
        {"type": "tool_call", "name": ..., "input": ..., "id": ...}  Phase 3
        {"type": "tool_result", "name": ..., "result": ..., "tool_use_id": ...} Phase 3
        {"type": "system_notice", "kind": ..., "content": ...}  视情况
        {"type": "error", "error": ...}         错误

    Phase 2 实现范围：只处理 content + thinking + 简单 error。
    """
    etype = event.get("type", "content")

    if etype == "content":
        text = event.get("text", "")
        if not text:
            return None
        return acp.update_agent_message_text(text)

    if etype == "thinking":
        text = event.get("text", "")
        if not text:
            return None
        return acp.update_agent_thought_text(text)

    if etype == "error":
        # Phase 2 简化处理：把错误内容当 agent message 推（让用户能看到）。
        # Phase 3 起改用 ACP 标准错误响应或 system_notice 卡片。
        err = event.get("error") or "(unknown error)"
        return acp.update_agent_message_text(f"\n\n⚠️ LLM 错误：{err}")

    if etype == "system_notice":
        # 大部分 system_notice（如 llm_upstream_retry / loop_detected）对 ACP 客户端不重要，
        # 但偶尔的关键提示（如 token_limit 警告）应该让用户看到。
        kind = event.get("kind", "")
        content = event.get("content") or event.get("text") or ""
        # Phase 2 白名单：只有重试/上游错误这类用户应该看到的才翻
        if kind in ("llm_upstream_retry", "model_switch_notice") and content:
            return acp.update_agent_message_text(f"\n[{kind}] {content}\n")
        logger.debug("event_translator: system_notice ignored, kind=%s", kind)
        return None

    if etype == "tool_call":
        # 柳如烟 tool_call：{name, input, id, tool_use_id}
        # tool_use_id 是稳定 id（贯穿 call → result 配对）；id 字段是兼容写法
        tc_id = event.get("tool_use_id") or event.get("id")
        if not tc_id:
            logger.warning("event_translator: tool_call missing id, skipping: %s", event)
            return None
        name = event.get("name") or "unknown_tool"
        return acp.start_tool_call(
            tool_call_id=tc_id,
            title=name,
            kind=_infer_tool_kind(name),  # type: ignore[arg-type]
            status="in_progress",
            raw_input=event.get("input"),
        )

    if etype == "tool_result":
        tc_id = event.get("tool_use_id") or event.get("id")
        if not tc_id:
            logger.warning("event_translator: tool_result missing tool_use_id, skipping: %s", event)
            return None
        result = event.get("result")
        is_err = _is_error_result(result)
        return acp.update_tool_call(
            tool_call_id=tc_id,
            status="failed" if is_err else "completed",
            raw_output=_stringify_tool_result(result),
        )

    # 其它未识别类型
    logger.debug("event_translator: untranslated event type=%s", etype)
    return None
