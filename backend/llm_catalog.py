# -*- coding: utf-8 -*-
"""LLM 模型目录 / 连接解析（平台能力）。

给被柳如烟子进程调用的领域应用（ai-content 自媒体、ai-quant 量化）提供「能直连的模型
信息」——从柳如烟 providers 配置（models.json）派生，让子进程**直连厂商**、复用柳如烟
配置、零重配。

只收 **openai_chat** 协议的模型：子进程侧的薄 llm_client 走 OpenAI 兼容直连，
anthropic / gemini 原生协议暂不支持直连（不进清单/目录）。

三个出口：
- ``list_text_models()``：可选模型清单（供前端下拉，含 provider 分组信息）。
- ``build_model_catalog(selected)``：完整模型目录 {default, models}（ai-content 多模型注入用）。
- ``resolve_model_env(selected)``：单模型连接 env（ai-quant 整条工作流一个模型注入用）。
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Tuple


def _iter_openai_models() -> Iterator[Tuple[Any, Any, str]]:
    """遍历 enabled + openai_chat + 有 base_url 的 provider 下的模型。

    yield (model_entry, provider_config, provider_id)。读配置失败则不产出（静默空）。
    """
    try:
        from backend.providers.store import load_providers_config
        cfg = load_providers_config()
    except Exception:
        return
    for pid, pc in (cfg.get("providers") or {}).items():
        if not getattr(pc, "enabled", True):
            continue
        if getattr(pc, "transport", "openai_chat") != "openai_chat":
            continue
        if not (getattr(pc, "base_url", "") or ""):
            continue
        for m in getattr(pc, "models", []):
            # 纯生图模型（image_gen）不进写作/对话清单——选了也调不通
            if "image_gen" in (getattr(m, "tags", []) or []):
                continue
            yield m, pc, pid


def current_model() -> str:
    """柳如烟全局当前模型（current_model）。"""
    try:
        from backend.providers.store import load_providers_config
        return load_providers_config().get("current_model", "") or ""
    except Exception:
        return ""


def list_text_models() -> List[Dict[str, str]]:
    """可选写作/对话模型清单（供前端下拉，前端按 provider 分组）。"""
    out: List[Dict[str, str]] = []
    for m, pc, pid in _iter_openai_models():
        out.append({
            "model": m.id,
            "name": m.name or m.id,
            "provider": pid,
            "provider_name": pc.name or pid,
        })
    return out


def build_model_catalog(selected_model: str = "") -> Dict[str, Any]:
    """完整模型目录 {default, models: {id: {base_url, api_key, transport, provider}}}。

    default = 传入的 selected_model / 柳如烟 current_model / 首个可用。
    """
    models: Dict[str, Any] = {}
    for m, pc, pid in _iter_openai_models():
        models[m.id] = {
            "base_url": pc.base_url,
            "api_key": pc.api_key or "",
            "transport": "openai_chat",
            "provider": pid,
        }
    default = (selected_model or "").strip() or current_model() or ""
    if default not in models and models:
        default = next(iter(models))
    return {"default": default, "models": models}


def resolve_model_env(selected_model: str = "") -> Dict[str, str]:
    """单模型连接 → {LLM_BASE_URL, LLM_API_KEY, LLM_MODEL}；解析不到返回 {}。

    用于"整条工作流用一个模型"的子进程（如量化议事 ai-quant），其 llm_client 直接
    读这三个环境变量。
    """
    cat = build_model_catalog(selected_model)
    default = cat.get("default") or ""
    entry = (cat.get("models") or {}).get(default)
    if not entry:
        return {}
    return {
        "LLM_BASE_URL": entry.get("base_url", ""),
        "LLM_API_KEY": entry.get("api_key", ""),
        "LLM_MODEL": default,
    }


__all__ = [
    "list_text_models",
    "build_model_catalog",
    "resolve_model_env",
    "current_model",
]
