"""语音转写路由。

- POST /voice/transcribe — 上传音频 multipart file，返 {text}
- GET  /voice/config     — 当前配置 + 可选 provider 列表 + 本地依赖状态
- PUT  /voice/config     — 更新配置（partial）

设计文档：docs/architecture_spec.md
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from backend.voice_transcription import (
    VoiceTranscriptionConfig,
    check_local_whisper_status,
    load_voice_config,
    save_voice_config,
    transcribe_audio,
)

router = APIRouter(prefix="/voice", tags=["voice"])


# ── /voice/transcribe ────────────────────────────────────────────────


@router.post("/transcribe")
async def transcribe(file: UploadFile = File(...)) -> Dict[str, str]:
    """前端 MediaRecorder 录的 webm / mp4 / wav 上传到这。

    错误码：
    - 400 空文件
    - 403 disabled
    - 413 超过大小上限
    - 500 转写失败（含 provider 未配 / 网络错 / 模型未装 / ...）
    """
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="empty audio")
    try:
        text = await transcribe_audio(audio_bytes, file.filename or "audio.webm")
        return {"text": text}
    except RuntimeError as e:
        msg = str(e)
        if "disabled" in msg:
            raise HTTPException(status_code=403, detail=msg)
        if "too large" in msg:
            raise HTTPException(status_code=413, detail=msg)
        raise HTTPException(status_code=500, detail=msg)


# ── /voice/config GET ────────────────────────────────────────────────


# OpenRouter 只代理 chat，不支持 /audio/transcriptions —— 排除掉避免用户误配
_PROVIDER_TRANSCRIBE_BLACKLIST = {"openrouter"}

# 各 provider 推荐的转写模型（前端默认填，可改）
_RECOMMENDED_MODELS: Dict[str, str] = {
    "siliconflow": "FunAudioLLM/SenseVoiceSmall",
    "openai": "whisper-1",
    "groq": "whisper-large-v3-turbo",
    "dashscope": "paraformer-realtime-v2",
    "modelscope": "iic/SenseVoiceSmall",
}


@router.get("/config")
async def get_config() -> Dict[str, Any]:
    """返当前配置 + 可选 provider 列表 + 本地 whisper 状态 + 推荐模型映射。"""
    from backend.providers.store import load_providers_config_cached

    cfg = load_voice_config()
    providers_cfg = load_providers_config_cached()

    eligible: List[Dict[str, Any]] = []
    for pid, pc in providers_cfg["providers"].items():
        if pid in _PROVIDER_TRANSCRIBE_BLACKLIST:
            continue
        if pc.transport != "openai_chat":
            continue
        if not pc.enabled:
            continue
        eligible.append(
            {
                "id": pc.id,
                "name": pc.name,
                "has_api_key": bool(pc.api_key),
                "is_aggregator": pc.is_aggregator,
            }
        )

    return {
        "config": cfg.model_dump(),
        "eligible_providers": eligible,
        "local_whisper_status": check_local_whisper_status(),
        "recommended_models": _RECOMMENDED_MODELS,
    }


# ── /voice/config PUT ────────────────────────────────────────────────


class VoiceConfigUpdate(BaseModel):
    """PUT /voice/config 的 partial-update body。None 表示不动。"""

    provider_type: Optional[str] = None
    provider_id: Optional[str] = None
    model: Optional[str] = None
    local_model_size: Optional[str] = None
    max_duration_seconds: Optional[int] = None
    max_audio_size_mb: Optional[int] = None


@router.put("/config")
async def update_config(body: VoiceConfigUpdate) -> Dict[str, Any]:
    cfg = load_voice_config()
    data = cfg.model_dump()
    update = body.model_dump(exclude_none=True)
    data.update(update)
    try:
        new_cfg = VoiceTranscriptionConfig.model_validate(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid config: {e}")
    save_voice_config(new_cfg)
    return {"status": "success", "config": new_cfg.model_dump()}
