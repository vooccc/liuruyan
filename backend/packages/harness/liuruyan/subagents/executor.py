"""v2 · F-4 · ExecutorPool 协议层（只读 snapshot）。

设计：docs/architecture_v2/01_master_plan.md Phase F + 02_runtime_core.md § 2

Phase F 三池设计目标是"scheduler / executor / isolated_loop"分层调度。旧调度 adapter 已下线，F-4 当前保留：

1. **协议层**（本文件）：定义 ``ExecutorPool`` Protocol 只读接口，让外部
   （chain mw / 健康检查 / 监控）通过 Protocol 查 pool 状态。
2. **进程级单例**（本文件 ``get/set_default_executor_pool``）：未来 executor
   pool 可装配到这里，``/healthz`` 等只读接口直接拿 Protocol 取数；未装配时
   调用方降级为 ``not_configured``。

Protocol 只暴露"只读"，保持边界干净，未来加 process pool / 远程 pool / cron 池都能复用。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PoolSnapshot:
    """ExecutorPool 当前状态快照。

    所有字段都是非负整数。``capacity`` 0 表示无上限。
    """

    # 总数：所有 kind / 所有 session 跨切片合计
    running: int = 0           # 正在跑（status=running）
    queued: int = 0            # 排队中（status=queued）
    pending: int = 0           # 等依赖（status=pending）
    capacity: int = 0          # 容量上限（max_concurrent_workers）

    # 元信息
    pool_name: str = "default"
    backend: str = "unknown"   # adapter 标识，例 "v1_worker_scheduler" / "harness_v2_three_pool"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "running": self.running,
            "queued": self.queued,
            "pending": self.pending,
            "capacity": self.capacity,
            "pool_name": self.pool_name,
            "backend": self.backend,
        }
        if self.extra:
            d["extra"] = dict(self.extra)
        return d


@runtime_checkable
class ExecutorPool(Protocol):
    """ExecutorPool 只读接口——查 pool 状态不动行为。

    实现方负责把内部状态（当前 executor / 未来三池 executor / 远程 pool）
    映射成 ``PoolSnapshot``。出错时实现方应该返**部分**字段填 0 + extra
    带 error 字段，而不是抛异常——/healthz 等 best-effort 调用方期望
    永远拿到一个 snapshot。
    """

    def snapshot(self) -> PoolSnapshot:
        """当前 pool 状态快照。永不抛异常。"""
        ...


# ── 进程级默认 pool 单例 ───────────────────────────────────────────────


_default_pool: Optional[ExecutorPool] = None
_default_pool_lock = threading.RLock()


def set_default_executor_pool(pool: Optional[ExecutorPool]) -> None:
    """设置进程级默认 ExecutorPool。

    executor pool 装配时调；测试用例可以传 None reset。多次调用静默覆盖（最后一个赢）。
    """
    global _default_pool
    with _default_pool_lock:
        _default_pool = pool


def get_default_executor_pool() -> Optional[ExecutorPool]:
    """获取进程级默认 ExecutorPool；未装配返 None。

    /healthz 等只读调用方拿这个；返 None 时降级（snapshot 字段填 'not_configured'）。
    """
    with _default_pool_lock:
        return _default_pool


def reset_default_executor_pool() -> None:
    """重置默认 pool——只给测试用。生产路径绝不调。"""
    global _default_pool
    with _default_pool_lock:
        _default_pool = None


__all__ = [
    "ExecutorPool",
    "PoolSnapshot",
    "get_default_executor_pool",
    "reset_default_executor_pool",
    "set_default_executor_pool",
]
