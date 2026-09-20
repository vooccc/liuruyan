"""Computer Use 审计：每个 action 落 jsonl，留 forensic trail。

设计原则：
- 一行一 JSON，append-only，断电安全
- 字段精简但够调查：ts / session_id / action / payload (precis) / result_code / duration_ms / error
- payload 里的 image_b64 / 大文本字段做 redact，避免审计文件爆炸
- 路径：~/.liuruyan/computer_use_audit.jsonl
- 文件单文件 ~50MB 自动 rotate（保留 .jsonl.1）跟 frontend_events 同款策略
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from backend import paths

logger = logging.getLogger(__name__)

_AUDIT_FILE_NAME = "computer_use_audit.jsonl"
_ROTATE_BYTES = 50 * 1024 * 1024  # 50MB

# 写盘锁：append + flush 是 process-safe 的，但多线程同时写还是要互斥
_lock = threading.Lock()


def audit_path() -> Path:
    return paths.data_dir() / _AUDIT_FILE_NAME


def _redact_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """删/截大字段，留下足够 forensic 又不爆炸。

    - 'text' 超过 200 字符截断 + 标 _truncated
    - 'image_b64' 整段去掉，只留 byte_size 提示
    - 其他字段透传
    """
    if not payload or not isinstance(payload, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in payload.items():
        if k == "image_b64" and isinstance(v, str):
            out[k] = f"<{len(v)} chars>"
            continue
        if k == "text" and isinstance(v, str) and len(v) > 200:
            out[k] = v[:200]
            out["_text_truncated"] = True
            out["_text_full_len"] = len(v)
            continue
        out[k] = v
    return out


def _redact_result(result: Any) -> Any:
    if isinstance(result, dict):
        return _redact_payload(result)
    return result


def write_event(
    *,
    session_id: str,
    action: str,
    payload: Optional[Dict[str, Any]] = None,
    result: Any = None,
    duration_ms: float = 0.0,
    error: Optional[str] = None,
    error_code: Optional[str] = None,
) -> None:
    """同步落盘一条 audit 记录（best-effort，永不抛）。"""
    record = {
        "ts": time.time(),
        "session_id": session_id,
        "action": action,
        "payload": _redact_payload(payload),
        "duration_ms": round(duration_ms, 2),
        "ok": error is None,
    }
    if error is None:
        record["result"] = _redact_result(result)
    else:
        record["error"] = error
        if error_code:
            record["error_code"] = error_code

    line = json.dumps(record, ensure_ascii=False)

    p = audit_path()
    with _lock:
        try:
            # rotate
            try:
                if p.exists() and p.stat().st_size > _ROTATE_BYTES:
                    rotated = p.with_suffix(p.suffix + ".1")
                    try:
                        if rotated.exists():
                            rotated.unlink()
                    except OSError:
                        pass
                    try:
                        os.replace(p, rotated)
                    except OSError:
                        pass
            except OSError:
                pass

            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        except Exception as exc:  # noqa: BLE001 audit 失败不能影响业务
            logger.warning("[computer_use] audit write failed: %s", exc)


def read_recent(limit: int = 50) -> list[Dict[str, Any]]:
    """读末尾 limit 条审计（给 read_logs / 设置面板 / replay 用）。"""
    p = audit_path()
    if not p.exists():
        return []
    out: list[Dict[str, Any]] = []
    try:
        with open(p, "rb") as f:
            # 文件不大就直接读全；rotate 上限 50MB 也不至于爆内存
            data = f.read()
    except OSError as exc:
        logger.warning("[computer_use] audit read failed: %s", exc)
        return []
    lines = data.splitlines()[-limit:]
    for raw in lines:
        try:
            out.append(json.loads(raw.decode("utf-8", errors="replace")))
        except json.JSONDecodeError:
            continue
    return out
