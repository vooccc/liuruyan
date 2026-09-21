"""
Gateway Launcher Terminal Spawner（2026-05-23 新增）。

把"复制启动命令 → 自己开终端粘贴"那一步省掉：直接由柳如烟 backend 拉起一个**新终端窗口**
（独立于柳如烟主进程）执行 launcher snippet 里的命令。

设计取舍：
- **新窗口 + detached**：用户能看到 CLI 输出、能交互；柳如烟关掉也不影响。
- **不捕获 stdout**：CLI 是交互式（claude/codex 都要 readline）；走父进程 stdout 没意义。
- **不返子进程对象**：fire-and-forget；前端只关心"启动成功了吗"。

平台分支：
- Windows：subprocess.Popen + CREATE_NEW_CONSOLE，调用 powershell.exe / cmd.exe
- macOS  ：osascript 拉起 Terminal.app（默认终端）
- Linux  ：探测 x-terminal-emulator / gnome-terminal / konsole；都没有就抛错

安全：路由层 _require_loopback 已限制本地访问；本模块不做额外校验，调用方负责。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Literal, Optional

ShellMode = Literal["powershell", "posix"]


@dataclass
class LaunchResult:
    ok: bool
    message: str
    pid: Optional[int] = None
    platform: str = ""


def launch_command_in_new_terminal(
    *,
    command: str,
    shell: ShellMode,
    title: Optional[str] = None,
    cwd: Optional[str] = None,
) -> LaunchResult:
    """在新终端窗口里执行 command。fire-and-forget。

    入参：
      command  : 启动命令字符串（已带 env 前缀 / quote 好）
      shell    : 'powershell' → Win 优先用 pwsh/powershell；'posix' → bash/sh
      title    : 可选窗口标题（仅 Windows 生效）
      cwd      : 可选工作目录。给定时新窗口会先 cd 进去再跑 command；
                 None 时各平台默认（Win=home / mac=Terminal 默认 / Linux=继承）。
                 用于 claude code 等需要"在某项目目录下启动"的场景。

    返回 LaunchResult：ok=True 时 pid 是新进程 id（仅供日志）；ok=False 时 message 写出错原因。

    注意：fire-and-forget；不等 child 退出。如果 child 因为 PATH 找不到 claude/codex 命令立刻挂掉，
    我们这里捕不到——用户会在弹出窗口里看到错误。要避免那种情况，调用方应预先检查命令可执行。
    """
    # cwd 校验：必须存在且是目录，否则忽略（避免 Popen 报 FileNotFoundError）
    effective_cwd: Optional[str] = None
    if cwd:
        cwd_path = os.path.expanduser(cwd)
        if os.path.isdir(cwd_path):
            effective_cwd = cwd_path
        else:
            return LaunchResult(
                ok=False,
                message=f"工作区路径不存在或不是目录：{cwd}",
                platform=platform.system(),
            )

    sys = platform.system()
    if sys == "Windows":
        return _launch_windows(command=command, shell=shell, title=title, cwd=effective_cwd)
    if sys == "Darwin":
        return _launch_macos(command=command, shell=shell, cwd=effective_cwd)
    if sys == "Linux":
        return _launch_linux(command=command, shell=shell, cwd=effective_cwd)
    return LaunchResult(ok=False, message=f"unsupported platform: {sys}", platform=sys)


# ────────────────────────── Windows ──────────────────────────


def _launch_windows(
    *, command: str, shell: ShellMode, title: Optional[str], cwd: Optional[str]
) -> LaunchResult:
    """Win：用 CREATE_NEW_CONSOLE 拉起新窗口，跑 powershell -NoExit -Command "..."。

    -NoExit 让窗口不在命令结束后自动关闭，方便用户看输出 / 继续交互。
    cwd 给定时：Popen 的 cwd 参数已经让新窗口起点目录正确，无需在 command 里 cd。
    """
    # 优先 pwsh（PowerShell 7+），fallback powershell.exe（系统自带 PS 5.1）
    exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
    # cmd.exe 不支持 $env: 语法；如果用户选 posix 想跑 sh 命令，在 Win 上没有 sh，就回退用 PowerShell 跑（多数 POSIX 命令在 PS 也能用）
    # 但 launcher snippet 的 posix 版本是 `VAR=val claude` 这种 → PS 不认。所以 Win 上 posix 模式直接拒绝
    if shell == "posix":
        return LaunchResult(
            ok=False,
            message="Windows 下不支持 posix shell 启动命令；请在面板切到 PowerShell 模式",
            platform="Windows",
        )

    creationflags = 0
    # CREATE_NEW_CONSOLE = 0x00000010：让 child 拥有独立窗口（不附着柳如烟父进程的隐藏 console）
    if hasattr(subprocess, "CREATE_NEW_CONSOLE"):
        creationflags |= subprocess.CREATE_NEW_CONSOLE
    # DETACHED_PROCESS 不要加：会和 CREATE_NEW_CONSOLE 互斥，且 child 会立刻无父→不能继承 stdin

    args = [exe, "-NoExit", "-Command", command]
    # cwd: 优先用 caller 提供的；没提供则起点 home（用户体验最熟悉）
    popen_cwd = cwd or os.path.expanduser("~")
    try:
        proc = subprocess.Popen(  # noqa: S603  # 信任 caller 已 quote 好 token
            args,
            creationflags=creationflags,
            # 不绑 stdin/stdout/stderr，让新窗口接管交互
            cwd=popen_cwd,
        )
    except FileNotFoundError as exc:
        return LaunchResult(ok=False, message=f"找不到 PowerShell：{exc}", platform="Windows")
    except OSError as exc:
        return LaunchResult(ok=False, message=f"启动失败：{exc}", platform="Windows")

    # 设标题（用 title 字段；powershell.exe 启动后无法直接传 title 给新窗口，需另起）
    # M5 简化：title 暂不设；用户可在 PS 里 `$host.ui.RawUI.WindowTitle = "..."`
    _ = title

    msg = f"已在新 PowerShell 窗口启动（cwd={popen_cwd}）" if cwd else "已在新 PowerShell 窗口启动"
    return LaunchResult(ok=True, message=msg, pid=proc.pid, platform="Windows")


# ────────────────────────── macOS ──────────────────────────


def _launch_macos(*, command: str, shell: ShellMode, cwd: Optional[str]) -> LaunchResult:
    """macOS：osascript 拉起 Terminal.app 跑命令。

    Terminal.app 默认 shell 通常是 bash/zsh，跑 POSIX 命令没问题；PowerShell 命令则需要用户装了 pwsh。
    cwd 给定时：在 command 前拼 `cd "<cwd>" && `，让新窗口起点目录正确。
    """
    if shell == "powershell":
        # mac 上 PowerShell 命令也能跑，但是 launcher snippet 的 PS 命令用了 $env: 这是 PS 专有语法
        # 用户在 mac 切到 PowerShell 模式的概率很低；如果用户真这么干，至少给个明确错误
        return LaunchResult(
            ok=False,
            message="macOS 下请切到 POSIX 模式（左上角 shell 切换）",
            platform="Darwin",
        )
    # cwd 拼到 command 前：osascript "do script" 不接受单独的 cwd 参数，只能在命令里 cd
    full_command = command
    if cwd:
        # cwd 已通过上层 isdir 校验；shell-escape 路径里的双引号
        cwd_escaped = cwd.replace('"', '\\"')
        full_command = f'cd "{cwd_escaped}" && {command}'
    # osascript 双引号里的 " 要转义
    safe_command = full_command.replace('"', '\\"')
    # `do script "<cmd>"` 会打开新 Terminal 窗口跑命令
    osascript_body = f'tell application "Terminal" to do script "{safe_command}"'
    try:
        proc = subprocess.Popen(["osascript", "-e", osascript_body])  # noqa: S603,S607
    except FileNotFoundError as exc:
        return LaunchResult(ok=False, message=f"找不到 osascript：{exc}", platform="Darwin")
    msg = f"已在新 Terminal.app 窗口启动（cwd={cwd}）" if cwd else "已在新 Terminal.app 窗口启动"
    return LaunchResult(ok=True, message=msg, pid=proc.pid, platform="Darwin")


# ────────────────────────── Linux ──────────────────────────


_LINUX_TERMINAL_CANDIDATES: List[str] = [
    # (binary, args_template) — args_template 里 {cmd} 占位
    "x-terminal-emulator",
    "gnome-terminal",
    "konsole",
    "xterm",
]


def _launch_linux(*, command: str, shell: ShellMode, cwd: Optional[str]) -> LaunchResult:
    if shell == "powershell":
        return LaunchResult(
            ok=False,
            message="Linux 下请切到 POSIX 模式",
            platform="Linux",
        )
    # 包成 `bash -c "<cmd>; exec bash"` 让命令结束后 shell 保持开着，方便用户继续操作
    inner = f'{command}; exec bash'
    for binary in _LINUX_TERMINAL_CANDIDATES:
        path = shutil.which(binary)
        if not path:
            continue
        # 各 emulator 调用约定略有不同，这里用 `-e bash -c ...` 这套 99% 情况下都认
        # cwd 通过 Popen 的 cwd 参数生效（bash 启动时已在该目录）
        try:
            proc = subprocess.Popen(  # noqa: S603
                [path, "-e", "bash", "-c", inner],
                start_new_session=True,
                cwd=cwd,  # None 时继承父进程 cwd
            )
        except OSError:
            continue
        msg = f"已在 {binary} 新窗口启动" + (f"（cwd={cwd}）" if cwd else "")
        return LaunchResult(
            ok=True,
            message=msg,
            pid=proc.pid,
            platform="Linux",
        )
    return LaunchResult(
        ok=False,
        message=(
            "未找到可用的终端模拟器（试过 "
            + ", ".join(_LINUX_TERMINAL_CANDIDATES)
            + "）；请手动复制命令到你的终端"
        ),
        platform="Linux",
    )


__all__ = ["launch_command_in_new_terminal", "LaunchResult", "ShellMode"]
