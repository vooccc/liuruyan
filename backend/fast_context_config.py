"""Fast Context 代码探索配置（设置 → 辅助功能 → 代码探索）。

支持：
1. 模型选择模式：
   - "session": 跟随当前聊天会话模型（默认，动态继承，无缝跟随会话切换）。
   - "custom": 自定义独立模型（从模型服务商列表中挑选专属模型，如 deepseek-chat、qwen2.5-coder）。
2. 自定义模型信息：
   - custom_provider: 选中的 Provider ID（如 deepseek, openrouter, siliconflow 等）。
   - custom_model: 选中的 Model ID（如 deepseek-chat, qwen2.5-coder-32b-instruct 等）。
3. Devin 凭据配置：
   - devin_token: Devin 个人 sessionToken（支持手动点击获取本机登录态，完全独立解耦）。
4. 检索偏好：
   - max_turns: 最大推演检索轮数（1-5 轮，默认 3 轮）。

持久化到 ``fast_context_config.json``，并在保存与启动时按需同步环境变量。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from . import paths
from .search_config import detect_local_devin, extract_local_devin_token

logger = logging.getLogger(__name__)

ENV_FAST_CONTEXT_MODEL = "FAST_CONTEXT_MODEL"
ENV_FAST_CONTEXT_PROVIDER = "FAST_CONTEXT_PROVIDER"
ENV_FAST_CONTEXT_DEVIN_TOKEN = "FAST_CONTEXT_DEVIN_TOKEN"


class FastContextConfig(BaseModel):
    """Fast Context 工具配置。"""

    mode: str = Field(
        default="session",
        description="推演引擎模式：'session'（跟随当前会话模型，默认）、'custom'（指定独立模型服务商模型）或 'devin'（借用 Devin 官方 Devstral 搜索子代理，备选）",
    )
    custom_provider: str = Field(
        default="",
        description="自定义模型所属的 provider_id（例如 'deepseek', 'openrouter'）",
    )
    custom_model: str = Field(
        default="",
        description="自定义模型的 model_id（例如 'deepseek-chat', 'claude-3-7-sonnet'）",
    )
    devin_token: str = Field(
        default="",
        description="Devin sessionToken 凭据（形如 devin-session-token$...）",
    )
    max_turns: int = Field(
        default=3,
        ge=1,
        le=5,
        description="最大多轮检索推演轮数（1-5 轮，默认 3）",
    )


def load_fast_context_config() -> FastContextConfig:
    """从 fast_context_config.json 读取配置。缺失 / 损坏则返回默认配置。"""
    p = paths.fast_context_config_path()
    cfg = FastContextConfig()
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
            cfg = FastContextConfig.model_validate(raw)
        except Exception as e:
            logger.warning("[FastContextConfig] Load config failed, fallback to default: %s", e)

    # 凭据自愈：若未单独配置 devin_token，自动复用 search_config 或环境变量中的凭据
    if not cfg.devin_token:
        fallback_key = os.getenv("DEVIN_API_KEY", "").strip()
        if not fallback_key:
            try:
                from .search_config import load_search_config
                s_cfg = load_search_config()
                fallback_key = (s_cfg.devin_api_key or "").strip()
            except Exception:
                pass
        if fallback_key:
            cfg.devin_token = fallback_key

    return cfg


def save_fast_context_config(cfg: FastContextConfig) -> None:
    """保存配置到 fast_context_config.json，并立即生效环境变量。"""
    p = paths.fast_context_config_path()
    p.write_text(
        json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    apply_fast_context_config_to_env(cfg)


def apply_fast_context_config_to_env(cfg: FastContextConfig | None = None) -> None:
    """将配置按需更新到 os.environ，保证各模块即时生效。"""
    cfg = cfg or load_fast_context_config()

    # Devin Token
    devin_token = (cfg.devin_token or "").strip()
    if devin_token:
        os.environ[ENV_FAST_CONTEXT_DEVIN_TOKEN] = devin_token
        # 兼容性回填：若系统未全局配置 DEVIN_API_KEY，亦可作为回退
        if not os.environ.get("DEVIN_API_KEY"):
            os.environ["DEVIN_API_KEY"] = devin_token
    else:
        os.environ.pop(ENV_FAST_CONTEXT_DEVIN_TOKEN, None)

    # 模型覆盖
    if cfg.mode == "custom" and cfg.custom_model.strip():
        os.environ[ENV_FAST_CONTEXT_MODEL] = cfg.custom_model.strip()
        if cfg.custom_provider.strip():
            os.environ[ENV_FAST_CONTEXT_PROVIDER] = cfg.custom_provider.strip()
    else:
        # session 模式下清理环境变量中的 model override，确保完全继承 session.model
        os.environ.pop(ENV_FAST_CONTEXT_MODEL, None)
        os.environ.pop(ENV_FAST_CONTEXT_PROVIDER, None)
