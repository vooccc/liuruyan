"""v2 · B-16 · TokenUsageMiddleware (mw 10) — chain 镜像版

钩子：``after_model``

职责：每轮 LLM 调用完成后，把 v1 ``_record_session_token_usage`` /
``_record_openai_usage`` 已经算到的 token 增量 emit 到 EventBus 让
TraceJournal 落盘——trace 维度的可观测性（per-trace JSONL 看到每轮 token
增量，而不只是 per-session 累计）。

参考：docs/architecture_v2/03_middlewares.md § 10

**当前形态：chain 镜像（v1 真接管 + chain emit）**

- v1 已完整实现：``LLMClient._record_openai_usage`` /
  ``AnthropicClient._record_session_token_usage`` 内部直接调
  ``SessionManager.record_token_usage`` 写 per-session usage.json + emit
  ``token_usage_updated`` 事件给前端 badge
- mw 10 不重复算账——只 emit ``token_usage_recorded`` 事件做 trace journal 标记

跟 mw 06 / mw 09 / mw 15 metric-only 模式语义对称（都是观察者形态）。

**输入**（state 字段）：
- ``state["last_round_token_usage"]``: dict | None — LeadAgent chain_hook 在每轮
  ``_round_done`` 后从 ``round_done["token_usage"]`` 写入。空 / 缺失 → mw 跳过。

**事件 schema**（payload）：
    {
        "input_tokens": int,
        "output_tokens": int,
        "cache_read": int,        # anthropic 才有，OpenAI 是 0
        "cache_create": int,      # anthropic 才有，OpenAI 是 0
        "round_number": int,      # 第几轮（从 1 开始）
    }

emit 后清掉 ``state["last_round_token_usage"]``，避免下一轮重复 emit 同样的值
（per-round 单独 emit，不累加）。

异常隔离：内部 try/except；任何异常 swallow + log。
"""

from __future__ import annotations

import logging
from typing import Any

from ..buses import AgentEvent, get_event_bus
from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


class TokenUsageMiddleware(AgentMiddleware):
    name = "token_usage"

    async def after_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            usage = state.get("last_round_token_usage")
            if not usage or not isinstance(usage, dict):
                return None

            bus = get_event_bus()
            if bus is None:
                # bus 未装配 → 静默跳过；保留 last_round_token_usage 让下次有 bus 时仍 emit
                log.debug(
                    "TokenUsageMiddleware: event_bus not configured, skip emit "
                    "(run=%s session=%s)",
                    ctx.run_id, ctx.session_id,
                )
                return None

            count = int(state.get("token_usage_emit_count") or 0)
            event = AgentEvent(
                type="token_usage_recorded",
                trace_id=ctx.trace_id or "",
                session_id=ctx.session_id or "",
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
                payload={
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "cache_read": int(usage.get("cache_read") or 0),
                    "cache_create": int(usage.get("cache_create") or 0),
                    "round_number": count + 1,
                },
            )
            await bus.publish(event)
            log.debug(
                "TokenUsage emit: session=%s agent=%s in=%d out=%d round=%d trace=%s",
                ctx.session_id, ctx.agent_id,
                event.payload["input_tokens"], event.payload["output_tokens"],
                count + 1, ctx.trace_id,
            )
            return {
                "last_round_token_usage": None,  # 清掉避免重复 emit
                "token_usage_emit_count": count + 1,
            }
        except Exception:
            log.exception(
                "TokenUsageMiddleware swallowed exception (run=%s)",
                ctx.run_id,
            )
            return None


__all__ = ["TokenUsageMiddleware"]
