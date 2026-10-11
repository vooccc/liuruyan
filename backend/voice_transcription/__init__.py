"""语音转写模块。

三引擎：disabled / whisper_api / local_whisper。
配置来自 models.json 的 voice_transcription 字段（复用 providers store 的文件）。

设计文档：docs/architecture_spec.md
"""
from .config import (
    VoiceTranscriptionConfig,
    load_voice_config,
    save_voice_config,
)
from .engine import (
    check_local_whisper_status,
    transcribe_audio,
)

__all__ = [
    "VoiceTranscriptionConfig",
    "load_voice_config",
    "save_voice_config",
    "transcribe_audio",
    "check_local_whisper_status",
]
