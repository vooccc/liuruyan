"""Fast Context 代码探索设置路由（设置 → 辅助功能 → 代码探索）。

- GET  /settings/fast-context  获取当前 Fast Context 配置（模型模式、自定义模型、Devin凭据与状态）
- PUT  /settings/fast-context  保存配置并即时生效到运行期与环境变量
- POST /settings/fast-context/fetch-devin-token  用户手动点击获取本机 Devin 登录态
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.fast_context_config import (
    FastContextConfig,
    apply_fast_context_config_to_env,
    load_fast_context_config,
    save_fast_context_config,
)
from backend.search_config import detect_local_devin, extract_local_devin_token

router = APIRouter(prefix="/settings/fast-context", tags=["settings"])


def _masked(key: str) -> str:
    """脱敏展示 Token"""
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
    cfg = load_fast_context_config()
    raw_token = (cfg.devin_token or "").strip()
    detected = detect_local_devin()

    return {
        "mode": cfg.mode,
        "custom_provider": cfg.custom_provider,
        "custom_model": cfg.custom_model,
        "devin_token": raw_token,
        "masked_token": _masked(raw_token),
        "configured_token": bool(raw_token),
        "max_turns": cfg.max_turns,
        "detected_devin": detected,
    }


@router.get("")
async def get_fast_context_settings() -> Dict[str, Any]:
    """返回当前 Fast Context 配置。"""
    return _snapshot()


class FastContextSettingsUpdate(BaseModel):
    """PUT 请求体。None 表示该字段不更新；空字符串表示清空。"""

    mode: Optional[str] = None
    custom_provider: Optional[str] = None
    custom_model: Optional[str] = None
    devin_token: Optional[str] = None
    max_turns: Optional[int] = Field(default=None, ge=1, le=5)


@router.put("")
async def update_fast_context_settings(body: FastContextSettingsUpdate) -> Dict[str, Any]:
    """保存 Fast Context 配置。"""
    cfg = load_fast_context_config()

    if body.mode is not None:
        if body.mode in ("devin", "custom", "session"):
            cfg.mode = body.mode
        else:
            cfg.mode = "session"
    if body.custom_provider is not None:
        cfg.custom_provider = body.custom_provider.strip()
    if body.custom_model is not None:
        cfg.custom_model = body.custom_model.strip()
    if body.devin_token is not None:
        cfg.devin_token = body.devin_token.strip()
    if body.max_turns is not None:
        cfg.max_turns = max(1, min(5, int(body.max_turns)))

    save_fast_context_config(cfg)
    return _snapshot()


@router.post("/fetch-devin-token")
async def fetch_local_devin_token() -> Dict[str, Any]:
    """用户在 Fast Context 设置面板中主动点击「获取本机登录态」时调用。

    完全自包含且按需读取本地 SQLite 数据库，不进行自动轮询，不额外消耗系统资源。
    """
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
