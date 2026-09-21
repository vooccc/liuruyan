"""Providers 配置读写。

数据文件：
- models.json：providers 字典 + current_model / current_provider / prompt_caching_enabled
- models.secrets.json：纯 api_key（不入 git），按 `provider:{id}` 索引

加载链路（load_providers_config）：
1. 读 models.json
2. 反序列化 providers 字典（缺失即空 dict）
3. 内置 13 个 provider 合入（缺失则补；已存在则刷新内置元数据，保留用户改动）
4. 从 secrets 注入 api_key
"""
from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import paths
from .builtin import BUILTIN_PROVIDERS
from .schema import ProviderConfig

logger = logging.getLogger(__name__)


def _models_config_path() -> Path:
    return paths.models_json_path()


def _secrets_path() -> Path:
    return paths.models_secrets_path()


def _load_raw_config() -> Dict[str, Any]:
    p = _models_config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as e:
        logger.warning("models.json 损坏 (%s)，按空配置处理", e)
        return {}


def _load_secrets() -> Dict[str, str]:
    p = _secrets_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _save_secrets(secrets: Dict[str, str]) -> None:
    _secrets_path().write_text(
        json.dumps(secrets, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── 内部辅助 ───────────────────────────────────────────────────────────


def _merge_builtin(providers: Dict[str, ProviderConfig]) -> Dict[str, ProviderConfig]:
    """把 14 个内置 provider 注入字典（缺失则补；已存在则刷新内置元数据，保留用户数据）。

    刷新：name / base_url(freeze 时) / transport / icon / description /
          freeze_url / is_aggregator / api_key_prefix / homepage / api_key_url
    保留：api_key / models / extra_headers / enabled
    """
    for bp in BUILTIN_PROVIDERS:
        if bp.id not in providers:
            providers[bp.id] = bp.model_copy(deep=True)
            continue
        user_cfg = providers[bp.id]
        user_cfg.name = bp.name
        if bp.freeze_url:
            user_cfg.base_url = bp.base_url
        user_cfg.transport = bp.transport
        user_cfg.icon = bp.icon or user_cfg.icon
        user_cfg.description = bp.description or user_cfg.description
        user_cfg.freeze_url = bp.freeze_url
        user_cfg.is_aggregator = bp.is_aggregator
        user_cfg.is_builtin = True
        if not user_cfg.api_key_prefix:
            user_cfg.api_key_prefix = bp.api_key_prefix
        user_cfg.homepage = bp.homepage or user_cfg.homepage
        user_cfg.api_key_url = bp.api_key_url or user_cfg.api_key_url
    return providers


def _inject_secrets(providers: Dict[str, ProviderConfig]) -> None:
    """从 secrets 注入 api_key（原地改）。"""
    secrets = _load_secrets()
    for pid, pc in providers.items():
        if pc.api_key:
            continue
        key = secrets.get(f"provider:{pid}")
        if key:
            pc.api_key = key


def _extract_secrets(providers: Dict[str, ProviderConfig]) -> Dict[str, str]:
    """从 providers 字典里抽 api_key 到 secrets dict。"""
    secrets: Dict[str, str] = {}
    for pid, pc in providers.items():
        if pc.api_key:
            secrets[f"provider:{pid}"] = pc.api_key
    return secrets


# ── 公开 API ───────────────────────────────────────────────────────────


def _normalize_translate_model(raw: Any) -> Optional[Dict[str, str]]:
    """规范化 translate_model 字段（兼容旧版无该字段 / 半填半空）。

    合法形态：{"provider_id": str, "model": str}（两个字段都非空字符串）。
    其他全部当 None 处理（用户没配独立翻译模型，fallback 到当前会话 model）。
    """
    if not isinstance(raw, dict):
        return None
    pid = raw.get("provider_id") or ""
    model = raw.get("model") or ""
    if not isinstance(pid, str) or not isinstance(model, str):
        return None
    pid = pid.strip()
    model = model.strip()
    if not pid or not model:
        return None
    return {"provider_id": pid, "model": model}


def load_providers_config() -> Dict[str, Any]:
    """加载完整 providers 配置。

    Returns:
        {
            "current_model": str,
            "current_provider": str,
            "prompt_caching_enabled": bool,
            "translate_model": {"provider_id": str, "model": str} | None,
            "providers": {provider_id: ProviderConfig},
        }
    """
    raw = _load_raw_config()
    raw_providers = raw.get("providers", {}) or {}
    providers: Dict[str, ProviderConfig] = {
        pid: ProviderConfig.model_validate(pdata)
        for pid, pdata in raw_providers.items()
    }

    providers = _merge_builtin(providers)
    _inject_secrets(providers)

    return {
        "current_model": raw.get("current_model", ""),
        "current_provider": raw.get("current_provider", ""),
        "prompt_caching_enabled": bool(raw.get("prompt_caching_enabled", True)),
        "translate_model": _normalize_translate_model(raw.get("translate_model")),
        "providers": providers,
    }


_cache_lock = threading.Lock()
_cached_signature: Optional[Tuple[Tuple[float, int], Tuple[float, int]]] = None
_cached_payload: Optional[Dict[str, Any]] = None


def _file_signature(p: Path) -> Tuple[float, int]:
    """(mtime, size)。文件不存在返回 (0.0, -1)。

    mtime + size 双因子：Windows / 部分文件系统对 mtime 是秒级精度，
    同秒内覆写可能拿不到 mtime 变化；size 同步比对兜底。"""
    try:
        st = p.stat()
        return (st.st_mtime, st.st_size)
    except FileNotFoundError:
        return (0.0, -1)


def _current_signature() -> Tuple[Tuple[float, int], Tuple[float, int]]:
    return (
        _file_signature(_models_config_path()),
        _file_signature(_secrets_path()),
    )


def invalidate_providers_config_cache() -> None:
    """显式失效 cache（save_providers_config 内部会调；外部一般无需手调）。"""
    global _cached_signature, _cached_payload
    with _cache_lock:
        _cached_signature = None
        _cached_payload = None


def load_providers_config_cached() -> Dict[str, Any]:
    """`load_providers_config()` 的带 mtime 失效缓存版本。

    用途：高频读路径（每次 chat 前 resolve model config）。
    失效：models.json 或 models.secrets.json 的 (mtime, size) 任一变化。
    返回值：每次都 deep copy；调用方可自由修改而不污染 cache。
    线程安全：threading.Lock 保护 signature/payload 读写。
    """
    global _cached_signature, _cached_payload
    sig = _current_signature()
    with _cache_lock:
        if _cached_signature == sig and _cached_payload is not None:
            return copy.deepcopy(_cached_payload)
    payload = load_providers_config()
    with _cache_lock:
        _cached_signature = sig
        _cached_payload = copy.deepcopy(payload)
    return copy.deepcopy(payload)


def save_providers_config(config: Dict[str, Any]) -> None:
    """保存 providers 配置：api_key 自动拆到 secrets 文件。"""
    providers: Dict[str, ProviderConfig] = config.get("providers", {})
    _save_secrets(_extract_secrets(providers))

    on_disk_providers: Dict[str, Dict[str, Any]] = {}
    for pid, pc in providers.items():
        pdump = pc.model_dump()
        pdump["api_key"] = ""
        on_disk_providers[pid] = pdump

    payload: Dict[str, Any] = {
        "current_model": config.get("current_model", ""),
        "current_provider": config.get("current_provider", ""),
        "prompt_caching_enabled": bool(config.get("prompt_caching_enabled", True)),
        "providers": on_disk_providers,
    }
    # translate_model 仅在配置非空时写入，避免在 models.json 里留 null 噪音。
    # load 时 _normalize_translate_model 会把缺失/不合法值统一成 None。
    norm_translate = _normalize_translate_model(config.get("translate_model"))
    if norm_translate:
        payload["translate_model"] = norm_translate
    _models_config_path().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    invalidate_providers_config_cache()


def get_translate_model() -> Optional[Dict[str, str]]:
    """读取用户配置的"翻译模型"。

    Returns:
        {"provider_id": str, "model": str} 或 None（未配置 / 配置不合法）。
    """
    return load_providers_config_cached().get("translate_model")


def set_translate_model(provider_id: str, model: str) -> Dict[str, str]:
    """配置"翻译模型"——思考块翻译等辅助任务优先用此 model（绕开主对话 reasoning model）。

    传空串清除（让翻译 fallback 到当前会话 model）。

    Returns:
        最终落库的 {"provider_id", "model"}；清除时返回 {"provider_id": "", "model": ""}。
    """
    config = load_providers_config()
    pid = (provider_id or "").strip()
    mid = (model or "").strip()
    if not pid or not mid:
        config["translate_model"] = None
        save_providers_config(config)
        return {"provider_id": "", "model": ""}
    pc = find_model(config, mid, pid)
    if pc is None:
        # 容错：provider hint 可能写错，全扫一遍兜底（对齐 set_active_model 的容错）
        pc = find_model(config, mid, None)
    if pc is None:
        raise ValueError(f"model '{mid}' (provider: {pid}) 不在可用列表")
    config["translate_model"] = {"provider_id": pc.id, "model": mid}
    save_providers_config(config)
    return {"provider_id": pc.id, "model": mid}


def get_provider(provider_id: str) -> Optional[ProviderConfig]:
    """按 id 查单个 provider（含 api_key 注入）。"""
    return load_providers_config()["providers"].get(provider_id)


def set_provider(provider: ProviderConfig) -> None:
    """写入单个 provider。"""
    config = load_providers_config()
    config["providers"][provider.id] = provider
    save_providers_config(config)


def reorder_providers(order: List[str]) -> List[str]:
    """按给定 id 列表重排 providers 字典顺序。

    Python 3.7+ dict 保持插入顺序，重排靠 dict comprehension 生效。
    `order` 里有的、且 providers 里存在的，按顺序排前面；剩下的（含未列出的内置）
    按原顺序追加到后面。返回最终生效的完整顺序（含追加项）。
    """
    config = load_providers_config()
    existing = config["providers"]
    ordered: Dict[str, ProviderConfig] = {}
    for pid in order:
        if pid in existing:
            ordered[pid] = existing[pid]
    for pid, pc in existing.items():
        if pid not in ordered:
            ordered[pid] = pc
    config["providers"] = ordered
    save_providers_config(config)
    return list(ordered.keys())


def delete_provider(provider_id: str) -> bool:
    """删除 provider（内置不允许删）。"""
    config = load_providers_config()
    pc = config["providers"].get(provider_id)
    if pc is None or pc.is_builtin:
        return False
    del config["providers"][provider_id]
    save_providers_config(config)
    return True


def find_model(
    config: Dict[str, Any], model_id: str, provider_id: Optional[str] = None
) -> Optional[ProviderConfig]:
    """在 providers 字典里定位拥有 `model_id` 的 provider。

    `provider_id` 若给定，仅在该 provider 下查；否则全局扫一遍取第一个匹配。
    返回 ProviderConfig；找不到返回 None。
    """
    providers: Dict[str, ProviderConfig] = config["providers"]
    if provider_id:
        pc = providers.get(provider_id)
        if pc and any(m.id == model_id for m in pc.models):
            return pc
        return None
    for pc in providers.values():
        if any(m.id == model_id for m in pc.models):
            return pc
    return None


def chat_model_catalog_view() -> Dict[str, Any]:
    """主聊天页用的模型目录视图：把所有 provider.models 平铺成 ChatModelPicker 期望的格式。

    Returns:
        {
            "custom_models": [{id,name,base_url,api_key,provider,tags,context_length,...}, ...],
            "current_model": str,
            "current_provider": str,
            "prompt_caching_enabled": bool,
        }

    每个 entry 带上所属 provider 的 base_url/api_key，便于前端编辑表单回填。

    tags 策略（2026-05-12 用户反馈：前端重复维护 vision 正则，新模型加了要改两遍代码）：
    - `ModelEntry.tags` 是权威源（provider 设置页"添加模型"流水线 + OpenRouter fetch 时
      已经把 infer_capabilities 结果写入），前端拿来直接用做 vision 拦截 / 胶囊显示
    - tags 为空时（老数据 / 用户手动编辑 models.json）用 infer_capabilities(m.id, m.name)
      兜底填充，保证前端永远能拿到可用 tags
    - tags 的唯一真源是 backend.providers.capabilities，前端不应再独立推断（保留前端
      inferModelCapabilities 只作为"没拿到 Model 对象"的边界 fallback）
    """
    from .capabilities import infer_capabilities, infer_context_length

    config = load_providers_config()
    providers: Dict[str, ProviderConfig] = config["providers"]

    custom_models: List[Dict[str, Any]] = []
    for pid, pc in providers.items():
        for m in pc.models:
            # tags 缺失时用 infer 兜底；m.context_length 缺失时同理
            effective_tags = list(m.tags) if m.tags else infer_capabilities(m.id, m.name)
            effective_ctx = m.context_length if m.context_length else infer_context_length(m.id)
            context_source = "configured" if m.context_length else "inferred"
            custom_models.append(
                {
                    "id": m.id,
                    "name": m.name or m.id,
                    "base_url": pc.base_url,
                    "api_key": pc.api_key,
                    "provider": pid,
                    "provider_name": pc.name or pid,
                    "transport": pc.transport,
                    "tags": effective_tags,
                    "context_length": effective_ctx,
                    "context_length_source": context_source,
                }
            )

    return {
        "custom_models": custom_models,
        "current_model": config.get("current_model", ""),
        "current_provider": config.get("current_provider", ""),
        "prompt_caching_enabled": bool(config.get("prompt_caching_enabled", True)),
    }


__all__ = [
    "load_providers_config",
    "load_providers_config_cached",
    "invalidate_providers_config_cache",
    "save_providers_config",
    "get_provider",
    "set_provider",
    "reorder_providers",
    "delete_provider",
    "find_model",
    "chat_model_catalog_view",
]
