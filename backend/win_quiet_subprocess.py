"""Windows 防黑窗：给所有未显式指定 creationflags 的子进程默认注入 CREATE_NO_WINDOW。

背景（2026-09-30）：打包版后端由 Tauri 以 sidecar 方式拉起（binaries/liuruyan-backend），
自身没有控制台。无控制台的父进程每 spawn 一个子进程，Windows 就给子进程新开一个
控制台窗口——MCP stdio server、ACP agent、computer_use sidecar、quant 等在启动期和
任务执行期反复拉子进程，表现为"不停地弹黑窗口"。

做法：main.py 顶部调用 apply()，替换 asyncio.create_subprocess_exec / shell 与
subprocess.Popen（subprocess.run 内部走 Popen，一并覆盖），为没有显式传
creationflags 的调用注入 CREATE_NO_WINDOW (0x08000000)。

不受影响的调用：
- 显式传了 creationflags 的：gateway/terminal_launcher.py 故意 CREATE_NEW_CONSOLE
  弹真终端；cmd_tool.py / command_manager.py 的 NO_WINDOW|NEW_PROCESS_GROUP。
- GUI 子进程（explorer 等）不受控制台标志影响。

非 Windows 平台与重复调用均为 no-op。
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000

_applied = False


def apply() -> None:
    """幂等；仅 Windows 生效。必须在任何子进程拉起之前调用（main.py 顶部）。"""
    global _applied
    if _applied or sys.platform != "win32":
        return
    _applied = True

    _orig_exec = asyncio.create_subprocess_exec
    _orig_shell = asyncio.create_subprocess_shell
    _orig_popen = subprocess.Popen

    def _quiet_exec(*args, **kwargs):
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
        return _orig_exec(*args, **kwargs)

    def _quiet_shell(*args, **kwargs):
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
        return _orig_shell(*args, **kwargs)

    class _QuietPopen(_orig_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
            super().__init__(*args, **kwargs)

    asyncio.create_subprocess_exec = _quiet_exec
    asyncio.create_subprocess_shell = _quiet_shell
    subprocess.Popen = _QuietPopen  # type: ignore[misc]

