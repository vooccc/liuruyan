"""v2 · LeadAgent — v1 主路径 → v2 chain shim（最小侵入版）。

在 backend/agent.py 的 Agent.chat / Agent.run_collected 顶层包一层 chain，
跑 before_agent + before_model + after_model 各一次。不接管工具循环，
所以现在 5 个 mw 真生效（context_setup / thread_data / skills_inject /
uploads_inject / dangling_tool_call），3 个降级 metric-only（loop_detection /
subagent_limit / clarification 看 history；tool_error_recovery 完全不跑），
2 个挂在链上但 before_tool 钩子未调（guardrail_chain / sandbox_audit
等工具循环上移到 chain 时自然激活）。

feature flag 在 Agent 层面（LIURUYAN_USE_V2_CHAIN，默认 ON）。

参考：
- docs/architecture_v2/03_middlewares.md § 二
- docs/architecture_spec.md § 五选 A
- docs/architecture_spec.md
"""

from __future__ import annotations

import logging
from dataclasses import replace as _dc_replace
from typing import Any, AsyncIterator

from ..buses import AgentEvent, EventBus, get_event_bus
from ..middlewares import (
    AgentMiddleware,
    ClarificationMiddleware,
    ContextSetupMiddleware,
    DanglingToolCallMiddleware,
    GuardrailChainMiddleware,
    LLMErrorHandlingMiddleware,
    LoopDetectionMiddleware,
    MemoryQueueMiddleware,
    MiddlewareRunner,
    RunContext,
    SandboxAuditMiddleware,
    SkillsInjectMiddleware,
    SubagentLimitMiddleware,
    SummarizationGateMiddleware,
    ThreadDataMiddleware,
    TitleGenMiddleware,
    TokenUsageMiddleware,
    ToolCall,
    ToolErrorRecoveryMiddleware,
    ToolResult,
    UploadsInjectMiddleware,
    UserMemoryInjectMiddleware,
    VirtualPathSubstitutionMiddleware,
)
from ..runtime.context import with_cancel_event, with_context, with_tool_hooks
from ..subagents import CancelEvent
from ..tracing import new_trace_id

# 注：这里不能 import backend.agent.Agent — 会被 boundary CI 红线挡掉
# （test_v2_harness_boundary）。LeadAgent 通过 duck-typing 访问 agent 的
# attribute（agent_id / skill_loader / llm_client / run_messages_collected）。
# 后续 Phase 可定义 AgentProtocol，当前 Any 即可。

log = logging.getLogger(__name__)


