"""Windows sidecar：跨进程跑 Computer Use 动作。

技术选型：
- 截屏：mss (ctypes 直调 BitBlt + GDI)
- 缩放/编码：Pillow
- 多屏：mss 自带 monitors 数组（[0]=all, [1]=primary, [2..]=others）
- 键鼠：pyautogui（FAILSAFE 已关，PAUSE=0）
- 剪贴板：pyperclip（中文/IME 必走粘贴模式）

调用形态：
    python helpers/win.py screenshot --payload '{"display_number": 1}'
    →  stdout: {"ok": true, "result": {...}}
    →  stderr: optional debug log

DPI / 高分屏：
- mss 启动时已经是 PER_MONITOR_AWARE，截图按物理像素返回
- pyautogui 在 PER_MONITOR_AWARE 进程里也按物理像素，跟 mss 一致
- 一切坐标 LLM 看到的都是物理像素，不用做虚拟坐标变换
"""
from __future__ import annotations

import base64
import io
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


# 让 sidecar 进程能 import backend.computer_use.helpers._common
# （sidecar.py spawn 时 cwd=项目根，但 sys.path 还是要保险加一下）
_HERE = Path(__file__).resolve()
_PROJECT_ROOT = _HERE.parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.computer_use.helpers._common import (  # noqa: E402
    emit_error,
    parse_key_sequence,
    run_dispatch,
)


