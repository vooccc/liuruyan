"""开发者模式开关。

启用源（OR 逻辑）：
1. 环境变量 `LIURUYAN_DEV_MODE` ∈ {1,true,yes,on} → 永久 on，无法 toggle off
2. 运行时 toggle（写盘到 `data/dev_mode_state.json`）→ 可在 UI 用「点击 5 次」手势切换

设计取舍：
- 单机本地应用的攻击面是"别人能上这台机器"。再加密码反而给自己制造摩擦。
- env 适合 dev 长开；UI toggle 适合用户偶尔进 dev 改 description。
- 所有"危险"端点（编辑 description / 自定义工具）用 `require_dev_mode()` 守门。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any, Dict

from fastapi import HTTPException

_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "dev_mode_state.json"
_lock = Lock()
# 进程内缓存避免每次 is_dev_mode_enabled 都磁盘 IO
_runtime_cache: Dict[str, Any] = {"loaded": False, "enabled": False}


def _load_runtime_flag() -> bool:
    if _runtime_cache["loaded"]:
        return bool(_runtime_cache["enabled"])
    if not _STATE_FILE.exists():
        _runtime_cache["loaded"] = True
        _runtime_cache["enabled"] = False
        return False
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8-sig"))
        enabled = bool(data.get("enabled"))
    except Exception:
        enabled = False
    _runtime_cache["loaded"] = True
    _runtime_cache["enabled"] = enabled
    return enabled


def _save_runtime_flag(enabled: bool) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(
        json.dumps({"enabled": bool(enabled)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _runtime_cache["loaded"] = True
    _runtime_cache["enabled"] = bool(enabled)


def _env_flag() -> bool:
    val = (os.environ.get("LIURUYAN_DEV_MODE") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def is_dev_mode_enabled() -> bool:
    return _env_flag() or _load_runtime_flag()


def get_dev_mode_state() -> Dict[str, Any]:
    """前端用于展示"通过哪条路径开启的"。"""
    env = _env_flag()
    runtime = _load_runtime_flag()
    return {
        "enabled": env or runtime,
        "via_env": env,
        "via_runtime_toggle": runtime,
        "can_toggle_off": not env,  # env 模式下不允许从 UI 关
    }


def set_runtime_toggle(enabled: bool) -> Dict[str, Any]:
    """切换运行时 toggle。env 已开时 toggle 仍可写但不影响最终结果。"""
    with _lock:
        _save_runtime_flag(enabled)
    return get_dev_mode_state()


def require_dev_mode() -> None:
    """FastAPI 依赖：未开启 dev mode 时直接 403。"""
    if not is_dev_mode_enabled():
        raise HTTPException(
            status_code=403,
            detail="dev mode disabled (set env LIURUYAN_DEV_MODE=1 or toggle via UI)",
        )
