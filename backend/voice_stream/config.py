"""voice_stream 配置读写。复用 models.json（顶层独立键 "voice_stream"）。

models.json 顶层结构（本模块只关心 "voice_stream"）：
    {
        "voice_stream": {                  # 流式语音输入（本模块，唯一的语音入口）
            "enabled": true,               # 默认开
            "provider_id": "local",        # 默认本地 SenseVoice（免 key）
            "model": "paraformer-realtime-v2",
            "language_hints": [],          # 空 = 自动检测语种
            "heartbeat": true,            # 长静音保活
            "max_session_seconds": 600
        },
        "providers": { ... }
    }

写入 merge 而非覆盖（保留 providers 等其他顶层字段）。
API key 不在这里存：取 providers 字典里 provider_id 对应条目的 api_key
（models.secrets.json 注入，与 chat 共用同一把 DashScope key）。

历史：旧 "voice_transcription" 键（批量转写，2026-11-15 已整体拆除）如残留在
models.json 里会被本模块忽略，可手动删除。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .. import paths

# 官方默认端点；若用业务空间专属域名可改 models.json 里的 ws_url
DEFAULT_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

# 已核实的 realtime 模型（run-task 协议一致；fun-asr-realtime 额外支持上下文热词）
SUPPORTED_MODELS = [
    "paraformer-realtime-v2",
    "paraformer-realtime-8k-v2",
    "fun-asr-realtime",
    "fun-asr-flash-8k-realtime",
    "qwen-audio-3.1-asr-flash-streaming",
    "qwen-audio-3.0-asr-flash-streaming",
]


class VoiceStreamConfig(BaseModel):
    """流式语音输入配置（models.json "voice_stream" 键）。"""

    enabled: bool = True
    """总开关（默认开）。false 时输入框不渲染麦克风按钮——旧的批量转写模块已拆除，
    没有回退路径，关 = 无语音输入。"""

    provider_id: str = "local"
    """识别引擎，默认本地："local"（本机 SenseVoice，免 key 全离线）/
    "dashscope"（云端 realtime，取 providers 里该 id 的 api_key）/ "mock"（测试用假引擎）。"""

    model: str = "paraformer-realtime-v2"
    """DashScope realtime 模型名（provider_id=dashscope 时生效）。"""

    ws_url: str = DEFAULT_WS_URL
    """上游 ASR WebSocket 端点。"""

    sample_rate: int = 16000
    """音频采样率（前端 PCM 采集固定 16k 单声道）。"""

    language_hints: List[str] = Field(default_factory=list)
    """语种提示，空列表 = 自动检测。"""

    heartbeat: bool = True
    """长静音保活（paraformer v2 支持）。"""

    max_session_seconds: int = 600
    """单次录音上限（服务端二次校验防绕过前端）。"""

    local_model_dir: str = ""
    """本地 SenseVoice 模型目录。空 = 默认 <data_dir>/models/voice_stream/
    （其下应有 model.int8.onnx / tokens.txt / silero_vad.onnx，或同名 release 子目录）。"""

    polish_enabled: bool = False
    """LLM 纠错润色：每句 final 先过一遍 LLM（修错别字/标点）再上屏。"""

    polish_provider_id: str = "dashscope"
    """润色用哪个 provider 的 key + base_url（OpenAI 兼容 chat）。"""

    polish_model: str = "qwen-turbo"
    """润色模型名（便宜快速的即可）。"""


def load_voice_stream_config() -> VoiceStreamConfig:
    """从 models.json 读 voice_stream 字段。缺失 / 损坏返默认。"""
    p = paths.models_json_path()
    if not p.exists():
        return VoiceStreamConfig()
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return VoiceStreamConfig()
    payload = raw.get("voice_stream") or {}
    try:
        return VoiceStreamConfig.model_validate(payload)
    except Exception:
        # schema 升级 / 损坏 → 走默认，不阻断
        return VoiceStreamConfig()


def save_voice_stream_config(cfg: VoiceStreamConfig) -> None:
    """合并写回 models.json，保留其他顶层字段。"""
    p = paths.models_json_path()
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            raw = {}
    else:
        raw = {}
    raw["voice_stream"] = cfg.model_dump()
    p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        from ..providers.store import invalidate_providers_config_cache

        invalidate_providers_config_cache()
    except Exception:
        pass


def resolve_api_key(provider_id: str) -> Optional[str]:
    """从 providers store 取 provider 的 api_key（含 secrets 注入）。不存在返回 None。"""
    try:
        from ..providers.store import get_provider

        pc = get_provider(provider_id)
        if pc is None:
            return None
        return (pc.api_key or "").strip() or None
    except Exception:
        return None


def stream_status() -> Dict[str, Any]:
    """GET /voice/stream/config 的返回体——前端降级判断的唯一依据。

    available = enabled 且所选 provider 可用（mock 恒 True；local 看模型文件；
    其余看 api_key）。
    """
    cfg = load_voice_stream_config()
    from .providers.local import local_model_status

    if cfg.provider_id == "mock":
        available = cfg.enabled
        has_key = True
        reason = "" if cfg.enabled else "voice_stream disabled"
    elif cfg.provider_id == "local":
        has_key = True
        local = local_model_status(cfg)
        model_ready = bool(local["ready"])
        available = cfg.enabled and model_ready
        if not cfg.enabled:
            reason = "voice_stream disabled"
        elif not model_ready:
            reason = f"local model files missing under {local['dir']}"
        else:
            reason = ""
    else:
        key = resolve_api_key(cfg.provider_id)
        has_key = bool(key)
        available = cfg.enabled and has_key
        if not cfg.enabled:
            reason = "voice_stream disabled"
        elif not has_key:
            reason = f"provider '{cfg.provider_id}' has no api_key"
        else:
            reason = ""
    polish_key = resolve_api_key(cfg.polish_provider_id)
    return {
        "enabled": cfg.enabled,
        "available": available,
        "provider": cfg.provider_id,
        "model": cfg.model,
        "has_api_key": has_key,
        "sample_rate": cfg.sample_rate,
        "language_hints": list(cfg.language_hints),
        "heartbeat": cfg.heartbeat,
        "max_session_seconds": cfg.max_session_seconds,
        "reason": reason,
        "supported_models": SUPPORTED_MODELS,
        "polish": {
            "enabled": cfg.polish_enabled,
            "provider_id": cfg.polish_provider_id,
            "model": cfg.polish_model,
            "has_api_key": bool(polish_key),
        },
        "local": local_model_status(cfg),
    }
