"""tool_use_id 跨层契约（backward-compat shim · P1-B-1 · 2026-05-02）。

P0 落地时本模块在 backend/tool_id_contract.py。P1-B-1 搬到 harness：
真值在 `liuruyan.llm.tool_id_contract`，本文件只 re-export 保持调用方
（anthropic_client / runtime_segments / llm_adapters/anthropic_native /
测试套）import 路径不变。

新代码请直接 `from liuruyan.llm.tool_id_contract import ...`。

为什么搬：harness 边界纪律 —— harness 永不 import backend.*。
P1-B-2 的 AnthropicApiClient（在 harness 里）需要 ensure_tool_id，
所以本模块的真身必须在 harness。
"""
from __future__ import annotations

from liuruyan.llm.tool_id_contract import ensure_tool_id, synthesize_tool_id

__all__ = ["ensure_tool_id", "synthesize_tool_id"]
