"""工具 description 覆盖层（仅 dev mode 写入）。

设计：
- 持久化文件：`<repo>/data/tool_overrides.json`
  形态 `{"<tool_name>": {"description": "...", "updated_at": "..."}}`。
- 启动时把覆盖应用到 `registry.tools[name]["description"]` 上（直接 mutate 单例）。
  这样 LLM 通过 `get_tool_definitions()` 拿到的就是覆盖后的版本。
- 首次覆盖前抓 baseline 进进程级 dict，reset 时还原。
  不把 baseline 持久化——baseline 来源永远是 `clean_registry.py` 源码本体，
  重启后从源码重新读，避免某次 baseline 被错版本污染后没法恢复。

红线：
- 普通模式（非 dev）即使文件存在也照样 apply。理由：用户 dev 改完 desc 后切回普通
  模式继续用，不应被强制还原。
- override 仅作用于 builtin 工具。MCP / IDE 工具 description 来自外部源
  （MCP server / 其他注册点），UI 不暴露编辑入口。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional

from backend.tools.builtin.clean_registry import registry

logger = logging.getLogger(__name__)

_OVERRIDES_FILE = Path(__file__).resolve().parent.parent / "data" / "tool_overrides.json"
_AUDIT_LOG_FILE = Path(__file__).resolve().parent.parent / "data" / "tool_override_log.jsonl"
_baseline: Dict[str, str] = {}  # tool_name -> 源码原始 description
_lock = Lock()
_applied = False


def _append_audit_log(entry: Dict[str, Any]) -> None:
    """追加一行审计日志到 tool_override_log.jsonl。

    设计：append-only，每次 set/reset 一行。出 bug 排查时直接 grep 工具名 + 时间。
    失败不抛——日志写不上不能阻断主流程。
    """
    try:
        _AUDIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with _AUDIT_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TOOL-OVERRIDES] audit log write failed: %s", exc)


def _load_overrides_file() -> Dict[str, Dict[str, Any]]:
    if not _OVERRIDES_FILE.exists():
        return {}
    try:
        data = json.loads(_OVERRIDES_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save_overrides_file(data: Dict[str, Dict[str, Any]]) -> None:
    _OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OVERRIDES_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def apply_overrides_to_registry() -> int:
    """应用持久化的 overrides 到全局 registry。返回应用条数。

    幂等：多次调用安全。每个工具首次被覆盖时抓 baseline。
    """
    global _applied
    overrides = _load_overrides_file()
    applied = 0
    with _lock:
        for name, payload in overrides.items():
            if name not in registry.tools:
                continue
            if not isinstance(payload, dict):
                continue
            new_desc = payload.get("description")
            if not isinstance(new_desc, str):
                continue
            tool = registry.tools[name]
            if name not in _baseline:
                _baseline[name] = tool.get("description", "") or ""
            tool["description"] = new_desc
            applied += 1
        _applied = True
    return applied


def get_baseline(name: str) -> Optional[str]:
    """返回首次覆盖前抓到的原始 description；未被覆盖过则返 None（此时 registry 里
    的就是源码本体）。"""
    return _baseline.get(name)


def is_overridden(name: str) -> bool:
    return name in _load_overrides_file()


def get_override_payload(name: str) -> Optional[Dict[str, Any]]:
    return _load_overrides_file().get(name)


def set_override(name: str, description: Optional[str]) -> Dict[str, Any]:
    """description=None → 清除覆盖、恢复 baseline；非 None → 写入覆盖。"""
    if name not in registry.tools:
        raise KeyError(name)
    ts = datetime.now().isoformat(timespec="seconds")
    with _lock:
        overrides = _load_overrides_file()
        tool = registry.tools[name]
        prev_description = tool.get("description", "") or ""

        if description is None:
            base = _baseline.pop(name, None)
            if base is not None:
                tool["description"] = base
            overrides.pop(name, None)
            _save_overrides_file(overrides)
            _append_audit_log({
                "ts": ts,
                "tool": name,
                "action": "reset",
                "prev_description": prev_description,
                "new_description": tool.get("description", ""),
                "prev_chars": len(prev_description),
                "new_chars": len(tool.get("description", "")),
            })
            return {
                "name": name,
                "description": tool.get("description", ""),
                "overridden": False,
            }

        if name not in _baseline:
            _baseline[name] = tool.get("description", "") or ""
        tool["description"] = description
        overrides[name] = {
            "description": description,
            "updated_at": ts,
        }
        _save_overrides_file(overrides)
        _append_audit_log({
            "ts": ts,
            "tool": name,
            "action": "set",
            "prev_description": prev_description,
            "new_description": description,
            "prev_chars": len(prev_description),
            "new_chars": len(description),
        })
        return {
            "name": name,
            "description": description,
            "overridden": True,
        }
