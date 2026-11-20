"""语音转写统一引擎：本地 SenseVoice (sherpa-onnx) + OpenAI 兼容 /v1/audio/transcriptions。

并发安全契约（.lry/rules/01-singleton-no-instance-state.md）：
- 缓存的仅是无状态 ONNX Recognizer 模板（受 threading.Lock 保护），每次转写新建 stream 与 VAD 实例，
  绝不保存 per-call 状态。
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import numpy as np

from backend.utils.llm_headers import merge_default_llm_headers

from .audio_decode import (
    API_SUPPORTED_EXTENSIONS,
    WINRT_NATIVE_EXTENSIONS,
    AudioDecodeError,
    TARGET_SAMPLE_RATE,
    decode_to_pcm16k,
    has_ffmpeg,
    has_winrt_audio_decoder,
    split_audio_for_api,
)
from .config import VoiceTranscriptionConfig, load_transcription_config
from .credentials import env_fallback_info, resolve_credentials
from .local_model import local_model_status, resolve_local_model_paths

logger = logging.getLogger(__name__)

# SenseVoice 支持显式 language 提示的值；其余值回退自动检测 ""
_SENSEVOICE_VALID_LANGS = {"", "auto", "zh", "en", "ja", "ko", "yue"}
_TAG_RE = re.compile(r"<\|[^|]+\|>")
_FILLER_RE = re.compile(
    r"(?:(?<=^)|(?<=[，。？！,\.\!\?；;、…\s]))([呃嗯啊哎噢哦]+)[，。？！,\.\!\?；;、…\s]*"
)

_MIME_OVERRIDES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".mpeg": "audio/mpeg",
    ".mpga": "audio/mpeg",
}


class TranscriptionError(Exception):
    """语音转写失败（配置缺失 / 音频无法解码 / 上游错误）。"""

    def __init__(self, message: str, *, upstream: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.message = message
        self.upstream = upstream


@dataclass
class TranscriptionSegment:
    start: Optional[float]
    end: Optional[float]
    text: str
    speaker: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"text": self.text}
        if self.start is not None:
            d["start"] = round(self.start, 2)
        if self.end is not None:
            d["end"] = round(self.end, 2)
        if self.speaker:
            d["speaker"] = self.speaker
        return d


@dataclass
class TranscriptionResult:
    text: str
    segments: List[TranscriptionSegment] = field(default_factory=list)
    backend: str = ""
    model: str = ""
    duration_sec: Optional[float] = None
    language: Optional[str] = None
    chunks_count: int = 1

    def to_dict(self, include_timestamps: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "text": self.text,
            "backend": self.backend,
            "model": self.model,
            "chunks_count": self.chunks_count,
        }
        if self.duration_sec is not None:
            out["duration_sec"] = round(self.duration_sec, 2)
        if self.language:
            out["language"] = self.language
        if include_timestamps or self.segments:
            out["segments"] = [s.to_dict() for s in self.segments]
        return out


# ── 进程级无状态 Recognizer 缓存 ───────────────────────────────────────

_rec_lock = threading.Lock()
_rec_cache: Dict[Tuple[str, str, str, int], Any] = {}


def _get_sensevoice_recognizer(
    model_path: Path,
    tokens_path: Path,
    language: str,
    num_threads: int,
) -> Any:
    import sherpa_onnx

    lang_norm = (language or "").strip().lower()
    if lang_norm not in _SENSEVOICE_VALID_LANGS:
        lang_norm = ""
    if lang_norm == "auto":
        lang_norm = ""

    key = (str(model_path), str(tokens_path), lang_norm, int(num_threads))
    with _rec_lock:
        rec = _rec_cache.get(key)
        if rec is None:
            # 最多保留 2 个不同语种实例，避免内存无限增长
            if len(_rec_cache) >= 2:
                _rec_cache.clear()
            rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model_path),
                tokens=str(tokens_path),
                num_threads=int(num_threads),
                language=lang_norm,
                use_itn=True,
            )
            _rec_cache[key] = rec
        return rec


def _clean_sensevoice_text(raw: str) -> str:
    if not raw:
        return ""
    cleaned = _TAG_RE.sub("", raw)
    return cleaned.strip()


def _cleanup_fillers(text: str) -> str:
    if not text:
        return text
    out = _FILLER_RE.sub("", text).strip()
    return out or text.strip()


def _split_long_pcm_segment(
    samples: np.ndarray,
    start_sample: int,
    max_samples: int,
    sr: int = TARGET_SAMPLE_RATE,
) -> List[Tuple[int, np.ndarray]]:
    """将超长语音段在局部低能量停顿点切分为多段（每段 <= max_samples）。"""
    n = samples.shape[0]
    if n <= max_samples:
        return [(start_sample, samples)]

    out: List[Tuple[int, np.ndarray]] = []
    cur = 0
    lookback = int(3.0 * sr)
    win = int(0.05 * sr)  # 50ms 能量窗
    while cur < n:
        end = min(n, cur + max_samples)
        if end < n:
            search_lo = max(cur + int(1.0 * sr), end - lookback)
            best_pos = end
            best_energy = float("inf")
            pos = search_lo
            while pos + win <= end:
                frame = samples[pos : pos + win]
                e = float(np.mean(np.square(frame)))
                if e < best_energy:
                    best_energy = e
                    best_pos = pos + win // 2
                pos += win
            end = best_pos
        seg = samples[cur:end]
        if seg.shape[0] >= int(0.15 * sr):
            out.append((start_sample + cur, seg))
        cur = end
    return out


def _recognize_local_sync(
    audio_bytes: bytes,
    filename: str,
    cfg: VoiceTranscriptionConfig,
    language: Optional[str],
    include_timestamps: bool,
) -> TranscriptionResult:
    model_paths = resolve_local_model_paths(cfg)
    if model_paths is None:
        raise TranscriptionError(
            "本地 SenseVoice 模型尚未就绪（缺少 model.int8.onnx / tokens.txt）。"
            "请前往「设置 → 辅助功能 → 语音转写」点击一键下载模型，或切换为 OpenAI 兼容接口。"
        )

    try:
        samples, duration_sec = decode_to_pcm16k(audio_bytes, filename)
    except AudioDecodeError as e:
        raise TranscriptionError(str(e), upstream=e) from e

    if samples.size == 0:
        return TranscriptionResult(
            text="",
            segments=[],
            backend="local_sensevoice",
            model="sensevoice-small-int8",
            duration_sec=duration_sec,
            language=language or cfg.language or None,
        )

    eff_lang = (language if language is not None else cfg.language) or ""
    recognizer = _get_sensevoice_recognizer(
        model_paths["model"],  # type: ignore[arg-type]
        model_paths["tokens"],  # type: ignore[arg-type]
        eff_lang,
        cfg.local_num_threads,
    )

    max_seg_samples = max(TARGET_SAMPLE_RATE * 5, int(cfg.local_max_segment_sec * TARGET_SAMPLE_RATE))
    raw_segments: List[Tuple[int, np.ndarray]] = []

    vad_path = model_paths.get("vad")
    if cfg.local_use_vad and vad_path is not None:
        import sherpa_onnx

        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = str(vad_path)
        vad_config.sample_rate = TARGET_SAMPLE_RATE
        vad_config.silero_vad.min_silence_duration = 0.5
        vad_config.silero_vad.min_speech_duration = 0.2
        if hasattr(vad_config.silero_vad, "max_speech_duration"):
            vad_config.silero_vad.max_speech_duration = float(cfg.local_max_segment_sec)

        vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=120)
        window_size = 512
        total = samples.shape[0]
        for offset in range(0, total, window_size):
            chunk = samples[offset : offset + window_size]
            if chunk.shape[0] < window_size:
                chunk = np.pad(chunk, (0, window_size - chunk.shape[0]))
            vad.accept_waveform(chunk)
            while not vad.empty:
                seg = vad.front
                vad.pop()
                seg_samples = np.asarray(seg.samples, dtype=np.float32)
                seg_start = int(getattr(seg, "start", 0))
                raw_segments.extend(_split_long_pcm_segment(seg_samples, seg_start, max_seg_samples))
        vad.flush()
        while not vad.empty:
            seg = vad.front
            vad.pop()
            seg_samples = np.asarray(seg.samples, dtype=np.float32)
            seg_start = int(getattr(seg, "start", 0))
            raw_segments.extend(_split_long_pcm_segment(seg_samples, seg_start, max_seg_samples))

    # VAD 关闭、缺失或未检出段落时，退化为整段/能量切分兜底
    if not raw_segments:
        raw_segments = _split_long_pcm_segment(samples, 0, max_seg_samples)

    segments: List[TranscriptionSegment] = []
    texts: List[str] = []
    for start_sample, seg_samples in raw_segments:
        if seg_samples.shape[0] < int(0.1 * TARGET_SAMPLE_RATE):
            continue
        stream = recognizer.create_stream()
        stream.accept_waveform(TARGET_SAMPLE_RATE, seg_samples)
        recognizer.decode_stream(stream)
        seg_text = _clean_sensevoice_text(getattr(stream.result, "text", "") or "")
        if not seg_text:
            continue
        texts.append(seg_text)
        if include_timestamps:
            start_sec = float(start_sample) / float(TARGET_SAMPLE_RATE)
            end_sec = float(start_sample + seg_samples.shape[0]) / float(TARGET_SAMPLE_RATE)
            segments.append(TranscriptionSegment(start=start_sec, end=end_sec, text=seg_text))

    full_text = "".join(texts).strip()
    if cfg.auto_polish and full_text:
        full_text = _cleanup_fillers(full_text)

    return TranscriptionResult(
        text=full_text,
        segments=segments if include_timestamps else [],
        backend="local_sensevoice",
        model="sensevoice-small-int8",
        duration_sec=duration_sec,
        language=eff_lang or "auto",
        chunks_count=max(1, len(raw_segments)),
    )


# ── OpenAI 兼容 /v1/audio/transcriptions ───────────────────────────────


def _build_transcriptions_url(base_url: str) -> str:
    b = base_url.strip().rstrip("/")
    if b.endswith("/audio/transcriptions"):
        return b
    return f"{b}/audio/transcriptions"


def _is_local_url(url: str) -> bool:
    low = url.lower()
    return "://127.0.0.1" in low or "://localhost" in low or "://[::1]" in low


def _guess_mime(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _raise_for_api_status(resp: httpx.Response) -> None:
    code = resp.status_code
    try:
        preview = (resp.text or "").strip()
    except Exception:
        preview = "<unreadable>"
    if len(preview) > 240:
        preview = preview[:240] + "…"

    if code in (401, 403):
        raise TranscriptionError(f"语音转写鉴权失败 ({code})：请检查 API Key。上游：{preview}")
    if code == 404:
        raise TranscriptionError(f"语音转写端点或模型不存在 ({code})：请检查 Base URL 与 Model。上游：{preview}")
    if code == 413:
        raise TranscriptionError(f"音频文件体积超出上游限制 (413)：{preview}")
    if code == 429:
        raise TranscriptionError(f"上游限流 (429)：请稍后重试或更换 Key。上游：{preview}")
    if 500 <= code < 600:
        raise TranscriptionError(f"上游服务异常 ({code})：{preview}")
    raise TranscriptionError(f"语音转写请求失败 ({code})：{preview}")


def _parse_api_response(
    resp: httpx.Response,
    resp_fmt: str,
    offset_sec: float,
) -> Tuple[str, List[TranscriptionSegment], Optional[float], Optional[str]]:
    ct = (resp.headers.get("content-type") or "").lower()
    if resp_fmt in ("text", "srt", "vtt") or "application/json" not in ct:
        text = (resp.text or "").strip()
        return text, [], None, None

    try:
        obj = resp.json()
    except Exception:
        return (resp.text or "").strip(), [], None, None

    if not isinstance(obj, dict):
        return str(obj).strip(), [], None, None

    text = str(obj.get("text") or "").strip()
    duration = obj.get("duration")
    dur_val = float(duration) if isinstance(duration, (int, float)) else None
    lang = str(obj.get("language") or "").strip() or None

    segs: List[TranscriptionSegment] = []
    raw_segs = obj.get("segments")
    if isinstance(raw_segs, list):
        for item in raw_segs:
            if not isinstance(item, dict):
                continue
            st = item.get("start")
            ed = item.get("end")
            st_f = (float(st) + offset_sec) if isinstance(st, (int, float)) else None
            ed_f = (float(ed) + offset_sec) if isinstance(ed, (int, float)) else None
            seg_text = str(item.get("text") or "").strip()
            speaker = str(item.get("speaker") or "").strip() or None
            if seg_text:
                segs.append(TranscriptionSegment(start=st_f, end=ed_f, text=seg_text, speaker=speaker))
    return text, segs, dur_val, lang


async def _transcribe_openai_compat(
    audio_bytes: bytes,
    filename: str,
    cfg: VoiceTranscriptionConfig,
    *,
    language: Optional[str],
    prompt: Optional[str],
    model: Optional[str],
    include_timestamps: bool,
) -> TranscriptionResult:
    creds = resolve_credentials(cfg)
    if not creds.base_url:
        raise TranscriptionError(
            "未配置 OpenAI 兼容语音转写服务商。请前往「设置 → 辅助功能 → 语音转写」选择 Provider 或填写 Base URL。"
        )
    if not creds.api_key and not _is_local_url(creds.base_url):
        raise TranscriptionError(
            "所选语音转写服务商尚未配置 API Key。请前往「设置 → 模型服务」或「语音转写」面板填写 Key。"
        )

    try:
        chunks = split_audio_for_api(audio_bytes, filename)
    except AudioDecodeError as e:
        raise TranscriptionError(str(e), upstream=e) from e

    url = _build_transcriptions_url(creds.base_url)
    headers = merge_default_llm_headers(creds.extra_headers, transport="openai_chat")
    headers.pop("Content-Type", None)
    if creds.api_key:
        headers["Authorization"] = f"Bearer {creds.api_key}"

    eff_model = (model or cfg.model or "whisper-1").strip()
    eff_lang = (language if language is not None else cfg.language).strip()
    eff_prompt = (prompt if prompt is not None else cfg.prompt).strip()

    req_fmt = cfg.response_format or "json"
    if include_timestamps and req_fmt == "json":
        req_fmt = "verbose_json"

    all_texts: List[str] = []
    all_segments: List[TranscriptionSegment] = []
    total_duration: Optional[float] = None
    detected_lang: Optional[str] = eff_lang or None

    async with httpx.AsyncClient(timeout=float(cfg.timeout_sec)) as client:
        for chunk_bytes, chunk_name, offset_sec in chunks:
            data: Dict[str, str] = {
                "model": eff_model,
                "response_format": req_fmt,
            }
            if eff_lang:
                data["language"] = eff_lang
            if eff_prompt:
                data["prompt"] = eff_prompt
            if cfg.temperature > 0:
                data["temperature"] = str(cfg.temperature)

            files = {"file": (chunk_name, chunk_bytes, _guess_mime(chunk_name))}
            try:
                resp = await client.post(url, data=data, files=files, headers=headers)
            except httpx.TimeoutException as e:
                raise TranscriptionError("语音转写请求超时，可在设置中调大超时时间或切本地引擎", upstream=e) from e
            except httpx.HTTPError as e:
                raise TranscriptionError(f"语音转写网络错误：{e}", upstream=e) from e

            # gpt-4o-transcribe 等模型不支持 verbose_json 时自动降级为 json 重试一次
            if resp.status_code == 400 and req_fmt == "verbose_json":
                body_low = (resp.text or "").lower()
                if "response_format" in body_low or "verbose_json" in body_low:
                    data["response_format"] = "json"
                    req_fmt = "json"
                    files = {"file": (chunk_name, chunk_bytes, _guess_mime(chunk_name))}
                    resp = await client.post(url, data=data, files=files, headers=headers)

            if resp.status_code != 200:
                _raise_for_api_status(resp)

            c_text, c_segs, c_dur, c_lang = _parse_api_response(resp, req_fmt, offset_sec)
            if c_text:
                all_texts.append(c_text)
            all_segments.extend(c_segs)
            if c_dur is not None:
                total_duration = (total_duration or 0.0) + c_dur
            if c_lang and not detected_lang:
                detected_lang = c_lang

    joined = "\n".join(all_texts).strip() if len(chunks) > 1 else "".join(all_texts).strip()
    if cfg.auto_polish and joined and req_fmt not in ("srt", "vtt"):
        joined = _cleanup_fillers(joined)

    return TranscriptionResult(
        text=joined,
        segments=all_segments if include_timestamps else [],
        backend="openai_compat",
        model=eff_model,
        duration_sec=total_duration,
        language=detected_lang,
        chunks_count=len(chunks),
    )


# ── 对外统一入口 ────────────────────────────────────────────────────────


async def transcribe_bytes(
    audio_bytes: bytes,
    filename: str = "audio.wav",
    *,
    cfg_override: Optional[VoiceTranscriptionConfig] = None,
    language: Optional[str] = None,
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    provider_type: Optional[str] = None,
    include_timestamps: bool = False,
) -> TranscriptionResult:
    """将音频字节流转写为文字。"""
    cfg = cfg_override or load_transcription_config()
    eff_backend = provider_type or cfg.provider_type

    if eff_backend == "local_sensevoice":
        return await asyncio.to_thread(
            _recognize_local_sync,
            audio_bytes,
            filename,
            cfg,
            language,
            include_timestamps,
        )
    if eff_backend == "openai_compat":
        return await _transcribe_openai_compat(
            audio_bytes,
            filename,
            cfg,
            language=language,
            prompt=prompt,
            model=model,
            include_timestamps=include_timestamps,
        )
    raise TranscriptionError(f"不支持的语音转写引擎类型: {eff_backend}")


async def transcribe_file(
    file_path: str | Path,
    *,
    cfg_override: Optional[VoiceTranscriptionConfig] = None,
    language: Optional[str] = None,
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    provider_type: Optional[str] = None,
    include_timestamps: bool = False,
) -> TranscriptionResult:
    """读取本地音视频文件并转写为文字。"""
    p = Path(file_path)
    if not p.exists():
        raise TranscriptionError(f"文件不存在: {file_path}")
    if not p.is_file():
        raise TranscriptionError(f"目标不是文件: {file_path}")
    try:
        audio_bytes = await asyncio.to_thread(p.read_bytes)
    except OSError as e:
        raise TranscriptionError(f"读取音频文件失败: {e}", upstream=e) from e

    return await transcribe_bytes(
        audio_bytes,
        filename=p.name,
        cfg_override=cfg_override,
        language=language,
        prompt=prompt,
        model=model,
        provider_type=provider_type,
        include_timestamps=include_timestamps,
    )


def transcription_status(cfg: Optional[VoiceTranscriptionConfig] = None) -> Dict[str, Any]:
    """计算当前转写模块可用性、本地模型状态与能力摘要（供设置页使用）。"""
    if cfg is None:
        cfg = load_transcription_config()
    local = local_model_status(cfg)
    ffmpeg_ok = has_ffmpeg()
    winrt_ok = has_winrt_audio_decoder()
    env_fb = env_fallback_info()

    if not cfg.enabled:
        available = False
        reason = "语音转写已在设置中停用"
    elif cfg.provider_type == "local_sensevoice":
        available = bool(local["ready"])
        reason = "" if available else f"本地模型文件未就绪（目录：{local['dir']}）"
    else:
        creds = resolve_credentials(cfg)
        if not creds.base_url:
            available = False
            reason = "未选择服务商或填写 Base URL"
        elif not creds.api_key and not _is_local_url(creds.base_url):
            available = False
            reason = "未配置 API Key"
        else:
            available = True
            reason = ""

    return {
        "enabled": cfg.enabled,
        "available": available,
        "reason": reason,
        "local": local,
        "capabilities": {
            "ffmpeg": ffmpeg_ok,
            "local_winrt": winrt_ok,
            "local_native_formats": list(WINRT_NATIVE_EXTENSIONS) if winrt_ok else [".wav"],
            "local_all_formats": ffmpeg_ok,
            "api_formats": list(API_SUPPORTED_EXTENSIONS),
        },
        "env_fallback": env_fb,
    }
