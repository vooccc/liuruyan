"""v2 · B-13 · MemoryQueueMiddleware (mw 15) — C3-b metric-only 版

钩子：``after_model``

职责：每轮 user→assistant 完成后 emit ``memory_refine_requested`` 事件到
EventBus，让订阅方（TraceJournal / 未来真接管的 subscriber）看到"这一轮
对话有提炼请求"。

参考：docs/architecture_v2/03_middlewares.md § 15

**当前版本是 metric-only（半激活）**

C3-b 调研后选择保守版的理由：
- v1 ``main.py:4976`` 通过 ``agent.trigger_refine_for_turn`` 真触发
  ``MemoryRefinerV2.process_and_save``，需要 ``message_span``（[start, end]
  消息索引）作为输入。``message_span`` 由 v1 main.py 拿 ``session.message_count``
  + 特殊路径（prefilled / recall）算出来——chain 内部**没有 session 状态访问**，
  算不出准确 span。
- 真接管要：① v1 main.py 通过 ContextVar 把 message_span 传进 chain，或
  ② 改 chain 入口签名加 message_span 参数。两种方案都比 C3-a "条件砍 +
  chain 接管"复杂得多，跨多窗口。
- C3-b 保守版让 mw 15 emit 事件作为 trace journal 标记 + 占位（结构就位
  + 测试已写）；等 C2 工具循环上移那一刀顺势把真触发也搬到 chain。

**当前 v1 主路径不动**——``main.py:4976`` 仍调 ``trigger_refine_for_turn``
真触发提炼。chain ON 时 mw 15 额外 emit 事件让 TraceJournal 落盘看到，
但**不重复触发提炼**（避免双倍 LLM 调用）。

钩子触发条件：
- state["messages"] 至少有 1 user + 1 assistant
- assistant 必须有文本（纯工具调用中间态跳过）
- ``state["memory_queue_emit_count"]`` 跟踪本 run 已 emit 次数（多轮对话每轮各 emit 一次）

事件 schema（payload）：
    {
        "user_message": str,           # 截断到 ≤ 500 字符
        "assistant_response": str,     # 截断到 ≤ 1000 字符
        "round_number": int,           # 第几轮（从 1 开始）
        "metric_only": True,           # 标记当前不真触发提炼，避免订阅方重复工作
    }

异常隔离：内部 try/except；任何异常 swallow + log。
"""

from __future__ import annotations

import logging
from typing import Any

from ..buses import AgentEvent, get_event_bus
from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


_USER_MAX = 500
_AI_MAX = 1000


class MemoryQueueMiddleware(AgentMiddleware):
    name = "memory_queue"

    async def after_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            messages = state.get("messages") or []
            if not isinstance(messages, list):
                return None

            user_msg, ai_msg = self._extract_last_round(messages)
            if user_msg is None or ai_msg is None:
                return None

            user_text = self._extract_text(user_msg)
            ai_text = self._extract_text(ai_msg)
            if not user_text or not ai_text:
                # AI 没文本（纯工具调用中间态）→ 等下一轮真有响应再 emit
                return None

            # 多轮：state["memory_queue_emit_count"] 累加（每个 chat run 内）；
            # 不防重——每轮都该 emit（v1 trigger_refine_for_turn 也是每轮调，
            # 由 cooldown 决策频率）。
            count = int(state.get("memory_queue_emit_count") or 0)

            bus = get_event_bus()
            if bus is None:
                # bus 未装配 → 静默跳过；不标记 count（下次有 bus 时仍 emit）
                log.debug(
                    "MemoryQueueMiddleware: event_bus not configured, skip emit "
                    "(run=%s session=%s)",
                    ctx.run_id, ctx.session_id,
                )
                return None

            event = AgentEvent(
                type="memory_refine_requested",
                trace_id=ctx.trace_id or "",
                session_id=ctx.session_id or "",
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
                payload={
                    "user_message": _trunc(user_text, _USER_MAX),
                    "assistant_response": _trunc(ai_text, _AI_MAX),
                    "round_number": count + 1,
                    "metric_only": True,
                },
            )
            await bus.publish(event)
            log.debug(
                "MemoryQueue emit: session=%s agent=%s round=%d trace=%s",
                ctx.session_id, ctx.agent_id, count + 1, ctx.trace_id,
            )
            return {"memory_queue_emit_count": count + 1}
        except Exception:
            log.exception(
                "MemoryQueueMiddleware swallowed exception (run=%s)",
                ctx.run_id,
            )
            return None

    # ── 内部 ────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_last_round(
        messages: list[Any],
    ) -> tuple[dict | None, dict | None]:
        """从 messages 末尾找最后一对 user → assistant。

        策略：从尾向前找第一个 assistant；再向前找最近的 user。两者之间
        允许有 system / tool 消息（不计入）。
        """
        last_assistant_idx: int | None = None
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if not isinstance(m, dict):
                continue
            if m.get("role") == "assistant":
                last_assistant_idx = i
                break
        if last_assistant_idx is None:
            return None, None

        # 向前找最近的 user
        for j in range(last_assistant_idx - 1, -1, -1):
            m = messages[j]
            if not isinstance(m, dict):
                continue
            if m.get("role") == "user":
                return m, messages[last_assistant_idx]
        return None, None

    @staticmethod
    def _extract_text(msg: dict) -> str:
        """从 OpenAI std message 提取文本（同 mw 14 TitleGen 实现）。"""
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        t = block.get("text")
                        if isinstance(t, str):
                            parts.append(t)
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts).strip()
        return ""


def _trunc(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "..."


__all__ = ["MemoryQueueMiddleware"]
