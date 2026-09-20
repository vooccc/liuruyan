"""Provider registry with metadata for all supported LLM providers."""

from dataclasses import dataclass
from typing import Dict


@dataclass
class ProviderInfo:
    """Information about an LLM provider."""
    key: str
    name: str
    website: str
    docs_url: str
    api_base: str
    supports_streaming: bool = True
    supports_function_calling: bool = True


# Provider registry - lightweight static data structure
PROVIDER_REGISTRY: Dict[str, ProviderInfo] = {
    "openai": ProviderInfo(
        key="openai",
        name="OpenAI",
        website="https://openai.com",
        docs_url="https://platform.openai.com/docs",
        api_base="https://api.openai.com/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "anthropic": ProviderInfo(
        key="anthropic",
        name="Anthropic",
        website="https://anthropic.com",
        docs_url="https://docs.anthropic.com",
        api_base="https://api.anthropic.com/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "google": ProviderInfo(
        key="google",
        name="Google AI",
        website="https://ai.google.dev",
        docs_url="https://ai.google.dev/docs",
        api_base="https://generativelanguage.googleapis.com/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "deepseek": ProviderInfo(
        key="deepseek",
        name="DeepSeek",
        website="https://www.deepseek.com",
        docs_url="https://platform.deepseek.com/docs",
        api_base="https://api.deepseek.com/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "zhipu": ProviderInfo(
        key="zhipu",
        name="智谱AI",
        website="https://www.zhipuai.cn",
        docs_url="https://open.bigmodel.cn/dev/api",
        api_base="https://open.bigmodel.cn/api/paas/v4",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "moonshot": ProviderInfo(
        key="moonshot",
        name="Moonshot AI",
        website="https://www.moonshot.cn",
        docs_url="https://platform.moonshot.cn/docs",
        api_base="https://api.moonshot.cn/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "baichuan": ProviderInfo(
        key="baichuan",
        name="百川智能",
        website="https://www.baichuan-ai.com",
        docs_url="https://platform.baichuan-ai.com/docs",
        api_base="https://api.baichuan-ai.com/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "minimax": ProviderInfo(
        key="minimax",
        name="MiniMax",
        website="https://www.minimaxi.com",
        docs_url="https://www.minimaxi.com/document",
        api_base="https://api.minimax.chat/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "doubao": ProviderInfo(
        key="doubao",
        name="豆包",
        website="https://www.volcengine.com/product/doubao",
        docs_url="https://www.volcengine.com/docs/82379",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "qwen": ProviderInfo(
        key="qwen",
        name="通义千问",
        website="https://tongyi.aliyun.com",
        docs_url="https://help.aliyun.com/zh/dashscope",
        api_base="https://dashscope.aliyuncs.com/api/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "mistral": ProviderInfo(
        key="mistral",
        name="Mistral AI",
        website="https://mistral.ai",
        docs_url="https://docs.mistral.ai",
        api_base="https://api.mistral.ai/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "cohere": ProviderInfo(
        key="cohere",
        name="Cohere",
        website="https://cohere.com",
        docs_url="https://docs.cohere.com",
        api_base="https://api.cohere.ai/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "groq": ProviderInfo(
        key="groq",
        name="Groq",
        website="https://groq.com",
        docs_url="https://console.groq.com/docs",
        api_base="https://api.groq.com/openai/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "together": ProviderInfo(
        key="together",
        name="Together AI",
        website="https://www.together.ai",
        docs_url="https://docs.together.ai",
        api_base="https://api.together.xyz/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
    "fireworks": ProviderInfo(
        key="fireworks",
        name="Fireworks AI",
        website="https://fireworks.ai",
        docs_url="https://docs.fireworks.ai",
        api_base="https://api.fireworks.ai/inference/v1",
        supports_streaming=True,
        supports_function_calling=True,
    ),
}
