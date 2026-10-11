"""Sidecar 共享：JSON 协议 + key 名规范化 + 启动样板。

JSON over stdin/stdout 协议：
    输入：argv = [helper.py, <action>, --payload, <json_str>]
    输出：stdout 一行 JSON：{"ok": true, "result": <any>}
                       或：{"ok": false, "error": {"code": "...", "message": "..."}}

设计取舍：
- spawn-per-call（每个 action 起一次 python 子进程）
  - 优点：天然内存隔离，崩溃不牵连主进程；无需端口/PID 管理
  - 缺点：~150ms 启动开销（pyautogui import 慢），但 Computer Use 是人类时间尺度
- stdout 第一行 JSON，stderr 留给调试日志
- key 名空间：扁平 token 集合（'ctrl', 'shift', 'enter', 'a', '1', ...），
  组合用 '+' 连接（'ctrl+shift+a'）。映射各平台 native 名（macOS 'cmd', Win 'win'）由各 helper 内部完成
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Dict


# ---- 共享 key 名规范化 ----
# 我们的 key token 以小写为主，xdotool 风格。各平台 helper 再映射到 native API。
# 这份字典只做**别名归一**，不做平台映射（平台映射在各 helper 自己里做）。
_KEY_ALIASES = {
    # 修饰键统一表达
    "command": "cmd",
    "meta": "cmd",
    "super": "cmd",
    "control": "ctrl",
    "option": "alt",
    "opt": "alt",
    # 编辑键
    "return": "enter",
    "escape": "esc",
    "del": "delete",
    "forwarddelete": "delete",
    "bs": "backspace",
    # 方向
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "pgup": "pageup",
    "pgdn": "pagedown",
}


def normalize_key_token(token: str) -> str:
    """归一单个 key token 为内部规范（小写 + 别名展开）。"""
    t = token.strip().lower()
    return _KEY_ALIASES.get(t, t)


def parse_key_sequence(sequence: str) -> list[str]:
    """'Ctrl+Shift+A' → ['ctrl', 'shift', 'a']。允许 '+' 或 '-' 分隔。"""
    if not sequence:
        return []
    # 把 '-' 当作 '+'（xdotool 同时支持这两种）
    raw = sequence.replace("-", "+").split("+")
    return [normalize_key_token(t) for t in raw if t.strip()]


# ---- JSON 输出 ----
def emit_ok(result: Any) -> None:
    sys.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def emit_error(message: str, code: str = "runtime_error") -> None:
    sys.stdout.write(json.dumps(
        {"ok": False, "error": {"code": code, "message": message}},
        ensure_ascii=False,
    ))
    sys.stdout.write("\n")
    sys.stdout.flush()


# ---- 启动样板 ----
def configure_stdio_utf8() -> None:
    """强制 stdout/stderr UTF-8，避免 Windows GBK 把中文 app 名/剪贴板内容损坏。

    （和 backend.tools.builtin.command_manager 处理 execute_command UTF-8
    locale 的思路一致——子进程必须显式 force UTF-8）
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def configure_pyautogui_safe() -> None:
    """关闭 pyautogui 的"鼠标移到 (0,0) 触发 fail-safe 中断"行为。

    LLM 截图后判定坐标偶尔会贴近角落，开着 fail-safe 会让 click(0, 0) 抛
    `pyautogui.FailSafeException`。我们改用自己的坐标越界检查（safety.py），
    更可控、错误信息也更清晰。
    """
    os.environ.setdefault("PYAUTOGUI_HIDE_SUPPORT_PROMPT", "1")
    try:
        import pyautogui  # 重依赖，按需 import
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0
    except Exception:
        pass


def parse_argv() -> tuple[str, Dict[str, Any]]:
    """argv 解析：返回 (action, payload_dict)。"""
    if len(sys.argv) < 2:
        raise SystemExit("usage: helper.py <action> [--payload <json>]")
    action = sys.argv[1]
    payload: Dict[str, Any] = {}
    if "--payload" in sys.argv:
        idx = sys.argv.index("--payload")
        if idx + 1 < len(sys.argv):
            try:
                payload = json.loads(sys.argv[idx + 1])
                if not isinstance(payload, dict):
                    payload = {}
            except json.JSONDecodeError as exc:
                emit_error(f"invalid payload JSON: {exc}", "bad_payload")
                raise SystemExit(2)
    return action, payload


def run_dispatch(action_table: Dict[str, Callable[[Dict[str, Any]], Any]]) -> int:
    """统一 main() 入口：解析 argv → 找 handler → 执行 → 输出 JSON。"""
    configure_stdio_utf8()
    configure_pyautogui_safe()
    try:
        action, payload = parse_argv()
    except SystemExit:
        return 2

    handler = action_table.get(action)
    if handler is None:
        emit_error(f"unsupported action: {action!r}", "unknown_action")
        return 2

    try:
        result = handler(payload)
        emit_ok(result if result is not None else {"ok": True})
        return 0
    except Exception as exc:  # noqa: BLE001 — sidecar 边界，转 JSON 错误
        emit_error(f"{type(exc).__name__}: {exc}")
        return 1
