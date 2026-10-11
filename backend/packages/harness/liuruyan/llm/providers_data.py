"""v2 · H · P1-D-1 · BUILTIN_PROVIDERS 元数据列表

15 个内置 provider，覆盖：
- 1 个 Anthropic native
- 14 个 OpenAI 兼容（OpenAI / 讯飞 / GLM / Kimi / DeepSeek / 豆包 / Mistral /
  Groq / Together / Fireworks / SiliconFlow / OpenRouter / MiniMax / Cohere）

加新 provider = 在本文件加一个 ProviderDef，**不改任何代码**。
（前提：transport 是已有 "anthropic_messages" / "openai_chat"。如果是新协议，
需要先加 ApiClient 实现 + 加新 transport 名。）

设计参考：
- docs/architecture_spec.md § 五（"加 Cohere = 改两处"样例）
- docs/provider_architecture_migration.md（2026-04-21 设计稿，15 个 provider）
"""
from __future__ import annotations

from typing import Tuple

from .provider_def import ProviderDef


def _make_anthropic_client(api_key: str, model: str, base_url=None, **kwargs):
    """AnthropicApiClient 工厂。延迟 import 避免循环依赖。

    丢弃 OpenAI 路径专用 kwarg（get_known_tool_names 等）—— LLMClient._get_api_client
    无条件透传给 registry，但 Anthropic 路径不需要这类参数，硬透会让
    AnthropicApiClient.__init__ TypeError。Anthropic 协议下 final_message 是
    tool_call 的权威源，本身就不需要 text_leaked_tool_call 救火 grammar。
    """
    from .clients.anthropic import AnthropicApiClient
    # 已知仅 OpenAI 兼容路径用的 kwarg，过滤掉防止透到 AnthropicApiClient.__init__
    kwargs.pop("get_known_tool_names", None)
    return AnthropicApiClient(api_key=api_key, model=model, base_url=base_url, **kwargs)


def _make_openai_client(api_key: str, model: str, base_url=None, **kwargs):
    """OpenAICompatibleApiClient 工厂。"""
    from .clients.openai_compat import OpenAICompatibleApiClient
    return OpenAICompatibleApiClient(api_key=api_key, model=model, base_url=base_url, **kwargs)


# ── Anthropic 原生 ────────────────────────────────────────────────────

ANTHROPIC = ProviderDef(
    id="anthropic",
    name="Anthropic",
    transport="anthropic_messages",
    api_client_factory=_make_anthropic_client,
    base_url=None,  # SDK 默认 https://api.anthropic.com
    api_key_env_vars=("ANTHROPIC_API_KEY",),
    aliases=("claude",),
    description="Anthropic Claude 系列原生 API（Messages 协议）。",
)


# ── OpenAI 兼容协议 ───────────────────────────────────────────────────

OPENAI = ProviderDef(
    id="openai",
    name="OpenAI",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url=None,  # SDK 默认 https://api.openai.com/v1
    api_key_env_vars=("OPENAI_API_KEY",),
    aliases=("gpt",),
    description="OpenAI GPT 系列官方端点。",
)

XUNFEI = ProviderDef(
    id="xunfei",
    name="讯飞星火",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://maas-api.xfyun.cn/v1",
    api_key_env_vars=("XUNFEI_API_KEY", "SPARK_API_KEY"),
    aliases=("xf", "spark", "星火"),
    description="讯飞星火 OpenAI 兼容协议（柳如烟双协议接入第一档）。",
)

GLM = ProviderDef(
    id="glm",
    name="智谱 GLM",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://open.bigmodel.cn/api/paas/v4",
    api_key_env_vars=("GLM_API_KEY", "ZHIPU_API_KEY"),
    aliases=("zhipu", "智谱"),
    description="智谱 GLM 系列（含 GLM4 / GLM5）。",
)

DEEPSEEK = ProviderDef(
    id="deepseek",
    name="DeepSeek",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://api.deepseek.com/v1",
    api_key_env_vars=("DEEPSEEK_API_KEY",),
    aliases=("ds",),
    description="DeepSeek V3 / R1（DSML 私有协议由 stream parser plugin 自动处理）。",
)

