"""`transcribe_audio` 内置工具：让 Agent 将本地录音 / 音视频文件转写为文字。

低耦合设计：
- 仅通过 `register(registry)` 挂载到 CleanToolRegistry；
- 删除 `backend/voice_transcription/` 目录后，`clean_registry.py` 内的 `try/except ImportError`
  会自动静默跳过，零残留、零报错。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from .config import load_transcription_config
from .engine import TranscriptionError, transcribe_file

logger = logging.getLogger(__name__)

TOOL_NAME = "transcribe_audio"

_CONTEXT_KEYS = {"agent_id", "session_id", "llm_client", "trace_id"}


def _drop_context(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k not in _CONTEXT_KEYS}


def _resolve_audio_path(filepath: str) -> Path:
    try:
        from backend.tools.builtin.file_tools import _resolve_read_path

        return _resolve_read_path(filepath)
    except Exception:
        return Path(filepath).expanduser().resolve()


def _suggest_missing(resolved: Path) -> Dict[str, Any]:
    try:
        from backend.tools.builtin.file_tools import _suggest_dir, _suggest_path

        if resolved.is_dir():
            return _suggest_dir(resolved)
        return _suggest_path(resolved)
    except Exception:
        return {}


def _is_blocked(filepath: str) -> bool:
    try:
        from backend.tools.builtin.file_tools import _is_blocked_device

        return _is_blocked_device(filepath)
    except Exception:
        return False


async def _handle_transcribe_audio(**kwargs: Any) -> str:
    args = _drop_context(kwargs)
    path = args.get("path")
    language = args.get("language")
    prompt = args.get("prompt")
    include_timestamps = bool(args.get("include_timestamps", False))

    if not path or not isinstance(path, str):
        return json.dumps({"error": "Missing required parameter: path"}, ensure_ascii=False)

    if _is_blocked(path):
        return json.dumps({"error": f"Cannot read '{path}': this is a device file"}, ensure_ascii=False)

    cfg = load_transcription_config()
    if not cfg.enabled:
        return json.dumps(
            {
                "error": (
                    "Voice transcription is currently disabled in Settings "
                    "(设置 → 辅助功能 → 语音转写). Please ask the user to enable it first."
                )
            },
            ensure_ascii=False,
        )

    resolved = _resolve_audio_path(path)
    if not resolved.exists():
        return json.dumps(
            {"error": f"Audio file not found: {path}", **_suggest_missing(resolved)},
            ensure_ascii=False,
        )
    if not resolved.is_file():
        return json.dumps(
            {"error": f"Not a file: {path}", **_suggest_missing(resolved)},
            ensure_ascii=False,
        )

    try:
        result = await transcribe_file(
            resolved,
            cfg_override=cfg,
            language=str(language).strip() if language else None,
            prompt=str(prompt).strip() if prompt else None,
            include_timestamps=include_timestamps,
        )
    except TranscriptionError as e:
        return json.dumps({"error": e.message, "path": str(resolved)}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("[transcribe_audio] unexpected error for %s", resolved)
        return json.dumps({"error": f"Transcription failed: {e}", "path": str(resolved)}, ensure_ascii=False)

    payload = {
        "ok": True,
        "path": str(resolved),
        **result.to_dict(include_timestamps=include_timestamps),
    }
    return json.dumps(payload, ensure_ascii=False)


def register(registry: Any) -> None:
    """向 CleanToolRegistry 注册 transcribe_audio 工具（幂等）。"""
    if TOOL_NAME in getattr(registry, "tools", {}):
        return
    from backend.tools.builtin.registry_core import CONCURRENCY_READ
    from backend.tools.builtin.registry_validation import _PATH_ALIAS_MAP

    aliases = {
        **_PATH_ALIAS_MAP,
        "audio_path": "path",
        "audio_file": "path",
        "media_path": "path",
    }

    registry.register(
        name=TOOL_NAME,
        description=(
            "Transcribe a local audio or video file into text using the configured Speech-to-Text engine "
            "(local offline SenseVoice or OpenAI-compatible /v1/audio/transcriptions API such as Whisper / "
            "GPT-4o-transcribe / Groq / SiliconFlow).\\n\\n"
            "WHEN TO USE:\\n"
            "- User attaches, uploads, or references an audio/video file (.wav, .mp3, .m4a, .ogg, .flac, "
            ".webm, .mp4) and needs custom `language` hint, `prompt`, or `include_timestamps=true`\\n"
            "- Converting meeting recordings, voice memos, or interviews with timestamps\\n\\n"
            "WHEN NOT TO USE:\\n"
            "- General file reading (text/code/PDF/Office/image/standard audio transcription) -> `read_file` already supports all of them\\n\\n"
            "Returns JSON: {ok, path, text, backend, model, duration_sec?, language?, segments? (when include_timestamps=true)}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path to the audio/video file (.wav, .mp3, .m4a, .ogg, .flac, .webm, .mp4). "
                        "Absolute path preferred; relative path resolves against working_directory / agent_dir / cwd."
                    ),
                },
                "language": {
                    "type": "string",
                    "description": "Optional ISO language hint (e.g. 'zh', 'en', 'ja', 'yue'). Omit for auto-detection.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Optional context prompt with domain terms, names, or expected spelling (used by OpenAI-compatible APIs).",
                },
                "include_timestamps": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, include timestamped segments ([{start, end, text}]) in the output.",
                },
            },
            "required": ["path"],
        },
        handler=_handle_transcribe_audio,
        is_async=True,
        concurrency_class=CONCURRENCY_READ,
        arg_aliases=aliases,
    )
