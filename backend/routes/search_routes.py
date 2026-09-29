"""搜索引擎设置路由（设置 → 辅助功能 → 搜索引擎）。

- GET /settings/search  三大 AI 引擎（Devin/Exa/Tavily）配置、优先级列表与本机 Devin 版本探测
- PUT /settings/search  保存配置并热生效到 os.environ
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.search_config import (
    ENV_KEYS,
    SUPPORTED_AI_ENGINES,
    SearchConfig,
    detect_local_devin,
    extract_local_devin_token,
    load_search_config,
    save_search_config,
)

router = APIRouter(prefix="/settings/search", tags=["settings"])


def _masked(key: str) -> str:
    """脱敏展示"""
    key = (key or "").strip()
    if not key:
        return ""
    if key.startswith("devin-session-token$"):
        suffix = key[-4:] if len(key) >= 28 else ""
        return f"devin-session••••••{suffix}"
    if len(key) <= 10:
        return "•" * len(key)
    return f"{key[:6]}{'•' * 6}{key[-4:]}"


def _snapshot() -> Dict[str, Any]:
    cfg = load_search_config()
    engines_data: Dict[str, Any] = {}

    for eng in SUPPORTED_AI_ENGINES:
        ui_key = (getattr(cfg, f"{eng}_api_key", "") or "").strip()
        env_var = ENV_KEYS.get(eng, "")
        env_present = bool((os.getenv(env_var) or "").strip()) if env_var else False
        env_fallback = env_present and not ui_key
        engines_data[eng] = {
            "api_key": ui_key,
            "masked": _masked(ui_key),
            "configured": bool(ui_key or env_present),
            "env_fallback": env_fallback,
            "enabled": cfg.enabled_engines.get(eng, True),
        }

    return {
        "engines": engines_data,
        "priority_order": cfg.priority_order,
        "detected_devin": detect_local_devin(),
        # 保持向后兼容
        "tavily": engines_data["tavily"],
    }


@router.get("")
async def get_search_settings() -> Dict[str, Any]:
    """返当前搜索引擎配置"""
    return _snapshot()


class SearchSettingsUpdate(BaseModel):
    """PUT body。None 表示该字段不动；空字符串表示清空。"""

    tavily_api_key: Optional[str] = None
    exa_api_key: Optional[str] = None
    devin_api_key: Optional[str] = None
    enabled_engines: Optional[Dict[str, bool]] = None
    priority_order: Optional[List[str]] = None


@router.put("")
async def update_search_settings(body: SearchSettingsUpdate) -> Dict[str, Any]:
    cfg = load_search_config()
    if body.tavily_api_key is not None:
        cfg.tavily_api_key = body.tavily_api_key.strip()
    if body.exa_api_key is not None:
        cfg.exa_api_key = body.exa_api_key.strip()
    if body.devin_api_key is not None:
        cfg.devin_api_key = body.devin_api_key.strip()
    if body.enabled_engines is not None:
        cfg.enabled_engines.update(body.enabled_engines)
    if body.priority_order is not None:
        # 确保包含全部受支持引擎
        cleaned = [e for e in body.priority_order if e in SUPPORTED_AI_ENGINES]
        for e in SUPPORTED_AI_ENGINES:
            if e not in cleaned:
                cleaned.append(e)
        cfg.priority_order = cleaned

    save_search_config(cfg)
    return _snapshot()


@router.post("/fetch-devin-token")
async def fetch_local_devin_token() -> Dict[str, Any]:
    """仅当用户在前端主动点击「读取本机 Token」按钮时调用，按需读取本地 SQLite 凭据"""
    token = extract_local_devin_token()
    if not token:
        return {
            "success": False,
            "error": "未能在本机 Devin 存储中检测到有效的已登录 sessionToken，请先在客户端登录后再试。",
        }
    return {
        "success": True,
        "token": token,
        "masked": _masked(token),
    }
