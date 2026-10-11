"""TTS 子系统：把 LLM 文字 → 音频。

零污染契约
==========
本模块**所有代码**集中在 ``backend/tts/`` + ``backend/routes/tts_routes.py`` +
``backend/utils/should_speak.py`` + ``backend/main.py`` 一行 ``include_router``。

不动 ``runtime_segments.py`` / ``llm_client.py`` / ``agent.py`` / ``models.json``
schema。配置独立持久化到 ``~/.liuruyan/tts.json``。

未来想撤 = 删 4 个目录 + 5 行删除（详见 ``docs/architecture_spec.md``）。

设计文档：``docs/architecture_spec.md``。
"""
from .config import (
    TTSConfig,
    load_tts_config,
    save_tts_config,
)
from .engine import synthesize

__all__ = [
    "TTSConfig",
    "load_tts_config",
    "save_tts_config",
    "synthesize",
]
