"""Windows cmd.exe command execution tool.

为什么单独一份：
- PowerShell 5.1 不支持 `&&` / `||`，常见 Unix 风格脚本（`cd dir && npm install`）直接炸；
  pwsh 7+ 支持但不一定都装。cmd.exe 上 `&&` / `&` / `||` 全支持，是 Windows
  上很多模型熟悉的 fallback。
- 跟 terminal_tool / powershell_tool 同构：cwd 走 subprocess cwd=（不进脚本字符串），
  cwd_file 走 env var %LIURUYAN_CWD_FILE%，execute() 异步 + stdout/stderr 分流 +
  stream_callback hook。

红线：不要把 cwd / cwd_file 拼进脚本——Windows 路径含 `\`、空格、中文都会炸。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Optional

_IS_WINDOWS = platform.system() == "Windows"


def _find_cmd() -> str:
    """Find cmd.exe."""
    if not _IS_WINDOWS:
        raise RuntimeError("cmd tool is only available on Windows")
    found = shutil.which("cmd")
    if found:
        return found
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    if os.path.isfile(system32):
        return system32
    raise RuntimeError("cmd.exe not found")


def _sanitize_env() -> dict:
    """Build a clean environment without sensitive API keys (mirror terminal_tool)."""
    blocklist = {
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE",
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
        "DEEPSEEK_API_KEY", "GOOGLE_API_KEY", "MISTRAL_API_KEY",
        "GROQ_API_KEY", "TOGETHER_API_KEY", "COHERE_API_KEY",
        "FIRECRAWL_API_KEY", "TAVILY_API_KEY", "EXA_API_KEY",
        "GH_TOKEN", "GITHUB_TOKEN", "SLACK_TOKEN",
    }
    return {k: v for k, v in os.environ.items() if k not in blocklist}


def _clean_ansi(text: str) -> str:
    """Strip ANSI escapes（cmd 输出几乎无 ANSI，但 nodejs / colored tools 会产生）。"""
    import re
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


def _interpret_exit_code(exit_code: int) -> str:
    if exit_code == 0:
        return "Success"
    codes = {1: "General error", 2: "Misuse"}
    return codes.get(exit_code, f"Exit code {exit_code}")


# cwd / cwd_file 不进脚本字符串。
# cwd 走 subprocess cwd=；cwd_file 走 env var %LIURUYAN_CWD_FILE%。
# 尾部用 @echo off 风格不污染 stdout，写 cwd 到 file（cmd 的 `cd` 不带参数即显示当前目录，
# 配合 `> "%LIURUYAN_CWD_FILE%"` 重定向到文件）。
# 红线（2026-05-15）：`cmd /C "<script>"` **只执行第一行**——多行 `\r\n` 后面的
# 全被忽略。所以必须用 `&`（无条件顺序执行）把 preamble / 用户命令 / trailer
# 链接成**单行字符串**。`&&` 是条件执行（前一条成功才走下一条），不能用。
#
# UTF-8 头：chcp 65001 把 cmd 控制台代码页切到 UTF-8，避免 ping/dir 等系统
# 命令的中文输出按 GBK 而我们 decode utf-8 乱码。`>nul 2>&1` 静音 chcp 自身回显。
_CMD_UTF8_PREAMBLE = "@chcp 65001 >nul 2>&1"

# cwd 持久化 trailer：用 setlocal 抓 ERRORLEVEL 后写 cwd。
# 注：`%ERRORLEVEL%` 在单行 `&` 链中是**解析时展开**而非运行时——所以
# `cmd & set RC=%ERRORLEVEL%` 抓的是 cmd 启动前的 ERRORLEVEL（多半 0），不是 cmd 退出码。
# 用 `call` 强制重新解析；或者干脆不用变量，直接 if 判断（这里走 if 判断省一步）。
# 不抓 exit code → cmd 自然把最后一条命令的 ERRORLEVEL 当退出码（默认行为），ok。
_CMD_CWD_TRAILER = (
    ' & (if defined LIURUYAN_CWD_FILE (cd > "%LIURUYAN_CWD_FILE%"))'
)


def build_cmd_script(command: str) -> str:
    """UTF-8 强制头 + 用户命令 + cwd 持久化尾部，**单行**用 `&` 链接。

    cmd /C 只跑第一行，必须单行；用 `&`（无条件）而非 `&&`（条件）链接，
    确保 trailer 永远跑（哪怕用户命令失败也要写 cwd）。

    红线：**不要**用 `(...)` 子 shell 包裹用户命令——用户命令里若有 `&&` 会跟
    cmd 的 subshell 解析冲突（`(echo a && echo b)` 直接 syntax error）。
    现在直接平铺：`preamble & user_command & trailer`，cmd 从左到右执行。
    """
    return f"{_CMD_UTF8_PREAMBLE} & {command}{_CMD_CWD_TRAILER}"


def build_cmd_argv(command: str) -> list:
    """供 BackgroundCommandManager 直接调用的 argv。

    cmd /S /C "<script>" —— /S 让 cmd 严格按字面处理外层引号，避免它擅自剥引号；
    /C 执行完即退出。我们把脚本作为一整个参数传入。
    """
    cmd_exe = _find_cmd()
    # cmd /C 接受多行字符串；我们用 `<script>` 而不是文件，避免临时文件残留
    return [cmd_exe, "/S", "/C", build_cmd_script(command)]


class CmdTool:
    """Execute commands via cmd.exe on Windows."""

    def __init__(self):
        if not _IS_WINDOWS:
            raise RuntimeError("cmd tool is only available on Windows")
        self.cwd = os.getcwd()
        self.env = _sanitize_env()
        temp_dir = tempfile.gettempdir()
        self.cwd_file = os.path.join(temp_dir, f"cmd_cwd_{os.getpid()}.txt")
        self.background_procs: Dict[int, subprocess.Popen] = {}

    def _resolve_effective_workdir(self, workdir: Optional[str]) -> Optional[str]:
        if workdir:
            return workdir
        try:
            from .file_tools import _current_working_dir
            wd = _current_working_dir()
            if wd:
                return str(wd)
        except Exception:
            pass
        return None

    def prepare_async_command(self, command: str, workdir: Optional[str] = None) -> dict:
        workdir = self._resolve_effective_workdir(workdir)
        if workdir:
            workdir_abs = os.path.abspath(os.path.expanduser(workdir))
            if not os.path.isdir(workdir_abs):
                raise ValueError(f"Working directory does not exist: {workdir_abs}")
            self.cwd = workdir_abs
        argv = build_cmd_argv(command)
        env = dict(self.env)
        env["LIURUYAN_CWD_FILE"] = self.cwd_file
        return {"argv": argv, "env": env, "cwd": self.cwd}

    async def execute(self, command: str, workdir: Optional[str] = None,
                      timeout: int = 60, background: bool = False,
                      max_output_chars: int = 100_000,
                      stream_callback=None) -> dict:
        """Execute a cmd.exe command.

        stream_callback: 可选 async callable（kind: 'stdout'|'stderr', text: str）。
        """
        workdir = self._resolve_effective_workdir(workdir)
        if workdir:
            workdir = os.path.abspath(os.path.expanduser(workdir))
            if not os.path.isdir(workdir):
                return {
                    "success": False, "output": "",
                    "stdout": "", "stderr": "",
                    "error": f"Working directory does not exist: {workdir}",
                }
            self.cwd = workdir

        cmd_script = build_cmd_script(command)
        env = dict(self.env)
        env["LIURUYAN_CWD_FILE"] = self.cwd_file

        try:
            cmd_exe = _find_cmd()
            argv = [cmd_exe, "/S", "/C", cmd_script]

            if background:
                popen_kwargs: Dict[str, Any] = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "stdin": subprocess.DEVNULL,
                    "env": env,
                    "cwd": self.cwd,
                }
                if os.name == "nt":
                    popen_kwargs["creationflags"] = 0x08000000 | 0x00000200
                proc_sync = subprocess.Popen(argv, **popen_kwargs)
                self.background_procs[proc_sync.pid] = proc_sync
                return {
                    "success": True,
                    "output": f"Started background process (PID: {proc_sync.pid})\n",
                    "stdout": "", "stderr": "", "error": "",
                }

            import asyncio as _asyncio
            kwargs: Dict[str, Any] = {
                "stdout": _asyncio.subprocess.PIPE,
                "stderr": _asyncio.subprocess.PIPE,
                "stdin": _asyncio.subprocess.DEVNULL,
                "cwd": self.cwd,
                "env": env,
            }
            if os.name == "nt":
                kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

            proc = await _asyncio.create_subprocess_exec(*argv, **kwargs)

            stdout_chunks: list = []
            stderr_chunks: list = []

            async def _pump(stream, kind: str, sink: list):
                if stream is None:
                    return
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    # cmd 在中文 Windows 上常输出 GBK；先 utf-8 试，errors='replace' 兜底。
                    # 严格 GBK 解码会把 ASCII 命令的英文输出搞砸，这里走宽松 utf-8 + replace
                    text = chunk.decode("utf-8", errors="replace")
                    sink.append(text)
                    if stream_callback is not None:
                        try:
                            await stream_callback(kind, text)
                        except Exception:
                            pass

            pump_out = _asyncio.create_task(_pump(proc.stdout, "stdout", stdout_chunks))
            pump_err = _asyncio.create_task(_pump(proc.stderr, "stderr", stderr_chunks))

            try:
                exit_code = await _asyncio.wait_for(proc.wait(), timeout=timeout)
                try:
                    await _asyncio.wait_for(
                        _asyncio.gather(pump_out, pump_err, return_exceptions=True),
                        timeout=2.0,
                    )
                except _asyncio.TimeoutError:
                    pass
            except _asyncio.TimeoutError:
                self._kill_proc_tree_async(proc)
                pump_out.cancel()
                pump_err.cancel()
                return {
                    "success": False,
                    "output": _clean_ansi("".join(stdout_chunks))[-max_output_chars:],
                    "stdout": _clean_ansi("".join(stdout_chunks))[-max_output_chars:],
                    "stderr": _clean_ansi("".join(stderr_chunks))[-max_output_chars:],
                    "error": f"Command timed out after {timeout} seconds",
                }

            stdout = _clean_ansi("".join(stdout_chunks))
            stderr = _clean_ansi("".join(stderr_chunks))

            def _truncate(s: str) -> str:
                if len(s) > max_output_chars:
                    return s[:max_output_chars] + f"\n\n[Output truncated at {max_output_chars} chars]"
                return s

            stdout = _truncate(stdout)
            stderr = _truncate(stderr)
            combined = stdout
            if stderr:
                combined = (stdout + ("\n" if stdout else "") + stderr) if stdout else stderr

            self._update_cwd()
            exit_msg = _interpret_exit_code(exit_code)
            success = exit_code == 0

            return {
                "success": success,
                "output": combined,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "error": "" if success else f"{exit_msg}\n{stderr or stdout}",
            }

        except Exception as e:
            return {
                "success": False, "output": "",
                "stdout": "", "stderr": "",
                "error": f"Failed to execute cmd command: {str(e)}",
            }

    def _kill_proc_tree_async(self, proc) -> None:
        try:
            import subprocess as _sp
            try:
                _sp.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                        capture_output=True, timeout=3)
            except (FileNotFoundError, _sp.TimeoutExpired):
                proc.terminate()
        except Exception:
            pass

    def _update_cwd(self):
        try:
            with open(self.cwd_file, "r", encoding="utf-8", errors="replace") as f:
                cwd_path = f.read().strip()
                if cwd_path and os.path.isdir(cwd_path):
                    self.cwd = cwd_path
        except (OSError, FileNotFoundError):
            pass

    def cleanup(self):
        try:
            os.unlink(self.cwd_file)
        except OSError:
            pass
        for pid, proc in list(self.background_procs.items()):
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            del self.background_procs[pid]
