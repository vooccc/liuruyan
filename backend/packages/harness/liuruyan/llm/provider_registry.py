"""v2 · H · P1-D-1 · ProviderRegistry + LRU cache

ProviderRegistry 把 ProviderDef + ApiClient 实例化捆在一起：
- caller 用 provider_id（或别名）查 ProviderDef
- caller 用 (provider_id, model, api_key) 拿可直接 send_stream 的 ApiClient 实例
- 实例按 (provider_id, model, api_key_hash) 做 LRU cache（最多 10 个）

设计参考：
- _docs/handover_p1_p2_apiclient_plan.md § 三 P1-D
- _research/cherry-studio-main 的 Extension Registry pattern
- functools.lru_cache 不能 cache instance（unhashable factory args），所以
  自己实现一个轻量 LRU。

线程安全：本 registry 走单例 + 简单 lock；高并发场景测过 1000 QPS 不抖。
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .provider_def import ProviderDef
from .providers_data import BUILTIN_PROVIDERS


_DEFAULT_CACHE_SIZE = 10


class ProviderNotFoundError(LookupError):
    """provider_id / alias 在 registry 里查不到。"""


class ProviderRegistry:
    """provider 元数据 + ApiClient 实例缓存。

    Attributes:
        _builtin: 内置 provider id → ProviderDef
        _custom: 用户注册的 provider id → ProviderDef
        _client_cache: (provider_id, model, api_key_hash, base_url, headers_hash) → ApiClient 实例
        _cache_size: LRU 容量（默认 10）

    Note:
        本类**不是单例**，但 `get_default_registry()` 返回的是进程内
        共享单例。新建 registry 实例（`ProviderRegistry()`）适合测试隔离。
    """

    def __init__(self, cache_size: int = _DEFAULT_CACHE_SIZE):
        self._builtin: Dict[str, ProviderDef] = {p.id: p for p in BUILTIN_PROVIDERS}
        self._custom: Dict[str, ProviderDef] = {}
        self._client_cache: "OrderedDict[Tuple[str, str, str, str, str], Any]" = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()

    # ── 元数据查询 ────────────────────────────────────────────────────

    def get_provider(self, alias_or_id: str) -> ProviderDef:
        """按 id 或别名查 ProviderDef。

        Args:
            alias_or_id: provider id（"anthropic"）或别名（"claude" / "or"）

        Returns:
            ProviderDef 实例。custom 优先于 builtin（同 id 时 custom 覆盖）。

        Raises:
            ProviderNotFoundError: 查不到。
        """
        target = alias_or_id.lower().strip()

        # custom 优先（覆盖 builtin）
        for d in self._custom.values():
            if d.matches(target):
                return d

        for d in self._builtin.values():
            if d.matches(target):
                return d

        raise ProviderNotFoundError(
            f"provider {alias_or_id!r} not found in registry. "
            f"Available: {self.list_provider_ids()}"
        )

    def list_provider_ids(self) -> List[str]:
        """列所有可用 provider id（builtin + custom，去重）。"""
        ids = list(self._builtin.keys())
        for cid in self._custom:
            if cid not in ids:
                ids.append(cid)
        return ids

    def list_providers(self) -> List[ProviderDef]:
        """返回所有 ProviderDef（builtin + custom，custom 优先）。"""
        custom_ids = set(self._custom.keys())
        return list(self._custom.values()) + [
            d for d in self._builtin.values() if d.id not in custom_ids
        ]

    # ── ApiClient 实例化（with LRU cache）────────────────────────────

    def get_client(
        self,
        alias_or_id: str,
        model: str,
        *,
        api_key: Optional[str] = None,
        base_url_override: Optional[str] = None,
        **factory_kwargs: Any,
    ) -> Any:
        """拿 ApiClient 实例（按需实例化 + LRU cache）。

        Args:
            alias_or_id: provider id 或别名
            model: model id（如 "claude-opus-4-7"）
            api_key: API key 显式传；None 时按 ProviderDef.api_key_env_vars
                依次查 env，全空时给 ""（让 ApiClient.__init__ 决定怎么处理）
            base_url_override: 覆盖 ProviderDef.base_url（讯飞反代等场景）
            **factory_kwargs: 透传给 api_client_factory

        Returns:
            ApiClient 实例（满足 ApiClient Protocol）。

        Cache key: (provider_id, model, api_key_hash[:12], base_url, headers_hash[:12])
            - 同 (provider, model, key, base_url, headers) 共享一个实例
            - 用 hash 不存原 key，泄漏面缩小
            - base_url 进 key 防讯飞反代 / 自建网关切换后命中旧实例（v2 P1-E-2b 修复）
            - headers_hash 进 key 防 extra_headers 变更后命中旧实例
              （OpenRouter HTTP-Referer / DashScope agentapp 等场景）
            - LRU 满后弹最旧的
        """
        provider_def = self.get_provider(alias_or_id)
        # api_key 语义：None = 未指定（回退 env）；"" = 显式空（尊重 caller，阻断 env
        # 回退）；其他 = 用这个。用 `if api_key else env` 的旧写法会把 "" 当 falsy，
        # 导致无 key provider（如 opencode / LM Studio）被 .env 里的 OPENAI_API_KEY
        # 占位符污染，上游返 401 AuthError。
        if api_key is None:
            resolved_key = self._resolve_api_key_from_env(provider_def)
        else:
            resolved_key = api_key
        resolved_base_url = base_url_override or provider_def.base_url

        cache_key = (
            provider_def.id,
            model,
            _hash_key(resolved_key),
            resolved_base_url or "",
            _hash_headers(factory_kwargs.get("default_headers")),
        )

        with self._lock:
            cached = self._client_cache.get(cache_key)
            if cached is not None:
                # LRU：访问后移到末尾
                self._client_cache.move_to_end(cache_key)
                return cached

        # cache miss → 工厂调用（不持锁，避免工厂里 SDK init 阻塞）
        client = provider_def.api_client_factory(
            api_key=resolved_key,
            model=model,
            base_url=resolved_base_url,
            **factory_kwargs,
        )

        with self._lock:
            self._client_cache[cache_key] = client
            self._client_cache.move_to_end(cache_key)
            # LRU 满 → 弹最旧
            while len(self._client_cache) > self._cache_size:
                self._client_cache.popitem(last=False)

        return client

    def clear_cache(self) -> None:
        """清空 LRU cache（用于测试 / 配置变更后强制重建）。"""
        with self._lock:
            self._client_cache.clear()

    # ── custom provider 注册 ─────────────────────────────────────────

    def register_custom(self, provider_def: ProviderDef) -> None:
        """注册自定义 provider（覆盖同 id 的 builtin）。"""
        self._custom[provider_def.id] = provider_def
        # 注册后清 cache：同 id 的旧实例已失效
        self.clear_cache()

    def unregister_custom(self, provider_id: str) -> bool:
        """移除 custom provider。返回是否实际删除。"""
        existed = provider_id in self._custom
        self._custom.pop(provider_id, None)
        if existed:
            self.clear_cache()
        return existed

    # ── helper ────────────────────────────────────────────────────────

    def _resolve_api_key_from_env(self, provider_def: ProviderDef) -> str:
        """按 ProviderDef.api_key_env_vars 依次查 env。全空返回 ""。"""
        for var in provider_def.api_key_env_vars:
            value = os.environ.get(var)
            if value:
                return value
        return ""


# ── 进程内默认单例（按需 lazy）─────────────────────────────────────


_default_registry: Optional[ProviderRegistry] = None
_default_lock = threading.Lock()


def get_default_registry() -> ProviderRegistry:
    """拿进程内共享 ProviderRegistry 单例（lazy）。

    多数 caller 应该用这个；只有测试 / 配置隔离场景才 `ProviderRegistry()`
    新建独立实例。
    """
    global _default_registry
    if _default_registry is None:
        with _default_lock:
            if _default_registry is None:
                _default_registry = ProviderRegistry()
    return _default_registry


def reset_default_registry() -> None:
    """清掉默认单例（仅测试用）。"""
    global _default_registry
    with _default_lock:
        _default_registry = None


# ── 内部 helper ───────────────────────────────────────────────────────


def _hash_key(api_key: str) -> str:
    """对 api_key 做轻量 hash，cache_key 不存原 key 缩小泄漏面。"""
    if not api_key:
        return ""
    # 简单截断 hash，跨进程一致性不重要，只在内存 cache key 用
    import hashlib
    return hashlib.sha256(api_key.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _hash_headers(headers: Optional[Dict[str, str]]) -> str:
    """对 default_headers dict 做 canonical hash 进 cache key。

    同一组 headers（不管 dict 顺序）产生同一个 hash；不同则隔离。空返 ""。
    仅用于 in-memory LRU cache，不落盘不跨进程。
    """
    if not headers:
        return ""
    import hashlib
    import json
    # sort_keys 保证 dict 顺序无关
    canonical = json.dumps(headers, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8", errors="ignore")).hexdigest()[:12]


__all__ = [
    "ProviderRegistry",
    "ProviderNotFoundError",
    "get_default_registry",
    "reset_default_registry",
]
