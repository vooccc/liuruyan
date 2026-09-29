"""跑一次 ACP prompt → 调 Agent.chat → yield 柳如烟原始事件。

Phase 3 升级：
- 接 Agent.chat（含 system prompt 注入 / 工具循环 / agent identity）
- 加载 session.conversation_history 作为上下文
- 持久化 user + 最终 assistant text 到 session（保证下一轮有历史）
- 设置 _current_session_id_var 上下文

不做（留给后续 Phase）：
- recall_context（跨会话记忆，Phase 6+）
- mask / emotion 更新（Phase 6+）
- 结构化 transcript 持久化 (append_assistant_structured)，工具调用细节会丢
  → Phase 3.5 接入。Phase 3 用最简单的 append_message 保留 user + final text
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional

from backend.agent import get_agent
from backend.llm_client import _current_session_id_var
from backend.session import get_session_manager

from .reverse_calls import reset_acp_context, set_acp_context

logger = logging.getLogger(__name__)


async def run_prompt(
    *,
    user_text: str,
    liuruyan_session_id: str,
    agent_id: str = "default",
    acp_conn: Any = None,
    acp_session_id: Optional[str] = None,
    client_capabilities: Any = None,
) -> AsyncIterator[dict]:
    """对一段用户文本调 Agent.chat，流式返回原始事件。

    Args:
        user_text: 用户输入的纯文本（已从 ACP content blocks 抽出）
        liuruyan_session_id: 柳如烟 SessionManager 创建的 session_id
        agent_id: 角色 id；ACP server 当前固定 default，Phase 6+ 加角色切换支持
        acp_conn: ACP client 反向通道（Phase 4：用于 fs/permission 反向调用）。
            None 时反向 wrapper 走原 handler（柳如烟自己读写文件）。
        acp_session_id: ACP 协议层 session id（带给反向调用）
        client_capabilities: client 在 initialize 时声明的能力（决定哪些反向调用真启用）

    Yields:
        原始柳如烟事件，格式见 event_translator.translate_event 文档。
    """
    session_mgr = get_session_manager()
    session = await session_mgr.get_session(liuruyan_session_id)
    if session is None:
        # session 找不到 = bridge 持有的 lry_sid 跟内存里的 SessionManager 不同步
        # 发生场景：new_session 创建后 session 因某些原因被淘汰
        logger.error("run_prompt: session %s not found", liuruyan_session_id)
        yield {
            "type": "error",
            "error": f"session {liuruyan_session_id} 未找到（需要重新建会话）",
        }
        return

    agent = get_agent(agent_id)
    history_snapshot = list(session.conversation_history)

    # 持久化 user message（在 chat 之前，避免下次 chat 看不到）
    await session_mgr.append_message(liuruyan_session_id, "user", user_text)

    # 设置 session_id 到 ContextVar（工具调用时取这个）
    sid_token = _current_session_id_var.set(liuruyan_session_id)
    # Phase 4：设置 ACP context（让 reverse_calls.py 的 wrapper 知道有 conn 可用）
    acp_tokens = None
    if acp_conn is not None and acp_session_id:
        acp_tokens = set_acp_context(acp_conn, acp_session_id, client_capabilities)
    try:
        logger.info(
            "run_prompt: agent=%s, lry_session=%s, history_len=%d, user_text_len=%d, acp_reverse=%s",
            agent_id,
            liuruyan_session_id,
            len(history_snapshot),
            len(user_text),
            bool(acp_tokens),
        )

        full_response = ""
        async for event in agent.chat(
            user_message=user_text,
            stream=True,
            session_id=liuruyan_session_id,
            conversation_history=history_snapshot,
            defer_refine=True,  # ACP 模式下不跑技能成长 / 记忆提炼（Phase 6+ 再看）
            user_message_already_in_history=False,
        ):
            etype = event.get("type")
            if etype == "content":
                full_response += event.get("text", "")
            yield event

        # 持久化最终 assistant 回复（Phase 3 最简实现：只存纯文本）
        # 工具调用细节进 transcript 是 Phase 3.5 的事；此处至少让"下一轮看到上一轮的回答"
        if full_response:
            try:
                await session_mgr.append_message(
                    liuruyan_session_id,
                    "assistant",
                    full_response,
                )
            except Exception:
                logger.exception("run_prompt: failed to persist assistant message")
    finally:
        _current_session_id_var.reset(sid_token)
        if acp_tokens is not None:
            try:
                reset_acp_context(acp_tokens)
            except Exception:
                logger.exception("reset_acp_context failed")
