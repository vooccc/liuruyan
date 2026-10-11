"""Google Antigravity / Pro 算力账号 ApiClient 适配器。

将 Google Antigravity 原生 CloudCode 流式通道桥接至柳如烟 v2 ApiClient Protocol，
从而原生复用 _chat_with_tools 完整工具循环（工具调用、多轮对话、参数自愈、并发调度等）。
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from liuruyan.llm.api_client import CoreRequest, RequestContext
from liuruyan.llm.events import (
    ParsedEvent,
    ContentDelta,
    ReasoningDelta,
    ToolCallStarted,
    ToolCallComplete,
    RoundEnd,
    Usage,
)
from backend.anthropic_client import LLMError, LLMErrorCode

logger = logging.getLogger(__name__)


class AntigravityApiClient:
    """满足 ApiClient Protocol 的 Google Antigravity 适配器。"""

    name = "google_antigravity"

    def __init__(self, model: str):
        self.model = model

    async def send_stream(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> AsyncIterator[ParsedEvent]:
        from .models_provider import stream_pro_chat

        target_model = req.model or self.model
        had_tool_call = False

        try:
            async for chunk in stream_pro_chat(
                "google-antigravity",
                target_model,
                req.messages,
                tools=req.tools,
            ):
                ctype = chunk.get("type")
                if ctype == "delta":
                    text = chunk.get("content", "")
                    if text:
                        yield ContentDelta(text=text)
                elif ctype == "thinking":
                    text = chunk.get("content", "")
                    if text:
                        yield ReasoningDelta(text=text)
                elif ctype == "tool_call":
                    had_tool_call = True
                    tc_id = chunk.get("id") or "call_unknown"
                    tc_name = chunk.get("name") or ""
                    tc_args = chunk.get("arguments") or "{}"
                    thought_sig = chunk.get("thought_signature")
                    yield ToolCallStarted(id=tc_id, name=tc_name)
                    yield ToolCallComplete(
                        id=tc_id,
                        name=tc_name,
                        arguments_raw=tc_args,
                        thought_signature=thought_sig,
                    )
                elif ctype == "usage":
                    u = chunk.get("usage", {})
                    yield Usage(payload=u)
                elif ctype == "error":
                    err_msg = chunk.get("error", "Google Antigravity error")
                    logger.error("[AntigravityApiClient] 上游错误: %s", err_msg)
                    err_code = LLMErrorCode.RATE_LIMIT if ("429" in err_msg or "配额" in err_msg or "quota" in err_msg.lower()) else LLMErrorCode.UPSTREAM_ERROR
                    raise LLMError(err_msg, err_code)

        except LLMError:
            raise
        except Exception as e:
            logger.error("[AntigravityApiClient] send_stream 异常: %s", e, exc_info=True)
            raise LLMError(f"Google Antigravity 调用异常: {e}", LLMErrorCode.UNKNOWN) from e

        yield RoundEnd(finish_reason="tool_calls" if had_tool_call else "stop")

    async def send_completion(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> Dict[str, Any]:
        """非流式调用，累积 send_stream 输出拼成 OpenAI std message-like dict
        （fallback / 健康检查路径用）。
        """
        content_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        finish_reason: Optional[str] = None
        usage: Any = None

        async for ev in self.send_stream(req, ctx):
            ev_type = ev.type
            if ev_type == "content_delta":
                content_parts.append(ev.text)
            elif ev_type == "tool_call_complete":
                tc_item = {
                    "id": ev.id,
                    "type": "function",
                    "function": {
                        "name": ev.name,
                        "arguments": ev.arguments_raw,
                    },
                }
                if getattr(ev, "thought_signature", None):
                    tc_item["thought_signature"] = ev.thought_signature
                tool_calls.append(tc_item)
            elif ev_type == "round_end":
                finish_reason = ev.finish_reason
            elif ev_type == "usage":
                usage = ev.payload

        return {
            "content": "".join(content_parts) if content_parts else None,
            "tool_calls": tool_calls if tool_calls else None,
            "finish_reason": finish_reason,
            "usage": usage,
            "model": req.model or self.model,
        }
