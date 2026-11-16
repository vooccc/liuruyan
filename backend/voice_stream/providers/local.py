"""本地 SenseVoice 流式识别 provider（sherpa-onnx，免 key、全离线）。

配方（借自 blurt 的 local-first 管线 + sherpa-onnx 官方示例）：
- SenseVoice 本身是离线（非流式）模型，"流式"由两层拼出来：
  1) silero VAD 实时切分语音段：静音超阈值 = 一段结束 → 整段送 SenseVoice → final
  2) 说话过程中每 PARTIAL_INTERVAL 秒对"当前段已积累音频"做一次识别 → partial
     （SenseVoice small 在 CPU 上 RTF << 1，几百毫秒延迟可接受）
- 模型：model.int8.onnx（~230MB，中英日韩粤）+ tokens.txt + silero_vad.onnx
- 模型加载是进程级单例：多个会话/多次录音复用，不重复吃内存。

模型目录解析（resolve_local_model_paths）：
  <dir>[/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/]/
      model.int8.onnx  tokens.txt  silero_vad.onnx
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional

import numpy as np

from ..config import VoiceStreamConfig
from .base import ProviderEvent, ProviderEventCallback, RealtimeProvider

# 说话中 partial 的刷新间隔（秒，音频时长）
_PARTIAL_INTERVAL = 0.8
# partial 识别窗口上限（秒）：只识别当前段尾部这么长，控制 CPU 峰值
_PARTIAL_WINDOW_SECONDS = 10.0
# VAD 静音判定（秒）：低于此值认为句子还没说完
_MIN_SILENCE = 0.5
# VAD 最短语音段（秒）：过短的片段丢弃（呼吸/碰麦）
_MIN_SPEECH = 0.2

_RELEASE_DIR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"


# ── 模型文件解析 + 就绪检查 ────────────────────────────────────────────

def default_local_model_dir() -> Path:
    from ... import paths

    return paths.data_dir() / "models" / "voice_stream"


def resolve_local_model_paths(cfg: VoiceStreamConfig) -> Optional[dict]:
    """返回 {"model": Path, "tokens": Path, "vad": Path}；不齐返 None。"""
    root = Path(cfg.local_model_dir) if cfg.local_model_dir else default_local_model_dir()
    if not root.is_dir():
        return None
    candidates = [root, root / _RELEASE_DIR]
    for base in candidates:
        model = base / "model.int8.onnx"
        tokens = base / "tokens.txt"
        vad = base / "silero_vad.onnx"
        if model.is_file() and tokens.is_file() and vad.is_file():
            return {"model": model, "tokens": tokens, "vad": vad}
    return None


def local_model_status(cfg: VoiceStreamConfig) -> dict:
    """设置页 / stream_status 用的就绪信息。"""
    p = resolve_local_model_paths(cfg)
    return {
        "dir": str(Path(cfg.local_model_dir) if cfg.local_model_dir else default_local_model_dir()),
        "ready": p is not None,
        "model": str(p["model"]) if p else "",
    }


# ── 引擎单例（进程级，所有会话共享） ──────────────────────────────────

class _Engine:
    """加载一次、永久复用的 recognizer 模板 + VAD 配置（Vad 实例每会话新建）。"""

    def __init__(self, paths_: dict) -> None:
        import sherpa_onnx

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(paths_["model"]),
            tokens=str(paths_["tokens"]),
            use_itn=True,
        )
        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = str(paths_["vad"])
        vad_config.sample_rate = 16000
        vad_config.silero_vad.min_silence_duration = _MIN_SILENCE
        vad_config.silero_vad.min_speech_duration = _MIN_SPEECH
        self.vad_config = vad_config
        self.sample_rate = 16000


_engine: Optional[_Engine] = None
_engine_paths: Optional[str] = None


def _get_engine(paths_: dict) -> _Engine:
    global _engine, _engine_paths
    key = str(paths_["model"])
    if _engine is None or _engine_paths != key:
        _engine = _Engine(paths_)
        _engine_paths = key
    return _engine


def _recognize_sync(engine: _Engine, samples: np.ndarray) -> str:
    """阻塞识别（线程池里跑）。samples: float32 [-1,1]。"""
    import sherpa_onnx

    stream = engine.recognizer.create_stream()
    stream.accept_waveform(engine.sample_rate, samples)
    engine.recognizer.decode_stream(stream)
    return (stream.result.text or "").strip()


# ── Provider ──────────────────────────────────────────────────────────

class LocalSenseVoiceProvider(RealtimeProvider):
    """一个实例 = 一次识别任务。不可复用。"""

    def __init__(self, cfg: VoiceStreamConfig) -> None:
        self._cfg = cfg
        self._on_event: Optional[ProviderEventCallback] = None
        self._engine: Optional[_Engine] = None
        self._vad = None
        self._speech: List[np.ndarray] = []   # 当前语音段（float32 样本块）
        self._speech_len = 0                  # 当前段样本数
        self._last_partial_len = 0            # 上次 partial 时的段长度
        self._sentences: List[str] = []
        self._pool: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False
        self._pending: Optional[asyncio.Future] = None

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self, on_event: ProviderEventCallback) -> None:
        self._on_event = on_event
        model_paths = resolve_local_model_paths(self._cfg)
        if model_paths is None:
            raise RuntimeError(
                f"local model files not found under "
                f"{self._cfg.local_model_dir or default_local_model_dir()}"
            )
        self._engine = _get_engine(model_paths)
        import sherpa_onnx

        # sherpa-onnx >= 2.0：流式 VAD 类是 VoiceActivityDetector（snake_case API）
        self._vad = sherpa_onnx.VoiceActivityDetector(
            self._engine.vad_config, buffer_size_in_seconds=120
        )
        await self._emit(ProviderEvent.started())

    async def feed(self, frame: bytes) -> None:
        if self._engine is None or self._vad is None or self._closed:
            return
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        self._vad.accept_waveform(samples)
        # 收割已完成的语音段（final；段缓冲清零，final 用 VAD 自己的完整段）
        # 注：sherpa-onnx >= 2.0 的 empty / front / is_speech_detected 是属性
        while not self._vad.empty:
            segment = self._vad.front
            self._vad.pop()
            seg = np.asarray(segment.samples, dtype=np.float32)
            self._speech, self._speech_len, self._last_partial_len = [], 0, 0
            await self._recognize_segment(seg)
        # 说话中 → 积累段音频 + 定时 partial
        if self._vad.is_speech_detected:
            self._append_speech(samples)
            if (
                self._speech_len - self._last_partial_len
                >= _PARTIAL_INTERVAL * self._engine.sample_rate
            ):
                self._last_partial_len = self._speech_len
                await self._emit_partial()

    async def finish(self) -> None:
        if self._engine is None or self._closed:
            return
        # 1) 段内残余（VAD 还没判定静音）：不足最短语音则丢弃
        if self._speech_len >= _MIN_SPEECH * self._engine.sample_rate:
            tail = np.concatenate(self._speech) if self._speech else np.zeros(0, np.float32)
            await self._recognize_segment(tail)
        self._speech, self._speech_len, self._last_partial_len = [], 0, 0
        # 2) 等在途识别完成后发 finished
        await self._drain_pending()
        await self._emit(ProviderEvent.finished("".join(self._sentences)))

    async def close(self) -> None:
        self._closed = True
        self._speech, self._speech_len = [], 0

    # ── 内部 ──────────────────────────────────────────────

    def _append_speech(self, samples: np.ndarray) -> None:
        """说话中的音频自行累积（VAD 只在段完成时吐完整段，说话中不保留）。"""
        self._speech.append(samples)
        self._speech_len += samples.shape[0]

    async def _recognize_segment(self, seg: np.ndarray) -> None:
        """一段完整语音 → final。识别在线程池里跑，不卡事件循环。"""
        if seg.shape[0] < _MIN_SPEECH * 16000 or self._engine is None:
            return
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, _recognize_sync, self._engine, seg)
        self._pending = fut
        try:
            text = await fut
        except Exception as e:
            self._pending = None
            await self._emit(ProviderEvent.failed("local_asr_error", str(e)))
            return
        self._pending = None
        if text:
            self._sentences.append(text)
            await self._emit(ProviderEvent.final(text))

    async def _emit_partial(self) -> None:
        """当前段尾部窗口 → partial（临时预览，不上输入框）。"""
        if self._engine is None or not self._speech:
            return
        window = int(_PARTIAL_WINDOW_SECONDS * self._engine.sample_rate)
        tail = np.concatenate(self._speech)
        if tail.shape[0] > window:
            tail = tail[-window:]
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(None, _recognize_sync, self._engine, tail)
        except Exception:
            return
        if text:
            await self._emit(ProviderEvent.partial(text))

    async def _drain_pending(self) -> None:
        if self._pending is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._pending), 15.0)
            except Exception:
                pass
            self._pending = None

    async def _emit(self, ev: dict) -> None:
        if self._on_event is not None and not self._closed:
            await self._on_event(ev)
