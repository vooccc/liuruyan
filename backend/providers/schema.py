"""Provider / Model 配置 schema（Pydantic）。

数据结构：
    {
        "current_model": "deepseek/deepseek-chat-v3.1:free",
        "current_provider": "openrouter",
        "prompt_caching_enabled": true,
        "providers": {
            "openrouter": ProviderConfig,
            "deepseek": ProviderConfig,
            ...
        }
    }

api_key 不入 models.json：运行时从 models.secrets.json 按 "provider:{id}" 注入，
  保存时拆出去（见 store.py）。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class ModelEntry(BaseModel):
    """provider 下已启用的单个模型。"""

    id: str
    """vendor 模型 id，如 "deepseek/deepseek-chat-v3.1:free" / "gpt-4o"。"""

    name: str = ""
    """人类可读显示名（默认取 id）。"""

    tags: List[str] = Field(default_factory=list)
    """能力 / 属性标签：free / reasoning / vision / function_calling / embedding / rerank / websearch。"""

    icon: str = ""
    """前端显示用 icon（emoji / 单字符）。"""

    added_by_user: bool = True
    """是否是用户从"模型管理"手动勾选加进来的（跟种子内置的区分）。"""

    pricing: Optional[Dict[str, str]] = None
    """定价（OpenRouter 风格，prompt/completion 为"0"表示免费）。"""

    context_length: Optional[int] = None
    """上下文长度。"""


class ProviderConfig(BaseModel):
    """一个 LLM provider 的完整配置。"""

    id: str
    """provider 唯一 id：openrouter / deepseek / openai / ...。"""

    name: str
    """人类可读显示名。"""

    base_url: str = ""
    """API base_url。内置有默认值；用户可改（freeze_url=true 时锁定）。"""

    api_key: str = ""
    """API Key；保存时自动拆到 models.secrets.json，运行时注入。"""

    api_key_prefix: str = ""
    """Key 前缀提示（UI 占位符），如 "sk-or-v1-"。"""

    transport: str = "openai_chat"
    """传输协议：openai_chat / anthropic_messages / gemini_native。"""

    require_api_key: bool = False
    """前端是否校验 Key 必填。本项目默认 false（留空也能保存）。"""

    freeze_url: bool = False
    """是否锁定 base_url（内置官方端点锁；用户自建 provider 允许改）。"""

    enabled: bool = True
    """provider 是否启用（禁用的不会在模型选择器里出现）。"""

    is_aggregator: bool = False
    """是否是聚合商（OpenRouter / SiliconFlow）。影响前端 UI 分组。"""

    is_builtin: bool = False
    """是否是内置 provider（用户不能删除，但可改 api_key 和 models）。"""

    extra_headers: Dict[str, str] = Field(default_factory=dict)
    """自定义 HTTP header，合入每个请求。OpenRouter HTTP-Referer / DashScope
    agentapp 等场景用。会透传到 ApiClient 的 default_headers。"""

    description: str = ""
    """UI 上的简短中文说明。"""

    icon: str = ""
    """UI 上的 icon（emoji 或单字符）。"""

    homepage: str = ""
    """官方网站 URL（UI 点链接跳去注册 Key）。"""

    api_key_url: str = ""
    """获取 Key 的具体页面 URL（优先于 homepage）。"""

    models: List[ModelEntry] = Field(default_factory=list)
    """该 provider 下用户已启用的模型列表。"""

    def public_dict(self) -> Dict:
        """序列化为前端可见 dict。

        设计抉择（2026-05-04）：直接返完整 api_key 给前端。柳如烟是本地桌面应用
        （Tauri + localhost FastAPI），后端只服务自己的前端；前端持有明文带来的
        UX 收益（type=password 自动按真实长度渲染掩码、眼睛切换零延迟）远大于
        虚构的"安全风险"。secrets.json 分离继续保留，是为了 git 不进、备份安全。
        """
        d = self.model_dump()
        d["has_api_key"] = bool(self.api_key)
        return d


__all__ = ["ProviderConfig", "ModelEntry"]
