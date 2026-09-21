"""v2 · B-15 / C5-b · SummarizationGateMiddleware (mw 09)

钩子：``before_model``

职责：每次 LLM 调用前检测 messages 累计 token 数，超过阈值时 emit 两条事件：
1. ``summarize_threshold_reached`` (C5-a)：通知 trace journal 上下文逼近限制
2. ``summarize_plan_ready`` (C5-b)：调 ``plan_summarization`` 算保留 / 压缩计划，
   让订阅方（C5-c 真 summarizer）看到"这一轮该压什么"

参考：_docs/architecture_v2/03_middlewares.md § 09

**plan-only（C5-b 阶段）**

- C5-a：metric-only emit + 不动 messages（已落）
- **C5-b（本切片）**：超阈值时**额外**调 plan_summarization 出 plan + emit
  ``summarize_plan_ready`` 事件携带保留 / 压缩 indices；**仍不替换 messages**。
- C5-c（待做）：mw 09 接 summarizer DI，按 plan 调 cheaper 模型跑摘要 + 真
  替换 messages（保留 keep_indices + compress_indices 压成单条 system message）

当前 v1 没有"主对话历史 summarization"功能；mw 09 的 plan / 真接管都是新功能。
分阶段是为了：每阶段单测可锁；plan 算法独立可演进；summarizer 模型调用解耦。

钩子触发条件：
- ``state["messages"]`` token 估算 > threshold（默认 60000）
- env ``LIURUYAN_SUMMARIZE_THRESHOLD_TOKENS`` 可调
- ``state["summarization_emit_count"]`` 防同 run 重复 emit

事件 schema：

```
type: summarize_threshold_reached
payload: {estimated_tokens, threshold, message_count, metric_only=True}

type: summarize_plan_ready
payload: {
    keep_indices: list[int],
    compress_indices: list[int],
    total_messages: int,
    reasoning: str,
    plan_only: True,    # C5-b 标记；C5-c 真接管时切到 applied=True
}
```

token 估算：保守用 ``sum(len(content_str)) / 3.5``（中文字符约 1.5 token /字，
英文约 0.25 token/字符；3.5 是混合估算系数）。后续真接管时换 tiktoken。

异常隔离：内部 try/except；任何异常 swallow + log。plan_summarization 失败
不阻断 threshold emit。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..buses import AgentEvent, get_event_bus
from .base import AgentMiddleware, RunContext, ThreadState
from .summarize_planner import apply_summary, plan_summarization

log = logging.getLogger(__name__)


_DEFAULT_THRESHOLD = 60000
_DEFAULT_RECENT_USER_ASSISTANT = 5
_DEFAULT_RECENT_TOOL_BLOCKS = 3
_ACTIVATE_FLAG_ENV = "LIURUYAN_SUMMARIZE_ACTIVATE"


def _read_threshold(context_length: Any = None) -> int:
    raw = (os.getenv("LIURUYAN_SUMMARIZE_THRESHOLD_TOKENS") or "").strip()
    if not raw:
        if isinstance(context_length, int) and context_length > 0:
            return int(context_length * 0.70)
        return _DEFAULT_THRESHOLD
    try:
        v = int(raw)
        if v <= 0:
            return _DEFAULT_THRESHOLD
        return v
    except (ValueError, TypeError):
        return _DEFAULT_THRESHOLD


def _read_recent_user_assistant() -> int:
    raw = (os.getenv("LIURUYAN_SUMMARIZE_RECENT_DIALOG") or "").strip()
    if not raw:
        return _DEFAULT_RECENT_USER_ASSISTANT
    try:
        v = int(raw)
        return v if v >= 0 else _DEFAULT_RECENT_USER_ASSISTANT
    except (ValueError, TypeError):
        return _DEFAULT_RECENT_USER_ASSISTANT


def _read_recent_tool_blocks() -> int:
    raw = (os.getenv("LIURUYAN_SUMMARIZE_RECENT_TOOL_BLOCKS") or "").strip()
    if not raw:
        return _DEFAULT_RECENT_TOOL_BLOCKS
    try:
        v = int(raw)
        return v if v >= 0 else _DEFAULT_RECENT_TOOL_BLOCKS
    except (ValueError, TypeError):
        return _DEFAULT_RECENT_TOOL_BLOCKS


def _is_activate_enabled() -> bool:
    """``LIURUYAN_SUMMARIZE_ACTIVATE`` env 判 mw 09 是否真接管摘要。"""
    raw = (os.getenv(_ACTIVATE_FLAG_ENV) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _serialize_messages_for_summary(
    messages: list[Any], compress_indices: list[int],
) -> str:
    """把 compress_indices 对应 messages 拼成单段文本喂 summarizer。

    格式（每条消息一段）：
    ```
    [system] system prompt content
    [user] user message text
    [assistant] assistant text content
    [assistant tool_calls] tool_a({...}) | tool_b({...})
    [tool tool_a] result text
    ```

    跳过非 dict / 非已知 role。
    """
    parts: list[str] = []
    for i in compress_indices:
        if i < 0 or i >= len(messages):
            continue
        m = messages[i]
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        content = m.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            chunks = []
            for block in content:
                if isinstance(block, dict):
                    t = block.get("text") or block.get("content") or ""
                    if isinstance(t, str):
                        chunks.append(t)
                elif isinstance(block, str):
                    chunks.append(block)
            text = "\n".join(chunks)
        # tool_calls 拼成可读形式
        tool_calls = m.get("tool_calls") or []
        if tool_calls and isinstance(tool_calls, list):
            tc_strs = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = fn.get("name") or "?"
                args = fn.get("arguments") or ""
                tc_strs.append(f"{name}({args})")
            if tc_strs:
                tag = f"[{role} tool_calls]"
                parts.append(f"{tag} {' | '.join(tc_strs)}")
                continue
        if role == "tool":
            tc_id = m.get("tool_call_id") or "?"
            parts.append(f"[tool {tc_id}] {text}")
            continue
        if role and (text or content is not None):
            parts.append(f"[{role}] {text}")

    return "\n\n".join(parts)


def _estimate_message_tokens(messages: list[Any]) -> int:
    """保守估算 messages 总 token 数。

    char_count / 3.5 — 中文 1.5 token/字、英文 0.25 token/字符的混合系数；
    后续真接管时换 tiktoken。
    """
    total_chars = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    txt = block.get("text") or block.get("content") or ""
                    if isinstance(txt, str):
                        total_chars += len(txt)
                elif isinstance(block, str):
                    total_chars += len(block)
        # tool_calls 也算（assistant.tool_calls 含 arguments）
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                args_str = fn.get("arguments") or ""
                if isinstance(args_str, str):
                    total_chars += len(args_str)
    return int(total_chars / 3.5)


class SummarizationGateMiddleware(AgentMiddleware):
    name = "summarization_gate"

    async def before_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            messages = state.get("messages") or []
            if not isinstance(messages, list) or not messages:
                return None

            threshold = _read_threshold(state.get("context_length"))
            estimated = _estimate_message_tokens(messages)
            if estimated <= threshold:
                return None

            # 同 run 防重复 emit：第一次超阈值时记录 + emit；后续 round 不再 emit
            # （等真接管摘要后能真压缩 messages 再考虑每轮检查）
            if state.get("summarization_emit_count"):
                return None

            bus = get_event_bus()
            if bus is None:
                # bus 未装配 → 静默跳过；不标记 count（下次有 bus 时仍 emit）
                log.debug(
                    "SummarizationGateMiddleware: event_bus not configured, skip "
                    "(run=%s session=%s estimated=%d)",
                    ctx.run_id, ctx.session_id, estimated,
                )
                return None

            # 1. emit threshold（C5-a 行为保留）
            threshold_event = AgentEvent(
                type="summarize_threshold_reached",
                trace_id=ctx.trace_id or "",
                session_id=ctx.session_id or "",
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
                payload={
                    "estimated_tokens": estimated,
                    "threshold": threshold,
                    "message_count": len(messages),
                    "metric_only": True,
                },
            )
            await bus.publish(threshold_event)

            # 2. emit plan-ready（C5-b 新增）—— plan_summarization 失败不阻断
            plan = None
            try:
                plan = plan_summarization(
                    messages,
                    recent_user_assistant=_read_recent_user_assistant(),
                    recent_tool_blocks=_read_recent_tool_blocks(),
                )
            except Exception:
                log.exception(
                    "SummarizationGateMiddleware plan_summarization failed (run=%s)",
                    ctx.run_id,
                )

            # 3. C5-c-4: 真接管路径——flag ON + summarizer 注入 + plan 算出
            #    时调 summarizer + apply_summary 替换 messages
            new_messages: list | None = None
            applied = False
            summary_chars = 0
            if (
                plan is not None
                and plan.compress_indices
                and _is_activate_enabled()
                and state.get("summarizer") is not None
            ):
                summarizer = state["summarizer"]
                try:
                    text_to_summarize = _serialize_messages_for_summary(
                        messages, plan.compress_indices,
                    )
                    summary_text = await summarizer(text_to_summarize)
                    if not isinstance(summary_text, str):
                        summary_text = ""
                    summary_text = summary_text.strip()
                    summary_chars = len(summary_text)
                    new_messages = apply_summary(messages, plan, summary_text)
                    applied = True
                    log.info(
                        "SummarizationGate APPLIED: session=%s agent=%s "
                        "before=%d after=%d summary_chars=%d trace=%s",
                        ctx.session_id, ctx.agent_id,
                        len(messages), len(new_messages),
                        summary_chars, ctx.trace_id,
                    )
                except Exception:
                    log.exception(
                        "SummarizationGateMiddleware summarizer/apply failed "
                        "(run=%s); fallback to plan-only",
                        ctx.run_id,
                    )
                    new_messages = None
                    applied = False

            # 4. emit plan-ready（携带 applied 标记 + summary_chars）
            if plan is not None:
                try:
                    plan_event = AgentEvent(
                        type="summarize_plan_ready",
                        trace_id=ctx.trace_id or "",
                        session_id=ctx.session_id or "",
                        agent_id=ctx.agent_id,
                        user_id=ctx.user_id,
                        payload={
                            "keep_indices": list(plan.keep_indices),
                            "compress_indices": list(plan.compress_indices),
                            "total_messages": len(messages),
                            "kept_count": plan.kept_count,
                            "compress_count": plan.compress_count,
                            "reasoning": plan.reasoning,
                            "plan_only": not applied,
                            "applied": applied,
                            "summary_chars": summary_chars,
                            "messages_after_apply": (
                                len(new_messages) if new_messages is not None else None
                            ),
                        },
                    )
                    await bus.publish(plan_event)
                    log.info(
                        "SummarizationGate plan: session=%s agent=%s keep=%d compress=%d "
                        "applied=%s trace=%s",
                        ctx.session_id, ctx.agent_id,
                        plan.kept_count, plan.compress_count, applied, ctx.trace_id,
                    )
                except Exception:
                    log.exception(
                        "SummarizationGateMiddleware plan_event publish failed (run=%s)",
                        ctx.run_id,
                    )

            log.info(
                "SummarizationGate emit: session=%s agent=%s estimated=%d threshold=%d trace=%s",
                ctx.session_id, ctx.agent_id, estimated, threshold, ctx.trace_id,
            )
            ret: dict[str, Any] = {"summarization_emit_count": 1}
            if new_messages is not None:
                ret["messages"] = new_messages
            return ret
        except Exception:
            log.exception(
                "SummarizationGateMiddleware swallowed exception (run=%s)",
                ctx.run_id,
            )
            return None


__all__ = ["SummarizationGateMiddleware"]
