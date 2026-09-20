"""liuruyan.subagents — Subagent 三池协议层（核）。

Phase F 落地：
- F-0a: CancelEvent（协作式取消信号）
- F-0b: AgentKind / AgentKindRegistry（agent kind 注册表）

具体 subagent 实现（worker / subagent / cron_job 等）待 v1 worker.py /
subagent/engine.py 改 shim 时挂进来；harness 不知道具体 kind（boundary 红线）。
"""

from .cancel_event import CancelEvent, CancelEventCancelled, wait_with_cancel
from .executor import (
    ExecutorPool,
    PoolSnapshot,
    get_default_executor_pool,
    reset_default_executor_pool,
    set_default_executor_pool,
)
from .registry import (
    AgentKind,
    AgentKindFactory,
    AgentKindRegistry,
    get_default_registry,
    reset_default_registry,
)

__all__ = [
    "AgentKind",
    "AgentKindFactory",
    "AgentKindRegistry",
    "CancelEvent",
    "CancelEventCancelled",
    "ExecutorPool",
    "PoolSnapshot",
    "get_default_executor_pool",
    "get_default_registry",
    "reset_default_executor_pool",
    "reset_default_registry",
    "set_default_executor_pool",
    "wait_with_cancel",
]
