"""v2 · B-5 · ClarificationMiddleware

截获 `ask_clarification` 工具调用，标记 `should_end=True` 中断主循环把
控制权交回用户。前端检测到 `clarification` payload 展示"待用户回复"。

参考：_docs/architecture_v2/03_middlewares.md § 17

钩子：after_model

链中位置：**必须最后**——它通过 should_end 中断 loop，前面 middleware 还
没执行的会被跳过；所以日志/记忆/统计类的要在它之前。

行为：
- 找最后一条 assistant.tool_calls 里有 ask_clarification →
  返回 `{"should_end": True, "clarification": {tool_call_id, args}}`
- 没有 → return None

调用层契约：调度器看到 state["should_end"]==True 应停止后续 hooks /
tool 执行 / 下一轮 LLM。

异常隔离：内部 try/except；任何异常 swallow + log。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


class ClarificationMiddleware(AgentMiddleware):
    name = "clarification"

    CLARIFICATION_TOOL_NAME = "ask_clarification"

    async def after_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            messages = state.get("messages")
            if not isinstance(messages, list) or not messages:
                return None

            # 找最后一条 assistant
            last_ai: dict | None = None
            for m in reversed(messages):
                if isinstance(m, dict) and m.get("role") == "assistant":
                    last_ai = m
                    break
            if last_ai is None:
                return None

            tcs = last_ai.get("tool_calls")
            if not isinstance(tcs, list) or not tcs:
                return None

            # 找 ask_clarification 调用（取第一个）
            for tc in tcs:
                clar = self._match_clarification(tc)
                if clar is not None:
                    return {
                        "should_end": True,
                        "clarification": clar,
                    }
            return None
        except Exception:
            log.exception(
                "ClarificationMiddleware swallowed exception (run=%s)",
                ctx.run_id,
            )
            return None

    def _match_clarification(self, tc: Any) -> dict[str, Any] | None:
        """tc 是 ask_clarification → 返回 {tool_call_id, args}；否则 None。"""
        if not isinstance(tc, dict):
            return None
        fn = tc.get("function")
        if not isinstance(fn, dict):
            return None
        if fn.get("name") != self.CLARIFICATION_TOOL_NAME:
            return None
        args = self._coerce_args(fn.get("arguments"))
        return {"tool_call_id": tc.get("id"), "args": args}

    @staticmethod
    def _coerce_args(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {"_raw": raw}
            except (json.JSONDecodeError, ValueError):
                return {"_raw": raw}
        return {}


__all__ = ["ClarificationMiddleware"]
