"""v2 · H · P2-B-2 · AnthropicThinkingPlugin

按 Anthropic model id 自动路由 thinking 模式：
- Claude Opus 4.6 / 4.7+ → `{"thinking": {"type": "adaptive"}}`
- 其他 Claude reasoning 模型（opus-4-0~5 / sonnet-4 / haiku-4）→
  `{"thinking": {"type": "enabled", "budget_tokens": N}}`
- 不支持 reasoning 的模型 → `{"temperature": 0}`

源算法：基于 AnthropicClient._build_thinking_kwargs，支持思考模型推理调度规范。

为什么 adaptive vs budgeted 互斥：
- Anthropic SDK 不允许同时传 thinking 和 temperature
- adaptive 模式（Claude Opus 4.5+）由模型自决定 budget，不能传 budget_tokens
- budgeted 模式由调用方设 budget_tokens

输入契约：plugin 不需要额外 extras 字段，按 req.model 自动判断。

兼容性（P2-C 切换前）：
- 如果 caller 已经在 req.extras 显式塞了 "thinking" 或 "temperature"，plugin
  尊重 caller 的选择透传不覆盖
- P2-C 切换后 caller 不再塞，plugin 自动接管

环境变量：
- `ANTHROPIC_THINKING_BUDGET_TOKENS` 默认 2000 —— budgeted 模式用
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any, Dict

from ..api_client import CoreRequest, RequestContext


# Claude Opus 4.5+ adaptive thinking model 标识子串（model id lowercase 后包含其一）
_ADAPTIVE_VERSIONS = ("4-6", "4.6", "4-7", "4.7")

# Budgeted thinking model 标识子串
_BUDGETED_MODELS = (
    "opus-4-0", "opus-4.0",
    "opus-4-1", "opus-4.1",
    "opus-4-2", "opus-4.2",
    "opus-4-3", "opus-4.3",
    "opus-4-4", "opus-4.4",
    "opus-4-5", "opus-4.5",
    "sonnet-4", "haiku-4",
)


class AnthropicThinkingPlugin:
    """Anthropic thinking / temperature 自动路由 plugin。

    搬自 backend AnthropicClient._build_thinking_kwargs 的算法。
    """

    name = "anthropic_thinking"

    def transform_request(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> CoreRequest:
        # caller 已经显式塞了 thinking 或 temperature → 尊重，不覆盖
        # （兼容 P2-C 切换前 caller 也算的状态；切换后 caller 不再塞）
        if "thinking" in req.extras or "temperature" in req.extras:
            return req

        thinking_kwargs = self._compute_thinking_kwargs(req.model)
        new_extras = {**req.extras, **thinking_kwargs}
        return dataclasses.replace(req, extras=new_extras)

    @staticmethod
    def _compute_thinking_kwargs(model: str) -> Dict[str, Any]:
        """按 model id 决定 thinking 配置。

        Returns:
            dict 含 "thinking" 或 "temperature" 之一（互斥）。
        """
        model_id = (model or "").lower()

        # 1. Claude Opus 4.5+ → adaptive thinking
        # is_adaptive_thinking_model：字串包含 4-6/4.6/4-7/4.7 + opus
        if "opus" in model_id and any(v in model_id for v in _ADAPTIVE_VERSIONS):
            return {"thinking": {"type": "adaptive"}}

        # 2. 其他 Claude reasoning 模型 → budgeted thinking
        if "claude" in model_id:
            if any(m in model_id for m in _BUDGETED_MODELS):
                budget = int(os.getenv("ANTHROPIC_THINKING_BUDGET_TOKENS", "2000"))
                return {"thinking": {"type": "enabled", "budget_tokens": budget}}

        # 3. 不支持 reasoning 的模型 → 不传 thinking，允许传 temperature
        return {"temperature": 0}


__all__ = ["AnthropicThinkingPlugin"]
