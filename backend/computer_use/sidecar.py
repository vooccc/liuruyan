"""Sidecar 调度器：spawn-per-call 跨进程跑平台 helper。

为什么 spawn-per-call 而不是常驻 server：
- 简单：无端口/keepalive/重连逻辑
- 隔离：单 action 崩了不牵连主 backend；GIL 也无所谓
- 单实例锁已经卡死了"同时只一个 action"的语义
- ~150ms 启动开销对人体动作时间尺度可忽略

平台路由：
    Windows  → backend/computer_use/helpers/win.py
    macOS    → backend/computer_use/helpers/mac.py    (M3 实现)
    Linux    → backend/computer_use/helpers/linux.py  (M3 实现)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Helper 默认子进程超时（秒）。截屏 5s，键鼠 3s，list_* 1s 都够。
DEFAULT_TIMEOUT_SEC = 10.0


def _platform_helper_path() -> Path:
    """按运行平台返回 helper 主程序绝对路径。"""
    here = Path(__file__).resolve().parent / "helpers"
    if sys.platform.startswith("win"):
        return here / "win.py"
    if sys.platform == "darwin":
        return here / "mac.py"
    if sys.platform.startswith("linux"):
        return here / "linux.py"
    raise RuntimeError(
        f"Computer Use sidecar: unsupported platform {sys.platform!r}. "
        f"Currently only Windows / macOS / Linux are supported."
    )


def is_platform_supported() -> bool:
    """供前端/工具入口预检。"""
    try:
        path = _platform_helper_path()
        return path.exists()
    except RuntimeError:
        return False


class SidecarError(RuntimeError):
    """Sidecar 调用失败：subprocess 非 0 退出 / JSON 解析失败 / payload error。"""

    def __init__(self, message: str, *, code: str = "sidecar_error", action: str = "?"):
        super().__init__(message)
        self.code = code
        self.action = action


async def call_sidecar(
    action: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    python_executable: Optional[str] = None,
) -> Any:
    """异步调用 helper sidecar 一次。

    Args:
        action: 协议中的 action 名（screenshot/click/type/...）。
        payload: 该 action 的参数 dict。
        timeout_sec: 子进程总超时；超时强制 kill 并抛 SidecarError。
        python_executable: 自定义 python 路径；默认走 sys.executable。
                            （PyInstaller 冻结时 sys.executable 是主 exe，不能拿来跑 py 脚本——
                             届时需要把 helper.py 一起打包并 ship 一个独立 python.exe，
                             目前先用 sys.executable 走开发模式。）

    Returns:
        helper 返回的 result（已 JSON 解码）。

    Raises:
        SidecarError: 任何失败路径。
    """
    helper_path = _platform_helper_path()
    if not helper_path.exists():
        raise SidecarError(
            f"sidecar helper not found at {helper_path}",
            code="helper_missing",
            action=action,
        )

    py = python_executable or sys.executable
    args = [py, str(helper_path), action]
    if payload:
        args.extend(["--payload", json.dumps(payload, ensure_ascii=False)])

    # 显式 UTF-8 给子进程，跟 _common.configure_stdio_utf8 双保险
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    logger.debug(
        "[computer_use] sidecar spawn action=%s payload_keys=%s timeout=%.1fs",
        action,
        list((payload or {}).keys()),
        timeout_sec,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise SidecarError(
            f"failed to spawn python {py!r}: {exc}",
            code="spawn_failed",
            action=action,
        ) from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_sec,
        )
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise SidecarError(
            f"sidecar action {action!r} timed out after {timeout_sec:.1f}s",
            code="timeout",
            action=action,
        ) from exc

    stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()

    if proc.returncode != 0 and not stdout:
        raise SidecarError(
            f"sidecar action {action!r} exited {proc.returncode}: {stderr or '(no stderr)'}",
            code=f"exit_{proc.returncode}",
            action=action,
        )

    # stdout 第一行 JSON 即结果
    first_line = stdout.splitlines()[0] if stdout else ""
    try:
        parsed = json.loads(first_line)
    except json.JSONDecodeError as exc:
        raise SidecarError(
            f"sidecar action {action!r} returned non-JSON stdout: {first_line[:200]!r}; "
            f"stderr={stderr[:200]!r}",
            code="bad_stdout",
            action=action,
        ) from exc

    if not parsed.get("ok", False):
        err_obj = parsed.get("error") or {}
        raise SidecarError(
            err_obj.get("message", "sidecar reported failure"),
            code=err_obj.get("code", "sidecar_error"),
            action=action,
        )

    if stderr:
        # 不当错误，记 debug 即可
        logger.debug("[computer_use] sidecar stderr (action=%s): %s", action, stderr[:500])

    return parsed.get("result")
