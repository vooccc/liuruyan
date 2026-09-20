"""Linux sidecar：复用跨平台 pyautogui/mss/pyperclip 路径，窗口枚举走 wmctrl。

调用形态同 win.py：
    python helpers/linux.py screenshot --payload '{"display_number": 1}'

平台特化：
- 'cmd' 键 → 'super'（已在 win.py _PYAUTOGUI_KEY_MAP 平台感知）
- list_windows / list_running_apps：优先 wmctrl，缺则降级 xdotool

依赖（系统包，自行安装）：
- wmctrl    # 列窗口最稳
- xdotool   # 降级方案
- xclip     # pyperclip 后端
- scrot/maim  # 截图后端（mss 不依赖）

Wayland 兼容性：
- mss 在 Wayland 下经常返黑图。受限于 Wayland 安全模型，没好办法绕过。
- 解决：建议用户切到 X11 session 跑（GDM 登录界面 → ⚙️ → Xorg）
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

_HERE = Path(__file__).resolve()
_PROJECT_ROOT = _HERE.parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 复用 win.py 的跨平台 action（screenshot/click/scroll/key/type/clipboard/wait）
from backend.computer_use.helpers import win as _win  # noqa: E402
from backend.computer_use.helpers._common import run_dispatch  # noqa: E402


# ============================================================================
# Linux 窗口/应用枚举（wmctrl 优先，xdotool 降级）
# ============================================================================
def _run_cmd(args: list, timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"{args[0]} not found — `sudo apt install {args[0]}`") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{args[0]} timeout: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} failed (rc={result.returncode}): {result.stderr.strip()}")
    return result.stdout or ""


def _enum_top_windows_linux() -> list:
    """优先 wmctrl -lpG（含 PID + 坐标），降级 xdotool。"""
    has_wmctrl = shutil.which("wmctrl") is not None
    has_xdotool = shutil.which("xdotool") is not None

    if not has_wmctrl and not has_xdotool:
        raise RuntimeError(
            "neither wmctrl nor xdotool available — install one: "
            "`sudo apt install wmctrl` (preferred)"
        )

    fg_hwnd = -1
    if has_xdotool:
        try:
            fg_hwnd = int(_run_cmd(["xdotool", "getactivewindow"]).strip())
        except Exception:
            fg_hwnd = -1

    if has_wmctrl:
        return _enum_via_wmctrl(fg_hwnd)
    return _enum_via_xdotool(fg_hwnd)


def _enum_via_wmctrl(fg_hwnd: int) -> list:
    # wmctrl -lpG 格式：hex_hwnd desktop pid x y w h host title
    out: list = []
    raw = _run_cmd(["wmctrl", "-lpG"])
    for line in raw.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        try:
            hwnd = int(parts[0], 16)
            pid = int(parts[2])
            x, y, w, h = int(parts[3]), int(parts[4]), int(parts[5]), int(parts[6])
        except ValueError:
            continue
        title = parts[8]
        if not title.strip():
            continue
        out.append({
            "hwnd": hwnd, "pid": pid, "title": title,
            "exe_name": _exe_name_from_pid(pid),
            "left": x, "top": y, "width": w, "height": h,
            "is_minimized": False,  # wmctrl 不直接给，省一次 IPC
            "is_foreground": hwnd == fg_hwnd,
        })
    return out


def _enum_via_xdotool(fg_hwnd: int) -> list:
    """xdotool 降级：拿 visible window 列表。"""
    out: list = []
    raw = _run_cmd(["xdotool", "search", "--onlyvisible", "--name", "."])
    for line in raw.splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        hwnd = int(line)
        try:
            title = _run_cmd(["xdotool", "getwindowname", str(hwnd)]).strip()
            geom = _run_cmd(["xdotool", "getwindowgeometry", "--shell", str(hwnd)])
            pid_s = _run_cmd(["xdotool", "getwindowpid", str(hwnd)]).strip()
        except Exception:
            continue
        if not title:
            continue
        geom_map = {}
        for ln in geom.splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1)
                geom_map[k.strip()] = v.strip()
        try:
            x = int(geom_map.get("X", 0))
            y = int(geom_map.get("Y", 0))
            w = int(geom_map.get("WIDTH", 0))
            h = int(geom_map.get("HEIGHT", 0))
            pid = int(pid_s) if pid_s.isdigit() else 0
        except ValueError:
            continue
        out.append({
            "hwnd": hwnd, "pid": pid, "title": title,
            "exe_name": _exe_name_from_pid(pid),
            "left": x, "top": y, "width": w, "height": h,
            "is_minimized": False,
            "is_foreground": hwnd == fg_hwnd,
        })
    return out


def _exe_name_from_pid(pid: int) -> str:
    """读 /proc/<pid>/comm 拿进程名。"""
    if pid <= 0:
        return ""
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _do_list_windows(_payload: Dict[str, Any]) -> Dict[str, Any]:
    wins = _enum_top_windows_linux()
    return {"windows": wins, "count": len(wins)}


def _do_list_running_apps(_payload: Dict[str, Any]) -> Dict[str, Any]:
    wins = _enum_top_windows_linux()
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
# Action 总表
# ============================================================================
ACTION_TABLE = dict(_win.ACTION_TABLE)
ACTION_TABLE["list_windows"] = _do_list_windows
ACTION_TABLE["list_running_apps"] = _do_list_running_apps


def main() -> int:
    return run_dispatch(ACTION_TABLE)


if __name__ == "__main__":
    raise SystemExit(main())
