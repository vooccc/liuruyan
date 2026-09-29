"""Terminal command execution tool - simplified from Hermes."""

import os
import platform
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

_IS_WINDOWS = platform.system() == "Windows"


def _find_bash() -> str:
    """Find bash for command execution."""
    if not _IS_WINDOWS:
        return (
            shutil.which("bash")
            or ("/usr/bin/bash" if os.path.isfile("/usr/bin/bash") else None)
            or ("/bin/bash" if os.path.isfile("/bin/bash") else None)
            or os.environ.get("SHELL")
            or "/bin/sh"
        )

    # Windows: find Git Bash
    custom = os.environ.get("GIT_BASH_PATH")
    if custom and os.path.isfile(custom):
        return custom

    found = shutil.which("bash")
    if found:
        return found

    for candidate in (
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "bin", "bash.exe"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate

    raise RuntimeError(
        "Git Bash not found. Please install Git for Windows from: https://git-scm.com/download/win"
    )


def _sanitize_env() -> dict:
    """Build a clean environment without sensitive API keys."""
    # 敏感环境变量黑名单
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
    """Remove ANSI escape codes from output."""
    import re
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)


_BASH_CWD_TRAILER = (
    '\n_exit_code=$?\n'
    'if [ -n "${LIURUYAN_CWD_FILE:-}" ]; then pwd > "$LIURUYAN_CWD_FILE"; fi\n'
    'exit $_exit_code\n'
)


def build_bash_script(command: str) -> str:
    """纯命令 + cwd 持久化尾部。cwd 不再出现在脚本里。"""
    # 顶部开 set -o pipefail （1）让 piped 命令的 exit code 反映真实状态，
    # （2）pwd 读取不会被上一条命令 set -e 中间断。
    return command + _BASH_CWD_TRAILER


def build_bash_argv(command: str) -> list:
    """返回可直接传给 asyncio.create_subprocess_exec 的 argv。"""
    bash = _find_bash()
    return [bash, "-c", build_bash_script(command)]


def _interpret_exit_code(exit_code: int, command: str) -> str:
    """Interpret common exit codes."""
    if exit_code == 0:
        return "Success"

    # grep 返回 1 表示没找到匹配，不是错误
    if exit_code == 1 and "grep" in command:
        return "No matches found (not an error)"

    # 常见错误码
    codes = {
        1: "General error",
        2: "Misuse of shell command",
        126: "Command cannot execute (permission denied)",
        127: "Command not found",
        130: "Terminated by Ctrl+C",
        137: "Killed (SIGKILL)",
        143: "Terminated (SIGTERM)",
    }

    return codes.get(exit_code, f"Exit code {exit_code}")


