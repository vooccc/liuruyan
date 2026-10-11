"""v2 · E-3a · backend dispatch 实现 — 把 IM 消息真路由到 v2 chain

设计：docs/architecture_v2/01_master_plan.md Phase E + handover_v2_phase_e_inbound_router_landed_2026_04_30.md § 二

DispatchFunc 协议（harness.channels.router）：
    async def dispatch(msg: InboundMessage, sk: SessionKey) -> AsyncIterator[OutboundMessage]

本模块的 ``make_dispatch(agent_resolver, session_manager_resolver)`` 返回符合
该协议的 dispatch 函数。

为什么是 factory：
- 测试时注入 mock resolvers
- 生产时注入 ``backend.agent.get_agent`` + ``backend.session.get_session_manager``
- harness 不能 import backend——所以 dispatch 实现必须在 backend 层

数据流：
1. 解析 sk.session_id → session（不存在自动 create，owner_user_id=sk.user_id）
2. session.load_conversation_history() 拿历史 messages（OpenAI std）
3. session_manager.append_message(sk.session_id, "user", msg.text) 落盘 user msg
4. agent.chat(msg.text, session_id=sk.session_id, conversation_history=history,
              user_message_already_in_history=True) 跑 v2 chain
5. 累积 content events 成 full_response
6. 流式 yield content 转 OutboundMessage(text=chunk, is_final=False)
7. 流末 yield is_final=True 终结 + session_manager.append_message 落盘 assistant msg

异常隔离：本模块只做"翻译 + 转流"，异常向上抛由 InboundRouter 接（router 已
做 swallow + log，不影响 consumer loop）。

事件类型转 OutboundMessage：
- ``content`` → text 流式（is_final=False）；累计成 full_response 落盘
- ``thinking`` → IM 不展示（思考过程对 IM 用户太杂）
- ``tool_call`` / ``tool_result`` → IM 不展示（避免噪声；前端会有专门 UI）
- ``system_notice`` → 关键 kind 转单独 OutboundMessage
- 其它未知类型 → 跳过（防御性）

跨平台流式语义：
- 钉钉 / 飞书：is_final=False 应该 update 单条卡片；is_final=True 落定
- web SSE：is_final=False 流 chunk；is_final=True 关流
- adapter 实现负责具体语义（OutboundMessage 携带 is_final 给 adapter 决策）
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Awaitable, Callable

# harness 协议层（核）
from liuruyan.buses import InboundMessage, OutboundMessage
from liuruyan.channels.router import DispatchFunc
from liuruyan.middlewares.base import RunContext
from liuruyan.multi_role import SessionKey
from liuruyan.tracing import new_trace_id

log = logging.getLogger(__name__)


# 注：resolver 类型故意 Any——避免 import backend.agent.Agent 等，让本模块
# 可以独立测试（mock）。生产用 backend.agent.get_agent / backend.session.get_session_manager
AgentResolver = Callable[[str], Any]
SessionManagerResolver = Callable[[], Any]


def make_dispatch(
    agent_resolver: AgentResolver,
    session_manager_resolver: SessionManagerResolver,
) -> DispatchFunc:
    """返回符合 InboundRouter.DispatchFunc 协议的 dispatch 函数。

    生产用法（gateway 启动时）：

    ```python
    from backend.agent import get_agent
    from backend.session import get_session_manager
    from backend.channels import make_dispatch

    dispatch = make_dispatch(get_agent, get_session_manager)
    router = InboundRouter(bus, store, dispatch=dispatch)
    await router.start()
    ```

    测试用法：

    ```python
    dispatch = make_dispatch(
        agent_resolver=lambda aid: fake_agent,
        session_manager_resolver=lambda: fake_mgr,
    )
    ```
    """

    async def dispatch(
        msg: InboundMessage, sk: SessionKey
    ) -> AsyncIterator[OutboundMessage]:
        agent = agent_resolver(sk.agent_id)
        session_mgr = session_manager_resolver()

        # per-agent model routing 阶段 3 Commit 3.4：dispatch 用新 resolve 路径
        # 老代码：_configure_runtime_model(agent) 改 LLMClient 单例字段，并发 IM
        # 消息会互踩。新路径：resolve 拿 cfg + 下面 agent.chat 外层 with use_call_config
        # 注入 ContextVar，per-task 隔离。
        # cfg=None 时 with no-op，LLMClient fallback 到 self 字段（老行为）——
        # 跟之前 _configure_runtime_model 抛异常时的降级语义等价。
        from backend.llm_call_config import (
            resolve_call_model_config,
            ModelResolutionError,
        )
        from backend.llm_client import use_call_config
        dispatch_call_config = None
        try:
            dispatch_call_config = resolve_call_model_config(
                agent_id=sk.agent_id,
                override=None,
            )
        except ModelResolutionError as exc:
            log.warning(
                "dispatch: resolve_call_model_config failed agent=%s err=%s "
                "(continuing with LLMClient fallback)",
                sk.agent_id, exc,
            )

        # 1. 解析或建 session（SessionManager 没 get_or_create——分两步：
        # get_session 取（含磁盘 recover）；None 时 create_session 建）
        session = await session_mgr.get_session(sk.session_id)
        if session is None:
            log.info(
                "dispatch: session not found, creating session_id=%s agent=%s user=%s",
                sk.session_id, sk.agent_id, sk.user_id,
            )
            session = await session_mgr.create_session(
                session_id=sk.session_id,
                agent_id=sk.agent_id,
                owner_user_id=sk.user_id,
            )

        # 2. 落盘 user msg + 拿含新 user 的 history snapshot
        # 顺序很重要：先 append 让 session.conversation_history 更新（session.append_message
        # 内部 line 338 同步 push 进内存），再 .copy() 拿 snapshot。下方传
        # user_message_already_in_history=True 让 build_messages 跳过末尾再追加，
        # 否则会导致：① 末尾连续两条 user msg；或 ② 末尾还是上一轮 assistant 触发
        # Anthropic 400 "model does not support assistant message prefill"。
        # 跟 main.py:/chat 路径（line 1742-1764）对称——那里也是先 append 再
        # snapshot session.conversation_history。
        await session_mgr.append_message(sk.session_id, "user", msg.text)
        history = list(getattr(session, "conversation_history", None) or
                       session.load_conversation_history() or [])

        # 3. 跑 agent.chat（内部走 v2 chain）
        full_response_parts: list[str] = []
        chat_error: Exception | None = None
        try:
          with use_call_config(dispatch_call_config):
            async for event in agent.chat(
                msg.text,
                stream=True,
                session_id=sk.session_id,
                conversation_history=history,
                user_message_already_in_history=True,
            ):
                etype = event.get("type")
                if etype == "content":
                    chunk = event.get("text") or ""
                    if not chunk:
                        continue
                    full_response_parts.append(chunk)
                    yield OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        text=chunk,
                        is_final=False,
                        thread_ts=msg.thread_ts,
                    )
                elif etype == "system_notice":
                    # 关键 system_notice 单独发一条让 IM 用户能看到
                    # （非关键的 mw / retry 通知不发，避免噪音）
                    kind = event.get("kind") or ""
                    if kind in _NOTICE_KINDS_TO_FORWARD:
                        notice_text = event.get("content") or ""
                        if notice_text:
                            yield OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                text=f"[{kind}] {notice_text}",
                                is_final=False,
                                thread_ts=msg.thread_ts,
                                metadata={"kind": kind},
                            )
                # thinking / tool_call / tool_result → IM 不展示
        except Exception as exc:
            # 让 IM 用户能看到错误反馈（否则消息石沉大海）。
            # 不再 raise——已经 yield 错误 outbound 给用户，调用方靠 finally
            # 块的 is_final=True 终结流；router 层不会因为这条消息把异常
            # 传播到 consumer loop。
            chat_error = exc
            log.exception(
                "dispatch: agent.chat raised; sending error outbound to IM "
                "session=%s",
                sk.session_id,
            )
            err_text = f"⚠️ 调用 LLM 失败：{type(exc).__name__}: {exc}"[:500]
            try:
                yield OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    text=err_text,
                    is_final=False,
                    thread_ts=msg.thread_ts,
                    metadata={"kind": "dispatch_error"},
                )
                full_response_parts.append(err_text)  # 落盘也记一条便于追查
            except Exception:
                log.exception("dispatch: yield error outbound failed")
        finally:
            # 4. 落盘 assistant msg + 流末终结消息（即使中途异常也要 finalize）
            full_response = "".join(full_response_parts).strip()
            if full_response:
                try:
                    await session_mgr.append_message(
                        sk.session_id, "assistant", full_response,
                    )
                except Exception:
                    log.exception(
                        "dispatch: append assistant msg failed session=%s",
                        sk.session_id,
                    )

            # 流末空消息标记 is_final=True；adapter 据此 close stream
            yield OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                text="",
                is_final=True,
                thread_ts=msg.thread_ts,
            )

    return dispatch


# 关键 system_notice kind 转 IM 文本通知；其它（loop_detection / retry / cache 等
# 内部信号）不发——避免噪音
_NOTICE_KINDS_TO_FORWARD: frozenset[str] = frozenset({
    "v2_chain_short_circuit",          # mw 17 ask_clarification 短路
    "v2_clarification_pending",        # mw 17 末轮检测
})


def make_run_context(sk: SessionKey, *, channel: str | None = None,
                     request_id: str | None = None) -> RunContext:
    """辅助：从 SessionKey 构造 RunContext（非主路径用——主路径走 agent.chat
    内部自己构造 ctx）。某些场景需要外层显式控制 trace_id / channel 时用。"""
    return RunContext(
        user_id=sk.user_id,
        agent_id=sk.agent_id,
        session_id=sk.session_id,
        run_id=None,
        trace_id=new_trace_id(),
        channel=channel,
        request_id=request_id,
    )


__all__ = [
    "AgentResolver",
    "SessionManagerResolver",
    "make_dispatch",
    "make_run_context",
]
