"""v2 · F-0b · subagents/registry — agent kind 注册表

设计：_docs/architecture_v2/01_master_plan.md Phase F + 02_runtime_core.md § 二

agent "kind" 是一类 subagent 的分类标识——决定执行参数（max_concurrent /
default_timeout）+ 怎么 spawn（factory）。

当前柳如烟注册的 kind：
- ``subagent``: spawn_subagent 阻塞调用专家子代理（当前为 placeholder factory）
- 未来：``cron_job`` / ``scene_role`` / ``scheduled_reminder`` 等

每个 kind 注册时声明：
- ``factory``: ``async (config: dict, cancel_event: CancelEvent) -> AsyncIterator[Event]``
- ``max_concurrent``: 同时跑的最大数（超过时 queue / reject）
- ``default_timeout_s``: 没显式 timeout 时的默认（避免无界长跑）
- ``can_cancel``: 是否支持 cancel_event 协作取消（False 时硬 task.cancel）

registry 是 process-wide singleton，启动时由 backend.subagent_bootstrap 注册
现有 kinds；harness 只提供协议 + 注册表，不知道具体实现（boundary 红线）。

API：

```python
from liuruyan.subagents import (
    AgentKind,
    AgentKindRegistry,
    get_default_registry,
)

# 注册（backend 启动时）
registry = get_default_registry()
registry.register(AgentKind(
    kind="subagent",
    factory=spawn_subagent_async_iter,
    max_concurrent=3,
    default_timeout_s=300.0,
    can_cancel=True,
))

# 查询（gateway / scheduler 用）
kind = registry.get("subagent")
if kind is None:
    raise ValueError("subagent kind not registered")
```
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


# AgentKind.factory 协议：
# async (config: dict, cancel_event: CancelEvent | None, **extras) -> AsyncIterator[Event]
# 我们用 Callable + 文档约定，不强 type（不同 kind 可能签名细节不同）
AgentKindFactory = Callable[..., Any]


@dataclass(slots=True)
class AgentKind:
    """一类 subagent 的元信息 + factory。

    ``kind`` 是唯一标识；同名重复注册会 warning + 覆盖（最后注册者赢）。
    """

    kind: str                                # 唯一标识（"subagent" / ...）
    factory: AgentKindFactory                # 创建实例 + 执行的 async callable
    max_concurrent: int = 5                  # 并发上限（同 kind 同时跑的数）
    default_timeout_s: float = 600.0         # 默认 timeout（秒）；0 = 无 timeout
    can_cancel: bool = True                  # 是否支持 cancel_event 协作取消
    metadata: dict[str, Any] = field(default_factory=dict)  # 扩展字段

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("AgentKind.kind must be non-empty")
        if not callable(self.factory):
            raise TypeError(
                f"AgentKind.factory must be callable, got {type(self.factory).__name__}"
            )
        if self.max_concurrent < 1:
            raise ValueError(
                f"AgentKind.max_concurrent must be >= 1, got {self.max_concurrent}"
            )


class AgentKindRegistry:
    """thread-safe agent kind 注册表。"""

    def __init__(self) -> None:
        self._kinds: dict[str, AgentKind] = {}
        self._lock = threading.RLock()

    def register(self, kind: AgentKind, *, overwrite: bool = False) -> None:
        """注册 kind。重复注册同 kind：

        - ``overwrite=True``: 静默覆盖
        - ``overwrite=False``: 抛 ValueError（默认；防止配置错误）

        ``register_factory`` 是 ``register`` 的语法糖（kwargs 构造 + 注册）。
        """
        with self._lock:
            if kind.kind in self._kinds and not overwrite:
                raise ValueError(
                    f"AgentKind {kind.kind!r} already registered "
                    f"(use overwrite=True to replace)"
                )
            if kind.kind in self._kinds:
                log.warning(
                    "[AgentKindRegistry] overwriting kind=%r", kind.kind,
                )
            self._kinds[kind.kind] = kind
            log.debug(
                "[AgentKindRegistry] registered kind=%r max_concurrent=%d "
                "default_timeout=%.1fs can_cancel=%s",
                kind.kind, kind.max_concurrent, kind.default_timeout_s,
                kind.can_cancel,
            )

    def register_factory(
        self,
        kind: str,
        factory: AgentKindFactory,
        *,
        max_concurrent: int = 5,
        default_timeout_s: float = 600.0,
        can_cancel: bool = True,
        metadata: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> AgentKind:
        """语法糖：参数构造 AgentKind + 注册。返新 AgentKind 实例。"""
        ak = AgentKind(
            kind=kind, factory=factory,
            max_concurrent=max_concurrent,
            default_timeout_s=default_timeout_s,
            can_cancel=can_cancel,
            metadata=dict(metadata or {}),
        )
        self.register(ak, overwrite=overwrite)
        return ak

    def get(self, kind: str) -> AgentKind | None:
        with self._lock:
            return self._kinds.get(kind)

    def list_kinds(self) -> list[str]:
        with self._lock:
            return sorted(self._kinds.keys())

    def unregister(self, kind: str) -> bool:
        """移除 kind；返 True 如命中。生产很少用——主要给测试隔离。"""
        with self._lock:
            if kind in self._kinds:
                del self._kinds[kind]
                return True
            return False

    def clear(self) -> None:
        """清所有。生产很少用——主要给测试隔离。"""
        with self._lock:
            self._kinds.clear()


# ── 进程级默认 registry ────────────────────────────────────────────────


_default_registry: AgentKindRegistry | None = None
_default_registry_lock = threading.RLock()


def get_default_registry() -> AgentKindRegistry:
    """进程级单例 registry。lazy 初始化。"""
    global _default_registry
    with _default_registry_lock:
        if _default_registry is None:
            _default_registry = AgentKindRegistry()
        return _default_registry


def reset_default_registry() -> None:
    """重置默认 registry——只给测试用。生产路径绝不调。"""
    global _default_registry
    with _default_registry_lock:
        _default_registry = None


__all__ = [
    "AgentKind",
    "AgentKindFactory",
    "AgentKindRegistry",
    "get_default_registry",
    "reset_default_registry",
]
