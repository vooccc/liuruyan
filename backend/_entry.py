"""打包 entry：把项目根塞进 sys.path 让 `backend` 以 package 形式加载。

直接拿 main.py 当 entry 会让 `from .guardian` 这类相对 import 炸掉
（顶层脚本没有 __package__）。这里绕一层：先 tweak sys.path，
再 `from backend.main import run_server`，让解释器把 backend 识别为 package。

开发模式下不走这条路径 —— `启动.bat` 里直接 `python backend/main.py`
触发 main.py 的 `__name__ == "__main__"`。

支持 PyInstaller 和 Nuitka 两种打包方式。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _is_frozen() -> bool:
    """检测是否在打包环境（PyInstaller 或 Nuitka）中运行。"""
    if getattr(sys, "frozen", False):
        return True
    # Nuitka onefile 解压到 Temp\onefile_XXX 目录
    if "onefile_" in os.path.abspath(__file__).lower():
        return True
    return False


def _ensure_backend_on_path() -> None:
    if _is_frozen():
        # PyInstaller onefile：_MEIPASS 是临时解压目录
        # Nuitka onefile：不设 _MEIPASS，__file__ 指向解压目录内的路径
        base = getattr(sys, "_MEIPASS", None)
        if base is None:
            # Nuitka onefile：__file__ 在解压目录里，父目录的父目录就是解压根
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    else:
        # dev 调试：_entry.py 在 backend/ 下，父目录就是项目根
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if base not in sys.path:
        sys.path.insert(0, base)
    for harness_cand in (
        os.path.join(base, "backend", "packages", "harness"),
        os.path.join(base, "packages", "harness"),
    ):
        if os.path.isdir(harness_cand) and harness_cand not in sys.path:
            sys.path.insert(0, harness_cand)


def _crash_log(exc: BaseException) -> None:
    """把崩溃 traceback 写到 ~/.liuruyan/backend_crash.log，方便无控制台模式调试。"""
    try:
        log_dir = Path.home() / ".liuruyan"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "backend_crash.log"
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Backend crashed at {os.popen('echo %DATE% %TIME%').read().strip()}\n")
            f.write(f"sys.executable = {sys.executable}\n")
            f.write(f"sys.frozen = {getattr(sys, 'frozen', False)}\n")
            f.write(f"__compiled__ = {hasattr(sys, '__compiled__')}\n")
            f.write(f"sys._MEIPASS = {getattr(sys, '_MEIPASS', 'NOT SET')}\n")
            f.write(f"__file__ = {__file__}\n")
            f.write(f"sys.path = {sys.path}\n")
            f.write("\n--- traceback ---\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    except Exception:
        pass  # 崩溃日志本身不能再崩


def _sanitize_working_directory() -> None:
    """如果进程启动时 working directory 是 Windows 系统目录（如 System32），切换到用户数据目录。"""
    if os.name == "nt":
        try:
            cwd = os.getcwd().lower()
            sys_root = os.environ.get("SystemRoot", r"C:\Windows").lower()
            win_dir = os.environ.get("WINDIR", r"C:\Windows").lower()
            if (
                cwd == sys_root
                or cwd.startswith(sys_root + "\\")
                or cwd == win_dir
                or cwd.startswith(win_dir + "\\")
                or "system32" in cwd
                or "syswow64" in cwd
            ):
                from backend import paths
                target = paths.data_dir()
                os.chdir(str(target))
        except Exception:
            pass


def main() -> None:
    _sanitize_working_directory()
    _ensure_backend_on_path()
    # Nuitka --windows-console-mode=disable 会让 sys.stderr / sys.stdout 为 None。
    # uvicorn logging 调用 sys.stderr.isatty() 会崩 AttributeError。
    # 用 devnull 替换 None，让 logging 正常工作。
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    try:
        from backend.main import run_server
        run_server()
    except Exception as e:
        _crash_log(e)
        raise


if __name__ == "__main__":
    main()
