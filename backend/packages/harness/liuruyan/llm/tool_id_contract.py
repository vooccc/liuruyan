"""tool_use_id 跨层契约（P0 · 2026-05-02 → 搬迁到 harness · P1-B-1 · 2026-05-02）。

背景：
    Anthropic Messages API 要求 tool_use block 的 `id` 与对应 tool_result block
    的 `tool_use_id` 严格匹配。柳如烟的消息流经过 4 层：
        parser (slice 1-6) → anthropic_client → runtime_segments → llm_adapters
    以前每层都容忍空值、各自兜底，结果 LLM 返空 id 时多个调用碰撞到同一个
    "tool_0" 占位，Anthropic 400 "unexpected tool_use_id: tool_0"。

契约：
    1. **每一层入口必须保证 tool_use_id 非空非 None。**
    2. 发现空值 → 立即 synthesize 一个全局唯一 id（不复用已有占位），
       并通过 print("[TOOL-ID-SYNTHESIZED] ...") 打日志指明上游是谁。
    3. 兜底只是止血；打了 SYNTHESIZED 日志说明上游漏了 id，是 bug 信号。

本模块纯函数 + 无副作用（日志除外），任何层都可安全 import。

P1-B-1 搬迁说明：
    原位置 `backend/tool_id_contract.py` 现在是 thin shim，re-export 本模块。
    新代码 / 新 ApiClient 实现请直接 `from liuruyan.llm.tool_id_contract import ...`。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def synthesize_tool_id(origin: str) -> str:
    """生成一个跨调用全局唯一的 tool_use_id 兜底值。

    Anthropic 对 id 字符集只允许 [a-zA-Z0-9_-]，前缀 `toolu_` 保持可读性
    （真 Anthropic id 也是这个前缀，避免误导排查）。

    Args:
        origin: 兜底触发点的标识（用于日志归因），如 "parser_empty_id"、
            "anthropic_client_final_block"、"runtime_segment_tool_call"、
            "anthropic_adapter_tool_call"、"anthropic_adapter_tool_result"。

    Returns:
        形如 "toolu_synth_7f3a1b2c4d5e6f78" 的字符串。
    """
    synth = f"toolu_synth_{uuid.uuid4().hex[:16]}"
    logger.warning("[TOOL-ID-SYNTHESIZED] origin=%s id=%s", origin, synth)
    return synth


def ensure_tool_id(value: Any, origin: str) -> str:
    """保证返回非空 tool_use_id 字符串。空值/None → synthesize。

    **不做字符集清洗**——那是 Anthropic adapter 的职责；本函数只保证"非空"。

    Args:
        value: 入参原值（可能是 None / "" / 真 id）
        origin: 触发点标识（详见 synthesize_tool_id）

    Returns:
        非空字符串。
    """
    if value is None:
        return synthesize_tool_id(origin)
    s = str(value).strip()
    if not s:
        return synthesize_tool_id(origin)
    return s


__all__ = ["ensure_tool_id", "synthesize_tool_id"]
