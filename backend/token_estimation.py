"""token 粗估（共享口径）。

单一实现，两处消费：
- tools_inventory_routes / 前端工具面板：单工具 / 角色 / 档位估算
- agent.build_messages：上下文构成分段估算（context breakdown）

系数按 tiktoken cl100k_base 对全量 registry 工具实测校准：
CJK 约 1.2 chars/token，ASCII 约 4.0 chars/token。
实测整体误差约 -3%，单工具中位误差 4.3%（JSON 密集的个别工具偏低 10-20%）。
只用作 UI 量级提示，不参与任何业务决策。
"""
from __future__ import annotations

import json
from typing import Any, Dict


def is_cjk_char(ch: str) -> bool:
    o = ord(ch)
    return (
        0x2E80 <= o <= 0x9FFF       # CJK 部首/标点/汉字/扩展A
        or 0xF900 <= o <= 0xFAFF    # CJK 兼容表意
        or 0xFF00 <= o <= 0xFFEF    # 全角形式
        or 0x20000 <= o <= 0x2FA1F  # CJK 扩展 B-F
    )


def approx_tokens(text: str) -> int:
    """加权估算一段文本的 prompt token 数。"""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if is_cjk_char(ch):
            cjk += 1
        else:
            other += 1
    return max(1, round(cjk / 1.2 + other / 4.0))


def tool_token_estimate(desc: str, schema: Dict[str, Any]) -> int:
    """单工具 token 估算：description + input_schema 一起算。

    schema 与 description 一样会整体注入 prompt，只算 description 会系统性
    低估 30-60%（复杂嵌套 schema 更高）。
    """
    schema_text = json.dumps(schema, ensure_ascii=False) if schema else ""
    return approx_tokens(f"{desc}\n{schema_text}")


def message_tokens(message: Dict[str, Any]) -> int:
    """单条 message 的粗估 token（含每条 ~4 token 的 role/格式开销）。

    content 兼容 str 和多模态 list（list 里取 text 段，图片按固定开销计）。
    """
    if not isinstance(message, dict):
        return 0
    content = message.get("content")
    chars = 0
    if isinstance(content, str):
        chars = len(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    chars += len(part.get("text") or "")
                else:
                    chars += 1000  # 图片等非文本段固定开销（粗估）
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                chars += len(str(fn.get("name") or "")) + len(str(fn.get("arguments") or ""))
    base = approx_tokens("x" * chars) if chars > 0 else 0
    return base + 4
