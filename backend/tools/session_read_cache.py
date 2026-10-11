"""
Session 级 read 缓存：read-before-edit gate 的数据底座

记录"本会话内对哪些文件调用过 read_file，文件 mtime 是多少"。
patch / multi_edit / write_file（覆写已有文件）入口检查：
- 文件在缓存里 + mtime 没变 → 通过
- 文件不在缓存里 → 报错"先 read_file"
- mtime 变了（外部改过） → 报错"文件已变，请重新 read_file"

设计要点：
- 内存级 dict，session_id 维度隔离，不需要持久化（chat 结束就忘）
- 弱引用 / 周期清理：超过 N 个会话或 N 小时不动的清掉
- 没有 session_id 的调用（脚本 / 后台 worker）→ 跳过 gate（不强制）
- 线程安全：dict 操作配 Lock
- "新建文件"模式不走 gate（path 不存在的 write_file 直接放行）
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple


# 会话过期时间：超过这个时间没动过的 session 缓存丢弃
_SESSION_TTL_SECONDS = 6 * 3600
# 清理触发阈值：超过 N 个 session 时跑一次清理
_CLEANUP_THRESHOLD = 100


class SessionReadCache:
    """每个 session 一份 {resolved_path: (mtime, last_read_ts)} dict"""

    _instance: Optional["SessionReadCache"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls) -> "SessionReadCache":
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._lock = threading.Lock()
        # session_id -> {resolved_path_str: (mtime, last_read_ts)}
        self._cache: Dict[str, Dict[str, Tuple[float, float]]] = {}
        # session_id -> last_touch_ts，用于过期清理
        self._touch: Dict[str, float] = {}

    @staticmethod
    def _resolve(path: str) -> Optional[str]:
        try:
            p = Path(path).expanduser()
            if not p.is_absolute():
                try:
                    from backend.tools.builtin.file_tools import _current_working_dir
                    base = _current_working_dir()
                    if base:
                        p = base / p
                except Exception:
                    pass
            return str(p.resolve())
        except Exception:
            return None

    @staticmethod
    def _safe_mtime(resolved: str) -> Optional[float]:
        try:
            return os.path.getmtime(resolved)
        except OSError:
            return None

    def record_read(self, session_id: Optional[str], path: str) -> None:
        """read_file 成功后调用：记下当前 mtime + 时间戳"""
        if not session_id or not path:
            return
        resolved = self._resolve(path)
        if resolved is None:
            return
        mtime = self._safe_mtime(resolved)
        if mtime is None:
            return  # 文件不存在 / 不可访问 → 不记
        now = time.time()
        with self._lock:
            self._cache.setdefault(session_id, {})[resolved] = (mtime, now)
            self._touch[session_id] = now
            self._maybe_cleanup_locked(now)

    def check_read_before_edit(
        self, session_id: Optional[str], path: str
    ) -> Tuple[bool, str]:
        """检查文件是否能被修改。返回 (allowed, reason)。

        策略：
        - session_id 缺 → 放行（脚本 / 后台调用）
        - 文件不存在 → 放行（新建文件场景，由调用方决定）
        - 在缓存里 + mtime 没变 → 放行
        - 在缓存里 + mtime 变了 → 拒（外部修改过）
        - 不在缓存里 → 拒（请先 read）
        """
        if not session_id or not path:
            return True, ""

        resolved = self._resolve(path)
        if resolved is None:
            return True, ""

        # 文件不存在 → 视为新建场景，放行
        if not os.path.exists(resolved):
            return True, ""

        with self._lock:
            session_dict = self._cache.get(session_id, {})
            entry = session_dict.get(resolved)

        current_mtime = self._safe_mtime(resolved)
        if current_mtime is None:
            return True, ""  # mtime 拿不到 → 不卡

        if entry is None:
            return False, (
                f"file not read in this session: {path}. "
                "Call read_file first to confirm current contents — "
                "this prevents acting on stale or hallucinated content."
            )

        cached_mtime, _ = entry
        # 容许 1ms 浮点误差
        if abs(current_mtime - cached_mtime) > 0.001:
            return False, (
                f"file changed since last read: {path}. "
                "Call read_file again to refresh — the file was modified "
                "by another process or a previous edit you didn't track."
            )

        return True, ""

    def invalidate(self, session_id: Optional[str], path: str) -> None:
        """patch / multi_edit / write_file 成功写入后调用：更新 mtime 让后续编辑可继续"""
        if not session_id or not path:
            return
        resolved = self._resolve(path)
        if resolved is None:
            return
        mtime = self._safe_mtime(resolved)
        if mtime is None:
            return
        now = time.time()
        with self._lock:
            self._cache.setdefault(session_id, {})[resolved] = (mtime, now)
            self._touch[session_id] = now

    def forget_session(self, session_id: str) -> None:
        with self._lock:
            self._cache.pop(session_id, None)
            self._touch.pop(session_id, None)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "sessions": len(self._cache),
                "files_total": sum(len(v) for v in self._cache.values()),
            }

    def _maybe_cleanup_locked(self, now: float) -> None:
        """周期性清理：必须持有 self._lock 调用"""
        if len(self._touch) < _CLEANUP_THRESHOLD:
            return
        cutoff = now - _SESSION_TTL_SECONDS
        expired = [sid for sid, ts in self._touch.items() if ts < cutoff]
        for sid in expired:
            self._cache.pop(sid, None)
            self._touch.pop(sid, None)


_cache_singleton: Optional[SessionReadCache] = None


def get_read_cache() -> SessionReadCache:
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = SessionReadCache()
    return _cache_singleton
