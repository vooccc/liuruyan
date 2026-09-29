"""Computer Use 单实例锁（lockfile + sessionId + signal-0 探活）。

为什么需要：屏幕/键鼠是全局独占资源，两个 session 同时控屏会互相打架。
跨进程语义：lockfile 在 ~/.liuruyan/computer_use.lock，含 {sessionId, pid, acquiredAt}。

接管规则：
- 同 sessionId  → reentrant，直接通过（同一会话连续 action）
- 不同 sessionId 但 pid 已死  → 强制接管（防 IDE 崩了再开就再也控不动）
- 不同 sessionId 且 pid 活着  → 拒绝，返回 holder 信息

注：这是 best-effort 的协作锁，不防恶意（攻击者可以直接删 lockfile）。
    Computer Use 的所有动作都过 audit.py，留 forensic trail 比强锁更重要。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from backend import paths

logger = logging.getLogger(__name__)

LOCK_FILENAME = "computer_use.lock"


def _lock_path() -> Path:
    return paths.data_dir() / LOCK_FILENAME


@dataclass
class LockHolder:
    session_id: str
    pid: int
    acquired_at: float

    @classmethod
    def from_dict(cls, d: dict) -> Optional["LockHolder"]:
        try:
            return cls(
                session_id=str(d["session_id"]),
                pid=int(d["pid"]),
                acquired_at=float(d.get("acquired_at", 0.0)),
            )
        except (KeyError, ValueError, TypeError):
            return None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "acquired_at": self.acquired_at,
        }


def _pid_alive(pid: int) -> bool:
    """探活当前 pid 是否存在。

    - 自己的 pid：直接返 True（避免 Windows os.kill(self, 0) 触发自身 KeyboardInterrupt 坑）
    - POSIX: os.kill(pid, 0) 不存在抛 ProcessLookupError，权限不够当作存活
    - Windows: 用 OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess
              也避免 os.kill 路径——Win32 上 os.kill 走 GenerateConsoleCtrlEvent / TerminateProcess，
              对 signal=0 的语义不一致
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True

    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但跨用户；为了避免误抢，当作存活
        return True
    except OSError:
        return False


def _pid_alive_windows(pid: int) -> bool:
    """OpenProcess + GetExitCodeProcess 探活，无 os.kill 副作用。"""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        # 兜底：极端环境无 ctypes 时当作存活，避免错误抢锁
        return True

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    OpenProcess = kernel32.OpenProcess
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenProcess.restype = wintypes.HANDLE
    GetExitCodeProcess = kernel32.GetExitCodeProcess
    GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    GetExitCodeProcess.restype = wintypes.BOOL
    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 拿不到 handle：进程不存在或无权限。无权限当存活更安全（不误抢）
        err = ctypes.get_last_error()
        # ERROR_INVALID_PARAMETER (87) 通常意味着进程不存在；ERROR_ACCESS_DENIED (5) 是权限
        if err == 87:
            return False
        return True
    try:
        exit_code = wintypes.DWORD(0)
        ok = GetExitCodeProcess(handle, ctypes.byref(exit_code))
        if not ok:
            return True
        return exit_code.value == STILL_ACTIVE
    finally:
        CloseHandle(handle)


def read_holder() -> Optional[LockHolder]:
    """读当前 holder，无锁文件或解析失败返回 None。"""
    p = _lock_path()
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
        d = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict):
        return None
    return LockHolder.from_dict(d)


@dataclass
class AcquireResult:
    acquired: bool
    reason: str  # 'fresh' | 'reentrant' | 'stale_taken_over' | 'blocked'
    holder: Optional[LockHolder] = None  # blocked 时是当前持有者；acquired 时是新持有者


def acquire(session_id: str) -> AcquireResult:
    """尝试拿锁。

    成功路径：
    - fresh: 锁文件不存在或失效
    - reentrant: 已是自己持有
    - stale_taken_over: 别人持有但 pid 已死，强制接管
    """
    if not session_id:
        # session_id 缺失时不允许操作（避免锁文件污染）
        return AcquireResult(False, "blocked", None)

    pid = os.getpid()
    now = time.time()
    p = _lock_path()
    existing = read_holder()

    if existing is not None:
        if existing.session_id == session_id:
            # 同会话续锁：刷新时间戳即可
            new = LockHolder(session_id=session_id, pid=pid, acquired_at=now)
            _write(p, new)
            return AcquireResult(True, "reentrant", new)
        if _pid_alive(existing.pid):
            return AcquireResult(False, "blocked", existing)
        # 持有进程已死 → 接管
        new = LockHolder(session_id=session_id, pid=pid, acquired_at=now)
        _write(p, new)
        logger.info(
            "[computer_use] taking over stale lock from session=%s pid=%d",
            existing.session_id, existing.pid,
        )
        return AcquireResult(True, "stale_taken_over", new)

    # 全新拿锁
    new = LockHolder(session_id=session_id, pid=pid, acquired_at=now)
    _write(p, new)
    return AcquireResult(True, "fresh", new)


def release(session_id: str) -> bool:
    """释放锁。仅当 holder 是自己时才删；否则不动（防误删别人的锁）。"""
    p = _lock_path()
    holder = read_holder()
    if holder is None:
        return True
    if holder.session_id != session_id:
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False


def _write(path: Path, holder: LockHolder) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(holder.to_dict(), ensure_ascii=False), encoding="utf-8")
    # 原子替换（POSIX 和 Windows 均原子）
    os.replace(tmp, path)
