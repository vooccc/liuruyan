"""TTS 引擎入口：根据 ``TTSConfig.provider_type`` dispatch 到具体 provider。

Engine 持有 provider 单例（无内部可变状态，复用安全），避免每次合成重建。
新增 provider 时只需在 ``_PROVIDER_REGISTRY`` 加一项。

设计文档：``_docs/handover_tts_design_2026-05-29.md`` §5.1
"""
from __future__ import annotations

import logging
from typing import Optional

from .agent_override import resolve_effective_config
from .config import TTSConfig, load_tts_config
from .providers import (
    AliyunCompatProvider,
    BaiduCompatProvider,
    GPTSoVITSCompatProvider,
    OpenAICompatProvider,
    TencentCompatProvider,
    TTSError,
    TTSProvider,
    TTSResult,
)

logger = logging.getLogger(__name__)


# provider 单例注册表。每个 provider 是无状态的，跨请求复用零风险。
_PROVIDER_REGISTRY: dict[str, TTSProvider] = {
    "openai_compat": OpenAICompatProvider(),
    "gptsovits_compat": GPTSoVITSCompatProvider(),
    "tencent_compat": TencentCompatProvider(),
    "baidu_compat": BaiduCompatProvider(),
    "aliyun_compat": AliyunCompatProvider(),
}


async def synthesize(
    text: str,
    cfg_override: Optional[TTSConfig] = None,
    agent_id: Optional[str] = None,
) -> TTSResult:
    """把文本合成为音频。

    Args:
        text: 已经过 ``should_speak`` 过滤的纯文本。注意：本函数**不再做** say 标签
            提取或长度判断——那是上游 ``/tts/synthesize`` 路由的职责。
        cfg_override: 试听场景临时用的配置（不写盘）。None 表示用当前持久化配置。
        agent_id: 当前生效的角色 id。非空时会读取 ``meta.json`` 的 ``tts_override``
            字段并合并到 cfg（角色级覆盖全局）。cfg_override 优先级最高，
            一旦传了就不再合并 agent override（试听语义：用户面板里改的字段 > 角色配置）。

    Raises:
        TTSError: 配置缺失 / provider 错误 / 上游异常。
    """
    if cfg_override is not None:
        cfg = cfg_override
    else:
        cfg = resolve_effective_config(load_tts_config(), agent_id)

    provider = _PROVIDER_REGISTRY.get(cfg.provider_type)
    if provider is None:
        raise TTSError(
            f"未实现的 TTS provider_type: {cfg.provider_type}。"
            f"当前支持：{', '.join(_PROVIDER_REGISTRY.keys())}。"
        )

    logger.debug(
        "[TTS-ENGINE] dispatch provider=%s len=%d",
        cfg.provider_type, len(text or ""),
    )
    return await provider.synthesize(text, cfg)
