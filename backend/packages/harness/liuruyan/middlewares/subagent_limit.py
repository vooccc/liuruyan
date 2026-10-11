"""v2 · B-3 · SubagentLimitMiddleware

截断超额 subagent 调用，强制 ≤ MAX_CONCURRENT_SUBAGENTS（默认 5，跟
02_runtime_core.md § 2.1 executor 池 8 = 5 + buffer 对齐）。

参考：docs/architecture_v2/03_middlewares.md § 16

钩子：after_model

逻辑：
- 扫最后一条 assistant.tool_calls
- 统计 subagent 类调用（name ∈ SUBAGENT_TOOL_NAMES）
- 超额则保留前 MAX_CONCURRENT 个，其余 drop，并在 content 末尾注入说明

兼容：SUBAGENT_TOOL_NAMES = {"task", "spawn_worker"}（v2 用 task，v1 用
spawn_worker；过渡期同时识别）。

异常隔离：内部 try/except，return None 不抛。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


class SubagentLimitMiddleware(AgentMiddleware):
    name = "subagent_limit"

    MAX_CONCURRENT_SUBAGENTS = 5
    SUBAGENT_TOOL_NAMES = frozenset({"task", "spawn_subagent"})

    async def after_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            messages = state.get("messages")
            if not isinstance(messages, list) or not messages:
                return None

            last_ai_idx = self._find_last_assistant(messages)
            if last_ai_idx < 0:
                return None

            ai_msg = messages[last_ai_idx]
            tcs = ai_msg.get("tool_calls")
            if not isinstance(tcs, list) or not tcs:
                return None

            # 收集 subagent 调用在 tool_calls 中的位置
            subagent_indices = [
                i
                for i, tc in enumerate(tcs)
                if self._is_subagent_call(tc)
            ]
            requested = len(subagent_indices)
            if requested <= self.MAX_CONCURRENT_SUBAGENTS:
                return None

            # 保留前 N 个 subagent，其余 drop（非 subagent 调用全部保留）
            keep_set = set(subagent_indices[: self.MAX_CONCURRENT_SUBAGENTS])
            drop_set = set(subagent_indices[self.MAX_CONCURRENT_SUBAGENTS :])
            new_tcs = [tc for i, tc in enumerate(tcs) if i not in drop_set]

            note = (
                f"\n\n(Note: {requested} task calls requested, "
                f"but max concurrent is {self.MAX_CONCURRENT_SUBAGENTS}. "
                f"Keeping the first {self.MAX_CONCURRENT_SUBAGENTS}.)"
            )
            new_ai = self._with_tool_calls_and_note(ai_msg, new_tcs, note)

            new_messages = list(messages)
            new_messages[last_ai_idx] = new_ai
            return {
                "messages": new_messages,
                "subagent_truncated": requested - self.MAX_CONCURRENT_SUBAGENTS,
            }
        except Exception:
            log.exception(
                "SubagentLimitMiddleware swallowed exception (run=%s)",
                ctx.run_id,
            )
            return None

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _find_last_assistant(messages: list[Any]) -> int:
        for j in range(len(messages) - 1, -1, -1):
            m = messages[j]
            if isinstance(m, dict) and m.get("role") == "assistant":
                return j
        return -1

    def _is_subagent_call(self, tc: Any) -> bool:
        if not isinstance(tc, dict):
            return False
        fn = tc.get("function")
        if not isinstance(fn, dict):
            return False
        name = fn.get("name")
        return isinstance(name, str) and name in self.SUBAGENT_TOOL_NAMES

    @staticmethod
    def _with_tool_calls_and_note(
        ai_msg: dict, new_tcs: list[Any], note: str
    ) -> dict:
        """复制 ai_msg，替换 tool_calls，把 note 拼到 content。"""
        new_ai = dict(ai_msg)
        new_ai["tool_calls"] = new_tcs
        existing = new_ai.get("content")
        if isinstance(existing, str):
            new_ai["content"] = existing + note
        elif existing is None:
            new_ai["content"] = note.lstrip()
        elif isinstance(existing, list):
            new_ai["content"] = existing + [{"type": "text", "text": note}]
        else:
            new_ai["content"] = [existing, {"type": "text", "text": note}]
        return new_ai


__all__ = ["SubagentLimitMiddleware"]
