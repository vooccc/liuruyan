"""TTS 凭证解析：从 providers store 借 api_key + base_url + extra_headers。

设计动机
========
M1.0 让用户在 TTSPane 单独填 base_url + api_key，结果用户问"这跟模型服务那块通了
吗？"——答案是**没通**，体验差且容易让用户重复填同一份凭证。

借鉴 voice_routes.py 的做法：**TTS 仅持有 provider_id 指针**，运行时去 providers
store 借 base_url / api_key / extra_headers。用户在「模型服务」配一次，所有"借口"
都能用（chat 主流程、语音转写、TTS、未来的 utility 翻译等）。

零污染纪律
==========
本模块**只读** providers store，不写入。TTSConfig 仍是独立 ~/.liuruyan/tts.json，
撤除 TTS 时无需动 providers schema。

Fallback 链
===========
1. cfg.provider_id 非空 → 借 providers store（推荐）
2. provider_id 为空 但 cfg.base_url 非空 → fallback 旧 M1.0 直填字段（兼容老用户）
3. 全空 → 返空 Credentials，调用方应抛 TTSError 引导用户去配
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

from .config import TTSConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Credentials:
    """合成请求实际用到的凭证三件套。

    全部可能是空字符串/空 dict：
    - openai-edge-tts wrapper 不要 key（仍能跑）
    - extra_headers 一般是空，部分聚合站需要自定义 X-XXX
    """

    base_url: str = ""
    api_key: str = ""
    extra_headers: Dict[str, str] = field(default_factory=dict)


def resolve_credentials(cfg: TTSConfig) -> Credentials:
    """根据 TTSConfig 决定最终用哪份凭证。"""

    # 路径 1：provider_id 走 providers store
    pid = (cfg.provider_id or "").strip()
    if pid:
        creds = _from_providers_store(pid)
        if creds is not None:
            return creds
        # provider_id 配了但 store 里查无此 id（被用户删除？）→ 退到 fallback
        logger.warning(
            "[TTS-CREDS] provider_id=%s not found in providers store, falling back to legacy fields",
            pid,
        )

    # 路径 2：旧版直填字段
    if cfg.base_url:
        return Credentials(
            base_url=cfg.base_url,
            api_key=cfg.api_key or "",
            extra_headers={},
        )

    return Credentials()


def _from_providers_store(provider_id: str) -> Optional[Credentials]:
    """去 providers store 拿凭证；查无此 id 返 None。"""
    try:
        from backend.providers.store import load_providers_config_cached
    except Exception as e:  # pragma: no cover
        logger.error("[TTS-CREDS] failed to import providers store: %s", e)
        return None

    cfg = load_providers_config_cached()
    pc = cfg["providers"].get(provider_id)
    if pc is None:
        return None
    return Credentials(
        base_url=pc.base_url or "",
        api_key=pc.api_key or "",
        extra_headers=dict(pc.extra_headers or {}),
    )


__all__ = ["Credentials", "resolve_credentials"]
