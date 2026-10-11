"""v2 · B-12 · UserMemoryInjectMiddleware (mw 03)

C3-a 首刀——chain 接管"该角色对该用户的记忆"注入到 system prompt。
v1 agent.build_messages 砍掉对应注入；chain ON 时 mw 03 唯一负责。

参考：
- docs/architecture_v2/03_middlewares.md § 03（已对齐"per-agent 单层"产品宪章）
- MEMORY feedback_agent_independent_user_view.md（每个 agent 独立 user 画像）

钩子：``before_model``

**职责边界（重要）**：

mw 03 **不**直接读 USER.md / MEMORY.md / 日记 / 反思 / 情感。harness 不能
import backend.memory_manager（boundary CI 红线）。所有读盘 + 温度排序 +
预算控制 + 截断逻辑由 v1 ``memory_manager.get_context_for_chat()`` 负责，
LeadAgent 调它的输出后**作为字符串塞 state["memory_text"]**；mw 03 仅
负责"把字符串包成 ``<memory>`` 标签注入 messages"。

为什么这样切：
1. 不重写 v1 已稳的温度排序 / 25KB 硬限 / 25 字截断算法（一句改错满
   13 角色）
2. mw 03 只做 prompt 拼装策略，纯字符串处理，零 backend 耦合
3. 未来 v2 真做读盘时（彻底搬迁 memory_manager），mw 03 不变

**输入**（state 字段）：
- ``state["memory_text"]``: str — LeadAgent 调 v1 ``Agent.get_memory_context()``
  的输出。空串 / 缺失 → mw 跳过不注入。

**输出**：
- ``state["memory_injected"] = True`` 防同 run 重复注入
- 注入到 messages 末尾的 system 区（最后一条 system 之后、第一条 user 之前）

**注入格式**：

跟 v1 ``build_messages:301-306`` 等价 — system message content 是
``"以下是你的记忆上下文：\n\n{memory_text}"``。这样 chain ON 跟 OFF 时
LLM 看到的 prompt 文本完全一致（除了顺序，保留 v1 行为）。

**异常隔离**：内部 try/except；任何异常 swallow + log。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


_MEMORY_PREFIX = "以下是你的记忆上下文：\n\n"


class UserMemoryInjectMiddleware(AgentMiddleware):
    name = "user_memory_inject"

    async def before_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            if state.get("memory_injected"):
                return None

            memory_text = state.get("memory_text") or ""
            if not isinstance(memory_text, str):
                return None
            memory_text = memory_text.strip()
            if not memory_text:
                # 没记忆：标记 injected 防同 run 反复尝试 — 但不写 messages
                return {"memory_injected": True}

            content = _MEMORY_PREFIX + memory_text
            new_messages = self._inject_before_first_user(
                state.get("messages") or [], content
            )
            log.debug(
                "UserMemoryInject: agent=%s session=%s memory_chars=%d",
                ctx.agent_id, ctx.session_id, len(memory_text),
            )
            return {"messages": new_messages, "memory_injected": True}
        except Exception:
            log.exception(
                "UserMemoryInjectMiddleware swallowed exception (run=%s)",
                ctx.run_id,
            )
            return None

    # ── inject ──────────────────────────────────────────────────────────

    @staticmethod
    def _inject_before_first_user(
        messages: list[Any], content: str
    ) -> list[Any]:
        """插入 system message：在第一条 user 之前。无 user 则末尾追加。

        跟 SkillsInjectMiddleware._inject_before_first_user 同模式。
        """
        new_msgs = list(messages)
        insert_idx = len(new_msgs)
        for i, m in enumerate(new_msgs):
            if isinstance(m, dict) and m.get("role") == "user":
                insert_idx = i
                break
        new_msgs.insert(insert_idx, {"role": "system", "content": content})
        return new_msgs


__all__ = ["UserMemoryInjectMiddleware"]
