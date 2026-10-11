"""v2 · B-2 · DanglingToolCallMiddleware

用户中断后，AIMessage 的 tool_calls 没有对应 ToolMessage 时，自动注入
placeholder ToolMessage —— 否则下一轮 LLM 会因 messages 不配对报 400。

参考：docs/architecture_v2/03_middlewares.md § 07
柳如烟历史：直接终结 MEMORY `gpt5_invalid_tool_pairing_fix` 那条 bug 的下次重发。

钩子：before_model

算法（OpenAI std）：
1. 遍历 messages，每遇到 assistant message + tool_calls：
2. 找到第一条非 tool message 的位置 `next_non_tool`（即 assistant 后的 tool 块结束）
3. 在 [i+1, next_non_tool) 区间内统计已响应的 tool_call_id
4. 对每个 tool_call.id 不在已响应集合的，紧贴 tool 块之后补
   `{"role": "tool", "tool_call_id": <id>, "content": "[Tool execution was interrupted by user]"}`

placeholder 必须紧贴 assistant 的 tool 块（不能跨 user/system），否则破坏
OpenAI 协议——assistant.tool_calls 后必须立即跟齐所有对应 ToolMessage。

异常隔离：内部 try/except，return None 不向上抛。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


class DanglingToolCallMiddleware(AgentMiddleware):
    name = "dangling_tool_call"

    PLACEHOLDER_CONTENT = "[Tool execution was interrupted by user]"

    async def before_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            messages = state.get("messages")
            if not isinstance(messages, list) or not messages:
                return None

            new_messages, filled = self._patch(messages)
            if filled == 0:
                return None
            return {"messages": new_messages, "dangling_tool_call_filled": filled}
        except Exception:
            log.exception(
                "DanglingToolCallMiddleware swallowed exception (run=%s)",
                ctx.run_id,
            )
            return None

    # ── core ─────────────────────────────────────────────────────────────

    def _patch(self, messages: list[Any]) -> tuple[list[Any], int]:
        """返回 (new_messages, filled_count)。filled=0 时 new_messages 内容等价。"""
        new_messages: list[Any] = []
        filled = 0
        n = len(messages)
        i = 0
        while i < n:
            msg = messages[i]
            tcs = self._tool_calls_of(msg)
            if not tcs:
                new_messages.append(msg)
                i += 1
                continue

            # 找 assistant 之后第一条非 tool message 的位置（即 tool 块的尽头）
            next_non_tool = i + 1
            while next_non_tool < n:
                bm = messages[next_non_tool]
                if isinstance(bm, dict) and bm.get("role") == "tool":
                    next_non_tool += 1
                else:
                    break

            # tool 块内已响应的 tool_call_id
            responded: set[str] = set()
            for j in range(i + 1, next_non_tool):
                bm = messages[j]
                if isinstance(bm, dict) and bm.get("role") == "tool":
                    tc_id = bm.get("tool_call_id")
                    if isinstance(tc_id, str) and tc_id:
                        responded.add(tc_id)

            # 计算缺失 id（保持 tool_calls 中的顺序）
            missing: list[str] = []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                if (
                    isinstance(tc_id, str)
                    and tc_id
                    and tc_id not in responded
                    and tc_id not in missing
                ):
                    missing.append(tc_id)

            # 装配：assistant + 真实 tool 块 + placeholders + 后续...
            new_messages.append(msg)
            for j in range(i + 1, next_non_tool):
                new_messages.append(messages[j])
            for mid in missing:
                new_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": mid,
                        "content": self.PLACEHOLDER_CONTENT,
                    }
                )
            filled += len(missing)
            i = next_non_tool

        return new_messages, filled

    @staticmethod
    def _tool_calls_of(msg: Any) -> list[Any]:
        """从一条 assistant message 拿 tool_calls 列表（非 assistant / 无 tool_calls 返 []）。"""
        if not isinstance(msg, dict):
            return []
        if msg.get("role") != "assistant":
            return []
        tcs = msg.get("tool_calls")
        if not isinstance(tcs, list):
            return []
        return tcs


__all__ = ["DanglingToolCallMiddleware"]
