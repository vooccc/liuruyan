"""v2 · B-11 · TitleGenMiddleware (mw 14)

钩子：``after_model``

职责：在第一轮 user→assistant 对话完成时，emit ``title_gen_requested``
事件到 EventBus，让 backend 订阅方异步用便宜模型生成 ≤8 字的会话标题
（替换 v1 当前的"取首条 user 消息前 30 字符傻截断"）。

参考：_docs/architecture_v2/03_middlewares.md § 14

设计要点：

1. **fire-and-forget**——mw 钩子内**不**调 LLM、**不**等结果；emit 完即返。
   实际生成 + 写回 session.title + 推 SSE 由 backend/title_gen_subscriber.py
   负责（在 main.py lifespan 注册）。harness 不能 import backend.* —— 通过
   EventBus 解耦是唯一正确做法。

2. **首轮检测**——只在"系统消息之外只有 1 条 user + 1 条 assistant"时触发。
   多轮对话之后再 chat，state["messages"] 已经多 user/assistant，自动跳过。

3. **state 防重发**——``state["title_gen_emitted"]`` 标记，同一 run 内即便
   after_model 钩子被调多次（理论上一次 chat 只调一次，但 chain shim 容错）
   也只 emit 一次。

4. **首轮但 assistant 没文本内容（纯工具调用）跳过**——不能拿"我在调工具"
   这种过程消息生成标题。等下一轮 assistant 真有 content 时才触发。但
   实际首轮基本都有 content（即便有 tool_call 也会先有 reasoning/preface），
   按 content 长度 > 0 判断。

5. **agent_id == 协作场景跳过**——主聊天 agent 的标题才有意义，协作房间
   走另一套。当前 mw 看 ``state["thread_data"]["agent_id"]``，IM 入口
   future-proof，不写死。

6. **异常 swallow**——03_middlewares § 一契约。bus 未装配（非常罕见，
   测试时可能）→ 静默跳过。

事件 schema（payload）：
    {
        "user_message": "用户首条消息",          # 截断到 ≤ 200 字符
        "ai_response_excerpt": "AI 首条响应",     # 截断到 ≤ 300 字符
    }

订阅方收到后调便宜模型生成标题，调 SessionManager.update_title 写回。
"""

from __future__ import annotations

import logging
from typing import Any

from ..buses import AgentEvent, get_event_bus
from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


# 事件载荷字段长度上限（防止前端 / 日志膨胀）
_USER_MESSAGE_MAX = 200
_AI_EXCERPT_MAX = 300


class TitleGenMiddleware(AgentMiddleware):
    name = "title_gen"

    async def after_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            # 已经 emit 过：跳过
            if state.get("title_gen_emitted"):
                return None

            messages = state.get("messages") or []
            if not isinstance(messages, list):
                return None

            user_msg, ai_msg = self._extract_first_round(messages)
            if user_msg is None or ai_msg is None:
                return None

            user_text = self._extract_text(user_msg)
            ai_text = self._extract_text(ai_msg)
            if not user_text:
                return None
            # AI 没有文本内容（首轮就纯工具调用）→ 等下一轮再说
            if not ai_text:
                return None

            bus = get_event_bus()
            if bus is None:
                # bus 未装配（测试 / 早期启动）→ 标记 emitted 防下次 retry，但
                # 不真发；记 debug 日志即可（这条不算异常）
                log.debug(
                    "TitleGenMiddleware: event_bus not configured, skip emit "
                    "(run=%s session=%s)",
                    ctx.run_id, ctx.session_id,
                )
                return {"title_gen_emitted": True}

            event = AgentEvent(
                type="title_gen_requested",
                trace_id=ctx.trace_id or "",
                session_id=ctx.session_id or "",
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
                payload={
                    "user_message": _trunc(user_text, _USER_MESSAGE_MAX),
                    "ai_response_excerpt": _trunc(ai_text, _AI_EXCERPT_MAX),
                },
            )
            await bus.publish(event)
            log.info(
                "TitleGen requested: session=%s agent=%s trace=%s",
                ctx.session_id, ctx.agent_id, ctx.trace_id,
            )
            return {"title_gen_emitted": True}
        except Exception:
            log.exception(
                "TitleGenMiddleware swallowed exception (run=%s)",
                ctx.run_id,
            )
            return None

    # ── 内部 ────────────────────────────────────────────────────────────

    @classmethod
    def _extract_first_round(
        cls,
        messages: list[Any],
    ) -> tuple[dict | None, dict | None]:
        """从 messages 中提取用于生成标题的 user / assistant 对；非目标轮次返 (None, None)。

        支持场景：
        1. 标准首轮：1 user + 1 assistant
        2. 首轮工具调用：1 user + 若干 assistant(tool_calls)/tool + 最终 assistant(text)
        3. 首轮纯寒暄问候（如“你好”/“hi”/“在吗”）时的次轮（第 2 轮）主题补偿
        """
        non_system: list[dict] = []
        for m in messages:
            if not isinstance(m, dict):
                return None, None
            role = m.get("role")
            if role == "system" or role == "tool":
                continue
            non_system.append(m)

        users = [m for m in non_system if m.get("role") == "user"]
        if not users:
            return None, None

        # 1. 严格第 1 轮（支持首轮带 1 次或多次 tool calls 的过程）
        if len(users) == 1:
            target_user = users[0]
            assistants_with_text = [
                m for m in non_system
                if m.get("role") == "assistant" and cls._extract_text(m)
            ]
            if not assistants_with_text:
                return None, None
            return target_user, assistants_with_text[-1]

        # 2. 次轮（第 2 轮）补偿：仅当首轮为极短寒暄问候时触发
        if len(users) == 2:
            u1_text = cls._extract_text(users[0]).lower().strip()
            _GREETINGS = {"你好", "您好", "hi", "hello", "在吗", "在么", "哈喽", "嗨", "早", "早安", "晚安"}
            if u1_text in _GREETINGS or len(u1_text) <= 3:
                target_user = users[1]
                assistants_with_text = [
                    m for m in non_system
                    if m.get("role") == "assistant" and cls._extract_text(m)
                ]
                if not assistants_with_text:
                    return None, None
                return target_user, assistants_with_text[-1]

        return None, None

    @staticmethod
    def _extract_text(msg: dict) -> str:
        """从 OpenAI std message 提取文本——content 可能是 str 或 list[block]。"""
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    # OpenAI: {"type": "text", "text": "..."}
                    # Anthropic ContentBlock: 同 schema
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


__all__ = ["TitleGenMiddleware"]
