"""
multi_edit 工具

设计目标：
- 一次工具调用做 N 处 find-replace，原子性（任意一处失败 → 整批回滚）

填充 / 验证 session_read_cache，配合 read-before-edit gate。
多文件读取靠同一轮并行多个 read_file（parallel tool calls），不再提供 read_files。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from .file_diff import build_file_diff_payload
from .file_tools import (
    _check_sensitive_path,
    _is_blocked_device,
    _is_expected_write_exception,
    _resolve_path,
)

logger = logging.getLogger(__name__)


def multi_edit_tool(
    path: str,
    edits: List[Dict[str, Any]],
) -> str:
    """对单个文件做 N 处原子 find-replace。

    Args:
        path: 文件路径
        edits: [{"old_string": str, "new_string": str, "replace_all": bool}]
            按数组顺序串行 apply，每次 apply 用上一次结果当输入。

    Returns:
        JSON {"success": True, "path": ..., "applied": N, "details": [...]}
        或 {"error": "...", "failed_at": idx, "applied_before_failure": K}
        失败时文件保持原样（不会写入半成品）。
    """
    try:
        if not path:
            return json.dumps({"error": "Missing required parameter: path"})
        if not edits or not isinstance(edits, list):
            return json.dumps({"error": "edits must be a non-empty list"})

        resolved = _resolve_path(path)

        if _is_blocked_device(str(resolved)):
            return json.dumps({"error": f"Cannot edit '{path}': device file"})

        sensitive_warning = _check_sensitive_path(str(resolved))
        if sensitive_warning:
            return json.dumps({"error": sensitive_warning})

        if not resolved.exists():
            return json.dumps({"error": f"File not found: {path}"})
        if not resolved.is_file():
            return json.dumps({"error": f"Not a file: {path}"})

        with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
            original = f.read()

        # 在内存里串行 apply 所有 edit；任一失败整批 abort
        current = original
        details: List[Dict[str, Any]] = []
        for idx, edit in enumerate(edits):
            if not isinstance(edit, dict):
                return json.dumps({
                    "error": f"edits[{idx}] must be an object",
                    "failed_at": idx,
                    "applied_before_failure": idx,
                })
            old_string = edit.get("old_string")
            new_string = edit.get("new_string")
            replace_all = bool(edit.get("replace_all", False))

            if old_string is None or new_string is None:
                return json.dumps({
                    "error": f"edits[{idx}] requires old_string and new_string",
                    "failed_at": idx,
                    "applied_before_failure": idx,
                })

            if old_string == new_string:
                return json.dumps({
                    "error": f"edits[{idx}].old_string equals new_string (no-op)",
                    "failed_at": idx,
                    "applied_before_failure": idx,
                })

            if old_string not in current:
                return json.dumps({
                    "error": (
                        f"edits[{idx}].old_string not found in file "
                        f"(after applying previous {idx} edits). "
                        f"old_string preview: {old_string[:80]!r}"
                    ),
                    "failed_at": idx,
                    "applied_before_failure": idx,
                })

            occurrences = current.count(old_string)
            if occurrences > 1 and not replace_all:
                return json.dumps({
                    "error": (
                        f"edits[{idx}].old_string appears {occurrences} times. "
                        "Add more context to make it unique, or pass replace_all=true."
                    ),
                    "failed_at": idx,
                    "applied_before_failure": idx,
                })

            if replace_all:
                current = current.replace(old_string, new_string)
                applied = occurrences
            else:
                current = current.replace(old_string, new_string, 1)
                applied = 1

            details.append({"index": idx, "applied": applied, "replace_all": replace_all})

        # 全部 apply 成功 → 一次性写回（原子写）
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(current)

        # 注入 sentinel diff 载荷给前端 FileDiffCard 渲染。original=before / current=after。
        diff_payload = build_file_diff_payload(
            path=str(resolved),
            before=original,
            after=current,
            extra={
                "success": True,
                "applied": len(edits),
                "details": details,
            },
        )
        return json.dumps(diff_payload, ensure_ascii=False)

    except Exception as e:
        if _is_expected_write_exception(e):
            return json.dumps({"error": f"Permission denied: {path}"})
        logger.exception(f"Error in multi_edit for {path}")
        return json.dumps({"error": str(e)})


_CONTEXT_KEYS = {"agent_id", "session_id", "llm_client", "trace_id"}


def _drop_context(kwargs):
    return {k: v for k, v in kwargs.items() if k not in _CONTEXT_KEYS}


def _handle_multi_edit(**kwargs):
    return multi_edit_tool(**_drop_context(kwargs))