# ---- 截屏 ----
def _do_screenshot(payload: Dict[str, Any]) -> Dict[str, Any]:
    """payload: {"display_number": int|None, "format": "png"|"jpeg" (default png), "max_width": int|None}

    返回：{"image_b64": str, "media_type": "image/png", "width": int, "height": int,
            "scale_factor": float, "display_number": int}
    """
    import mss

    display_number = payload.get("display_number")
    fmt = (payload.get("format") or "png").lower()
    max_width = payload.get("max_width")
    if fmt not in ("png", "jpeg"):
        raise ValueError(f"unsupported format: {fmt!r} (use 'png' or 'jpeg')")

    with mss.mss() as sct:
        monitors = sct.monitors  # [0]=virtual_all, [1..]=每个物理屏
        if not monitors or len(monitors) < 2:
            raise RuntimeError("mss returned no monitors (no display attached?)")
        if display_number is None:
            target_idx = 1  # primary
        else:
            target_idx = int(display_number)
            if target_idx < 1 or target_idx >= len(monitors):
                raise ValueError(
                    f"display_number {target_idx} out of range "
                    f"(have {len(monitors) - 1} display(s); use 1..{len(monitors) - 1})"
                )
        monitor = monitors[target_idx]
        raw = sct.grab(monitor)
        # 现成 BGRA 像素 → Pillow Image
        from PIL import Image
        img = Image.frombytes("RGB", raw.size, raw.rgb)

    # 可选缩放
    if isinstance(max_width, int) and max_width > 0 and img.width > max_width:
        ratio = max_width / img.width
        new_size = (max_width, int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    if fmt == "jpeg":
        img.convert("RGB").save(buf, format="JPEG", quality=80, optimize=True)
        media_type = "image/jpeg"
    else:
        img.save(buf, format="PNG", optimize=True)
        media_type = "image/png"

    data = buf.getvalue()
    b64 = base64.b64encode(data).decode("ascii")

    # scale_factor：mss 的 monitor 本身就是物理像素，width/height 也是物理像素
    # （Windows DPI scaling 由 SetProcessDpiAwareness 控制，mss 内部已开 PER_MONITOR_AWARE）
    return {
        "image_b64": b64,
        "media_type": media_type,
        "width": img.width,
        "height": img.height,
        "scale_factor": 1.0,
        "display_number": target_idx,
        "byte_size": len(data),
    }


# ---- 多屏枚举 ----
def _do_list_displays(_payload: Dict[str, Any]) -> Dict[str, Any]:
    import mss

    out = []
    with mss.mss() as sct:
        for idx, mon in enumerate(sct.monitors):
            if idx == 0:
                continue  # 跳过 virtual all
            out.append({
                "display_number": idx,
                "left": mon.get("left"),
                "top": mon.get("top"),
                "width": mon.get("width"),
                "height": mon.get("height"),
                "is_primary": idx == 1,
            })
    # 可选：用 screeninfo 拿 scale_factor（DPI）
    try:
        from screeninfo import get_monitors
        sinfo = list(get_monitors())
        for entry in out:
            for m in sinfo:
                if (m.x, m.y) == (entry["left"], entry["top"]):
                    if hasattr(m, "name"):
                        entry["name"] = m.name
                    break
    except Exception:
        pass
    return {"displays": out, "count": len(out)}


# ============================================================================
# M2: 鼠标 / 键盘 / 剪贴板
# ============================================================================
#
# pyautogui 是按"鼠标真实移动+短暂 sleep"的方式实现 click，跟 LLM 训练数据里
# Anthropic computer_20250124 的"原子点击"语义一致。所有 action 完成后小睡
# MOVE_SETTLE_MS 让目标 app 来得及处理事件再让下一个 action 进来。
MOVE_SETTLE_MS = 0.05  # 50ms

# pyautogui key map：内部 token → pyautogui 接受的字符串。
# 我们的 token 来自 _common.parse_key_sequence（小写 + 别名归一）。
# 'cmd' 是平台敏感的（pyautogui 各平台名字不一样）：
#   Windows: cmd → 'win'（winleft）
#   macOS  : cmd → 'command'（pyautogui 在 mac 上认 'command' / 'cmd'）
#   Linux  : cmd → 'super'（X11 super 键）
# 其它键 pyautogui 跨平台已统一，不需要平台分支。
_CMD_KEY_BY_PLATFORM = {
    "win32": "win",
    "darwin": "command",
    "linux": "super",
}


def _resolve_cmd_key() -> str:
    if sys.platform.startswith("win"):
        return _CMD_KEY_BY_PLATFORM["win32"]
    if sys.platform == "darwin":
        return _CMD_KEY_BY_PLATFORM["darwin"]
    return _CMD_KEY_BY_PLATFORM["linux"]


_PYAUTOGUI_KEY_MAP: Dict[str, str] = {
    "cmd": _resolve_cmd_key(),
    "esc": "esc",
    "enter": "enter",
    "backspace": "backspace",
    "delete": "delete",
    "tab": "tab",
    "space": "space",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "capslock": "capslock",
    "ctrl": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "fn": "fn",
}


def _key_to_pyautogui(token: str) -> str:
    """单 token → pyautogui 名。未知 token 直接返自己（pyautogui 接受 'a' 'b' '1' 等）。"""
    return _PYAUTOGUI_KEY_MAP.get(token, token)


def _coord(payload: Dict[str, Any]) -> tuple[int, int]:
    """从 payload 取 [x, y]；缺失/形状错抛 ValueError。"""
    coord = payload.get("coordinate")
    if not isinstance(coord, list) or len(coord) != 2:
        raise ValueError("coordinate must be [x, y] (two integers)")
    try:
        x, y = int(coord[0]), int(coord[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"coordinate values must be integers: {coord!r}") from exc
    return x, y


# ---- 鼠标点击家族 ----
def _do_left_click(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _do_click(payload, button="left", clicks=1)


def _do_right_click(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _do_click(payload, button="right", clicks=1)


def _do_middle_click(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _do_click(payload, button="middle", clicks=1)


def _do_double_click(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _do_click(payload, button="left", clicks=2)


def _do_triple_click(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _do_click(payload, button="left", clicks=3)


def _do_click(payload: Dict[str, Any], *, button: str, clicks: int) -> Dict[str, Any]:
    """所有 click 家族走这一条；含可选修饰键序列。"""
    import pyautogui

    x, y = _coord(payload)
    modifiers: Optional[List[str]] = payload.get("modifiers") or []
    pyautogui_mods = [_key_to_pyautogui(m) for m in modifiers]

    # 修饰键按下 → click → 抬起
    for m in pyautogui_mods:
        pyautogui.keyDown(m)
    try:
        pyautogui.click(
            x=x, y=y,
            clicks=clicks,
            interval=0.05 if clicks > 1 else 0.0,
            button=button,
            duration=0,
        )
    finally:
        for m in reversed(pyautogui_mods):
            pyautogui.keyUp(m)

    time.sleep(MOVE_SETTLE_MS)
    return {"x": x, "y": y, "button": button, "clicks": clicks}


# ---- 鼠标移动 / 拖拽 / down/up ----
def _do_mouse_move(payload: Dict[str, Any]) -> Dict[str, Any]:
    import pyautogui
    x, y = _coord(payload)
    pyautogui.moveTo(x=x, y=y, duration=0)
    time.sleep(MOVE_SETTLE_MS)
    return {"x": x, "y": y}


def _do_cursor_position(_payload: Dict[str, Any]) -> Dict[str, Any]:
    import pyautogui
    pos = pyautogui.position()
    return {"x": int(pos[0]), "y": int(pos[1])}


def _do_left_mouse_down(payload: Dict[str, Any]) -> Dict[str, Any]:
    import pyautogui
    if "coordinate" in payload:
        x, y = _coord(payload)
        pyautogui.moveTo(x=x, y=y, duration=0)
    pyautogui.mouseDown(button="left")
    return {"button": "left", "state": "down"}


def _do_left_mouse_up(payload: Dict[str, Any]) -> Dict[str, Any]:
    import pyautogui
    if "coordinate" in payload:
        x, y = _coord(payload)
        pyautogui.moveTo(x=x, y=y, duration=0)
    pyautogui.mouseUp(button="left")
    time.sleep(MOVE_SETTLE_MS)
    return {"button": "left", "state": "up"}


def _do_left_click_drag(payload: Dict[str, Any]) -> Dict[str, Any]:
    """从当前位置拖到 coordinate；如有 start_coordinate 先移过去再拖。"""
    import pyautogui

    end_x, end_y = _coord(payload)
    start = payload.get("start_coordinate")
    if isinstance(start, list) and len(start) == 2:
        sx, sy = int(start[0]), int(start[1])
        pyautogui.moveTo(x=sx, y=sy, duration=0)

    pyautogui.dragTo(x=end_x, y=end_y, duration=0.3, button="left")
    time.sleep(MOVE_SETTLE_MS)
    return {"end_x": end_x, "end_y": end_y}


# ---- 滚轮 ----
def _do_scroll(payload: Dict[str, Any]) -> Dict[str, Any]:
    """payload: {"coordinate": [x,y]?, "scroll_direction": "up|down|left|right", "scroll_amount": int}

    pyautogui.scroll(amount, x, y) — 正数=向上，负数=向下；hscroll=水平
    """
    import pyautogui

    direction = (payload.get("scroll_direction") or "down").lower()
    amount = int(payload.get("scroll_amount", 3))
    if amount < 1:
        amount = 1
    if amount > 100:
        amount = 100

    if "coordinate" in payload:
        x, y = _coord(payload)
        pyautogui.moveTo(x=x, y=y, duration=0)

    # pyautogui scroll 量级 = "click"（一次滚轮顿挫）* magic factor
    # 各应用对 wheel delta 反应不一，用经验值 100 ≈ 一次顿挫
    delta = amount * 100
    if direction in ("up",):
        pyautogui.scroll(delta)
    elif direction in ("down",):
        pyautogui.scroll(-delta)
    elif direction in ("right",):
        try:
            pyautogui.hscroll(delta)
        except (NotImplementedError, AttributeError):
            # pyautogui Windows 上 hscroll 可能不支持 → 退化到 shift+wheel
            pyautogui.keyDown("shift")
            pyautogui.scroll(delta)
            pyautogui.keyUp("shift")
    elif direction in ("left",):
        try:
            pyautogui.hscroll(-delta)
        except (NotImplementedError, AttributeError):
            pyautogui.keyDown("shift")
            pyautogui.scroll(-delta)
            pyautogui.keyUp("shift")
    else:
        raise ValueError(f"unsupported scroll_direction: {direction!r}")

    time.sleep(MOVE_SETTLE_MS)
    return {"direction": direction, "amount": amount}


# ---- 键盘 ----
def _do_key(payload: Dict[str, Any]) -> Dict[str, Any]:
    """按下 key 序列一次或多次。

    payload: {"text": "ctrl+c", "repeat": 1?}
    单键如 'enter'，组合键如 'ctrl+shift+a'，单字符如 'a'
    """
    import pyautogui

    text = payload.get("text") or ""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("key action requires non-empty 'text' (e.g. 'ctrl+c', 'enter')")
    repeat = int(payload.get("repeat", 1))
    if repeat < 1:
        repeat = 1
    if repeat > 50:
        repeat = 50

    tokens = parse_key_sequence(text)
    if not tokens:
        raise ValueError(f"could not parse key sequence: {text!r}")
    pyautogui_keys = [_key_to_pyautogui(t) for t in tokens]

    for _ in range(repeat):
        if len(pyautogui_keys) == 1:
            pyautogui.press(pyautogui_keys[0])
        else:
            pyautogui.hotkey(*pyautogui_keys)
        if repeat > 1:
            time.sleep(0.03)

    return {"sequence": text, "repeat": repeat, "tokens": tokens}


def _do_hold_key(payload: Dict[str, Any]) -> Dict[str, Any]:
    """按住 key 一段时间。

    payload: {"text": "shift", "duration": <seconds>}
    """
    import pyautogui

    text = payload.get("text") or ""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("hold_key requires 'text' naming key(s) to hold")
    duration = float(payload.get("duration", 0.5))
    if duration <= 0:
        raise ValueError("hold_key duration must be > 0")
    if duration > 10:
        duration = 10.0  # 上限 10s 防 LLM 失误

    tokens = parse_key_sequence(text)
    pyautogui_keys = [_key_to_pyautogui(t) for t in tokens]

    for k in pyautogui_keys:
        pyautogui.keyDown(k)
    try:
        time.sleep(duration)
    finally:
        for k in reversed(pyautogui_keys):
            pyautogui.keyUp(k)

    return {"sequence": text, "duration": duration}


# ---- 文本输入（含中文/IME 友好剪贴板模式）----
def _do_type(payload: Dict[str, Any]) -> Dict[str, Any]:
    """输入文本。

    payload: {"text": str, "via_clipboard": bool?}

    via_clipboard 默认决策：
    - 显式传 → 听用户/LLM
    - 未传 → 包含非 ASCII（中文等）自动开 clipboard 模式
    """
    import pyautogui

    text = payload.get("text")
    if not isinstance(text, str):
        raise ValueError("type action requires 'text' (string)")
    if not text:
        return {"typed": 0, "via_clipboard": False}

    via_clipboard = payload.get("via_clipboard")
    if via_clipboard is None:
        via_clipboard = any(ord(ch) > 127 for ch in text)

    if via_clipboard:
        # 保存当前剪贴板 → 写入 → ctrl+v → 恢复（clipboardGuard 模式）
        import pyperclip

        try:
            saved = pyperclip.paste()
        except Exception:
            saved = None
        try:
            pyperclip.copy(text)
            time.sleep(0.05)  # 让系统剪贴板服务来得及消化
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.1)  # 等粘贴完成
        finally:
            # 恢复（best-effort）
            if saved is not None:
                try:
                    pyperclip.copy(saved)
                except Exception:
                    pass
        return {"typed": len(text), "via_clipboard": True}

    # ASCII fast path
    pyautogui.typewrite(text, interval=0.005)
    return {"typed": len(text), "via_clipboard": False}


# ---- 剪贴板 ----
def _do_read_clipboard(_payload: Dict[str, Any]) -> Dict[str, Any]:
    import pyperclip
    try:
        text = pyperclip.paste()
    except Exception as exc:
        raise RuntimeError(f"clipboard read failed: {exc}") from exc
    return {"text": text or ""}


def _do_write_clipboard(payload: Dict[str, Any]) -> Dict[str, Any]:
    import pyperclip
    text = payload.get("text", "")
    if not isinstance(text, str):
        raise ValueError("write_clipboard requires 'text' (string)")
    pyperclip.copy(text)
    return {"written": len(text)}


# ---- 等待 ----
def _do_wait(payload: Dict[str, Any]) -> Dict[str, Any]:
    duration = float(payload.get("duration", 0.5))
    if duration < 0:
        raise ValueError("wait duration must be >= 0")
    if duration > 10:
        duration = 10.0
    time.sleep(duration)
    return {"waited": duration}


# ============================================================================
# M6: 窗口 / 应用枚举（Win32 EnumWindows）
# ============================================================================
def _enum_top_windows() -> list:
    """枚举所有可见 + 有标题的顶层窗口。返回 list[dict]。

    每条含：hwnd / pid / title / exe_name / left / top / width / height /
            is_minimized / is_foreground
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi

    EnumWindows = user32.EnumWindows
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    GetWindowTextLengthW = user32.GetWindowTextLengthW
    GetWindowTextW = user32.GetWindowTextW
    IsWindowVisible = user32.IsWindowVisible
    IsIconic = user32.IsIconic
    GetForegroundWindow = user32.GetForegroundWindow
    GetWindowThreadProcessId = user32.GetWindowThreadProcessId
    GetWindowRect = user32.GetWindowRect

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    OpenProcess = kernel32.OpenProcess
    OpenProcess.restype = wintypes.HANDLE
    CloseHandle = kernel32.CloseHandle
    GetModuleBaseNameW = psapi.GetModuleBaseNameW

    foreground_hwnd = GetForegroundWindow()
    out: list = []

    def _exe_name(pid: int) -> str:
        if pid <= 0:
            return ""
        h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            n = GetModuleBaseNameW(h, None, buf, 260)
            return buf.value if n else ""
        finally:
            CloseHandle(h)

    def _cb(hwnd, _lparam):
        if not IsWindowVisible(hwnd):
            return True
        n = GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if not title.strip():
            return True

        pid = wintypes.DWORD(0)
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wintypes.RECT()
        GetWindowRect(hwnd, ctypes.byref(rect))

        out.append({
            "hwnd": int(hwnd),
            "pid": int(pid.value),
            "title": title,
            "exe_name": _exe_name(int(pid.value)),
            "left": int(rect.left),
            "top": int(rect.top),
            "width": int(rect.right - rect.left),
            "height": int(rect.bottom - rect.top),
            "is_minimized": bool(IsIconic(hwnd)),
            "is_foreground": int(hwnd) == int(foreground_hwnd),
        })
        return True

    EnumWindows(EnumWindowsProc(_cb), 0)
    return out


def _do_list_windows(_payload: Dict[str, Any]) -> Dict[str, Any]:
    """枚举所有可见顶层窗口。"""
    wins = _enum_top_windows()
    return {"windows": wins, "count": len(wins)}


def _do_list_running_apps(_payload: Dict[str, Any]) -> Dict[str, Any]:
    """按进程聚合：每个 PID 一条，含 exe_name、窗口数、主标题（最大窗口）、是否前台。"""
    wins = _enum_top_windows()
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
ACTION_TABLE = {
    # M1
    "screenshot": _do_screenshot,
    "list_displays": _do_list_displays,
    # M6: 窗口/应用枚举
    "list_windows": _do_list_windows,
    "list_running_apps": _do_list_running_apps,
    # M2: 鼠标
    "left_click": _do_left_click,
    "right_click": _do_right_click,
    "middle_click": _do_middle_click,
    "double_click": _do_double_click,
    "triple_click": _do_triple_click,
    "mouse_move": _do_mouse_move,
    "cursor_position": _do_cursor_position,
    "left_mouse_down": _do_left_mouse_down,
    "left_mouse_up": _do_left_mouse_up,
    "left_click_drag": _do_left_click_drag,
    "scroll": _do_scroll,
    # M2: 键盘
    "key": _do_key,
    "hold_key": _do_hold_key,
    "type": _do_type,
    # M2: 剪贴板
    "read_clipboard": _do_read_clipboard,
    "write_clipboard": _do_write_clipboard,
    # M2: 等待
    "wait": _do_wait,
}


def main() -> int:
    return run_dispatch(ACTION_TABLE)


if __name__ == "__main__":
    raise SystemExit(main())
