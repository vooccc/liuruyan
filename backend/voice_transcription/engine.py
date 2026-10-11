"""三引擎 dispatch + whisper_api / local_whisper 两个具体实现。

入口：transcribe_audio(audio_bytes, filename) -> str
工具：check_local_whisper_status() -> dict
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config import VoiceTranscriptionConfig, load_voice_config

logger = logging.getLogger(__name__)


# ── 公共入口 ─────────────────────────────────────────────────────────


async def transcribe_audio(audio_bytes: bytes, filename: str = "audio.webm") -> str:
    """把音频 bytes 转成文字。

    Raises:
        RuntimeError: disabled / 配置无效 / 文件过大 / 转写失败
    """
    cfg = load_voice_config()
    if cfg.provider_type == "disabled":
        raise RuntimeError("voice transcription disabled")

    size_mb = len(audio_bytes) / 1024 / 1024
    if size_mb > cfg.max_audio_size_mb:
        raise RuntimeError(
            f"audio too large: {size_mb:.1f}MB > {cfg.max_audio_size_mb}MB"
        )

    # 落盘临时文件：OpenAI SDK 和 faster-whisper 都更友好吃 file path / handle
    suffix = Path(filename).suffix or ".webm"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(audio_bytes)
        tmp.close()
        if cfg.provider_type == "whisper_api":
            return await _transcribe_via_api(tmp.name, cfg)
        if cfg.provider_type == "local_whisper":
            return await _transcribe_local(tmp.name, cfg)
        raise RuntimeError(f"unknown provider_type: {cfg.provider_type}")
    finally:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except Exception:
            pass


def check_local_whisper_status() -> Dict[str, Any]:
    """检查 ffmpeg 和 faster-whisper 是否可用。供设置页展示安装状态。"""
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    faster_whisper_ok = False
    try:
        import faster_whisper  # noqa: F401

        faster_whisper_ok = True
    except ImportError:
        pass
    return {
        "available": ffmpeg_ok and faster_whisper_ok,
        "ffmpeg_installed": ffmpeg_ok,
        "faster_whisper_installed": faster_whisper_ok,
    }


# ── whisper_api 引擎 ─────────────────────────────────────────────────


def _get_provider_creds(provider_id: str) -> Optional[Tuple[str, str]]:
    """从柳如烟 providers store 拿 (base_url, api_key)。

    base_url 规整：
    - OpenAI 兼容端点统一带 /v1 后缀（DeepSeek 内置 base 是 'https://api.deepseek.com'
      不带 /v1，要补）
    - Gemini 端点 base 是 'https://.../v1beta/openai/'，不能再补 /v1
    """
    from ..providers.store import get_provider

    pc = get_provider(provider_id)
    if pc is None:
        return None
    base = pc.base_url.rstrip("/")
    # gemini 'v1beta/openai' 这种特殊路径直接保留；其他无 /v1 后缀的补一下
    if "/openai" not in base and not base.endswith("/v1"):
        base = base + "/v1"
    return (base, pc.api_key or "")


async def _transcribe_via_api(file_path: str, cfg: VoiceTranscriptionConfig) -> str:
    creds = _get_provider_creds(cfg.provider_id)
    if creds is None:
        raise RuntimeError(f"provider '{cfg.provider_id}' not found")
    base_url, api_key = creds
    if not api_key:
        raise RuntimeError(f"provider '{cfg.provider_id}' has no api_key")

    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RuntimeError("openai package not installed")

    from backend.utils.llm_headers import merge_default_llm_headers

    model_name = cfg.model or "whisper-1"
    # 注入项目级默认 headers：whisper / 兼容 STT 端点也可能走聚合 router，
    # 同样需要伪装成 标准兼容 User-Agent 避免反爬。
    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=60,
        default_headers=merge_default_llm_headers(),
    )
    try:
        with open(file_path, "rb") as f:
            transcript = await client.audio.transcriptions.create(
                model=model_name,
                file=f,
            )
        text = (getattr(transcript, "text", None) or "").strip()
        if not text:
            raise RuntimeError("empty transcription")
        logger.debug("[VOICE-API] transcribed: %s", text[:80])
        return text
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning("[VOICE-API] failed: %s", e, exc_info=True)
        raise RuntimeError(f"api transcription failed: {e}")


# ── local_whisper 引擎 ───────────────────────────────────────────────

# 进程内单例缓存：避免每次重新加载模型（small 也要 5-10 秒）。
# key=model_size，value=faster_whisper.WhisperModel
_LOCAL_MODEL_CACHE: Dict[str, Any] = {}


def _get_local_model(size: str):
    """懒加载 + 进程内缓存。第一次会下载模型文件（small ~250MB）。

    device='auto'：有 CUDA 用 GPU，否则 CPU
    compute_type='auto'：根据 device 选 int8（CPU）或 float16（GPU）
    """
    if size in _LOCAL_MODEL_CACHE:
        return _LOCAL_MODEL_CACHE[size]
    from faster_whisper import WhisperModel

    model = WhisperModel(size, device="auto", compute_type="auto")
    _LOCAL_MODEL_CACHE[size] = model
    return model


async def _transcribe_local(file_path: str, cfg: VoiceTranscriptionConfig) -> str:
    status = check_local_whisper_status()
    if not status["available"]:
        missing = []
        if not status["ffmpeg_installed"]:
            missing.append("ffmpeg (系统级，需要 PATH 可见)")
        if not status["faster_whisper_installed"]:
            missing.append("pip install faster-whisper")
        raise RuntimeError(f"local whisper unavailable, missing: {', '.join(missing)}")

    def _run() -> str:
        model = _get_local_model(cfg.local_model_size)
        # language=None 让 whisper 自动检测语言（中英文混输也 OK）
        segments, _info = model.transcribe(file_path, language=None)
        return " ".join(seg.text.strip() for seg in segments).strip()

    try:
        text = await asyncio.to_thread(_run)
        if not text:
            raise RuntimeError("empty transcription")
        logger.debug("[VOICE-LOCAL] transcribed: %s", text[:80])
        return text
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning("[VOICE-LOCAL] failed: %s", e, exc_info=True)
        raise RuntimeError(f"local transcription failed: {e}")