class TerminalTool:
    """Execute shell commands in a bash environment."""

    def __init__(self):
        # 会话状态
        self.cwd = os.getcwd()
        self.env = _sanitize_env()

        # 临时文件用于持久化 CWD
        temp_dir = tempfile.gettempdir()
        self.cwd_file = os.path.join(temp_dir, f"terminal_cwd_{os.getpid()}.txt")

        # 后台进程跟踪
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
        """为 BackgroundCommandManager.start 准备 argv / env / cwd 三元组。

        和 execute() 共享 cwd / env 状态：用户 cd 到某目录后用 blocking=False 跑，
        子进程仍然在该目录开始；后台命令也会更新 self.cwd_file 让后续前台命令接续。
        """
        workdir = self._resolve_effective_workdir(workdir)
        if workdir:
            workdir_abs = os.path.abspath(os.path.expanduser(workdir))
            if not os.path.isdir(workdir_abs):
                raise ValueError(f"Working directory does not exist: {workdir_abs}")
            self.cwd = workdir_abs
        argv = build_bash_argv(command)
        # cwd_file 通过 env var 传递，避免脚本拼接问题
        env = dict(self.env)
        env["LIURUYAN_CWD_FILE"] = self.cwd_file
        return {"argv": argv, "env": env, "cwd": self.cwd}

    async def execute(self, command: str, workdir: Optional[str] = None,
                     timeout: int = 60, background: bool = False,
                     max_output_chars: int = 100_000,
                     stream_callback=None) -> dict:
        """Execute a shell command.

        stream_callback: 可选 async callable（kind: 'stdout'|'stderr', text: str）——
            增量推送给 SSE 之类。返回值仍是全量拼接后的 stdout/stderr。
        """
        # 验证工作目录
        workdir = self._resolve_effective_workdir(workdir)
        if workdir:
            workdir = os.path.abspath(os.path.expanduser(workdir))
            if not os.path.isdir(workdir):
                return {
                    "success": False,
                    "output": "",
                    "stdout": "",
                    "stderr": "",
                    "error": f"Working directory does not exist: {workdir}"
                }
            self.cwd = workdir

        # 脚本不再拼接 cwd / cwd_file；cwd 走 subprocess cwd=，cwd_file 走 env var
        bash_script = build_bash_script(command)
        env = dict(self.env)
        env["LIURUYAN_CWD_FILE"] = self.cwd_file

        try:
            bash = _find_bash()
            argv = [bash, "-c", bash_script]

            if background:
                # 后台模式：保留原语义（没人走这条路，后台走 BackgroundCommandManager）
                popen_kwargs: Dict[str, Any] = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "stdin": subprocess.DEVNULL,
                    "env": env,
                    "cwd": self.cwd,
                    "preexec_fn": None if _IS_WINDOWS else os.setsid,
                }
                if _IS_WINDOWS:
                    popen_kwargs["creationflags"] = 0x08000000 | 0x00000200
                proc_sync = subprocess.Popen(argv, **popen_kwargs)
                self.background_procs[proc_sync.pid] = proc_sync
                return {
                    "success": True,
                    "output": f"Started background process (PID: {proc_sync.pid})\n",
                    "stdout": "", "stderr": "", "error": "",
                }

            # 异步运行：stdout/stderr 分流读取，增量推送到 stream_callback
            import asyncio as _asyncio
            kwargs: Dict[str, Any] = {
                "stdout": _asyncio.subprocess.PIPE,
                "stderr": _asyncio.subprocess.PIPE,
                "stdin": _asyncio.subprocess.DEVNULL,
                "cwd": self.cwd,
                "env": env,
            }
            if not _IS_WINDOWS:
                kwargs["start_new_session"] = True
            else:
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
                # 等 pump 收尾（合理 grace）
                try:
                    await _asyncio.wait_for(_asyncio.gather(pump_out, pump_err, return_exceptions=True), timeout=2.0)
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
                    "error": f"Command timed out after {timeout} seconds"
                }

            stdout = _clean_ansi("".join(stdout_chunks))
            stderr = _clean_ansi("".join(stderr_chunks))

            def _truncate(s: str) -> str:
                if len(s) > max_output_chars:
                    return s[:max_output_chars] + f"\n\n[Output truncated at {max_output_chars} chars]"
                return s

            stdout = _truncate(stdout)
            stderr = _truncate(stderr)
            # output 字段保留 stdout+stderr 合并的向后兼容表示
            combined = stdout
            if stderr:
                combined = (stdout + ("\n" if stdout else "") + stderr) if stdout else stderr

            self._update_cwd()
            exit_msg = _interpret_exit_code(exit_code, command)
            success = exit_code == 0
            if not success and exit_code == 1 and "grep" in command:
                success = True

            return {
                "success": success,
                "output": combined,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "error": "" if success else f"{exit_msg}\n{stderr or stdout}"
            }

        except Exception as e:
            return {
                "success": False,
                "output": "",
                "stdout": "", "stderr": "",
                "error": f"Failed to execute command: {str(e)}"
            }

    def _kill_proc_tree_async(self, proc) -> None:
        """同步杀掉 asyncio.subprocess 进程树（timeout 路径兑底）。"""
        try:
            if _IS_WINDOWS:
                import subprocess as _sp
                try:
                    _sp.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                            capture_output=True, timeout=3)
                except (FileNotFoundError, _sp.TimeoutExpired):
                    proc.terminate()
            else:
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
        except Exception:
            pass

    def _kill_process(self, proc: subprocess.Popen):
        """Kill process and all children."""
        try:
            if _IS_WINDOWS:
                proc.terminate()
            else:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:
                pass

    def _update_cwd(self):
        """Read CWD from temp file."""
        try:
            with open(self.cwd_file, 'r', encoding='utf-8', errors='replace') as f:
                cwd_path = f.read().strip()
                if cwd_path and os.path.isdir(cwd_path):
                    self.cwd = cwd_path
        except (OSError, FileNotFoundError):
            pass

    def cleanup(self):
        """Clean up temp files and background processes."""
        # 清理临时文件
        try:
            os.unlink(self.cwd_file)
        except OSError:
            pass

        # 终止后台进程
        for pid, proc in list(self.background_procs.items()):
            try:
                if proc.poll() is None:  # 仍在运行
                    self._kill_process(proc)
            except Exception:
                pass
            del self.background_procs[pid]
