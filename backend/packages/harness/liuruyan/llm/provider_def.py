"""v2 · H · P1-D-1 · ProviderDef dataclass

ProviderDef 是一个 LLM provider 的元数据定义。配合 ProviderRegistry 实现
"加 provider = 改一行配置"的零侵入扩展。

设计参考：
- _docs/handover_p1_p2_apiclient_plan.md § 三 P1-D
- _docs/provider_architecture_migration.md（2026-04-21 设计稿）
- _research/cherry-studio-main 里的 ApiClient 注册机制
- _research/hermes-agent-main 的 models.dev 三层合并

字段说明：
- id: provider 主名（"anthropic" / "openai" / "deepseek" / ...）
- name: 人类可读显示名（前端 UI 用）
- base_url: SDK 端点 URL；None 表示走 SDK 默认端点（仅官方 OpenAI / Anthropic
  允许，所有兼容协议必须显式给）
- transport: 路由协议
    - "anthropic_messages": 用 AnthropicApiClient
    - "openai_chat": 用 OpenAICompatibleApiClient
    - 未来 "gemini_native" / "bedrock_invoke" / "codex_responses"
- api_client_factory: callable，签名 `(api_key, model, base_url, **kwargs)
  -> ApiClient`。registry 调用时传入运行时 api_key + 用户选的 model。
- api_key_env_vars: 环境变量名候选列表（按优先级）。registry 自动从 env
  读 api_key（caller 没显式传时）。
- aliases: provider 别名 tuple（如 "or" → "openrouter"）。
- is_aggregator: 是否是 aggregator（OpenRouter / SiliconFlow）。前端可以
  按这个字段对 model 列表做不同 UI 处理。

⚠️ frozen=True 不深复制 mutable default：tuple/list 字段只用 tuple
（field default_factory 也不是 frozen 友好）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Tuple


@dataclass(frozen=True)
class ProviderDef:
    """LLM provider 元数据 + ApiClient 工厂。"""

    id: str
    """provider 主名，如 "anthropic" / "openai" / "deepseek"。"""

    name: str
    """人类可读显示名，如 "Anthropic" / "OpenAI" / "DeepSeek"。"""

    transport: str
    """路由协议："anthropic_messages" / "openai_chat" / 未来扩展。"""

    api_client_factory: Callable[..., Any]
    """ApiClient 工厂 callable。签名:
        (api_key: str, model: str, base_url: Optional[str], **kwargs) -> ApiClient

    工厂在 ProviderRegistry.get_client 时被调用，参数从 caller 传入。
    工厂内部 import ApiClient 实现并实例化。
    """

    base_url: Optional[str] = None
    """SDK 端点 URL。None 表示用 SDK 默认（官方端点）。"""

    api_key_env_vars: Tuple[str, ...] = field(default_factory=tuple)
    """环境变量名 tuple，按优先级从前往后查。空表示不自动从 env 读。"""

    aliases: Tuple[str, ...] = field(default_factory=tuple)
    """provider 别名，如 ("or", "openrouter")。"""

    is_aggregator: bool = False
    """是否是 aggregator（OpenRouter / SiliconFlow），影响前端模型列表 UI。"""

    description: str = ""
    """简短中文描述，前端 provider 列表显示用。"""

    def matches(self, alias_or_id: str) -> bool:
        """是否匹配 alias 或 id（不区分大小写）。"""
        target = alias_or_id.lower().strip()
        if target == self.id.lower():
            return True
        return target in {a.lower() for a in self.aliases}


__all__ = ["ProviderDef"]
