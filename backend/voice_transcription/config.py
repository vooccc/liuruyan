"""voice_transcription 独立配置读写（``<data_dir>/voice_transcription.json``）。

零污染契约：
- 独立 JSON 文件，不写进 models.json、不改 backend/paths.py。
- 撤除本模块时删除 backend/voice_transcription/ 与该 JSON 文件即可，对核心配置零残留。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .. import paths

ProviderType = Literal["local_sensevoice", "openai_compat"]
ResponseFormat = Literal["json", "text", "srt", "vtt", "verbose_json"]


def transcription_config_path() -> Path:
    """``<data_dir>/voice_transcription.json`` 绝对路径。"""
    return paths.data_dir() / "voice_transcription.json"


class VoiceTranscriptionConfig(BaseModel):
    """语音转写全量配置。"""

    # ── 全局开关与引擎选择 ──────────────────────────────────────
    enabled: bool = True
    """总开关。False 时 Agent 工具 transcribe_audio 与正式转写接口拒绝执行；设置页测试仍可用。"""

    provider_type: ProviderType = "local_sensevoice"
    """转写引擎：
    - local_sensevoice: 本地 SenseVoice ONNX + Silero VAD（默认，免 key 全离线）
    - openai_compat: OpenAI 兼容 /v1/audio/transcriptions（OpenAI / Groq / SiliconFlow / 本地 Whisper 服务）
    """

    # ── OpenAI 兼容接口配置 ─────────────────────────────────────
    provider_id: str = ""
    """指向 models.json 里的 OpenAI 兼容 provider id（如 'openai' / 'groq' / 'siliconflow'）。
    空字符串表示不使用模型服务商列表，转而使用下方自定义 base_url / api_key 或环境变量兜底。
    """

    base_url: str = ""
    """自定义 OpenAI 兼容 Base URL（仅 provider_id 为空时生效，如 http://127.0.0.1:8000/v1）。"""

    api_key: str = ""
    """自定义 API Key（仅 provider_id 为空时生效；public_view 中脱敏为 has_api_key）。"""

    model: str = "whisper-1"
    """转写模型名（如 whisper-1 / gpt-4o-transcribe / whisper-large-v3-turbo / FunAudioLLM/SenseVoiceSmall）。"""

    response_format: ResponseFormat = "json"
    """接口响应格式（json 最通用；verbose_json 可返回时间戳；不支持时后端会自动回退 json）。"""

    language: str = ""
    """默认语种提示（空串=自动检测；常见：zh / en / ja / yue / ko）。"""

    prompt: str = ""
    """可选词汇/上下文提示（专有名词、人名纠偏；OpenAI 兼容接口生效）。"""

    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    """采样温度（0.0 最稳定）。"""

    timeout_sec: int = Field(default=120, ge=10, le=600)
    """单次转写 HTTP 请求超时（秒）。"""

    # ── 本地 SenseVoice 配置 ────────────────────────────────────
    local_model_dir: str = ""
    """本地 SenseVoice 模型目录。留空则自动优先复用 <data_dir>/models/voice_stream/（与流式语音共享模型），
    其次检查 <data_dir>/models/voice_transcription/。
    """

    local_use_vad: bool = True
    """长音频是否启用 Silero VAD 自动按静音停顿切句（推荐开启，可同时输出分段时间戳）。"""

    local_max_segment_sec: float = Field(default=25.0, ge=5.0, le=60.0)
    """本地单段最大音频时长（秒），防止连续长语音超出模型上下文窗口或内存峰值过高。"""

    local_num_threads: int = Field(default=2, ge=1, le=16)
    """本地 ONNX 推理线程数（默认 2）。"""

    # ── 后处理 ──────────────────────────────────────────────────
    auto_polish: bool = False
    """是否对转写结果做轻量句首语气词清理（默认关闭，保持忠实原文转写）。"""


def load_transcription_config() -> VoiceTranscriptionConfig:
    """从 ``<data_dir>/voice_transcription.json`` 读取配置。缺失或损坏返回默认值。"""
    p = transcription_config_path()
    if not p.exists():
        return VoiceTranscriptionConfig()
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return VoiceTranscriptionConfig()
    try:
        return VoiceTranscriptionConfig.model_validate(raw)
    except Exception:
        return VoiceTranscriptionConfig()


def save_transcription_config(cfg: VoiceTranscriptionConfig) -> None:
    """整文件写回 ``voice_transcription.json``。"""
    p = transcription_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_transcription_config(patch: dict) -> VoiceTranscriptionConfig:
    """partial-update：None 字段保持原状，其余合并校验后写盘。"""
    current = load_transcription_config().model_dump()
    current.update({k: v for k, v in patch.items() if v is not None})
    new_cfg = VoiceTranscriptionConfig.model_validate(current)
    save_transcription_config(new_cfg)
    return new_cfg


def public_view(cfg: Optional[VoiceTranscriptionConfig] = None) -> dict:
    """返回前端可见配置（api_key 脱敏为 has_api_key 布尔，绝不回传明文密钥）。"""
    if cfg is None:
        cfg = load_transcription_config()
    raw = cfg.model_dump()
    raw["has_api_key"] = bool((raw.pop("api_key", "") or "").strip())
    return raw
