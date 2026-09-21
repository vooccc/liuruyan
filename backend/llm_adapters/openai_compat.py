"""OpenAI ChatCompletion 兼容路径的消息适配器。

P1-C thin shim（2026-05-02）：
    本文件原 165 行实现已搬到 `liuruyan.llm.clients.openai_message_converter`。
    本 shim 仅 re-export，保持 backend 调用方
    （agent.py / llm_adapters/__init__.py）import 路径不变。

    新代码请直接 `from liuruyan.llm.clients.openai_message_converter import ...`。
"""
from __future__ import annotations

from liuruyan.llm.clients.openai_message_converter import (
    _enforce_role_alternation,
    _strip_orphan_tool_calls,
    _strip_orphan_tool_messages,
    to_openai_messages,
)

__all__ = [
    "to_openai_messages",
    "_strip_orphan_tool_calls",
    "_strip_orphan_tool_messages",
    "_enforce_role_alternation",
]
