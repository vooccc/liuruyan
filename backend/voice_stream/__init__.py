"""voice_stream：流式语音输入（realtime ASR）孤立模块。

与现有批量转写 backend/voice_transcription 完全独立：
- 不 import voice_transcription 的任何代码；voice_transcription 也不依赖本包。
- 配置存 models.json 顶层 "voice_stream" 键（与 "voice_transcription" 平级）。
- main.py 只加 2 行注册；想整体移除时删掉本目录 + 还原 main.py 2 行即可。

对外入口：
    from backend.voice_stream.routes import router   # FastAPI APIRouter
路由：
    GET  /voice/stream/config   可用性/配置（前端据此降级）
    WS   /voice/stream/ws       浏览器 PCM 流 ↔ DashScope realtime ASR
"""
from __future__ import annotations

from .config import (
    VoiceStreamConfig,
    load_voice_stream_config,
    save_voice_stream_config,
    stream_status,
)
from .providers.base import RealtimeProvider, ProviderEvent

__all__ = [
    "VoiceStreamConfig",
    "load_voice_stream_config",
    "save_voice_stream_config",
    "stream_status",
    "RealtimeProvider",
    "ProviderEvent",
    "routes_router",
]


def routes_router():
    """惰性 import，避免包 import 时连带拉 FastAPI 依赖链（测试友好）。"""
    from .routes import router

    return router
