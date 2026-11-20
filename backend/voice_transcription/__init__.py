"""voice_transcription：语音转写孤立模块（文件批量转写 + Agent `transcribe_audio` 工具）。

与流式语音输入（backend/voice_stream/）完全解耦：
- 不 import voice_stream 任何代码（仅默认优先复用同一份本地 SenseVoice 模型目录，避免重复占用 230MB）；
- 独立持久化配置到 ``<data_dir>/voice_transcription.json``，不污染 models.json / paths.py；
- 可随时整目录删除：删掉本目录 + 还原 main.py 2 行 router 注册 + 删掉前端面板即可，
  clean_registry.py 通过 try/except ImportError 自动跳过。
"""
from __future__ import annotations

from .config import (
    VoiceTranscriptionConfig,
    load_transcription_config,
    public_view,
    save_transcription_config,
    transcription_config_path,
    update_transcription_config,
)
from .engine import (
    TranscriptionError,
    TranscriptionResult,
    TranscriptionSegment,
    transcribe_bytes,
    transcribe_file,
    transcription_status,
)

__all__ = [
    "VoiceTranscriptionConfig",
    "TranscriptionError",
    "TranscriptionResult",
    "TranscriptionSegment",
    "transcription_config_path",
    "load_transcription_config",
    "save_transcription_config",
    "update_transcription_config",
    "public_view",
    "transcribe_bytes",
    "transcribe_file",
    "transcription_status",
    "routes_router",
    "register_tool",
]


def routes_router():
    """惰性 import，避免包 import 时连带拉 FastAPI 依赖链。"""
    from .routes import router

    return router


def register_tool(registry) -> None:
    """注册 transcribe_audio 工具到指定 registry。"""
    from .tool import register

    register(registry)
