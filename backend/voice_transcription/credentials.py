"""语音转写凭证解析：只读复用 providers store，支持自定义直填与环境变量兜底。

优先级：
1. cfg.provider_id 非空 → 从 providers store 借 base_url / api_key / extra_headers
2. cfg.base_url 或 cfg.api_key 非空 → 使用面板自定义端点与密钥
3. 环境变量 OPENAI_API_KEY (+ 可选 OPENAI_BASE_URL) 兜底
4. 全部为空 → 返回空 Credentials
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Optional

from .config import VoiceTranscriptionConfig

logger = logging.getLogger(__name__)

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class Credentials:
    """OpenAI 兼容语音转写接口凭证。"""

    base_url: str = ""
    api_key: str = ""
    extra_headers: Dict[str, str] = field(default_factory=dict)
    source: str = "none"  # providers_store | custom_fields | env_fallback | none


def resolve_credentials(cfg: VoiceTranscriptionConfig) -> Credentials:
    """按优先级解析 OpenAI 兼容端点凭证。"""
    pid = (cfg.provider_id or "").strip()
    if pid:
        creds = _from_providers_store(pid)
        if creds is not None:
            return creds
        logger.warning(
            "[VOICE-TRANSCRIPTION] provider_id=%s not found in providers store, falling back",
            pid,
        )

    custom_url = (cfg.base_url or "").strip()
    custom_key = (cfg.api_key or "").strip()
    if custom_url or custom_key:
        return Credentials(
            base_url=custom_url or DEFAULT_OPENAI_BASE_URL,
            api_key=custom_key,
            extra_headers={},
            source="custom_fields",
        )

    env_key = os.getenv("OPENAI_API_KEY", "").strip()
    env_url = os.getenv("OPENAI_BASE_URL", "").strip()
    if env_key or env_url:
        return Credentials(
            base_url=env_url or DEFAULT_OPENAI_BASE_URL,
            api_key=env_key,
            extra_headers={},
            source="env_fallback",
        )

    return Credentials(source="none")


def env_fallback_info() -> Dict[str, object]:
    """返回环境变量兜底状态（脱敏，供前端展示）。"""
    env_key = os.getenv("OPENAI_API_KEY", "").strip()
    env_url = os.getenv("OPENAI_BASE_URL", "").strip()
    return {
        "has_openai_api_key": bool(env_key),
        "base_url": env_url or DEFAULT_OPENAI_BASE_URL,
    }


def _from_providers_store(provider_id: str) -> Optional[Credentials]:
    try:
        from backend.providers.store import load_providers_config_cached
    except Exception as e:  # pragma: no cover
        logger.error("[VOICE-TRANSCRIPTION] failed to import providers store: %s", e)
        return None

    store = load_providers_config_cached()
    pc = store.get("providers", {}).get(provider_id)
    if pc is None:
        return None
    return Credentials(
        base_url=(pc.base_url or "").strip(),
        api_key=(pc.api_key or "").strip(),
        extra_headers=dict(pc.extra_headers or {}),
        source="providers_store",
    )


__all__ = ["Credentials", "DEFAULT_OPENAI_BASE_URL", "resolve_credentials", "env_fallback_info"]
