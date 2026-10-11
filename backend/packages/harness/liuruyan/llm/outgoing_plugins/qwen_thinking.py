"""v2 · H · P2-D · QwenThinkingPlugin

Qwen3 系列 model 的 thinking 模式软开关：通过末条 user message 末尾追加
``/think`` 或 ``/no_think`` 关键词控制本次响应是否带 reasoning。

Qwen3 协议背景：
- Qwen3 hybrid model（如 qwen3-30b-a3b / qwen3-72b-instruct 等）支持双模式
- 末条 user message 末尾追 ``/think``：本次响应包含 reasoning（thinking_content）
- 末条 user message 末尾追 ``/no_think``：本次响应不包含 reasoning（更省 token）
- 都不追：走 vendor 配置默认（默认通常 no_think，hybrid 行为）

设计选择 — 默认 no-op + caller 显式 opt-in：
- 默认 plugin 不修改 messages（透传），保证现有 caller 0 行为变化
- caller 通过 `extras["qwen_thinking_mode"]` 显式控制：
  * `"think"` → 追 `/think`
  * `"no_think"` → 追 `/no_think`
  * 其他值 / 缺字段 → no-op（透传）

为什么不默认追 /no_think：
- 没有真机验证 Qwen3 各种 base_url（百炼 / SiliconFlow / OpenRouter aggregator）
  对 `/think` 协议的兼容度，强行默认改 messages 可能在第三方 endpoint 上出错
- 默认 no-op + caller 显式 opt-in 是最安全形态，未来用真机验证后再改默认

边界 case：
- 末条 user 的 content 是 list（multimodal）：plugin 不动（避免破坏图片块结构）
- messages 末尾不是 user role（系统设计上 user→assistant 收尾）：plugin 不动
- messages 为空：plugin 不动
- content 已含 `/think` 或 `/no_think` 后缀：plugin 不重复追加

参考：通用供应商模型列表 `qwenThinkingPlugin`（已废弃但可参考路由风格）。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List

from ..api_client import CoreRequest, RequestContext


_VALID_MODES = ("think", "no_think")
_TRAILER_TOKENS = ("/think", "/no_think")


class QwenThinkingPlugin:
    """Qwen3 hybrid thinking 软开关 plugin。

    仅 Qwen3 系列 model 启用（builder 注册时用 predicate 路由）；本类不再
    判 model id（builder 已守门）。
    """

    name = "qwen_thinking"

    def transform_request(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> CoreRequest:
        mode = req.extras.get("qwen_thinking_mode")
        if mode not in _VALID_MODES:
            return req

        new_messages = self._maybe_append_trailer(req.messages, mode)
        if new_messages is req.messages:
            return req  # 不可改（如末条非 user / content 是 list / 已有 trailer）

        # 消费后从 extras 移除（避免下游 plugin 误用 / log 泄漏意图）
        new_extras = {**req.extras}
        new_extras.pop("qwen_thinking_mode", None)

        return dataclasses.replace(req, messages=new_messages, extras=new_extras)

    @staticmethod
    def _maybe_append_trailer(
        messages: List[Dict[str, Any]],
        mode: str,
    ) -> List[Dict[str, Any]]:
        """末条 user.content 是 str 时追加 trailer；否则返原 messages。"""
        if not messages:
            return messages

        last = messages[-1]
        if last.get("role") != "user":
            return messages

        content = last.get("content")
        if not isinstance(content, str):
            return messages

        # 已有 trailer 不重复追加
        stripped = content.rstrip()
        if any(stripped.endswith(t) for t in _TRAILER_TOKENS):
            return messages

        trailer = f"/{mode}"
        new_content = f"{content}\n\n{trailer}" if content else trailer

        new_last = {**last, "content": new_content}
        return messages[:-1] + [new_last]


__all__ = ["QwenThinkingPlugin"]
