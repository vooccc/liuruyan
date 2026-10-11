"""v2 · B-6 · ContextSetupMiddleware

从 RunContext 提取身份字段注入 ContextVar，让后续 middleware / tool 可以
通过 `get_effective_user_id()` 等读到当前 run 的身份。

参考：docs/architecture_v2/03_middlewares.md § 01

钩子：before_agent

**关键例外**：所有其它 middleware 都"异常 swallow + log"，
**这个不**——必要字段（user_id / agent_id）缺失时直接 raise，让调用层
立刻 abort run。理由：后续 middleware 都假设 ContextVar 已经设好；
没设好继续跑会得到错误的身份归属（写到错的用户 memory / 错的 session）。

设置方式：直接调底层 ContextVar.set()，不 reset。
作用域由调用层负责：chain runner 应该用 `with_context(...)` 包整个 run，
退出 run 时 with_context 的 LIFO reset 会把 ContextVar 还原。
asyncio 默认每个 task 复制 ContextVar 副本，跨 task 不会污染。
"""

from __future__ import annotations

import logging
from typing import Any

from ..runtime.context import (
    _agent_id,
    _channel,
    _request_id,
    _run_id,
    _session_id,
    _trace_id,
    _user_id,
)
from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


class ContextSetupMiddleware(AgentMiddleware):
    name = "context_setup"

    async def before_agent(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        # 例外：异常不 swallow，必要字段缺失立刻 abort run
        if not ctx.user_id or not isinstance(ctx.user_id, str):
            raise ValueError(
                f"ContextSetup: user_id required, got {ctx.user_id!r}"
            )
        if not ctx.agent_id or not isinstance(ctx.agent_id, str):
            raise ValueError(
                f"ContextSetup: agent_id required, got {ctx.agent_id!r}"
            )

        # 直接 set 底层 ContextVar；调用层用 with_context wrap 整个 run 来 reset
        _user_id.set(ctx.user_id)
        _agent_id.set(ctx.agent_id)
        if ctx.session_id is not None:
            _session_id.set(ctx.session_id)
        if ctx.run_id is not None:
            _run_id.set(ctx.run_id)
        if ctx.trace_id is not None:
            _trace_id.set(ctx.trace_id)
        if ctx.channel is not None:
            _channel.set(ctx.channel)
        if ctx.request_id is not None:
            _request_id.set(ctx.request_id)

        log.debug(
            "ContextSetup: user=%s agent=%s session=%s run=%s trace=%s",
            ctx.user_id, ctx.agent_id, ctx.session_id, ctx.run_id, ctx.trace_id,
        )
        return None


__all__ = ["ContextSetupMiddleware"]
