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
try:
    from liuruyan.llm.tool_id_contract import ensure_tool_id, synthesize_tool_id
except ImportError:
    import logging
    import uuid
    from typing import Any

    _logger = logging.getLogger(__name__)

    def synthesize_tool_id(origin: str) -> str:
        synth = f"toolu_synth_{uuid.uuid4().hex[:16]}"
        _logger.warning("[TOOL-ID-SYNTHESIZED] origin=%s id=%s", origin, synth)
        return synth

    def ensure_tool_id(value: Any, origin: str) -> str:
        if value is None:
            return synthesize_tool_id(origin)
        s = str(value).strip()
        if not s:
            return synthesize_tool_id(origin)
        return s

__all__ = ["ensure_tool_id", "synthesize_tool_id"]