KIMI = ProviderDef(
    id="kimi",
    name="Moonshot Kimi",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://api.moonshot.cn/v1",
    api_key_env_vars=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
    aliases=("moonshot",),
    description="月之暗面 Kimi 系列。",
)

DOUBAO = ProviderDef(
    id="doubao",
    name="豆包",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://ark.cn-beijing.volces.com/api/v3",
    api_key_env_vars=("DOUBAO_API_KEY", "ARK_API_KEY", "VOLC_API_KEY"),
    aliases=("ark", "volc"),
    description="字节跳动豆包 / 火山方舟。",
)

MISTRAL = ProviderDef(
    id="mistral",
    name="Mistral AI",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://api.mistral.ai/v1",
    api_key_env_vars=("MISTRAL_API_KEY",),
    aliases=(),
    description="Mistral 官方 API。",
)

GROQ = ProviderDef(
    id="groq",
    name="Groq",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://api.groq.com/openai/v1",
    api_key_env_vars=("GROQ_API_KEY",),
    aliases=(),
    description="Groq LPU 高速推理端点。",
)

TOGETHER = ProviderDef(
    id="together",
    name="Together AI",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://api.together.xyz/v1",
    api_key_env_vars=("TOGETHER_API_KEY",),
    aliases=(),
    description="Together 开源模型平台。",
)

FIREWORKS = ProviderDef(
    id="fireworks",
    name="Fireworks AI",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://api.fireworks.ai/inference/v1",
    api_key_env_vars=("FIREWORKS_API_KEY",),
    aliases=(),
    description="Fireworks AI 开源模型推理。",
)

SILICONFLOW = ProviderDef(
    id="siliconflow",
    name="SiliconFlow",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://api.siliconflow.cn/v1",
    api_key_env_vars=("SILICONFLOW_API_KEY", "SF_API_KEY"),
    aliases=("sf",),
    is_aggregator=True,
    description="硅基流动 aggregator（聚合开源模型）。",
)

OPENROUTER = ProviderDef(
    id="openrouter",
    name="OpenRouter",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://openrouter.ai/api/v1",
    api_key_env_vars=("OPENROUTER_API_KEY",),
    aliases=("or",),
    is_aggregator=True,
    description="OpenRouter aggregator（聚合 100+ 模型，跨厂商路由）。",
)

MINIMAX = ProviderDef(
    id="minimax",
    name="MiniMax",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://api.minimax.chat/v1",
    api_key_env_vars=("MINIMAX_API_KEY",),
    aliases=(),
    description="MiniMax abab 系列。",
)

COHERE = ProviderDef(
    id="cohere",
    name="Cohere",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://api.cohere.ai/compatibility/v1",
    api_key_env_vars=("COHERE_API_KEY",),
    aliases=(),
    description="Cohere Command 系列（OpenAI 兼容端点）。",
)

GEMINI = ProviderDef(
    id="gemini",
    name="Google Gemini",
    transport="openai_chat",
    api_client_factory=_make_openai_client,
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    api_key_env_vars=("GEMINI_API_KEY", "GOOGLE_AI_API_KEY"),
    aliases=("google",),
    description="Google Gemini OpenAI 兼容协议端点（v1beta/openai）。",
)


# ── 内置列表 ───────────────────────────────────────────────────────────

BUILTIN_PROVIDERS: Tuple[ProviderDef, ...] = (
    ANTHROPIC,
    OPENAI,
    XUNFEI,
    GLM,
    DEEPSEEK,
    KIMI,
    DOUBAO,
    MISTRAL,
    GROQ,
    TOGETHER,
    FIREWORKS,
    SILICONFLOW,
    OPENROUTER,
    MINIMAX,
    COHERE,
    GEMINI,
)

__all__ = [
    "BUILTIN_PROVIDERS",
    "ANTHROPIC",
    "OPENAI",
    "XUNFEI",
    "GLM",
    "DEEPSEEK",
    "KIMI",
    "DOUBAO",
    "MISTRAL",
    "GROQ",
    "TOGETHER",
    "FIREWORKS",
    "SILICONFLOW",
    "OPENROUTER",
    "MINIMAX",
    "COHERE",
    "GEMINI",
]
