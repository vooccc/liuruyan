"""会话上下文检查点：完整 transcript 与模型工作上下文之间的稳定桥梁。

设计约束：
- 原始 transcript 永不改写；检查点只保存模型可见的结构化摘要。
- ``source_hash`` 只覆盖会真正进入模型的字段，忽略 timestamp/client_id 等 UI 元数据。
- 摘要正文不含创建时间、随机 ID 等易变内容，保证两次请求字节级稳定、对 Prompt Cache 友好。
- 单独文件原子替换，崩溃时最多丢失本次检查点，不会损坏聊天记录。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)

CONTEXT_CHECKPOINT_SCHEMA_VERSION = 1
CONTEXT_SUMMARY_PROMPT_VERSION = "conversation-structured-summary-v1"

# 与 history_entry_to_openai_std / provider adapter 真正消费的字段对齐。
# timestamp/client_id/agent_id 等展示或追踪元数据不参与 hash，避免无意义失效。
_MODEL_VISIBLE_KEYS = (
    "role",
    "content",
    "tool_calls",
    "tool_call_id",
    "reasoning_content",
    "name",
)


def _model_visible_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: entry[key]
        for key in _MODEL_VISIBLE_KEYS
        if key in entry
    }


def history_prefix_hash(history: List[Dict[str, Any]], count: int) -> str:
    """返回前 ``count`` 条历史的模型可见内容哈希。"""
    safe_count = max(0, min(int(count or 0), len(history)))
    payload = [_model_visible_entry(item) for item in history[:safe_count]]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_context_checkpoint(
    checkpoint: Any,
    history: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """验证检查点能否安全应用到当前原始历史。"""
    if not isinstance(checkpoint, dict):
        return False, "not_a_dict"
    if checkpoint.get("schema_version") != CONTEXT_CHECKPOINT_SCHEMA_VERSION:
        return False, "schema_version_mismatch"
    if checkpoint.get("summary_prompt_version") != CONTEXT_SUMMARY_PROMPT_VERSION:
        return False, "summary_prompt_version_mismatch"

    covered = checkpoint.get("covered_message_count")
    if not isinstance(covered, int) or covered <= 0 or covered > len(history):
        return False, "covered_message_count_invalid"

    summary = checkpoint.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return False, "summary_missing"

    expected_hash = history_prefix_hash(history, covered)
    if checkpoint.get("source_hash") != expected_hash:
        return False, "source_hash_mismatch"
    return True, "ok"


def make_context_checkpoint(
    *,
    session_id: str,
    history: List[Dict[str, Any]],
    covered_message_count: int,
    covered_non_system_count: int,
    summary: str,
    summary_data: Optional[Dict[str, Any]] = None,
    generation: int = 1,
    summary_model: Optional[str] = None,
    tokens_before: Optional[int] = None,
    tokens_after: Optional[int] = None,
) -> Dict[str, Any]:
    """构造可持久化检查点；易变元数据不会拼入 ``summary``。"""
    covered = int(covered_message_count)
    return {
        "schema_version": CONTEXT_CHECKPOINT_SCHEMA_VERSION,
        "summary_prompt_version": CONTEXT_SUMMARY_PROMPT_VERSION,
        "session_id": str(session_id or ""),
        "covered_message_count": covered,
        "covered_non_system_count": int(covered_non_system_count or 0),
        "source_hash": history_prefix_hash(history, covered),
        "summary": summary.strip(),
        "summary_data": dict(summary_data or {}),
        "generation": max(1, int(generation or 1)),
        "summary_model": summary_model or None,
        "tokens_before": int(tokens_before) if isinstance(tokens_before, int) else None,
        "tokens_after": int(tokens_after) if isinstance(tokens_after, int) else None,
        "created_at": datetime.now().isoformat(),
    }


def load_context_checkpoint(path: Path) -> Optional[Dict[str, Any]]:
    """读取检查点；损坏或非对象内容按不存在处理。"""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("[CONTEXT_CHECKPOINT] failed to read %s", path, exc_info=True)
        return None
    return payload if isinstance(payload, dict) else None


def save_context_checkpoint(path: Path, checkpoint: Dict[str, Any]) -> None:
    """用同目录临时文件 + ``os.replace`` 原子保存检查点。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(checkpoint, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def invalidate_context_checkpoint(path: Path) -> None:
    """删除派生检查点；原 transcript 不受影响。"""
    for candidate in (path, path.with_name(f".{path.name}.tmp")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "[CONTEXT_CHECKPOINT] failed to remove %s",
                candidate,
                exc_info=True,
            )


def checkpoint_summary_message(checkpoint: Dict[str, Any]) -> Dict[str, str]:
    """把检查点转换成稳定的模型 system message。"""
    return {
        "role": "system",
        "content": str(checkpoint.get("summary") or "").strip(),
    }


__all__ = [
    "CONTEXT_CHECKPOINT_SCHEMA_VERSION",
    "CONTEXT_SUMMARY_PROMPT_VERSION",
    "checkpoint_summary_message",
    "history_prefix_hash",
    "invalidate_context_checkpoint",
    "load_context_checkpoint",
    "make_context_checkpoint",
    "save_context_checkpoint",
    "validate_context_checkpoint",
]
