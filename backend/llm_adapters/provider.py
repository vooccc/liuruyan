"""Provider 探测：根据 model/provider/endpoint 推断路由目标。

P1-D-2 升级（2026-05-02）：infer_provider_kind 内部优先查
ProviderRegistry——provider 参数命中 registry 时按 ProviderDef.transport
转 ProviderKind；未命中 fallback 现有"按字符串模糊匹配"规则。

调用方（agent.py）签名/语义不变，纯增量增强。
"""
from __future__ import annotations

import os
from typing import Literal

ProviderKind = Literal["anthropic", "openai_compat", "unknown"]

_PROVIDER_CACHE: dict[str, ProviderKind] = {}


def _transport_to_kind(transport: str) -> ProviderKind:
    """ProviderDef.transport → ProviderKind。"""
    if transport == "anthropic_messages":
        return "anthropic"
    if transport == "openai_chat":
        return "openai_compat"
    # 未知 transport 保守默认（未来扩展 gemini_native / bedrock_invoke 等
    # 时按需加映射）
    return "anthropic"


def _try_registry_lookup(provider: str | None) -> ProviderKind | None:
    """优先查 ProviderRegistry，命中返 ProviderKind，未命中或异常返 None。

    Note:
        try/except 包住 import + lookup —— 保证 registry 即使在迁移过程中
        某些环境没就绪也不破现有路径（fallback 兜底）。
    """
    if not provider:
        return None
    try:
        from liuruyan.llm import (
            ProviderNotFoundError,
            get_default_registry,
        )
        try:
            pdef = get_default_registry().get_provider(provider)
            return _transport_to_kind(pdef.transport)
        except ProviderNotFoundError:
            return None
    except ImportError:
        # harness 包不可用（极少数环境/启动顺序问题），直接 fallback
        return None


def infer_provider_kind(
    model: str | None = None,
    provider: str | None = None,
    endpoint: str | None = None,
) -> ProviderKind:
    """从 model / provider / endpoint 推断路由目标。

    优先级：
    1. ProviderRegistry 命中（provider 参数能查到 ProviderDef）→ transport 决定
    2. 现有 fallback 规则（按 model / provider / endpoint 字符串模糊匹配）

    fallback 规则：
    - model 含 "claude" / endpoint 含 "anthropic" → anthropic
    - provider 含 "xunfei" / "xf" / "星火" → openai_compat
    - endpoint 含 "openai" → openai_compat
    - model 含 "openai" / "gpt" → openai_compat
    - 其他 → anthropic（保守默认）

    缓存：按 (model, provider, endpoint) 联合 key 缓存结果。
    """
    key = f"{model or ''}:{provider or ''}:{endpoint or ''}"
    if key in _PROVIDER_CACHE:
        return _PROVIDER_CACHE[key]

    # P1-D-2: 优先走 ProviderRegistry
    registry_result = _try_registry_lookup(provider)
    if registry_result is not None:
        _PROVIDER_CACHE[key] = registry_result
        return registry_result

    # Fallback 现有规则
    result: ProviderKind = "anthropic"
    ep = (endpoint or "").lower()
    prov = (provider or "").lower()
    m = (model or "").lower()

    if "claude" in m or "anthropic" in ep or ep == "api.anthropic.com":
        result = "anthropic"
    elif "xunfei" in prov or "xf" in ep or "星火" in prov:
        result = "openai_compat"
    elif ep and ("openai" in ep or "api.openai" in ep):
        result = "openai_compat"
    elif "openai" in m or "gpt" in m:
        result = "openai_compat"
    else:
        result = "anthropic"

    _PROVIDER_CACHE[key] = result
    return result


def clear_provider_cache() -> None:
    """清除 provider 缓存（用于测试或动态切换配置）。"""
    global _PROVIDER_CACHE
    _PROVIDER_CACHE = {}
