"""macOS sidecar：复用跨平台 pyautogui/mss/pyperclip 路径，窗口枚举走 AppleScript。

调用形态同 win.py：
    python helpers/mac.py screenshot --payload '{"display_number": 1}'

平台特化：
- 'cmd' 键 → 'command'（已在 win.py _PYAUTOGUI_KEY_MAP 平台感知）
- list_windows / list_running_apps 走 osascript（System Events）

无障碍权限：macOS 必须在「系统设置 → 隐私 → 辅助功能」给 python/终端授权，
否则 pyautogui 鼠标键盘 silently 失效。这是系统约束，不是 bug。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

_HERE = Path(__file__).resolve()
_PROJECT_ROOT = _HERE.parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 直接复用 win.py 里基于 mss + pyautogui + pyperclip 的全部跨平台 action
# （cmd 键已在 win.py 的 _PYAUTOGUI_KEY_MAP 用 sys.platform 自动选 'command'）
from backend.computer_use.helpers import win as _win  # noqa: E402
from backend.computer_use.helpers._common import run_dispatch  # noqa: E402


# ============================================================================
# macOS 窗口/应用枚举（AppleScript via osascript）
# ============================================================================
_APPLESCRIPT_LIST_WINDOWS = r'''
tell application "System Events"
    set output to ""
    repeat with proc in (every process whose visible is true and background only is false)
        set procName to name of proc
        try
            set procPID to unix id of proc
        on error
            set procPID to 0
        end try
        try
            repeat with win in (every window of proc)
                set winTitle to ""
                try
                    set winTitle to name of win
                end try
                if winTitle is missing value then set winTitle to ""
                set winPos to {0, 0}
                try
                    set winPos to position of win
                end try
                set winSize to {0, 0}
                try
                    set winSize to size of win
                end try
                set output to output & procName & "|" & procPID & "|" & winTitle ¬
                    & "|" & (item 1 of winPos) & "|" & (item 2 of winPos) ¬
                    & "|" & (item 1 of winSize) & "|" & (item 2 of winSize) & linefeed
            end repeat
        end try
    end repeat
    return output
end tell
'''


def _run_osascript(script: str, timeout: float = 4.0) -> str:
    """跑 AppleScript 返 stdout。失败抛 RuntimeError。"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("osascript not found (macOS only)") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"osascript timeout: {exc}") from exc
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        # 1743 = "not allowed assistive access"
        if "1743" in err or "assistive" in err.lower():
            raise RuntimeError(
                "macOS 辅助功能权限缺失：请在「系统设置 → 隐私 → 辅助功能」勾选 Python / 终端"
            )
        raise RuntimeError(f"osascript failed (rc={result.returncode}): {err}")
    return result.stdout or ""


def _get_frontmost_pid() -> int:
    """当前前台应用 PID。"""
    try:
        out = _run_osascript(
            'tell application "System Events" to unix id of first application process whose frontmost is true',
            timeout=2.0,
        )
        return int(out.strip())
    except Exception:
        return -1


def _enum_top_windows_mac() -> list:
    raw = _run_osascript(_APPLESCRIPT_LIST_WINDOWS)
    fg_pid = _get_frontmost_pid()
    out: list = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) != 7:
            continue
        proc_name, pid_s, title, x_s, y_s, w_s, h_s = parts
        try:
            pid = int(pid_s)
        except ValueError:
            pid = 0
        try:
            x, y, w, h = int(x_s), int(y_s), int(w_s), int(h_s)
        except ValueError:
            x, y, w, h = 0, 0, 0, 0
        if not title.strip():
            continue
        out.append({
            "hwnd": 0,  # mac 没有 HWND，留 0 兼容契约
            "pid": pid,
            "title": title,
            "exe_name": proc_name,
            "left": x, "top": y, "width": w, "height": h,
            "is_minimized": False,  # AppleScript 拿这个要遍历，性价比低先 False
            "is_foreground": pid == fg_pid,
        })
    return out


def _do_list_windows(_payload: Dict[str, Any]) -> Dict[str, Any]:
    wins = _enum_top_windows_mac()
    return {"windows": wins, "count": len(wins)}


def _do_list_running_apps(_payload: Dict[str, Any]) -> Dict[str, Any]:
    """复用 win.py 的聚合逻辑（按 PID 聚合）。"""
    wins = _enum_top_windows_mac()
    by_pid: Dict[int, Dict[str, Any]] = {}
    for w in wins:
        pid = w["pid"]
        existing = by_pid.get(pid)
        area = max(0, w["width"]) * max(0, w["height"])
        if existing is None:
            by_pid[pid] = {
                "pid": pid,
                "exe_name": w["exe_name"],
                "main_title": w["title"],
                "_main_area": area,
                "window_count": 1,
                "has_foreground": w["is_foreground"],
            }
        else:
            existing["window_count"] += 1
            if w["is_foreground"]:
                existing["has_foreground"] = True
            if area > existing["_main_area"]:
                existing["_main_area"] = area
                existing["main_title"] = w["title"]
    apps = []
    for entry in by_pid.values():
        entry.pop("_main_area", None)
        apps.append(entry)
    apps.sort(key=lambda a: (not a["has_foreground"], -a["window_count"], a["exe_name"].lower()))
    return {"apps": apps, "count": len(apps)}


# ============================================================================
# Action 总表：复用 win.py 全部跨平台 action + 替换 list_windows / list_running_apps
# ============================================================================
ACTION_TABLE = dict(_win.ACTION_TABLE)
ACTION_TABLE["list_windows"] = _do_list_windows
ACTION_TABLE["list_running_apps"] = _do_list_running_apps


def main() -> int:
    return run_dispatch(ACTION_TABLE)


if __name__ == "__main__":
    raise SystemExit(main())
