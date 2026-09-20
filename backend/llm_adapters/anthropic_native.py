"""OpenAI ChatCompletion 标准消息 → Anthropic Messages API 格式。

P1-B-2 thin shim（2026-05-02）：
    本文件原 234 行实现已搬到 `liuruyan.llm.clients.anthropic_message_converter`。
    本 shim 仅 re-export，保持 backend 调用方
    （agent.py / llm_adapters/__init__.py / 测试套）import 路径不变。

    新代码请直接 `from liuruyan.llm.clients.anthropic_message_converter import ...`。

为什么搬：harness/llm/clients/anthropic.py（AnthropicApiClient）在 harness 里
要做 messages → SDK params 转换。按"harness 永不 import backend.*"硬约束，
converter 真身必须在 harness。
"""
from __future__ import annotations

from liuruyan.llm.clients.anthropic_message_converter import (
    _enforce_role_alternation,
    _ensure_user_leading,
    _sanitize_tool_id,
    _strip_orphan_tool_results,
    _strip_orphan_tool_uses,
    convert_tools_to_anthropic,
    to_anthropic_messages,
)

__all__ = [
    "to_anthropic_messages",
    "convert_tools_to_anthropic",
    "_sanitize_tool_id",
    "_strip_orphan_tool_uses",
    "_strip_orphan_tool_results",
    "_enforce_role_alternation",
    "_ensure_user_leading",
]
