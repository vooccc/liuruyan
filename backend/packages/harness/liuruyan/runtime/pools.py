"""池模型 — 4 线程池 + 1 进程池单例。

设计（docs/architecture_v2/02_runtime_core.md § 2）：

- scheduler(3)     subagent / cron / IM dispatch 调度（轻量 submit）
- executor(8)      subagent 执行（带 timeout）= MAX_CONCURRENT_SUBAGENTS(5) + buffer
- isolated_loop(3) 同步入口在 already-running event loop 中调用时
- io(16)           文件 IO / 子进程 / 长 IO
- cpu(2 lazy)      token 计数批处理 / 向量化（绕 GIL；启动慢，懒启动）

⚠️ 绝对禁止：scheduler 池里 pool.submit().result() 等 executor 池任务 → 死锁
⚠️ 绝对禁止：在 ProcessPoolExecutor 任务里读 contextvar（跨进程不传）→ 显式传参
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)

# ── 模块级单例（按"懒构造 + 加锁"模式，支持 shutdown 后重建）─────────────

_lock = threading.Lock()

_scheduler_pool: Optional[ThreadPoolExecutor] = None
_executor_pool: Optional[ThreadPoolExecutor] = None
_isolated_loop_pool: Optional[ThreadPoolExecutor] = None
_io_pool: Optional[ThreadPoolExecutor] = None
_cpu_pool: Optional[ProcessPoolExecutor] = None

# 容量常量（外部可读）
SCHEDULER_WORKERS = 3
EXECUTOR_WORKERS = 8
ISOLATED_LOOP_WORKERS = 3
IO_WORKERS = 16
CPU_WORKERS = 2


def _new_thread_pool(n: int, prefix: str) -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=n, thread_name_prefix=prefix)


def get_scheduler_pool() -> ThreadPoolExecutor:
    """轻量调度池。绝不在这里 .result() 等 executor 池任务。"""
    global _scheduler_pool
    if _scheduler_pool is None:
        with _lock:
            if _scheduler_pool is None:
                _scheduler_pool = _new_thread_pool(SCHEDULER_WORKERS, "liuruyan-scheduler-")
    return _scheduler_pool


def get_executor_pool() -> ThreadPoolExecutor:
    """subagent 执行池。带 timeout 的长跑任务都走这里。"""
    global _executor_pool
    if _executor_pool is None:
        with _lock:
            if _executor_pool is None:
                _executor_pool = _new_thread_pool(EXECUTOR_WORKERS, "liuruyan-exec-")
    return _executor_pool


def get_isolated_loop_pool() -> ThreadPoolExecutor:
    """已在 running event loop 中需要跑同步入口时用。"""
    global _isolated_loop_pool
    if _isolated_loop_pool is None:
        with _lock:
            if _isolated_loop_pool is None:
                _isolated_loop_pool = _new_thread_pool(ISOLATED_LOOP_WORKERS, "liuruyan-isoloop-")
    return _isolated_loop_pool


def get_io_pool() -> ThreadPoolExecutor:
    """文件 / 子进程 / 长 IO。asyncio.to_thread 用这个池更可控。"""
    global _io_pool
    if _io_pool is None:
        with _lock:
            if _io_pool is None:
                _io_pool = _new_thread_pool(IO_WORKERS, "liuruyan-io-")
    return _io_pool


def get_cpu_pool() -> ProcessPoolExecutor:
    """CPU 重活。⚠️ 跨进程 contextvar 不传，必须显式传参。"""
    global _cpu_pool
    if _cpu_pool is None:
        with _lock:
            if _cpu_pool is None:
                _cpu_pool = ProcessPoolExecutor(max_workers=CPU_WORKERS)
    return _cpu_pool


# ── 关闭 ──────────────────────────────────────────────────────────────


def shutdown_pools(timeout: float = 10.0) -> None:
    """优雅关闭所有池。按依赖顺序：scheduler → executor → isolated_loop → io → cpu。

    每个池给独立的（缩短的）超时；某个池超时不阻塞其它。
    超时后强制 cancel_futures（Python 3.9+）。

    可重复调用：调一次后所有 get_*_pool() 重新构造新实例。
    """
    global _scheduler_pool, _executor_pool, _isolated_loop_pool, _io_pool, _cpu_pool

    per_pool_timeout = max(0.5, timeout / 5)
    pools_to_close: list[tuple[str, Optional[ThreadPoolExecutor | ProcessPoolExecutor]]] = [
        ("scheduler", _scheduler_pool),
        ("executor", _executor_pool),
        ("isolated_loop", _isolated_loop_pool),
        ("io", _io_pool),
        ("cpu", _cpu_pool),
    ]

    for name, pool in pools_to_close:
        if pool is None:
            continue
        try:
            # cancel_futures=True (Py 3.9+) 取消队列中未开始的任务
            pool.shutdown(wait=True, cancel_futures=True)
            logger.debug("[pools] %s pool shutdown ok", name)
        except Exception:
            logger.exception("[pools] %s pool shutdown failed", name)

    with _lock:
        _scheduler_pool = None
        _executor_pool = None
        _isolated_loop_pool = None
        _io_pool = None
        _cpu_pool = None


def is_initialized() -> bool:
    """readyz 用：是否至少有一个池已构造（懒启动）。"""
    return any(
        p is not None
        for p in (_scheduler_pool, _executor_pool, _isolated_loop_pool, _io_pool, _cpu_pool)
    )


def warmup() -> None:
    """显式预热所有池（除 cpu 外，cpu 启动慢继续懒启动）。

    Gateway lifespan 启动时调用，避免首请求延迟尖刺。
    """
    get_scheduler_pool()
    get_executor_pool()
    get_isolated_loop_pool()
    get_io_pool()


__all__ = [
    "SCHEDULER_WORKERS",
    "EXECUTOR_WORKERS",
    "ISOLATED_LOOP_WORKERS",
    "IO_WORKERS",
    "CPU_WORKERS",
    "get_scheduler_pool",
    "get_executor_pool",
    "get_isolated_loop_pool",
    "get_io_pool",
    "get_cpu_pool",
    "shutdown_pools",
    "is_initialized",
    "warmup",
]
