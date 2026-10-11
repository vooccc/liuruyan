"""ModelFetcher 注册表 + 内置 fetcher 实现。

借鉴 通用供应商模型列表 listModels.ts 的"策略注册表"设计：
- 每个 fetcher 声明自己 match 哪种 provider（按 id）
- 第一命中胜出，永远 fallback 到 openai_compatible_fetcher
- match 顺序在 FETCHERS 注册时固定

API：
    fetched = await discover_models(provider_config)
    # → List[DiscoveredModel]，含 id / name / tags / is_free / pricing / context_length

支持的 fetcher：
- openrouter   → /api/v1/models（含 pricing → free 判定）
- ollama       → /api/tags（本地 OpenAI 兼容协议外的私有协议）
- openai_compat（fallback）→ /v1/models（带 Bearer token）

用户决策（2026-05-04）：
- 不强制要 api_key（与 ProviderConfig.require_api_key=false 配套）
  → 公开端点（OpenRouter / Ollama）允许空 Key 拉
  → 私有端点（OpenAI / Anthropic）空 Key 时返 401，路由层捕获返友好错误
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from .capabilities import infer_capabilities, infer_context_length
from .free_detection import is_free_model
from .schema import ProviderConfig


@dataclass
class DiscoveredModel:
    """从远端拉到的单个模型条目（前端导入前的预览数据）。"""

    id: str
    name: str = ""
    owned_by: str = ""
    description: str = ""
    is_free: bool = False
    free_reason: str = ""  # "pricing" / "suffix" / "token" / "whitelist"
    tags: List[str] = field(default_factory=list)
    pricing: Optional[Dict[str, Any]] = None
    context_length: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "owned_by": self.owned_by,
            "description": self.description,
            "is_free": self.is_free,
            "free_reason": self.free_reason,
            "tags": self.tags,
            "pricing": self.pricing,
            "context_length": self.context_length,
        }


FetcherFunc = Callable[[ProviderConfig, httpx.AsyncClient], Awaitable[List[DiscoveredModel]]]


@dataclass
class ModelFetcher:
    """单个 fetcher 注册项。"""

    name: str
    match: Callable[[ProviderConfig], bool]
    fetch: FetcherFunc


# ── 工具：构造 headers ────────────────────────────────────────────────


def _auth_headers(provider: ProviderConfig) -> Dict[str, str]:
    # 起手注入项目级默认 headers（标准兼容 User-Agent 及来源标识），
    # 防 OpenRouter / SiliconFlow 等聚合 router 在 /models 列表端点也做反爬。
    from backend.utils.llm_headers import get_default_llm_headers
    headers: Dict[str, str] = get_default_llm_headers(transport=provider.transport)
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
        headers["X-Api-Key"] = provider.api_key
        headers["x-api-key"] = provider.api_key
    if (provider.transport or "").lower() == "anthropic_messages":
        headers.setdefault("anthropic-version", "2023-06-01")
    # provider.extra_headers 优先级最高（用户显式配的），覆盖默认 + auth headers。
    if provider.extra_headers:
        headers.update(provider.extra_headers)
    return headers


# URL 规范化集中在 backend.utils.api_host（对齐 Cherry packages/shared/utils/api/utils.ts）。
# 历史上本文件曾自带一个简化版 format_api_host + _API_VERSION_RE，后被通用模块取代；
# 这里 re-export 保持对外签名稳定（test_provider_fetcher_url_format.py 等仍可 import）。
from backend.utils.api_host import format_api_host  # noqa: F401  (re-export)


def _strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


def _enrich(model_id: str, name: str, pricing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """通用富化：按 id/name 推 free + tags + context_length。"""
    free, reason = is_free_model(model_id, name, pricing)
    tags = infer_capabilities(model_id, name)
    if free and "free" not in tags:
        tags.insert(0, "free")
    return {
        "is_free": free,
        "free_reason": reason,
        "tags": tags,
        "context_length": infer_context_length(model_id),
    }


# ── 内置 fetcher 实现 ────────────────────────────────────────────────


async def _fetch_openrouter(
    provider: ProviderConfig, client: httpx.AsyncClient
) -> List[DiscoveredModel]:
    """OpenRouter /api/v1/models 拉取（公开端点，免 Key 也能拿）。

    响应字段含 pricing.prompt / pricing.completion / context_length / description。
    """
    base = _strip_trailing_slash(provider.base_url) or "https://openrouter.ai/api/v1"
    url = f"{base}/models"
    response = await client.get(url, headers=_auth_headers(provider), timeout=20.0)
    response.raise_for_status()
    data = response.json()

    raw = data.get("data") or []
    out: List[DiscoveredModel] = []
    seen: set[str] = set()
    for m in raw:
        mid = m.get("id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        pricing = m.get("pricing") or {}
        meta = _enrich(mid, m.get("name", "") or "", pricing)
        out.append(
            DiscoveredModel(
                id=mid,
                name=m.get("name") or mid,
                description=m.get("description") or "",
                pricing=pricing or None,
                is_free=meta["is_free"],
                free_reason=meta["free_reason"],
                tags=meta["tags"],
                context_length=m.get("context_length") or meta["context_length"],
            )
        )
    return out


async def _fetch_ollama(
    provider: ProviderConfig, client: httpx.AsyncClient
) -> List[DiscoveredModel]:
    """Ollama /api/tags（私有协议，不走 OpenAI /v1/models）。

    注意：Ollama 的 base_url 在柳如烟里是 `http://localhost:11434/v1`，
    我们要先剥掉 `/v1` 才能访问 `/api/tags`。
    """
    base = _strip_trailing_slash(provider.base_url) or "http://localhost:11434"
    if base.endswith("/v1"):
        base = base[:-3]
    if base.endswith("/api"):
        base = base[:-4]
    url = f"{base}/api/tags"
    response = await client.get(url, timeout=10.0)
    response.raise_for_status()
    data = response.json()

    out: List[DiscoveredModel] = []
    seen: set[str] = set()
    for m in data.get("models", []) or []:
        mid = m.get("name")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        meta = _enrich(mid, mid)
        out.append(
            DiscoveredModel(
                id=mid,
                name=mid,
                owned_by="ollama",
                is_free=True,           # 本地模型一律标 free
                free_reason="local",
                tags=meta["tags"] + (["free"] if "free" not in meta["tags"] else []),
                context_length=meta["context_length"],
            )
        )
    return out


async def _fetch_openai_compat(
    provider: ProviderConfig, client: httpx.AsyncClient
) -> List[DiscoveredModel]:
    """OpenAI / Anthropic 兼容协议 /v1/models（fallback，覆盖 99% 场景）。

    URL 规范化对齐 通用供应商模型列表：
      - 用户填 https://llhub.top → 自动补成 https://llhub.top/v1
      - 用户填 https://llhub.top/v1 → 不重复加
      - 用户填 https://llhub.top# → 锁原样不补版本

    Anthropic 官方 /v1/models 与 OpenAI 同样返回 data[].id，可共用解析；
    transport=anthropic_messages 时 _auth_headers 会自动加 anthropic-version。
    """
    if not provider.base_url:
        raise ValueError(f"provider {provider.id} 缺少 base_url")
    base = format_api_host(provider.base_url)
    if not base:
        raise ValueError(f"provider {provider.id} base_url 无效")
    url = f"{base}/models"
    response = await client.get(url, headers=_auth_headers(provider), timeout=20.0)
    response.raise_for_status()
    data = response.json()
    raw = data.get("data") or data.get("models") or []

    out: List[DiscoveredModel] = []
    seen: set[str] = set()
    for m in raw:
        if isinstance(m, str):
            mid = m
            name = m
            owned_by = ""
        else:
            mid = m.get("id") or m.get("model") or ""
            # Anthropic 用 display_name，OpenAI 用 name；都没有就用 id
            name = m.get("name") or m.get("display_name") or mid
            owned_by = m.get("owned_by") or ""
        if not mid or mid in seen:
            continue
        seen.add(mid)
        meta = _enrich(mid, name)
        out.append(
            DiscoveredModel(
                id=mid,
                name=name,
                owned_by=owned_by,
                is_free=meta["is_free"],
                free_reason=meta["free_reason"],
                tags=meta["tags"],
                context_length=meta["context_length"],
            )
        )
    out.sort(key=lambda x: x.id)
    return out


# ── 注册表 ────────────────────────────────────────────────────────────


FETCHERS: List[ModelFetcher] = [
    ModelFetcher(
        name="openrouter",
        match=lambda p: p.id == "openrouter",
        fetch=_fetch_openrouter,
    ),
    ModelFetcher(
        name="ollama",
        match=lambda p: p.id == "ollama" or "11434" in (p.base_url or ""),
        fetch=_fetch_ollama,
    ),
    # 永远最后：OpenAI 兼容兜底
    ModelFetcher(
        name="openai_compat",
        match=lambda p: True,
        fetch=_fetch_openai_compat,
    ),
]


def select_fetcher(provider: ProviderConfig) -> ModelFetcher:
    """按注册顺序找第一个 match 的 fetcher。"""
    for f in FETCHERS:
        if f.match(provider):
            return f
    return FETCHERS[-1]


# ── 公开 API ──────────────────────────────────────────────────────────


def _detect_system_proxy(target_url: str) -> Optional[str]:
    """读系统代理（对齐行业通用标准 的 'system' 模式行为）。

    - Windows：`urllib.request.getproxies()` 自动从注册表读 IE/系统代理
      （也就是 v2rayN / Clash / SwitchyOmega 设的那个）
    - macOS/Linux：读 HTTPS_PROXY / HTTP_PROXY env var
    - 本地地址（localhost / 127.0.0.1）一律不走代理

    Args:
        target_url: 目标 URL，用于判断是否本地地址

    Returns:
        proxy URL（如 http://127.0.0.1:7890）或 None
    """
    import urllib.request
    # 本地地址绕开代理（Ollama / LM Studio 必须直连）
    lowered = target_url.lower()
    if "localhost" in lowered or "127.0.0.1" in lowered or "://0.0.0.0" in lowered:
        return None
    proxies = urllib.request.getproxies()
    return proxies.get("https") or proxies.get("http") or None


async def discover_models(
    provider: ProviderConfig,
    client: Optional[httpx.AsyncClient] = None,
) -> List[DiscoveredModel]:
    """对一个 provider 拉取模型列表。

    Args:
        provider: 完整 ProviderConfig（含 api_key + extra_headers）
        client: 可选共享 httpx 客户端（测试可注入 mock）

    Returns:
        DiscoveredModel 列表（每个含 free/tags/pricing/context_length）

    Raises:
        httpx.HTTPStatusError: HTTP 4xx/5xx
        ValueError: 配置缺关键字段（如 base_url）
        其他 httpx 异常
    """
    fetcher = select_fetcher(provider)
    if client is not None:
        return await fetcher.fetch(provider, client)
    # 自建 client：对齐行业通用标准 行为，自动读系统代理
    # OpenAI / Anthropic / Gemini 等海外端点在国内必须走代理才能连
    proxy = _detect_system_proxy(provider.base_url or "")
    async with httpx.AsyncClient(proxy=proxy) as own_client:
        return await fetcher.fetch(provider, own_client)


__all__ = [
    "DiscoveredModel",
    "ModelFetcher",
    "FETCHERS",
    "select_fetcher",
    "discover_models",
    "format_api_host",
]
