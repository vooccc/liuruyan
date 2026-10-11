"""voice_transcription 字段读写。复用 models.json，不单独存。

models.json 顶层结构：
    {
        "current_model": ...,
        "current_provider": ...,
        "prompt_caching_enabled": ...,
        "voice_transcription": {
            "provider_type": "disabled" | "whisper_api" | "local_whisper",
            "provider_id": "siliconflow",
            "model": "FunAudioLLM/SenseVoiceSmall",
            "local_model_size": "small",
            "max_duration_seconds": 300,
            "max_audio_size_mb": 25
        },
        "providers": { ... }
    }

写入要 merge 而非覆盖（保留 providers 字典与其他顶层字段）。
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from .. import paths


ProviderType = Literal["disabled", "whisper_api", "local_whisper"]
LocalModelSize = Literal["tiny", "base", "small", "medium", "large-v3"]


class VoiceTranscriptionConfig(BaseModel):
    """语音转写配置（用户可改）。"""

    provider_type: ProviderType = "disabled"
    """转写引擎类型。disabled 时前端隐藏麦克风按钮。"""

    provider_id: str = ""
    """whisper_api 模式下用哪个 provider（id 取自 providers 字典）。"""

    model: str = ""
    """API 端点的模型名。
    - SiliconFlow: FunAudioLLM/SenseVoiceSmall（中文优）
    - OpenAI: whisper-1
    - Groq: whisper-large-v3-turbo
    """

    local_model_size: LocalModelSize = "small"
    """local_whisper 模式下的模型大小。"""

    max_duration_seconds: int = 300
    """录音最长 5 分钟。服务端二次校验防绕过前端。"""

    max_audio_size_mb: int = 25
    """单次上传音频上限 25MB（约 30 分钟 webm/opus）。"""


def load_voice_config() -> VoiceTranscriptionConfig:
    """从 models.json 读 voice_transcription 字段。缺失 / 损坏返默认。"""
    p = paths.models_json_path()
    if not p.exists():
        return VoiceTranscriptionConfig()
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return VoiceTranscriptionConfig()
    payload = raw.get("voice_transcription") or {}
    try:
        return VoiceTranscriptionConfig.model_validate(payload)
    except Exception:
        # schema 升级 / 损坏 → 走默认，不阻断
        return VoiceTranscriptionConfig()


def save_voice_config(cfg: VoiceTranscriptionConfig) -> None:
    """合并写回 models.json，保留 providers 字典与其他顶层字段。

    主动让 providers store 的 mtime+size cache 失效，避免读到旧 providers。
    """
    p = paths.models_json_path()
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            raw = {}
    else:
        raw = {}
    raw["voice_transcription"] = cfg.model_dump()
    p.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        from ..providers.store import invalidate_providers_config_cache

        invalidate_providers_config_cache()
    except Exception:
        pass
