"""免费模型判定（多策略融合）。

策略优先级：
1. **pricing 字段**（OpenRouter 风格）—— prompt + completion 都为 "0" 即免费
2. **id 后缀**：`:free` / `-free` / `_free`
3. **id/name 含 "free"**（cherry-studio 风格 fallback）
4. **白名单兜底**：硅基流动、智谱免费版、Gemini Flash 等已知公开免费模型

设计参考：
- _research/QwenPaw-main src/qwenpaw/providers/provider_manager.py:_is_free_model
- _research/cherry-studio-main src/renderer/src/utils/model.ts:isFreeModel

返回：
- (is_free: bool, reason: str)  reason 用于前端 hover 提示来源
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple


# 已知公开免费模型 id 子串白名单（小写）
_FREE_MODEL_HINTS = (
    "qwen2.5-7b-instruct",       # 硅基流动免费
    "qwen2-7b-instruct",
    "deepseek-v2.5",             # 部分聚合商免费
    "glm-4-flash",               # 智谱完全免费
    "glm-4-flashx",
    "gemini-1.5-flash",          # Google 免费配额
    "gemini-2.0-flash",
    "gemini-flash",
    "internlm",                  # 通常免费
)


def _pricing_is_zero(pricing: Optional[Dict[str, Any]]) -> bool:
    """OpenRouter 风格 pricing 字段判定。

    pricing = {
        "prompt": "0", "completion": "0",
        "image": "0", "request": "0", ...
    }
    全是 "0" / 0 / "0.0" 才算免费。
    """
    if not pricing or not isinstance(pricing, dict):
        return False
    important = ("prompt", "completion")
    for key in important:
        if key not in pricing:
            return False
        try:
            val = float(pricing[key])
        except (TypeError, ValueError):
            return False
        if val > 0:
            return False
    return True


_FREE_SUFFIX_RE = re.compile(r"[:\-_]free\b", re.IGNORECASE)
_FREE_TOKEN_RE = re.compile(r"\bfree\b", re.IGNORECASE)


def is_free_model(
    model_id: str,
    name: Optional[str] = None,
    pricing: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """判定一个模型是否免费。

    Args:
        model_id: 模型 id（如 "deepseek/deepseek-chat-v3.1:free"）
        name: 显示名（可选，参与文本匹配）
        pricing: 厂商返回的 pricing 字段（可选，OpenRouter / SiliconFlow 等有）

    Returns:
        (is_free, reason)
            - reason: "pricing" / "suffix" / "token" / "whitelist" / ""
    """
    if not model_id:
        return False, ""

    # 1. pricing
    if _pricing_is_zero(pricing):
        return True, "pricing"

    blob = f"{model_id} {name or ''}".lower()

    # 2. 后缀（最强信号，避免误把"freeman"模型当免费）
    if _FREE_SUFFIX_RE.search(blob):
        return True, "suffix"

    # 3. 含 "free" token
    if _FREE_TOKEN_RE.search(blob):
        return True, "token"

    # 4. 白名单
    for hint in _FREE_MODEL_HINTS:
        if hint in blob:
            return True, "whitelist"

    return False, ""


__all__ = ["is_free_model"]
