"""v2 · B-9 · MiddlewareRunner — middleware 链调度器

把 build_middleware_chain 返回的 list[AgentMiddleware] 串起来，5 个钩子各自
按链顺序串行调用。

参考：_docs/architecture_v2/03_middlewares.md § 一 + § 二

契约：
- 异常默认 swallow + log，但 STRICT_MIDDLEWARES 集合内的中间件**异常向上抛**
  （目前只有 ContextSetup —— 后续 middleware 都假设 ContextVar 已设好，
   ContextSetup 失败必须 abort run）
- 钩子返回 dict → 用 `state.update(...)` merge（浅 merge，列表/dict 字段
  整体替换，不深合并；middleware 之间通过约定 key 协作）
- 钩子返回 None → 不改 state
- `state["should_end"] == True` → 立刻中断后续 middleware（不再调用任何 hook）
- before_tool 特殊：
  - 返回 ToolCall 实例 → 替换当前 tool_call，传给下一个 middleware
  - 返回 dict 含 `short_circuit` key → 立即返回 (current_tc, short_circuit_dict)，
    调用层应该跳过工具执行，把 short_circuit 当作工具结果
  - 返回普通 dict → state.update + 继续
  - 返回 None → 不改

调用层使用方式（伪代码）：

    runner = MiddlewareRunner(build_middleware_chain(config))
    state = {}
    with with_context(user_id=ctx.user_id, ...):
        await runner.run_before_agent(state, ctx)
        while not state.get("should_end"):
            await runner.run_before_model(state, ctx)
            ai_msg = await llm_router.invoke(state["messages"])
            state["messages"].append(ai_msg)
            await runner.run_after_model(state, ctx)
            if state.get("should_end"):
                break
            for tc in extract_tool_calls(ai_msg):
                effective_tc, short_circuit = await runner.run_before_tool(state, tc, ctx)
                if short_circuit:
                    result = short_circuit["short_circuit"]
                else:
                    result = await tool_runner.invoke(effective_tc)
                await runner.run_after_tool(state, effective_tc, result, ctx)
                state["messages"].append(result.to_message())
"""

from __future__ import annotations

import logging
from typing import Any

from .base import AgentMiddleware, RunContext, ThreadState, ToolCall, ToolResult

log = logging.getLogger(__name__)


class MiddlewareRunner:
    """串行调度 middleware 链的 5 个钩子。"""

    # 这些 middleware 的异常**不 swallow**，向上抛 abort run
    STRICT_MIDDLEWARES = frozenset({"context_setup"})

    def __init__(self, chain: list[AgentMiddleware]):
        self.chain = list(chain)

    # ── 简单钩子 ──────────────────────────────────────────────────────────

    async def run_before_agent(
        self, state: ThreadState, ctx: RunContext
    ) -> ThreadState:
        for mw in self.chain:
            if state.get("should_end"):
                break
            await self._invoke_simple(mw, "before_agent", state, ctx)
        return state

    async def run_before_model(
        self, state: ThreadState, ctx: RunContext
    ) -> ThreadState:
        for mw in self.chain:
            if state.get("should_end"):
                break
            await self._invoke_simple(mw, "before_model", state, ctx)
        return state

    async def run_after_model(
        self, state: ThreadState, ctx: RunContext
    ) -> ThreadState:
        for mw in self.chain:
            if state.get("should_end"):
                break
            await self._invoke_simple(mw, "after_model", state, ctx)
        return state

    async def _invoke_simple(
        self,
        mw: AgentMiddleware,
        hook_name: str,
        state: ThreadState,
        ctx: RunContext,
    ) -> None:
        """共享逻辑：try invoke + dict merge + 异常按 STRICT 决定 swallow/raise。"""
        try:
            result = await getattr(mw, hook_name)(state, ctx)
        except Exception:
            if mw.name in self.STRICT_MIDDLEWARES:
                raise
            log.exception(
                "MiddlewareRunner: %s.%s swallowed exception",
                mw.name,
                hook_name,
            )
            return
        if isinstance(result, dict):
            state.update(result)

    # ── before_tool（复杂：替换 / 短路）────────────────────────────────

    async def run_before_tool(
        self,
        state: ThreadState,
        tool_call: ToolCall,
        ctx: RunContext,
    ) -> tuple[ToolCall, dict[str, Any] | None]:
        """返回 (effective_tool_call, short_circuit_dict_or_None)。

        - effective_tool_call: 经过 middleware 链后实际要执行的 ToolCall
          （没有替换则等于传入的 tool_call；返回值不可能是 None）
        - short_circuit: 含 `short_circuit` key 时，调用层应跳过工具执行
        """
        current = tool_call
        for mw in self.chain:
            if state.get("should_end"):
                break
            try:
                result = await mw.before_tool(state, current, ctx)
            except Exception:
                if mw.name in self.STRICT_MIDDLEWARES:
                    raise
                log.exception(
                    "MiddlewareRunner: %s.before_tool swallowed exception",
                    mw.name,
                )
                continue

            if isinstance(result, ToolCall):
                current = result
            elif isinstance(result, dict):
                if "short_circuit" in result:
                    # 短路也允许同时 merge 其他 state 字段
                    extras = {k: v for k, v in result.items() if k != "short_circuit"}
                    if extras:
                        state.update(extras)
                    return current, result
                state.update(result)
            # None 不改
        return current, None

    # ── after_tool ──────────────────────────────────────────────────────

    async def run_after_tool(
        self,
        state: ThreadState,
        tool_call: ToolCall,
        result: ToolResult,
        ctx: RunContext,
    ) -> ThreadState:
        for mw in self.chain:
            if state.get("should_end"):
                break
            try:
                ret = await mw.after_tool(state, tool_call, result, ctx)
            except Exception:
                if mw.name in self.STRICT_MIDDLEWARES:
                    raise
                log.exception(
                    "MiddlewareRunner: %s.after_tool swallowed exception",
                    mw.name,
                )
                continue
            if isinstance(ret, dict):
                state.update(ret)
        return state


__all__ = ["MiddlewareRunner"]
