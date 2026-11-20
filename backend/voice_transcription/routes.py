"""voice_transcription REST 路由（孤立包自带，不经 backend/routes/__init__.py）。

端点：
- GET  /voice/transcription/config          获取当前配置（脱敏）、可用服务商与本地模型状态
- PUT  /voice/transcription/config          partial-update 配置
- POST /voice/transcription/local/download  一键后台下载本地 SenseVoice 三件套
- POST /voice/transcription/transcribe      上传音视频文件执行转写（支持设置页测试与临时 override）

零污染装配：main.py 仅需 2 行 include_router；删除本目录即可完全撤除。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from .config import (
    ProviderType,
    ResponseFormat,
    VoiceTranscriptionConfig,
    load_transcription_config,
    public_view,
    update_transcription_config,
)
from .engine import TranscriptionError, transcribe_bytes, transcription_status
from .local_model import start_model_download

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice/transcription", tags=["voice-transcription"])

_MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200MB

# OpenRouter 仅代理 chat，不支持 /audio/transcriptions
_PROVIDER_BLACKLIST = {"openrouter", "deepseek", "anthropic", "zhipu", "kimi"}

_RECOMMENDED_MODELS: Dict[str, List[str]] = {
    "openai": [
        "gpt-4o-transcribe",
        "gpt-4o-mini-transcribe",
        "whisper-1",
        "gpt-4o-transcribe-diarize",
    ],
    "groq": [
        "whisper-large-v3-turbo",
        "whisper-large-v3",
    ],
    "siliconflow": [
        "FunAudioLLM/SenseVoiceSmall",
        "TeleAI/TeleSpeechASR",
    ],
    "_default": [
        "whisper-1",
        "gpt-4o-transcribe",
        "gpt-4o-mini-transcribe",
        "whisper-large-v3-turbo",
        "FunAudioLLM/SenseVoiceSmall",
    ],
}


def _list_eligible_providers() -> List[Dict[str, Any]]:
    """从 providers store 筛选可走 OpenAI 兼容协议的服务商（只读）。"""
    try:
        from backend.providers.store import load_providers_config_cached
    except Exception:
        return []

    cfg = load_providers_config_cached()
    out: List[Dict[str, Any]] = []
    for pid, pc in cfg.get("providers", {}).items():
        if pid in _PROVIDER_BLACKLIST:
            continue
        if pc.transport != "openai_chat":
            continue
        if not pc.enabled:
            continue
        out.append(
            {
                "id": pc.id,
                "name": pc.name or pc.id,
                "has_api_key": bool(pc.api_key),
                "is_aggregator": pc.is_aggregator,
                "base_url": pc.base_url or "",
                "models": [{"id": m.id, "name": m.name or m.id} for m in pc.models],
            }
        )
    return out


@router.get("/config")
async def get_transcription_config() -> Dict[str, Any]:
    """设置面板加载入口。"""
    cfg = load_transcription_config()
    return {
        "config": public_view(cfg),
        "eligible_providers": _list_eligible_providers(),
        "recommended_models": _RECOMMENDED_MODELS,
        **transcription_status(cfg),
    }


class VoiceTranscriptionConfigUpdate(BaseModel):
    """PUT /voice/transcription/config 的 partial-update body。None 表示不改。"""

    enabled: Optional[bool] = None
    provider_type: Optional[ProviderType] = None
    provider_id: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    response_format: Optional[ResponseFormat] = None
    language: Optional[str] = None
    prompt: Optional[str] = None
    temperature: Optional[float] = None
    timeout_sec: Optional[int] = None
    local_model_dir: Optional[str] = None
    local_use_vad: Optional[bool] = None
    local_max_segment_sec: Optional[float] = None
    local_num_threads: Optional[int] = None
    auto_polish: Optional[bool] = None


@router.put("/config")
async def put_transcription_config(body: VoiceTranscriptionConfigUpdate) -> Dict[str, Any]:
    """partial-update 语音转写配置并立即写盘。"""
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        new_cfg = update_transcription_config(patch)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"配置无效：{e}")
    return {
        "status": "ok",
        "config": public_view(new_cfg),
        **transcription_status(new_cfg),
    }


@router.post("/local/download")
async def start_local_transcription_download() -> Dict[str, Any]:
    """启动本地 SenseVoice 模型三件套后台下载（幂等）。"""
    started = start_model_download()
    return {
        "status": "ok",
        "started": started,
        **transcription_status(),
    }


@router.post("/transcribe")
async def transcribe_upload(
    file: UploadFile = File(..., description="音频或视频文件"),
    language: Optional[str] = Form(default=None),
    prompt: Optional[str] = Form(default=None),
    model: Optional[str] = Form(default=None),
    provider_type: Optional[str] = Form(default=None),
    include_timestamps: bool = Form(default=False),
    test: bool = Form(default=False),
    override_json: Optional[str] = Form(default=None),
) -> Dict[str, Any]:
    """上传音频文件并转写为文字。"""
    cfg = load_transcription_config()
    if override_json:
        try:
            ov = json.loads(override_json)
            if isinstance(ov, dict):
                merged = cfg.model_dump()
                merged.update({k: v for k, v in ov.items() if v is not None})
                cfg = VoiceTranscriptionConfig.model_validate(merged)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"override_json 无效：{e}")

    if not test and not cfg.enabled:
        raise HTTPException(status_code=403, detail="语音转写已在设置中停用")

    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="请提供要转写的音频文件")

    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
            await file.close()
            raise HTTPException(
                status_code=413,
                detail=f"文件体积过大（限制 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB）",
            )
        chunks.append(chunk)
    audio_bytes = b"".join(chunks)

    try:
        result = await transcribe_bytes(
            audio_bytes,
            filename=file.filename,
            cfg_override=cfg,
            language=language if language not in (None, "") else None,
            prompt=prompt if prompt not in (None, "") else None,
            model=model if model not in (None, "") else None,
            provider_type=provider_type if provider_type not in (None, "") else None,
            include_timestamps=include_timestamps,
        )
    except TranscriptionError as e:
        raise HTTPException(status_code=400, detail=e.message)
    except Exception as e:  # noqa: BLE001
        logger.exception("[VOICE-TRANSCRIPTION] unexpected error")
        raise HTTPException(status_code=500, detail=f"转写内部错误：{e}")

    return {
        "status": "ok",
        **result.to_dict(include_timestamps=include_timestamps),
    }
