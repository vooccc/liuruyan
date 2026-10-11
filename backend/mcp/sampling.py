"""Sampling 桥接（M4.2）。

server 反向请求"帮我跑一次推理" → SDK callback → 这里 →
1. 安全门：config.sampling.enabled / allowed_models 白名单 / max_tokens_cap / max_rpm
2. 通过 LLMClient.chat_completion_async 调本地默认模型
3. 返回 CreateMessageResult

策略：默认 enabled=False（禁用 sampling 是安全默认；用户在配置里显式开）。
RPM 用极简 token-bucket（无锁，单 loop 安全），server 维度独立。

★ 不直接暴露用户主对话历史 / system prompt——sampling.includeContext 字段告诉
我们 server 想要什么上下文，本实现 v1 一律按 'none' 处理（不混入会话历史），
等 M4.2.1 用户反馈后再决定是否实现 'thisServer' / 'allServers'。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from mcp.types import (
    CreateMessageRequestParams,
    CreateMessageResult,
    ErrorData,
    TextContent,
)

logger = logging.getLogger(__name__)


# server_id → list[timestamp]，最近 N 次调用时间
_call_log: dict[str, list[float]] = {}


def _check_rpm(server_id: str, max_rpm: int) -> bool:
    """简易 RPM 滑动窗口。返回 True 通过，False 触发限流。"""
    if max_rpm <= 0:
        return True  # 0 表示不限
    now = time.time()
    bucket = _call_log.setdefault(server_id, [])
    # 清掉 1 分钟以前的
    cutoff = now - 60.0
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= max_rpm:
        return False
    bucket.append(now)
    return True


def _convert_messages(params: CreateMessageRequestParams) -> list[dict[str, Any]]:
    """MCP messages → OpenAI-style messages。

    MCP message: {role: 'user'|'assistant', content: TextContent|ImageContent}
    柳如烟 LLMClient: {role, content: str}
    """
    out: list[dict[str, Any]] = []
    if params.systemPrompt:
        out.append({"role": "system", "content": params.systemPrompt})
    for m in params.messages or []:
        content = m.content
        text = ""
        if isinstance(content, list):
            text = "\n".join(
                getattr(c, "text", "") or ""
                for c in content
                if getattr(c, "type", "") == "text"
            )
        elif hasattr(content, "text"):
            text = content.text or ""
        elif isinstance(content, str):
            text = content
        out.append({"role": m.role, "content": text})
    return out


async def sampling_callback_for(
    server_id: str, params: CreateMessageRequestParams
) -> CreateMessageResult | ErrorData:
    """SDK 调进来；按本 server 的 sampling 策略决定是否处理。"""
    from .controller import get_controller

    cfg = get_controller().get_config(server_id)
    if cfg is None or cfg.sampling is None or not cfg.sampling.enabled:
        return ErrorData(code=-32601, message="sampling not enabled for this server")

    # 模型白名单（用户没配 allowed_models 视为只允许默认模型）
    requested = None
    if params.modelPreferences and params.modelPreferences.hints:
        for h in params.modelPreferences.hints:
            if h.name:
                requested = h.name
                break
    allowed = cfg.sampling.allowed_models or []
    if allowed and requested and requested not in allowed:
        return ErrorData(
            code=-32602,
            message=f"requested model '{requested}' not in allowed_models {allowed}",
        )

    # RPM
    if not _check_rpm(server_id, int(cfg.sampling.max_rpm or 0)):
        return ErrorData(
            code=-32029,  # in line with -32xxx range；不是 RPC 标准但保留分段
            message=f"sampling rate limit exceeded ({cfg.sampling.max_rpm} rpm) for {server_id}",
        )

    # max_tokens 上限
    cap = int(cfg.sampling.max_tokens_cap or 4096)
    requested_max = int(params.maxTokens or cap)
    effective_max = min(requested_max, cap)

    # 调本地 LLM
    try:
        from backend.llm_client import get_llm_client

        client = get_llm_client()
        messages = _convert_messages(params)
        result = await client.chat_completion_async(
            messages,
            max_tokens=effective_max,
            temperature=float(params.temperature) if params.temperature is not None else 0.7,
        )
        text = (result or {}).get("content") or ""
    except Exception as e:  # noqa: BLE001
        logger.exception("[MCP-SAMPLING] LLM call failed")
        return ErrorData(code=-32603, message=f"LLM error: {e}")

    return CreateMessageResult(
        role="assistant",
        content=TextContent(type="text", text=text),
        model=getattr(client, "model", "liuruyan-default"),
        stopReason="endTurn",
    )


__all__ = ["sampling_callback_for"]