class LeadAgent:
    """包 v1 LLM 调用，按 chain 顺序跑 before/after hooks。"""

    def __init__(
        self,
        agent: Any,
        *,
        summarizer: Any | None = None,
    ) -> None:
        """``summarizer`` 是 C5-c 摘要功能 DI（可选）：

        async ``(text_to_summarize: str) -> str`` 协议。LeadAgent 把它写到
        每次 run 的 state["summarizer"]，mw 09 在 before_model 看到 + flag
        LIURUYAN_SUMMARIZE_ACTIVATE=1 时调它做真摘要 + 替换 messages。

        默认 None：mw 09 退回 plan-only 模式（不真触发摘要）。
        """
        self.agent = agent
        self.summarizer = summarizer
        self.chain: list[AgentMiddleware] = self._build_chain()
        self.runner = MiddlewareRunner(self.chain)

    @staticmethod
    def _build_chain() -> list[AgentMiddleware]:
        # 顺序按 03_middlewares.md § 二（17 序号里现在是 01/02/04/05/07/08/11/12/13/16/17）。
        # ToolErrorRecovery 钩子是 after_tool，最小 shim 不调；保留位置占位，
        # 未来工具循环上移到 chain 时不必改顺序。
        # GuardrailChain / SandboxAudit 都是 before_tool 钩子，挂上但 chain shim
        # 不调；等工具循环上移自然激活（D-3 handover 已说明）。
        return [
            ContextSetupMiddleware(),       # 01 真生效
            ThreadDataMiddleware(),         # 02 真生效（写 state["thread_data"]）
            UserMemoryInjectMiddleware(),   # 03 真生效（C3-a：注入记忆 system message）
            SkillsInjectMiddleware(),       # 04 真生效
            UploadsInjectMiddleware(),      # 05 真生效（占位 [] 不注入）
            LLMErrorHandlingMiddleware(),   # 06 metric-only emit（C4-a 保守版）
            DanglingToolCallMiddleware(),   # 07 真生效
            LoopDetectionMiddleware(),      # 08 metric-only（看 history）
            SummarizationGateMiddleware(),  # 09 metric-only emit（C5-a 保守版）
            TokenUsageMiddleware(),         # 10 chain 镜像（v1 真算账 + chain emit token_usage_recorded）
            VirtualPathSubstitutionMiddleware(),  # D-5：/mnt/* → 物理路径；必须在 mw 11 之前
            GuardrailChainMiddleware(),     # 11 真生效（C1' hook）
            SandboxAuditMiddleware(),       # 12 真生效（C1' hook）
            ToolErrorRecoveryMiddleware(),  # 13 真生效（C1' hook）
            TitleGenMiddleware(),           # 14 真生效（首轮 emit title_gen_requested）
            MemoryQueueMiddleware(),        # 15 metric-only emit（C3-b 保守版）
            SubagentLimitMiddleware(),      # 16 metric-only（看 history）
            ClarificationMiddleware(),      # 17 metric-only — 必须最后
        ]

    def _build_state(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            equipped = self.agent.skill_loader.get_equipped_skills(
                self.agent.agent_id
            ) or []
        except Exception:
            log.exception("LeadAgent._build_state: get_equipped_skills failed")
            equipped = []
        # C3-a：mw 03 UserMemoryInject 消费 — 调 v1 memory_manager 拿
        # USER+MEMORY+日记+反思+情感整段拼装好的字符串。失败 → "" → mw 跳过注入。
        # 不直接 import backend.memory_manager（boundary 红线）；走 duck-typing
        # 调 v1 Agent.get_memory_context()——v1 内部已 prefetch + 温度排序 + 截断。
        # v5（2026-10-24）：带上最后一条 user 消息原文，供开篇项目自动识别
        # （namespace autobind；见 backend/namespace_manager.autobind_project）。
        try:
            last_user_text = ""
            for _m in reversed(messages):
                if _m.get("role") == "user":
                    _c = _m.get("content")
                    if isinstance(_c, str):
                        last_user_text = _c
                    elif isinstance(_c, list):
                        last_user_text = "\n".join(
                            str(_b.get("text", "")) for _b in _c
                            if isinstance(_b, dict) and _b.get("type") == "text"
                        )
                    break
            memory_text = (
                self.agent.get_memory_context(user_message=last_user_text) or ""
            )
        except Exception:
            log.exception("LeadAgent._build_state: get_memory_context failed")
            memory_text = ""
        get_context_length = getattr(self.agent, "get_active_context_length", None)
        try:
            context_length = get_context_length() if callable(get_context_length) else None
        except Exception:
            log.exception("LeadAgent._build_state: get_active_context_length failed")
            context_length = None
        return {
            "messages": messages,
            "should_end": False,
            "clarification": None,
            # 真数据
            "enabled_skills": equipped,            # list[str]，mw 自己 normalize
            "memory_text": memory_text,            # str，mw 03 UserMemoryInject 注入用
            "context_length": context_length,      # mw 09 动态摘要阈值
            # mw 02 ThreadData 会 before_agent 阶段整体替换 thread_data；
            # 这里给一个最小占位避免万一 mw 02 异常前下游读到 KeyError
            # （mw 02 自己异常路径会写 _error 字段，但保留 agent_id 作为最早期 fallback）。
            "thread_data": {"agent_id": self.agent.agent_id},
            # 占位（下个切片接通）
            "uploaded_files": [],
            "token_usage": {},
            "subagent_count": 0,
            "tool_calls_history": [],
            # C5-c-2: summarizer DI 暴露给 mw 09 真接管摘要时调用
            # None 时 mw 09 退回 plan-only 模式
            "summarizer": self.summarizer,
        }

    @staticmethod
    def _short_circuit_event(state: dict[str, Any]) -> dict[str, Any]:
        clar = state.get("clarification")
        content = ""
        if isinstance(clar, dict):
            content = str(clar.get("question") or clar.get("args") or clar)
        elif clar:
            content = str(clar)
        return {
            "type": "system_notice",
            "kind": "v2_chain_short_circuit",
            "content": content or "（chain 提前结束本轮）",
        }

    @staticmethod
    def _pending_clarification_event() -> dict[str, Any]:
        return {
            "type": "system_notice",
            "kind": "v2_clarification_pending",
            "content": "（chain 检测到 ask_clarification，下一轮自然带上）",
        }

    @staticmethod
    def _ensure_trace_id(ctx: RunContext) -> RunContext:
        """ctx.trace_id 缺失时给个新顶层 trace_id（用于 EventBus / TraceJournal 归一）。"""
        if ctx.trace_id:
            return ctx
        return _dc_replace(ctx, trace_id=new_trace_id())

    # ── v1 dispatch_batch hook 适配器（C1'）─────────────────────────────────
    # 把 v2 chain runner 包装成 v1 dispatch_batch 期望的纯 dict 接口。
    # v1 内部 ContextVar get 出我们 set 的 hook，调时拿到 v1 dict；这里转 v2
    # ToolCall/ToolResult 跑 runner 钩子，再把结果转回 v1 dict。

    def _build_v1_pre_hook(
        self, state: dict[str, Any], ctx: RunContext
    ):
        """生成 v1 pre_tool_hook：mw 11 GuardrailChain 短路 / mw 改写 args 都走这里。"""

        async def pre(
            name: str, arguments: dict, tool_call_id: str
        ) -> dict[str, Any] | None:
            try:
                v2_tc = ToolCall(
                    id=tool_call_id or "",
                    name=name,
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
                effective_tc, short_circuit = await self.runner.run_before_tool(
                    state, v2_tc, ctx
                )
                if short_circuit and "short_circuit" in short_circuit:
                    sc = short_circuit["short_circuit"]
                    # v2 mw 一般返 ToolResult；取 content 当 v1 result 字符串
                    content = (
                        sc.content if isinstance(sc, ToolResult)
                        else (sc if isinstance(sc, str) else str(sc))
                    )
                    return {"short_circuit": content}
                # mw 改写了 args（如未来虚拟路径替换）→ 告诉 v1 用新 args 执行
                if effective_tc.arguments != v2_tc.arguments:
                    return {"replaced_args": dict(effective_tc.arguments)}
            except Exception:
                log.exception(
                    "LeadAgent v1 pre_hook swallowed exception (tool=%s)", name
                )
            return None

        return pre

    def _build_v1_post_hook(
        self, state: dict[str, Any], ctx: RunContext
    ):
        """生成 v1 post_tool_hook：mw 13 ToolErrorRecovery 错误归一化走这里。

        实现细节：mw 13 是 in-place 改 ToolResult.content（不通过返回值替换）；
        所以这里跑完 runner 后比对 v2_tr.content 是否变了，变了就告诉 v1 替换。
        """

        async def post(
            name: str,
            arguments: dict,
            tool_call_id: str,
            result: Any,
            is_error: bool,
        ) -> dict[str, Any] | None:
            try:
                content_str = (
                    result if isinstance(result, str) else str(result)
                )
                v2_tc = ToolCall(
                    id=tool_call_id or "",
                    name=name,
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
                v2_tr = ToolResult(
                    tool_call_id=tool_call_id or "",
                    content=content_str,
                    is_error=is_error,
                )
                await self.runner.run_after_tool(state, v2_tc, v2_tr, ctx)
                if v2_tr.content != content_str:
                    return {"replaced_result": v2_tr.content}
            except Exception:
                log.exception(
                    "LeadAgent v1 post_hook swallowed exception (tool=%s)", name
                )
            return None

        return post

    # ── v2 C2-c chain_hook 反向注入到 v1 _chat_with_tools ───────────────────
    # 让 v1 在每轮工具循环完成后调 chain runner 的 after_model 钩子，让 mw 08
    # LoopDetection / mw 14 TitleGen / mw 15 MemoryQueue / mw 16 SubagentLimit /
    # mw 17 Clarification 等真生效；返 True 时 v1 立即终止循环（chain 决定
    # should_end，比 v1 自己的 budget/guard 决策优先级高）。

    def _build_chain_hook(
        self, state: dict[str, Any], ctx: RunContext
    ):
        """生成 v1 chat 的 chain_hook 回调。

        每轮 LLM 完成 + 工具 dispatch 完成后被 v1 _chat_with_tools 调一次。
        本 hook 内部：
        1. 把 round_done 信息同步进 chain state（让 mw 看到完整 messages 和
           tool_calls_history）
        2. 跑 runner.run_after_model 让 after_model 钩子真生效
        3. 返 state["should_end"] 决定 v1 是否终止循环
        """

        async def hook(round_done: dict) -> bool:
            try:
                # state["messages"] 与 llm_client 工作 messages 是**同一个 list 引用**
                # （见 _build_state line 131），chain_hook 返回后 llm_client 主路径
                # （_chat_with_tools line 1716）会 append assistant_msg + tool_result_msgs。
                # mw 14 / mw 15 读 state["messages"] 末尾时自然能看到最新对。
                #
                # 2026-05-10 bug 修复：本处曾经 `msgs.append(round_done["assistant_msg"])`
                # 重复 append 同一个 dict，导致 messages 里每轮都出现两条紧挨的相同
                # assistant.tool_calls，DeepSeek 报 400 "insufficient tool messages"。
                # chain_hook 是 observer（跑 mw 钩子），不是 messages 的 mutator；owner
                # 只能是 llm_client 主路径。下面累加 tool_calls_history 属于 chain 自己
                # 的状态（不跟 llm_client 共享），保留。
                # （原死代码：round_done 只有复数 `tool_result_msgs`，单数字段从未 yield，
                #  原 `msgs.append(round_done["tool_result_msg"])` 条件永远 False。）

                # 累加 executed_tool_calls 进 history（mw 08 LoopDetection 看 history）
                if round_done.get("has_tool_calls"):
                    history = state.setdefault("tool_calls_history", [])
                    for tc in round_done.get("executed_tool_calls") or []:
                        if isinstance(tc, dict):
                            history.append(tc)

                # C4-b：v1 异常路径调 chain_hook 时携带 llm_error；写进 state 让
                # mw 06 LLMErrorHandling after_model 看到后 emit 事件。
                # 只在异常 round 设置；正常 round_done 不带 llm_error 字段，
                # mw 06 自己 emit 后清掉 state["last_llm_error"]。
                if round_done.get("llm_error"):
                    state["last_llm_error"] = round_done["llm_error"]

                # mw 10 TokenUsage chain 镜像：每轮 _round_done 携带 token_usage
                # 增量（_chat_one_round 在 _record_*_usage 后提取）；写进 state
                # 让 mw 10 after_model 看到后 emit token_usage_recorded 事件做
                # trace journal 标记。mw 10 emit 后清掉 state["last_round_token_usage"]。
                if round_done.get("token_usage"):
                    state["last_round_token_usage"] = round_done["token_usage"]

                # 跑 after_model 钩子（mw 06/08/10/14/15/16/17 真生效）
                await self.runner.run_after_model(state, ctx)

                return bool(state.get("should_end", False))
            except Exception:
                log.exception(
                    "LeadAgent chain_hook swallowed exception (run=%s round=%s)",
                    ctx.run_id, round_done.get("finish_reason"),
                )
                return False

        return hook

    @staticmethod
    async def _emit(
        bus: EventBus | None,
        ctx: RunContext,
        type_: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """安全 emit：bus 为 None 直接跳过；publish 异常 swallow + log。"""
        if bus is None:
            return
        try:
            await bus.publish(
                AgentEvent(
                    type=type_,
                    trace_id=ctx.trace_id or "",
                    session_id=ctx.session_id or "",
                    agent_id=ctx.agent_id,
                    user_id=ctx.user_id,
                    payload=payload or {},
                )
            )
        except Exception:
            log.exception(
                "LeadAgent emit failed type=%s trace=%s",
                type_, ctx.trace_id,
            )

    async def run_streaming(
        self,
        messages: list[dict[str, Any]],
        ctx: RunContext,
        *,
        session_id: str | None,
        prompt_cache_info: dict[str, Any] | None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式：跑 chain 钩子 + 透传 v1 llm_client.chat 事件。"""
        ctx = self._ensure_trace_id(ctx)
        state = self._build_state(messages)
        bus = get_event_bus()  # 进程级单例；未装配时 None → emit 自动跳过
        # F-2c：顶层 CancelEvent。subagent / worker runtime 从 ContextVar
        # 拿到后调 .child(...) 派子 event 透给 v1 scheduler.submit；外部（chain
        # mw / IM channel cancel route）调 cancel_event.cancel(reason) 时所有
        # 派出去的 worker 自动级联取消。
        cancel_event = CancelEvent(trace_id=ctx.trace_id or "")

        with with_context(
            user_id=ctx.user_id,
            agent_id=ctx.agent_id,
            session_id=ctx.session_id or None,
            run_id=ctx.run_id or None,
            trace_id=ctx.trace_id or None,
            channel=ctx.channel or None,
            request_id=ctx.request_id or None,
        ), with_cancel_event(cancel_event):
            # context_setup（strict）：失败直接抛出（fail-loud abort）
            await self.runner.run_before_agent(state, ctx)
            if state.get("should_end"):
                yield self._short_circuit_event(state)
                return

            await self.runner.run_before_model(state, ctx)
            if state.get("should_end"):
                yield self._short_circuit_event(state)
                return

            log.info(
                "[v2.chain] streaming agent=%s session=%s run=%s trace=%s",
                ctx.agent_id, ctx.session_id, ctx.run_id, ctx.trace_id,
            )

            # C1'：把 chain runner 包成 v1 dispatch_batch hook，让 mw 11/12/13
            # 真正在每次工具调用前后跑（before_tool 短路、after_tool 错误归一化等）。
            # C2-c：把 chain runner.run_after_model 包成 v1 _chat_with_tools 的
            # chain_hook，让 mw 08 LoopDetection / mw 14 TitleGen / mw 15 /
            # mw 16 / mw 17 等 after_model 钩子在每轮 LLM 完成后真生效。
            pre_hook = self._build_v1_pre_hook(state, ctx)
            post_hook = self._build_v1_post_hook(state, ctx)
            chain_hook = self._build_chain_hook(state, ctx)
            with with_tool_hooks(pre=pre_hook, post=post_hook):
                async for event in self.agent.llm_client.chat(
                    state["messages"],
                    stream=True,
                    use_tools=True,
                    agent_id=self.agent.agent_id,
                    session_id=session_id,
                    prompt_cache_info=prompt_cache_info,
                    chain_hook=chain_hook,
                ):
                    yield event

            # C2-c 后：after_model 钩子已通过 chain_hook 在每轮跑过；
            # 这里只剩 clarification 信号检查（如果 mw 17 在最后一轮命中）。
            if state.get("clarification"):
                yield self._pending_clarification_event()

            # emit llm_done 进 EventBus（C-3）— TraceJournal 等会订阅落盘
            await self._emit(bus, ctx, "llm_done", {
                "channel": ctx.channel or "chat",
                "stream": True,
            })

    async def run_collected(
        self,
        messages: list[dict[str, Any]],
        ctx: RunContext,
        *,
        collect_events: bool,
        use_tools: bool,
        session_id: str | None,
    ) -> dict[str, Any]:
        """非流式：跑 chain 钩子 + 委托 agent.run_messages_collected 收集结果。

        直接调 run_messages_collected（v1 已稳）而非展开它，chain 仅在
        其调用前后注入。返回 dict 形状与 run_messages_collected 一致：
        {text, notice_events, events, trace_events}。
        """
        ctx = self._ensure_trace_id(ctx)
        state = self._build_state(messages)
        bus = get_event_bus()
        # F-2c：顶层 CancelEvent — 跟 run_streaming 同模式
        cancel_event = CancelEvent(trace_id=ctx.trace_id or "")

        with with_context(
            user_id=ctx.user_id,
            agent_id=ctx.agent_id,
            session_id=ctx.session_id or None,
            run_id=ctx.run_id or None,
            trace_id=ctx.trace_id or None,
            channel=ctx.channel or None,
            request_id=ctx.request_id or None,
        ), with_cancel_event(cancel_event):
            await self.runner.run_before_agent(state, ctx)
            if state.get("should_end"):
                return self._short_circuit_collected(state)

            await self.runner.run_before_model(state, ctx)
            if state.get("should_end"):
                return self._short_circuit_collected(state)

            log.info(
                "[v2.chain] collected agent=%s session=%s run=%s trace=%s",
                ctx.agent_id, ctx.session_id, ctx.run_id, ctx.trace_id,
            )

            # C1'：collected 路径同样接通 v1 dispatch_batch hook
            pre_hook = self._build_v1_pre_hook(state, ctx)
            post_hook = self._build_v1_post_hook(state, ctx)
            with with_tool_hooks(pre=pre_hook, post=post_hook):
                result = await self.agent.run_messages_collected(
                    state["messages"],
                    collect_events=collect_events,
                    use_tools=use_tools,
                    session_id=session_id,
                )

            await self.runner.run_after_model(state, ctx)
            await self._emit(bus, ctx, "llm_done", {
                "channel": ctx.channel or "chat",
                "stream": False,
            })
            if state.get("clarification"):
                pending = self._pending_clarification_event()
                notice_events = list(result.get("notice_events") or [])
                notice_events.append(pending)
                events = list(result.get("events") or [])
                if collect_events:
                    events.append(pending)
                return {
                    **result,
                    "notice_events": notice_events,
                    "events": events,
                }
            return result

    def _short_circuit_collected(self, state: dict[str, Any]) -> dict[str, Any]:
        notice = self._short_circuit_event(state)
        return {
            "text": notice["content"],
            "notice_events": [notice],
            "events": [notice],
            "trace_events": [],
        }


__all__ = ["LeadAgent"]
