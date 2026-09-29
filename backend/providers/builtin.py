"""内置 provider 种子数据（15 个）。

用户决策（coding agent dialog 2026-05-04，2026-05-13 追加 Groq）：
- OpenRouter / DeepSeek / OpenAI / Anthropic / Kimi-CN / 智谱-CN / 硅基流动-CN /
  DashScope / ModelScope / Groq / Ollama / LM Studio / Gemini / MiniMax
- 2026-09-27 追加百度千帆（qianfan，埋推广大使返佣链接）
- 所有 provider 允许保存空 Key（require_api_key=false）
- freeze_url=true 锁定官方 base_url（用户改不了，避免误配）

每个内置 provider 出厂 models 数组留空——首次启动时 "模型管理" 弹窗拉取列表，
用户勾选后写入 models[]。避免内置一堆过时 id 让新手迷茫。

设计参考：
- _research/QwenPaw-main provider_manager.py 的 BUILTIN 列表
- cherry-studio-main 的 system providers
"""
from __future__ import annotations

from typing import List

from .schema import ProviderConfig


BUILTIN_PROVIDERS: List[ProviderConfig] = [
    ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_prefix="sk-or-v1-",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=True,
        is_aggregator=True,
        is_builtin=True,
        description="聚合 300+ 模型，含大量 :free 免费模型（DeepSeek / Llama / Qwen / Gemini Flash 等）。",
        icon="🛣️",
        homepage="https://openrouter.ai",
        api_key_url="https://openrouter.ai/keys",
    ),
    ProviderConfig(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        api_key_prefix="sk-",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=True,
        is_builtin=True,
        description="国产性价比王：DeepSeek V3 / R1 推理模型。",
        icon="🐋",
        homepage="https://www.deepseek.com",
        api_key_url="https://platform.deepseek.com/api_keys",
    ),
    ProviderConfig(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_prefix="sk-",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=True,
        is_builtin=True,
        description="官方 OpenAI：GPT-4o / GPT-5 / o3 等。",
        icon="🌐",
        homepage="https://openai.com",
        api_key_url="https://platform.openai.com/api-keys",
    ),
    ProviderConfig(
        id="anthropic",
        name="Anthropic",
        base_url="https://api.anthropic.com",
        api_key_prefix="sk-ant-",
        transport="anthropic_messages",
        require_api_key=False,
        freeze_url=True,
        is_builtin=True,
        description="Claude 原生：Sonnet / Opus / Haiku。走 /v1/messages 协议。",
        icon="🅰️",
        homepage="https://www.anthropic.com",
        api_key_url="https://console.anthropic.com/settings/keys",
    ),
    ProviderConfig(
        id="gemini",
        name="Google Gemini",
        # Google 官方 OpenAI 兼容端点，不走原生 gRPC
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_prefix="",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=True,
        is_builtin=True,
        description="Google Gemini OpenAI 兼容端点：2.5 Pro / Flash / Flash Lite 等。",
        icon="♊",
        homepage="https://ai.google.dev",
        api_key_url="https://aistudio.google.com/apikey",
    ),
    ProviderConfig(
        id="kimi",
        name="Kimi (Moonshot)",
        base_url="https://api.moonshot.cn/v1",
        api_key_prefix="sk-",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=True,
        is_builtin=True,
        description="月之暗面 Kimi：200K 长上下文。",
        icon="🌙",
        homepage="https://www.moonshot.cn",
        api_key_url="https://platform.moonshot.cn/console/api-keys",
    ),
    ProviderConfig(
        id="zhipu",
        name="智谱 BigModel",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_prefix="",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=True,
        is_builtin=True,
        description="智谱 GLM-4.5 / GLM-4 系列。",
        icon="🧠",
        homepage="https://open.bigmodel.cn",
        api_key_url="https://open.bigmodel.cn/usercenter/apikeys",
    ),
    ProviderConfig(
        id="siliconflow",
        name="硅基流动",
        base_url="https://api.siliconflow.cn/v1",
        api_key_prefix="sk-",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=True,
        is_aggregator=True,
        is_builtin=True,
        description="聚合 DeepSeek / Qwen / GLM 等，含免费额度。",
        icon="🌊",
        homepage="https://siliconflow.cn",
        api_key_url="https://cloud.siliconflow.cn/account/ak",
    ),
    ProviderConfig(
        id="qianfan",
        name="百度千帆",
        base_url="https://qianfan.baidubce.com/v2",
        api_key_prefix="bce-v3/",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=True,
        is_builtin=True,
        description="文心一言 ERNIE 系列：ERNIE-4.0 / 3.5 / Speed 等。",
        icon="🌀",
        homepage="https://cloud.baidu.com/campaign/ambassador-product/index.html?ambassadorId=85437244eb3446ee995d8704cf65fe25",
        api_key_url="https://cloud.baidu.com/campaign/ambassador-product/index.html?ambassadorId=85437244eb3446ee995d8704cf65fe25",
    ),
    ProviderConfig(
        id="dashscope",
        name="阿里云百炼（DashScope）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_prefix="sk-",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=True,
        is_builtin=True,
        description="通义千问 Qwen / Qwen3 Coder / Qwen-VL 等。",
        icon="🏮",
        homepage="https://www.aliyun.com/minisite/goods?userCode=skkwgjn3",
        api_key_url="https://www.aliyun.com/minisite/goods?userCode=skkwgjn3",
    ),
    ProviderConfig(
        id="modelscope",
        name="ModelScope 魔搭",
        base_url="https://api-inference.modelscope.cn/v1",
        api_key_prefix="ms-",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=True,
        is_aggregator=True,
        is_builtin=True,
        description="阿里魔搭社区推理：大量免费开源模型（Qwen / DeepSeek / GLM / LLaMA）。",
        icon="🎨",
        homepage="https://modelscope.cn",
        api_key_url="https://modelscope.cn/my/myaccesstoken",
    ),
    ProviderConfig(
        id="groq",
        name="Groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_prefix="gsk_",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=True,
        is_builtin=True,
        description="Groq LPU 超低延迟推理：Llama / Mixtral / Whisper-large-v3-turbo 等。",
        icon="⚡",
        homepage="https://groq.com",
        api_key_url="https://console.groq.com/keys",
    ),
    ProviderConfig(
        id="minimax",
        name="MiniMax",
        base_url="https://api.minimax.chat/v1",
        api_key_prefix="",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=True,
        is_builtin=True,
        description="MiniMax abab / M-series 文本 + 语音 + 视频。",
        icon="〰️",
        homepage="https://www.minimax.chat",
        api_key_url="https://platform.minimaxi.com/user-center/basic-information/interface-key",
    ),
    ProviderConfig(
        id="ollama",
        name="Ollama（本地）",
        base_url="http://localhost:11434/v1",
        api_key_prefix="",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=False,  # 本地端口用户可能改
        is_builtin=True,
        description="本地部署：ollama pull 后即可用。无需 Key。",
        icon="🦙",
        homepage="https://ollama.com",
    ),
    ProviderConfig(
        id="lmstudio",
        name="LM Studio（本地）",
        base_url="http://localhost:1234/v1",
        api_key_prefix="",
        transport="openai_chat",
        require_api_key=False,
        freeze_url=False,
        is_builtin=True,
        description="LM Studio 本地 OpenAI 兼容服务。无需 Key。",
        icon="🏠",
        homepage="https://lmstudio.ai",
    ),
]


def get_builtin_provider_ids() -> List[str]:
    """返回内置 provider id 列表（按注册顺序）。"""
    return [p.id for p in BUILTIN_PROVIDERS]


def find_builtin(provider_id: str) -> ProviderConfig | None:
    """按 id 查内置 provider。"""
    for p in BUILTIN_PROVIDERS:
        if p.id == provider_id:
            return p
    return None


__all__ = ["BUILTIN_PROVIDERS", "get_builtin_provider_ids", "find_builtin"]
