"""协作社交 API（/social/consult + /social/collab-chat[+DELETE]）。

从 main.py 拆出（路由瘦身收尾），路径与行为保持完全一致。这三条端点与 chat
流式深度耦合（多角色协作流式引擎 + 副流合并 + 关系/游戏化收尾联动），是 social
域最后迁出的部分。依赖均可直接 import，不反向依赖 main.py：
- 流式：runtime_protocol / runtime_segments / sse_manager / llm_client
- 社交/游戏化：social_service（affinity helper、关系更新、状态落盘）
- 编排/会话/角色/记忆：orchestrator / session / agent / agent_config / memory_manager

引擎 _stream_collab_agent_turn 与 consult/collab 的持久化 helper 仅这三条端点使用，
故一并迁入本模块。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from backend.agent import get_agent
from backend.agent_config import get_agent_manager
from backend.llm_call_config import ModelResolutionError, resolve_call_model_config
from backend.llm_client import use_call_config
from backend.memory_manager import get_memory_manager
from backend.orchestrator import get_orchestrator
from backend.runtime_protocol import (
    build_runtime_transport_event as _build_runtime_transport_event,
)
from backend.runtime_segments import (
    build_session_payload_from_segments as _build_session_payload_from_segments,
    collect_runtime_event as _collect_runtime_event,
    encode_sse as _encode_sse,
    extract_assistant_text as _extract_assistant_text,
)
from backend.session import get_session_manager
from backend.social_service import (
    apply_pair_affinity_update as _apply_pair_affinity_update,
    classify_collab_event as _classify_collab_event,
    ensure_social_graph as _ensure_social_graph,
    flush_social_and_game_state as _flush_social_and_game_state,
)
from backend.sse_manager import get_sse_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["social-collab"])

# 与 main.py 一致：协作副流订阅用的 SSE 管理器单例。
sse_manager = get_sse_manager()


class ConsultRequest(BaseModel):
    from_agent: str
    from_session: str
    to_agent: str
    question: str = ""
    reason: str = ""
    stream: bool = False

class CollabChatRequest(BaseModel):
    scene_id: str                           # 场景 ID
    message: str                            # 用户消息
    from_agent: Optional[str] = None        # 指定发言角色（None=按轮次）— 兼容旧前端
    speakers: Optional[List[str]] = None    # 指定发言序列（None/空=按 turn_order；非空=只让这些人按这个顺序发言，允许重复）
    model: Optional[str] = None             # 指定模型
    session_id: Optional[str] = None        # 复用已有 session

async def _stream_collab_agent_turn(
    agent,
    agent_id: str,
    user_message: str,
    session_id: str,
    system_extra: str,
    requested_model: Optional[str],
    turn_state: Dict[str, Any],
):
    # per-agent model routing 阶段 3 Commit 3.3：collab 协作回合 LLM 调用用新 resolve 路径
    # 2026-05-14 修：补传 session，对齐单聊路径（main.py:1976-1981）。
    # 之前漏传 → session.model 优先级被跳过，并且 resolve 抛异常时
    # call_config=None → use_call_config(None) 是 no-op → 走 LLMClient 单例的
    # 上一个 stale 状态（典型：用户刚在单聊切到无 Key provider，群聊就继承空 Key 报 401
    # "missing Authorization: Bearer <key>"）。
    collab_session = None
    try:
        collab_session = await get_session_manager().get_session(session_id)
    except Exception:
        collab_session = None
    collab_call_config = None
    try:
        collab_call_config = resolve_call_model_config(
            agent_id=agent_id,
            override=requested_model or None,
            session=collab_session,
        )
    except ModelResolutionError as exc:
        logger.warning("[COLLAB-RESOLVE-FAIL] agent=%s override=%r err=%s", agent_id, requested_model, exc)

    # 2026-05-22 修：协作回合补传 conversation_history，对齐单聊路径（main.py:2117-2210）。
    # 之前 agent.chat() 收到 conversation_history=None → build_messages 当作全新对话 →
    # 角色每轮都"失忆"，只看到 full_message（当轮用户消息 + 同轮其他角色回复），
    # 看不到上一轮的用户消息和回复。_persist_collab_messages 是落盘了，但下一轮没人读。
    # 注：copy() 防止同步对话历史时被原地改坏；本回合"用户消息+同轮其他角色回复"
    # 已被 collab_chat 拼进 user_message 里了，不需要再塞历史。
    collab_history = None
    if collab_session is not None:
        try:
            collab_history = collab_session.conversation_history.copy()
        except Exception as exc:
            logger.warning("[COLLAB-HISTORY-FAIL] session=%s err=%s", session_id, exc)
            collab_history = None

    response_text = ""
    collected_segments: List[Dict[str, Any]] = []
    turn_state.clear()

    async def _consume_with_sidecar():
        """stream=True + 副流合并：跑 agent.chat() 同时订阅 sse_manager，
        把工具内部推的 command_chunk / acp_delegate_chunk 实时混入响应。

        参考 chat /chat 的 pump_agent + pump_sidecar 双协程模式（main.py:2200-2255）。
        议事厅 session_id 是 collab_session.id；sse_manager 推的事件不带 agent_id，
        转发时强制注入当轮 agent_id 让前端 applyEventToCollabBubble 路由到对应角色 bubble。

        过滤策略沿用 chat 路径：带 worker_id / route_id 的事件归 useDispatchEventConsumer
        独立长连接消费，不混进 collab 流。
        """
        nonlocal response_text
        AGENT_DONE = object()
        AGENT_ERROR = object()
        merged_queue: "asyncio.Queue[Any]" = asyncio.Queue()

        async def pump_agent():
            try:
                with use_call_config(collab_call_config):
                    async for event in agent.chat(
                        user_message=user_message,
                        stream=True,
                        session_id=session_id,
                        conversation_history=collab_history,
                        system_extra=system_extra,
                    ):
                        await merged_queue.put(("agent", event))
            except Exception as e:
                await merged_queue.put((AGENT_ERROR, e))
            finally:
                await merged_queue.put((AGENT_DONE, None))

        async def pump_sidecar():
            try:
                async for event in sse_manager.subscribe(session_id, last_event_id=None):
                    if isinstance(event, dict) and (event.get("worker_id") or event.get("route_id")):
                        continue
                    await merged_queue.put(("sidecar", event))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[COLLAB-SIDECAR] error agent=%s sid=%s: %s", agent_id, session_id, e)

        agent_task = asyncio.create_task(pump_agent())
        sidecar_task = asyncio.create_task(pump_sidecar())

        try:
            agent_finished = False
            while not agent_finished:
                kind, item = await merged_queue.get()
                if kind is AGENT_DONE:
                    agent_finished = True
                    continue
                if kind is AGENT_ERROR:
                    raise item
                if kind == "agent":
                    response_text += _collect_runtime_event(collected_segments, item, agent_id=agent_id)
                    payload = _build_runtime_transport_event(
                        item,
                        agent_id=agent_id,
                        content_type="agent_content",
                    )
                    if payload:
                        yield _encode_sse(payload)
                elif kind == "sidecar":
                    if isinstance(item, dict):
                        sidecar_payload = dict(item)
                        sidecar_payload.setdefault("agent_id", agent_id)
                        yield _encode_sse(sidecar_payload)
        finally:
            sidecar_task.cancel()
            try:
                await sidecar_task
            except (asyncio.CancelledError, Exception):
                pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _consume_no_stream():
        """stream=False 兜底路径：不开 sidecar（一次性返回，无中途副流事件）。"""
        nonlocal response_text
        with use_call_config(collab_call_config):
            async for event in agent.chat(
                user_message=user_message,
                stream=False,
                session_id=session_id,
                conversation_history=collab_history,
                system_extra=system_extra,
            ):
                response_text += _collect_runtime_event(collected_segments, event, agent_id=agent_id)
                payload = _build_runtime_transport_event(
                    event,
                    agent_id=agent_id,
                    content_type="agent_content",
                )
                if payload and payload.get("type") != "agent_content":
                    yield _encode_sse(payload)

        if response_text:
            yield _encode_sse({
                "type": "agent_content",
                "agent_id": agent_id,
                "content": response_text,
            })

    try:
        async for chunk in _consume_with_sidecar():
            yield chunk
    except Exception:
        try:
            async for chunk in _consume_no_stream():
                yield chunk
        except Exception as exc:
            turn_state["error"] = str(exc)
            return

    turn_state["response_text"] = response_text
    turn_state["payload"] = _build_session_payload_from_segments(
        collected_segments,
        response_text,
        agent_id,
    )


async def _persist_collab_messages(
    session_id: str,
    user_message: str,
    responses: Dict[str, str],
    payloads: Dict[str, Dict[str, Any]],
) -> None:
    session_mgr = get_session_manager()
    collab_session = await session_mgr.get_session(session_id)
    if not collab_session:
        return

    await session_mgr.append_message(session_id, "user", user_message)
    for agent_id, response_text in responses.items():
        payload = _resolve_runtime_message_payload(payloads.get(agent_id), response_text, agent_id)
        # 协作消息走 OpenAI std 写盘
        await session_mgr.append_assistant_structured(
            session_id,
            segments=payload.get("segments", []),
            text=response_text,
            agent_id=agent_id,
        )


def _resolve_runtime_message_payload(
    payload: Optional[Dict[str, Any]],
    response_text: str,
    agent_id: str,
) -> Dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("segments"), list) and payload.get("segments"):
        return payload
    return _build_session_payload_from_segments(None, response_text, agent_id)


def _build_consult_message_payload(
    agent_id: str,
    agent_name: str,
    response_text: str,
    timestamp: Optional[str] = None,
    runtime_events: Optional[List[Dict[str, Any]]] = None,
) -> Dict:
    """构造征求意见消息的标准返回/持久化结构。"""
    safe_response = (response_text or "").strip()
    display_content = f"【征求意见 · {agent_name}】\n{safe_response}"
    segments: List[Dict[str, Any]] = []
    for event in runtime_events or []:
        if not isinstance(event, dict) or event.get("type") == "content":
            continue
        _collect_runtime_event(segments, event, agent_id=agent_id)
    segments.append({
        "id": len(segments),
        "type": "text",
        "content": display_content,
        "agent_id": agent_id,
    })
    payload = _build_session_payload_from_segments(segments, display_content, agent_id)
    return {
        "role": "assistant",
        "content": payload["content"],
        "timestamp": timestamp or datetime.now().isoformat(),
        "segments": payload["segments"],
    }

@router.post("/social/consult")
async def consult_agent(body: ConsultRequest):
    """征求另一角色的一次性意见，并回流到当前主聊天。"""
    if not body.from_agent or not body.from_session or not body.to_agent:
        raise HTTPException(status_code=400, detail="缺少 from_agent / from_session / to_agent")

    session_mgr = get_session_manager()
    session = await session_mgr.get_session(body.from_session)
    if not session:
        raise HTTPException(status_code=404, detail="原会话不存在")

    agent_mgr = get_agent_manager()
    to_agent_info = agent_mgr.get_agent(body.to_agent)
    if not to_agent_info:
        raise HTTPException(status_code=404, detail=f"角色 {body.to_agent} 不存在")

    from_agent_info = agent_mgr.get_agent(body.from_agent) or {}
    consult_question = (body.question or body.reason or "").strip() or "请基于当前对话，补充你最值得参考的一点意见。"

    normalized_history = []
    for item in session.conversation_history:
        role = item.get("role")
        content = item.get("content", "")
        if role == "assistant":
            content = _extract_assistant_text(content)
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            normalized_history.append({"role": role, "content": content})

    consulted_agent = get_agent(body.to_agent)
    if not consulted_agent:
        raise HTTPException(status_code=404, detail=f"角色 {body.to_agent} 不可用")

    # per-agent model routing 阶段 3 Commit 3.3：consult LLM 调用用新 resolve 路径
    # 老调用 _configure_runtime_model(consulted_agent) 不传 override，走全局默认；
    # 新路径 resolve 不传 override，同义。
    consult_call_config = None
    try:
        consult_call_config = resolve_call_model_config(
            agent_id=body.to_agent,
            override=None,
        )
    except ModelResolutionError as exc:
        logger.warning("[CONSULT-RESOLVE-FAIL] agent=%s err=%s", body.to_agent, exc)

    system_extra = (
        "## 征求意见任务\n"
        f"你现在不是来接管主聊天，而是被 {from_agent_info.get('name', body.from_agent)} 临时征求意见。\n"
        "请基于已有对话上下文，给出一次性、直接可用的补充建议。\n"
        "不要假装自己已经加入长期多人协作，也不要要求切换模式。"
    )
    consult_prompt = (
        f"当前主聊角色 {from_agent_info.get('name', body.from_agent)} 想听听你的看法。\n"
        f"征求意见：{consult_question}"
    )
    consulted_name = to_agent_info.get("name", body.to_agent)
    display_prefix = f"【征求意见 · {consulted_name}】\n"

    async def persist_consult_response(
        response_text: str,
        timestamp: Optional[str] = None,
        runtime_events: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict:
        message_payload = _build_consult_message_payload(
            body.to_agent,
            consulted_name,
            response_text,
            timestamp=timestamp,
            runtime_events=runtime_events,
        )
        # 协作场景的 consult 响应也走 OpenAI std 写盘
        await session_mgr.append_assistant_structured(
            session.id,
            segments=message_payload.get("segments", []),
            text=message_payload["content"],
            agent_id=body.to_agent,
        )
        return message_payload

    def build_success_response(response_text: str, message_payload: Dict) -> Dict:
        return {
            "status": "success",
            "session_id": session.id,
            "question": consult_question,
            "agent_id": body.to_agent,
            "agent_name": consulted_name,
            "response": response_text,
            "message": message_payload,
        }

    def build_error_payload(exc: Exception) -> tuple[int, Dict]:
        status_code = 502
        error_message = getattr(exc, "message", None) or str(exc)
        error_code = getattr(getattr(exc, "code", None), "value", None)
        retryable = bool(getattr(exc, "retryable", False))
        retry_after_ms = getattr(exc, "retry_after_ms", None)

        if isinstance(exc, HTTPException):
            status_code = exc.status_code
            error_message = str(exc.detail)
            error_code = "http_error"
            retryable = False
            retry_after_ms = None
        elif error_code == "authentication":
            status_code = 401
        elif error_code == "rate_limit":
            status_code = 429
        elif error_code == "overloaded":
            status_code = 503
        elif error_code == "context_too_long":
            status_code = 400
        elif error_code == "response_format_error":
            status_code = 422
        elif error_code == "network_error":
            status_code = 502

        payload = {
            "status": "error",
            "session_id": session.id,
            "question": consult_question,
            "agent_id": body.to_agent,
            "agent_name": consulted_name,
            "error": error_message,
            "error_code": error_code or "unknown",
            "retryable": retryable,
        }
        if retry_after_ms is not None:
            payload["retry_after_ms"] = retry_after_ms
        return status_code, payload

    if not body.stream:
        try:
            with use_call_config(consult_call_config):
                run_result = await consulted_agent.run_collected(
                    user_message=consult_prompt,
                    session_id=body.from_session,
                    conversation_history=normalized_history,
                    system_extra=system_extra,
                    collect_events=True,
                )
            response_text = (run_result.get("text") or "").strip()
            runtime_events = [
                event
                for event in (run_result.get("events") or [])
                if isinstance(event, dict)
            ]
            response_text = response_text.strip()
            if not response_text:
                raise ValueError("征求意见失败：未获得有效回复")

            message_payload = await persist_consult_response(
                response_text,
                runtime_events=runtime_events,
            )
            return build_success_response(response_text, message_payload)
        except Exception as e:
            status_code, payload = build_error_payload(e)
            return JSONResponse(status_code=status_code, content=payload)

    def encode_sse(payload: Dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async def generate():
        response_text = ""
        runtime_events: List[Dict[str, Any]] = []
        start_timestamp = datetime.now().isoformat()
        try:
            yield encode_sse({
                "type": "consult_start",
                "session_id": session.id,
                "agent_id": body.to_agent,
                "agent_name": consulted_name,
                "question": consult_question,
                "display_prefix": display_prefix,
                "timestamp": start_timestamp,
            })

            with use_call_config(consult_call_config):
              async for event in consulted_agent.chat(
                user_message=consult_prompt,
                stream=True,
                session_id=body.from_session,
                conversation_history=normalized_history,
                system_extra=system_extra,
              ):
                etype = event.get("type", "content")
                payload = _build_runtime_transport_event(
                    event,
                    agent_id=body.to_agent,
                    session_id=session.id,
                )
                if payload is None:
                    continue
                runtime_events.append(dict(event))
                if etype == "content":
                    response_text += event.get("text", "")
                yield encode_sse(payload)

            response_text_stripped = response_text.strip()
            if not response_text_stripped:
                raise ValueError("征求意见失败：未获得有效回复")

            message_payload = await persist_consult_response(
                response_text_stripped,
                timestamp=start_timestamp,
                runtime_events=runtime_events,
            )
            yield encode_sse({
                "type": "consult_end",
                "status": "success",
                "session_id": session.id,
                "question": consult_question,
                "agent_id": body.to_agent,
                "agent_name": consulted_name,
                "response": response_text_stripped,
                "message": message_payload,
            })
            yield "data: [DONE]\n\n"
        except Exception as e:
            _, payload = build_error_payload(e)
            payload["type"] = "error"
            yield encode_sse(payload)
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

@router.post("/social/collab-chat")
async def collab_chat(body: CollabChatRequest):
    """协作对话 SSE 流

    用户发消息 → Orchestrator 安排发言顺序
    → 各角色依次流式回复 → 回复注入下一个角色的上下文
    → 所有角色回复完毕 → 场景推进
    首次请求创建正式 session，解散时删除。
    """
    orch = get_orchestrator()
    agent_mgr = get_agent_manager()
    session_mgr = get_session_manager()

    # 确定 scene_id：优先从请求体，其次从 session metadata
    scene_id = body.scene_id
    if not scene_id and body.session_id:
        # 从 session metadata 恢复 scene_id
        existing_session = await session_mgr.get_session(body.session_id)
        if existing_session and existing_session.metadata.get("scene_id"):
            scene_id = existing_session.metadata["scene_id"]

    if not scene_id:
        raise HTTPException(status_code=400, detail="缺少 scene_id 且无法从 session 恢复")

    scene = orch.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail=f"场景 {scene_id} 不存在")

    # Session 管理
    session_id = body.session_id
    if not body.session_id:
        # 首次请求，创建正式 session
        participant_names = []
        for pid in scene.participants:
            info = agent_mgr.get_agent(pid)
            participant_names.append(info.get("name", pid) if info else pid)
        # 同上方 create_collab_scene：标题只放参与者，场景类型走前端 tag。
        title = '、'.join(participant_names) if participant_names else "协作"
        session = await session_mgr.create_session(
            agent_id=scene.participants[0] if scene.participants else "default",
            title=title,
            model=body.model,
            collab_type="scene",
            scene_id=scene_id,
            scene_type=scene.scene_type,
            participants=list(scene.participants or []),
        )
        session_id = session.id
    orch = get_orchestrator()
    scene = orch.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="场景不存在")
    if not scene.is_active:
        raise HTTPException(status_code=400, detail="场景已结束")

    session_mgr = get_session_manager()
    agent_mgr = get_agent_manager()

    async def stream_collab():
        """协作聊天 SSE 流"""
        import asyncio as _asyncio

        # 首条事件返回 session_id
        yield _encode_sse({"type": "session_info", "session_id": session_id})

        # 确定发言顺序
        # 优先级：speakers（新版多选+排序）> from_agent（旧版单人插队）> 默认 turn_order
        # speakers 语义：只让列表里的人按指定顺序发言，允许同一角色重复出现（如 a→b→a）。
        # 校验：每项必须是当前场景参与者；非参与者直接丢弃，避免后端拒绝整条请求带来糟糕 UX。
        participant_set = set(scene.participants or [])
        if body.speakers:
            turn_order = [aid for aid in body.speakers if aid in participant_set]
            # 全被过滤光（前端传了一堆 stale id）→ 退回默认顺序，至少能让群里有人发言。
            if not turn_order:
                turn_order = list(scene.turn_order)
        else:
            turn_order = list(scene.turn_order)
            if body.from_agent and body.from_agent in turn_order:
                # 指定角色先发言（旧版单人插队语义）
                turn_order.remove(body.from_agent)
                turn_order.insert(0, body.from_agent)

        # 收集各角色回复，注入后续角色上下文
        accumulated_responses: Dict[str, str] = {}
        accumulated_payloads: Dict[str, Dict[str, Any]] = {}

        for idx, agent_id in enumerate(turn_order):
            try:
                # 获取角色信息
                agent_info = agent_mgr.get_agent(agent_id)
                if not agent_info:
                    continue

                agent_name = agent_info.get("name", agent_id)

                # 构建场景上下文 system prompt
                other_info = {}
                for other_id in scene.participants:
                    if other_id != agent_id:
                        other_agents = agent_mgr.get_agent(other_id)
                        other_name = other_agents.get("name", other_id) if other_agents else other_id
                        # 从社交图获取关系信息
                        graph = _ensure_social_graph()
                        rel = graph.get_relationship(agent_id, other_id)
                        other_info[other_id] = {
                            "name": other_name,
                            "bond_type": ", ".join(rel.bond_types) if rel else "未知",
                            "affinity": rel.affinity if rel else 0,
                            "what_you_know": "、".join(rel.get_knowledge(agent_id)) if rel else "",
                        }

                scene_prompt = orch.build_scene_system_prompt(
                    scene=scene,
                    agent_id=agent_id,
                    agent_name=agent_name,
                    other_agents_info=other_info,
                )

                # 构建用户消息（包含之前角色的回复）
                context_parts = []
                context_parts.append(f"用户说：{body.message}")
                for prev_id, prev_resp in accumulated_responses.items():
                    prev_name = agent_mgr.get_agent(prev_id)
                    prev_name = prev_name.get("name", prev_id) if prev_name else prev_id
                    context_parts.append(f"{prev_name}说：{prev_resp}")

                full_message = "\n\n".join(context_parts)

                # 发送 SSE 事件：角色开始发言
                yield _encode_sse({
                    "type": "agent_start",
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "turn": idx + 1,
                    "total_turns": len(turn_order),
                })

                # 调用角色 LLM
                agent = get_agent(agent_id)
                if not agent:
                    yield _encode_sse({
                        "type": "agent_error",
                        "agent_id": agent_id,
                        "error": "角色不可用",
                    })
                    continue

                turn_state: Dict[str, Any] = {}
                async for chunk in _stream_collab_agent_turn(
                    agent=agent,
                    agent_id=agent_id,
                    user_message=full_message,
                    session_id=session_id,
                    system_extra=scene_prompt,
                    requested_model=body.model,
                    turn_state=turn_state,
                ):
                    yield chunk

                if turn_state.get("error"):
                    yield _encode_sse({
                        "type": "agent_error",
                        "agent_id": agent_id,
                        "error": turn_state["error"],
                    })
                    continue

                response_text = turn_state.get("response_text", "")
                accumulated_responses[agent_id] = response_text
                accumulated_payloads[agent_id] = _resolve_runtime_message_payload(
                    turn_state.get("payload"),
                    response_text,
                    agent_id,
                )

                # 发送 SSE 事件：角色发言结束
                yield _encode_sse({
                    "type": "agent_end",
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                })

            except Exception as e:
                yield _encode_sse({
                    "type": "agent_error",
                    "agent_id": agent_id,
                    "error": str(e),
                })
                continue

        # ===== 收尾：持久化 + 关系更新 + 推进场景 =====
        # 关键：所有步骤必须容错，确保末尾的 [DONE] 一定被 yield，
        # 否则前端会收到 TypeError: network error（流被异常截断）。
        try:
            if accumulated_responses:
                try:
                    await _persist_collab_messages(
                        session_id=session_id,
                        user_message=body.message,
                        responses=accumulated_responses,
                        payloads=accumulated_payloads,
                    )
                except Exception as e:
                    logger.error("[collab_chat] 持久化协作消息失败: %s", e, exc_info=True)

                try:
                    participants = [pid for pid in scene.participants if pid in accumulated_responses]
                    if len(participants) >= 2:
                        event_type = _classify_collab_event(scene.scene_type)
                        description = (body.message or "")[:80]
                        for i, agent_a in enumerate(participants):
                            for agent_b in participants[i + 1:]:
                                _apply_pair_affinity_update(agent_a, agent_b, event_type, description)
                        _flush_social_and_game_state()
                except Exception as e:
                    logger.error("[collab_chat] 关系/状态更新失败: %s", e, exc_info=True)

            # 推进场景（用 local scene_id；body.scene_id 在续聊时可能为空）
            try:
                last_response = list(accumulated_responses.values())[-1] if accumulated_responses else None
                orch.advance_scene(scene_id, last_response)
                updated_scene = orch.get_scene(scene_id)
                yield _encode_sse({
                    "type": "scene_update",
                    "scene_id": scene_id,
                    "is_active": updated_scene.is_active if updated_scene else False,
                    "phase": updated_scene.phase if updated_scene else "resolved",
                })
            except Exception as e:
                logger.error("[collab_chat] 推进场景失败: %s", e, exc_info=True)
        finally:
            # 无论如何都送出终止哨兵，前端才能干净关流
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream_collab(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

@router.delete("/social/collab-chat/{scene_id}")
async def dismiss_collab_scene(scene_id: str):
    """解散叙事场景（解决场景 + 删除关联 session）"""
    orch = get_orchestrator()
    session_mgr = get_session_manager()
    scene = orch.get_scene(scene_id)
    if scene:
        # 通过 metadata.collab_type + scene_id 查找关联 session
        for sid, sess in list(session_mgr._sessions.items()):
            if (sess.metadata.get("collab_type") == "scene"
                and sess.metadata.get("scene_id") == scene_id):
                if sess.agent_id:
                    get_memory_manager(agent_id=sess.agent_id).on_session_end(
                        session_id=sess.id,
                        metadata=sess.metadata or {},
                    )
                await session_mgr.delete_session(sid)
                break
        # 解决场景
        orch.resolve_scene(scene_id)
    return {"status": "success", "scene_id": scene_id}
