"""PowerShell command execution tool for Windows."""

import os
import platform
import shutil
import signal
import subprocess
import tempfile
from typing import Any, Dict, Optional

_IS_WINDOWS = platform.system() == "Windows"


def _find_powershell() -> str:
    """Find PowerShell executable."""
    if not _IS_WINDOWS:
        raise RuntimeError("PowerShell tool is only available on Windows")

    # 优先使用 PowerShell 7+ (pwsh)
    pwsh = shutil.which("pwsh")
    if pwsh:
        return pwsh

    # 回退到 Windows PowerShell 5.1
    powershell = shutil.which("powershell")
    if powershell:
        return powershell

    # 尝试系统路径
    system32 = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(system32):
        return system32

    raise RuntimeError("PowerShell not found. Please ensure PowerShell is installed.")


def _sanitize_env() -> dict:
    """Build a clean environment without sensitive API keys."""
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


def _interpret_exit_code(exit_code: int) -> str:
    """Interpret PowerShell exit codes."""
    if exit_code == 0:
        return "Success"

    codes = {
        1: "General error",
        2: "Misuse of command",
        -1: "Command failed",
    }

    return codes.get(exit_code, f"Exit code {exit_code}")


# cwd / cwd_file 不再拼接进脚本：
# - cwd 走 subprocess `cwd=` 参数
# - cwd_file 走 env var $env:LIURUYAN_CWD_FILE
# 避免 'Set-Location ...' 拼接时 path 含单引号 / 特殊字符被 PS 解析出错。
# UTF-8 输出头：强制 PowerShell 控制台输出编码为 UTF-8。
# 不加这个，Windows 中文系统上 `ping` / `dir` / `tracert` 等系统命令会输出 GBK，
# 我们用 utf-8 decode 就出乱码（“正在 Ping baidu.com” → “���� Ping baidu.com”）。
# OutputEncoding 控 PS cmdlet 输出；[Console]::OutputEncoding 控子进程（ping.exe等）输出。
# chcp 65001 避免在某些环境上 stdout 被上层以系统代码页 reinterpret。
_PS_UTF8_PREAMBLE = (
    "$ErrorActionPreference = 'Continue'\n"
    "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}\n"
    "try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}\n"
    "try { chcp 65001 > $null 2>&1 } catch {}\n"
)

_PS_CWD_TRAILER = (
    "\n$__rc = $LASTEXITCODE\n"
    "if ($null -eq $__rc) { $__rc = 0 }\n"
    "try { if ($env:LIURUYAN_CWD_FILE) { (Get-Location).Path | Out-File -FilePath $env:LIURUYAN_CWD_FILE -Encoding UTF8 -NoNewline } } catch {}\n"
    "exit $__rc\n"
)


def build_powershell_script(command: str) -> str:
    """UTF-8 强制头 + 纯命令 + cwd 持久化尾部。cwd 不出现在脚本里。"""
    # PS 5.1 不支持 && / ||，但支持 ;。trailer 走换行语句分隔，免免边着踩。
    return _PS_UTF8_PREAMBLE + command + _PS_CWD_TRAILER


def build_powershell_argv(command: str) -> list:
    """供 BackgroundCommandManager 直接调用的 argv。"""
    powershell = _find_powershell()
    return [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        build_powershell_script(command),
    ]


class PowerShellTool:
    """Execute PowerShell commands on Windows."""

    def __init__(self):
        if not _IS_WINDOWS:
            raise RuntimeError("PowerShell tool is only available on Windows")

        # 会话状态
        self.cwd = os.getcwd()
        self.env = _sanitize_env()

        # 临时文件用于持久化 CWD
        temp_dir = tempfile.gettempdir()
        self.cwd_file = os.path.join(temp_dir, f"powershell_cwd_{os.getpid()}.txt")

        # 后台进程跟踪
        self.background_procs: Dict[int, subprocess.Popen] = {}

    def prepare_async_command(self, command: str, workdir: Optional[str] = None) -> dict:
        """为 BackgroundCommandManager.start 准备 argv / env / cwd 三元组。"""
        if workdir:
            workdir_abs = os.path.abspath(os.path.expanduser(workdir))
            if not os.path.isdir(workdir_abs):
                raise ValueError(f"Working directory does not exist: {workdir_abs}")
            self.cwd = workdir_abs
        argv = build_powershell_argv(command)
        env = dict(self.env)
        env["LIURUYAN_CWD_FILE"] = self.cwd_file
        return {"argv": argv, "env": env, "cwd": self.cwd}

    async def execute(self, command: str, workdir: Optional[str] = None,
                     timeout: int = 60, background: bool = False,
                     max_output_chars: int = 100_000,
                     stream_callback=None) -> dict:
        """Execute a PowerShell command.

        stream_callback: 可选 async callable（kind: 'stdout'|'stderr', text: str）。
        """
        # 验证工作目录
        if workdir:
            workdir = os.path.abspath(os.path.expanduser(workdir))
            if not os.path.isdir(workdir):
                return {
                    "success": False,
                    "output": "",
                    "stdout": "", "stderr": "",
                    "error": f"Working directory does not exist: {workdir}"
                }
            self.cwd = workdir

        # 脚本不再拼接 cwd / cwd_file；cwd 走 subprocess cwd=，cwd_file 走 env var
        ps_script = build_powershell_script(command)
        env = dict(self.env)
        env["LIURUYAN_CWD_FILE"] = self.cwd_file

        try:
            powershell = _find_powershell()
            argv = [powershell, "-NoProfile", "-NonInteractive", "-Command", ps_script]

            if background:
                # 后台模式：保留原语义（后台主路径走 BackgroundCommandManager）
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

            # 异步运行：stdout/stderr 分流 + 增量推送
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
                "error": "" if success else f"{exit_msg}\n{stderr or stdout}"
            }

        except Exception as e:
            return {
                "success": False,
                "output": "",
                "stdout": "", "stderr": "",
                "error": f"Failed to execute PowerShell command: {str(e)}"
            }

    def _kill_proc_tree_async(self, proc) -> None:
        """同步杀掉 asyncio.subprocess 进程树。"""
        try:
            import subprocess as _sp
            try:
                _sp.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                        capture_output=True, timeout=3)
            except (FileNotFoundError, _sp.TimeoutExpired):
                proc.terminate()
        except Exception:
            pass

    def _kill_process(self, proc: subprocess.Popen):
        """Kill process and all children."""
        try:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
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
