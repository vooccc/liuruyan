"""LLM 纠错润色（polish pass）：ASR final 句先过一遍便宜 LLM 再上屏。

- 修错别字 / 同音字 / 标点，不改变原意、不增删内容、不回答句子内容。
- 用 OpenAI 兼容 chat 接口（provider base_url + key 复用 providers store）。
- 任何失败（没 key / 网络 / 超时）都静默回退原文——纠错是增强，不能阻断输入。
"""
from __future__ import annotations

import logging
from typing import Optional

from .config import VoiceStreamConfig, resolve_api_key

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是语音识别结果校对器。用户会给你一句语音转写的原始文本，"
    "你只做一件事：修正错别字、同音字错误和标点，让句子通顺。"
    "严格要求：不改变原意、不增删内容、不回答或续写内容、"
    "不添加任何解释或引号，只输出修正后的文本。如果原文没有错误，原样输出。"
)

_TIMEOUT = 12.0


async def polish_text(text: str, cfg: VoiceStreamConfig) -> str:
    """润色一句话。失败/超时/空返回原文。"""
    key = resolve_api_key(cfg.polish_provider_id)
    if not key:
        return text
    base_url = _resolve_base_url(cfg.polish_provider_id)
    if not base_url:
        return text
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=base_url, api_key=key, timeout=_TIMEOUT, max_retries=0)
        resp = await client.chat.completions.create(
            model=cfg.polish_model or "qwen-turbo",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out or text
    except Exception as e:
        logger.warning("voice_stream polish failed (fallback to raw): %s", e)
        return text


def _resolve_base_url(provider_id: str) -> Optional[str]:
    """取 provider 的 OpenAI 兼容 base_url（chat 用什么，润色就用什么）。"""
    try:
        from ..providers.store import get_provider

        pc = get_provider(provider_id)
        if pc is None:
            return None
        return (pc.base_url or "").strip() or None
    except Exception:
        return None
