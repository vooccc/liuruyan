"""v2 · E-4'b-2 · channels_web routes — POST inbound + GET outbound SSE

设计：_docs/architecture_v2/01_master_plan.md Phase E + handover_v2_phase_e_web_channel_landed.md

把 ``WebChannel``（backend/channels/web.py）包成 HTTP 端点：

```
POST /v2/channels/web/inbound
  body: {chat_id, user_id, text, [thread_ts, topic_id, msg_type]}
  → web_channel.publish_inbound(InboundMessage(...))
  → MessageBus → InboundRouter → dispatch → v2 chain → publish_outbound
  → web_channel.send → per-chat queue
  → SSE 客户端流出

GET /v2/channels/web/outbound/{chat_id}  (text/event-stream)
  → async for msg in web_channel.stream(chat_id):
       yield encode_sse_event({type, channel, chat_id, text, is_final, ...})
```

启用条件：
- ``LIURUYAN_V2_CHANNELS=1``（整套）
- ``LIURUYAN_V2_CHANNELS_WEB=1``（web channel ephemeral 注册）

未启用时所有 endpoint 返 503（v2 channels not enabled）。

测试 / curl 用法：

```bash
# Terminal A: 监听 SSE
curl -N http://localhost:8000/v2/channels/web/outbound/chat_demo

# Terminal B: 发消息
curl -X POST http://localhost:8000/v2/channels/web/inbound \
  -H "Content-Type: application/json" \
  -d '{"chat_id":"chat_demo","user_id":"alice","text":"你好"}'
```
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.sse_manager import encode_sse_event
from backend.v2_bootstrap import get_v2_channels_handle
from liuruyan.buses import InboundMessage

log = logging.getLogger(__name__)


router = APIRouter(prefix="/v2/channels/web", tags=["v2-channels-web"])


# ── 请求 / 响应 schema ────────────────────────────────────────────────


class InboundPayload(BaseModel):
    """POST /inbound 的 JSON body schema。

    跟 ``liuruyan.buses.InboundMessage`` 字段对齐，但作为 HTTP boundary
    用 pydantic 校验。
    """

    chat_id: str = Field(..., min_length=1, description="平台原生会话 ID")
    user_id: str = Field(..., min_length=1, description="平台原生用户 ID")
    text: str = Field(..., description="用户消息文本（允许空字符串以支持系统消息）")
    msg_type: str = Field("chat", description="chat/command/system")
    thread_ts: Optional[str] = Field(None, description="可选 thread/楼中楼时间戳")
    topic_id: Optional[str] = Field(None, description="可选话题 ID")
    metadata: dict[str, Any] = Field(default_factory=dict)


class InboundResponse(BaseModel):
    ok: bool
    queued: bool


# ── endpoints ─────────────────────────────────────────────────────────


@router.post("/inbound", response_model=InboundResponse)
async def inbound(payload: InboundPayload) -> InboundResponse:
    """收 web 客户端消息 → 进 MessageBus → InboundRouter → v2 chain。

    503 if v2 channels / web channel 未启用（``LIURUYAN_V2_CHANNELS{,_WEB}=1``）。
    """
    handle = get_v2_channels_handle()
    if handle is None or handle.web_channel is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "v2 web channel not enabled "
                "(set LIURUYAN_V2_CHANNELS=1 + LIURUYAN_V2_CHANNELS_WEB=1)"
            ),
        )

    msg = InboundMessage(
        channel="web",
        chat_id=payload.chat_id,
        user_id=payload.user_id,
        text=payload.text,
        msg_type=payload.msg_type,  # type: ignore[arg-type]
        thread_ts=payload.thread_ts,
        topic_id=payload.topic_id,
        metadata=payload.metadata,
    )
    queued = await handle.web_channel.publish_inbound(msg)
    if not queued:
        # MessageBus inbound 队列满 → 503 让客户端 retry-after
        raise HTTPException(
            status_code=503,
            detail="inbound queue full; retry shortly",
        )
    return InboundResponse(ok=True, queued=True)


@router.get("/outbound/{chat_id}")
async def outbound(chat_id: str) -> StreamingResponse:
    """SSE 流：拿该 chat_id 的 OutboundMessage。

    客户端断开时 ``async for`` 自动 cancel → WebChannel.stream finally 清 queue。
    """
    handle = get_v2_channels_handle()
    if handle is None or handle.web_channel is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "v2 web channel not enabled "
                "(set LIURUYAN_V2_CHANNELS=1 + LIURUYAN_V2_CHANNELS_WEB=1)"
            ),
        )

    web_channel = handle.web_channel

    async def _sse_stream():
        try:
            async for msg in web_channel.stream(chat_id):
                event = {
                    "type": "outbound",
                    "channel": msg.channel,
                    "chat_id": msg.chat_id,
                    "text": msg.text,
                    "is_final": msg.is_final,
                }
                if msg.thread_ts:
                    event["thread_ts"] = msg.thread_ts
                if msg.metadata:
                    event["metadata"] = msg.metadata
                yield encode_sse_event(event)
                if msg.is_final:
                    # 流结束帧——主流程结束发 ``done`` event 让客户端 close
                    yield encode_sse_event({"type": "done", "chat_id": chat_id})
                    return
        except Exception:
            log.exception(
                "[v2:channels:web] SSE stream failed chat=%s", chat_id,
            )
            yield encode_sse_event({"type": "error", "chat_id": chat_id})

    return StreamingResponse(
        _sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx 不缓冲（如果反代）
        },
    )


__all__ = ["router"]
