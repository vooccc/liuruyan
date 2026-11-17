"""本地 SenseVoice 流式识别 provider（sherpa-onnx，免 key、全离线）。

配方（借自 blurt 的 local-first 管线 + sherpa-onnx 官方示例）：
- SenseVoice 本身是离线（非流式）模型，"流式"由两层拼出来：
  1) silero VAD 实时切分语音段：静音超阈值 = 一段结束 → 整段送 SenseVoice → final
  2) 说话过程中每 PARTIAL_INTERVAL 秒对"当前段已积累音频"做一次识别 → partial
     （SenseVoice small 在 CPU 上 RTF << 1，几百毫秒延迟可接受）
     partial 是累积式：长段超过尾部窗口的部分在自然停顿处"密封"为固定前缀；
     尾部识别前按帧能量裁掉静音（SenseVoice 对静音会幻觉出 Yeah/Okay）
- 入口噪声门控：低于自适应门限（底噪 EMA × 2.5，封顶 0.06）的帧抹平成纯
  静音再喂 VAD/段缓冲——高底噪麦克风会让 VAD 一直判"语音中"不切句，
  静音帧持续进识别器，每 0.8s 冒一堆 Okay/Yeah。门控后全链路一致。
  VAD 仍卡死时（段内连续门控静音 >2s）软兜底：有效部分强制落 final +
  重建 VAD，静音期彻底停止识别。
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
# partial 尾部识别窗口（秒）：只重新识别当前段尾部这么长，控制 CPU 峰值。
# 窗口外的头部音频会被"密封"（只识别一次，文本固定为预览前缀）——
# 否则滑动窗口会让超过 10 秒的长段前文从预览里消失（前端草稿是整体替换式渲染）。
_PARTIAL_WINDOW_SECONDS = 10.0
# 帧能量（RMS，float32 [-1,1] 刻度）低于此视为静音：用于密封切点找停顿、
# 尾部裁静音。SenseVoice 对静音/底噪会幻觉出 "Yeah." "Okay." 之类的词，
# 识别前必须把静音裁掉，否则预览全是垃圾。
_SILENCE_RMS = 0.01
# 密封切点回看范围（秒）：在窗口边界往回最多找这么长的自然停顿来切，
# 保证密封块是完整短语而不是拦腰截断
_SEAL_LOOKBACK_SECONDS = 3.0
# 尾部裁静音时保留的余量（秒）：最后一个活动帧之后再带这么长的音频
_TAIL_MARGIN_SECONDS = 0.3
# VAD 静音判定（秒）：低于此值认为句子还没说完
_MIN_SILENCE = 0.5
# VAD 最短语音段（秒）：过短的片段丢弃（呼吸/碰麦）
_MIN_SPEECH = 0.2
# 入口噪声门控：门限 = max(_SILENCE_RMS, 底噪估计×2.5)，封顶 _NOISE_GATE_MAX。
# 低于门限的帧抹平成纯静音再喂 VAD/段缓冲——高增益麦克风的底噪会让 silero VAD
# 一直判"语音中"不切句，静音帧持续进识别器，每 0.8s 幻觉出一堆 Okay/Yeah。
_NOISE_GATE_MAX = 0.06
# 软兜底：段内连续门控静音超过此秒数（VAD 卡死没切句）→ 强制把有效部分
# 落成 final 并重建 VAD，静音期彻底停止识别
_FORCE_FINAL_SILENCE = 2.0

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
        # 长段累积预览：段超过尾部窗口后，窗口外头部"密封"一次的文本前缀
        self._sealed_text = ""
        self._sealed_len = 0                  # 已密封音频的样本数（相对当前段起点）
        # 每帧 (结束样本位置, RMS)：密封切点找停顿 / 尾部裁静音用
        self._frames: List[tuple] = []
        # 自适应底噪估计（RMS）+ 段内连续门控静音样本数（软兜底计时用）
        self._noise_floor = _SILENCE_RMS
        self._silence_since = 0
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
        # 入口噪声门控：底噪/静音帧抹平（时间轴保留）再喂 VAD 和段缓冲——
        # 一处门控全链路受益：VAD 能正常切句、段内帧 RMS 真实反映活动、
        # 密封切点能找到停顿、尾部裁剪能裁掉静音，Okay/Yeah 幻觉无从产生
        rms = float(np.sqrt(np.mean(np.square(samples))))
        self._update_noise_floor(rms)
        gate = min(max(_SILENCE_RMS, self._noise_floor * 2.5), _NOISE_GATE_MAX)
        if rms < gate:
            samples = np.zeros_like(samples)
        self._vad.accept_waveform(samples)
        # 收割已完成的语音段（final；段缓冲清零，final 用 VAD 自己的完整段）
        # 注：sherpa-onnx >= 2.0 的 empty / front / is_speech_detected 是属性
        while not self._vad.empty:
            segment = self._vad.front
            self._vad.pop()
            seg = np.asarray(segment.samples, dtype=np.float32)
            self._reset_segment()
            await self._recognize_segment(seg)
        # 说话中 → 积累段音频 + 定时 partial
        if self._vad.is_speech_detected:
            self._append_speech(samples)
            if rms < gate:
                self._silence_since += samples.shape[0]
            else:
                self._silence_since = 0
            # 软兜底：VAD 卡死（底噪误判）一直不切句时，段末尾静音超时
            # 就强制把有效部分落成 final，静音期彻底停止识别
            if (
                self._silence_since >= _FORCE_FINAL_SILENCE * self._engine.sample_rate
                and self._active_end(self._speech_len) is not None
            ):
                await self._force_finalize()
                return
            if (
                self._speech_len - self._last_partial_len
                >= _PARTIAL_INTERVAL * self._engine.sample_rate
            ):
                self._last_partial_len = self._speech_len
                await self._emit_partial()
        else:
            self._silence_since = 0

    async def finish(self) -> None:
        if self._engine is None or self._closed:
            return
        # 1) 段内残余（VAD 还没判定静音）：裁掉静音尾巴后不足最短语音则丢弃
        if self._speech:
            full = np.concatenate(self._speech)
            end = self._active_end(full.shape[0])
            if end is not None and end >= int(_MIN_SPEECH * self._engine.sample_rate):
                await self._recognize_segment(full[:end])
        self._reset_segment()
        # 2) 等在途识别完成后发 finished
        await self._drain_pending()
        await self._emit(ProviderEvent.finished("".join(self._sentences)))

    async def close(self) -> None:
        self._closed = True
        self._speech, self._speech_len = [], 0

    # ── 内部 ──────────────────────────────────────────────

    def _append_speech(self, samples: np.ndarray) -> None:
        """说话中的音频自行累积（VAD 只在段完成时吐完整段，说话中不保留）。"""
        self._frames.append(
            (self._speech_len + samples.shape[0], float(np.sqrt(np.mean(np.square(samples)))))
        )
        self._speech.append(samples)
        self._speech_len += samples.shape[0]

    def _reset_segment(self) -> None:
        """清空当前段的所有状态（VAD 切句 / 软兜底 / finish 时调用）。"""
        self._speech, self._speech_len, self._last_partial_len = [], 0, 0
        self._sealed_text, self._sealed_len, self._frames = "", 0, []
        self._silence_since = 0

    def _update_noise_floor(self, rms: float) -> None:
        """自适应跟踪底噪（不对称 EMA：上调快、下调慢）。

        只用"像底噪"的帧（rms < floor×4）更新，说话帧不污染估计。
        上调快：会话刚开始时初始估计偏低，几帧内就要追上真实底噪，
        否则开头几秒门控漏静音；下调慢：避免偶发低能量帧把门限压穿。
        """
        if rms < self._noise_floor * 4:
            if rms > self._noise_floor:
                self._noise_floor = 0.9 * self._noise_floor + 0.1 * rms
            else:
                self._noise_floor = 0.95 * self._noise_floor + 0.05 * rms
        self._noise_floor = max(self._noise_floor, 0.002)

    async def _force_finalize(self) -> None:
        """VAD 卡死兜底：段末尾连续门控静音超时 → 有效部分落 final，重建 VAD。

        重建会丢弃 VAD 内部缓冲（此时必然是底噪误判的"语音中"状态），
        避免它稍后吐出同一段造成重复 final。
        """
        if self._engine is None:
            return
        full = np.concatenate(self._speech) if self._speech else np.zeros(0, np.float32)
        end = self._active_end(full.shape[0])
        self._reset_segment()
        if end is not None and end >= int(_MIN_SPEECH * self._engine.sample_rate):
            await self._recognize_segment(full[:end])
        import sherpa_onnx

        self._vad = sherpa_onnx.VoiceActivityDetector(
            self._engine.vad_config, buffer_size_in_seconds=120
        )

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
        """累积式预览：已密封前缀 + 尾部活动语音识别。

        段超过尾部窗口后，窗口外头部音频在最近的自然停顿处"密封"（只识别
        一次，文本固定为前缀）。尾部识别前按帧能量裁掉静音——SenseVoice 对
        静音/底噪会幻觉出 "Yeah." "Okay."，不裁会把预览变成乱码。
        """
        if self._engine is None or not self._speech:
            return
        sr = self._engine.sample_rate
        loop = asyncio.get_running_loop()
        window = int(_PARTIAL_WINDOW_SECONDS * sr)
        full = np.concatenate(self._speech)

        # 1) 头部超出尾部窗口 → 密封（切点优先落在自然停顿，块是完整短语）
        if full.shape[0] - self._sealed_len > window:
            target = full.shape[0] - window
            cut = self._find_pause_cut(target)
            if cut - self._sealed_len < int(_MIN_SPEECH * sr):
                # 停顿切点贴着已密封区（残余过短）：直接在窗口边界切，防尾窗无限膨胀
                cut = max(target, self._sealed_len + int(_MIN_SPEECH * sr))
            try:
                sealed = await loop.run_in_executor(
                    None, _recognize_sync, self._engine, full[self._sealed_len : cut]
                )
            except Exception:
                return
            self._sealed_text += sealed
            self._sealed_len = cut
            # 已密封区间的帧不再参与后续切点/裁剪，顺手裁掉
            self._frames = [f for f in self._frames if f[0] > self._sealed_len]

        # 2) 尾部裁到最近的活动帧（静音尾巴是幻觉重灾区）
        active_end = self._active_end(full.shape[0])
        if active_end is None:
            return
        tail = full[self._sealed_len : active_end]
        if tail.shape[0] < int(_MIN_SPEECH * sr):
            return
        try:
            text = await loop.run_in_executor(None, _recognize_sync, self._engine, tail)
        except Exception:
            return
        preview = (self._sealed_text + text).strip()
        if preview:
            await self._emit(ProviderEvent.partial(preview))

    def _find_pause_cut(self, target: int) -> int:
        """在 [target-回看范围, target] 里找最近的自然停顿切点（帧 RMS 低于阈值）。

        找不到（连续说话无停顿）就返回 target，接受拦腰切——预览有 final 兜底。
        """
        sr = self._engine.sample_rate
        lo = max(self._sealed_len, target - int(_SEAL_LOOKBACK_SECONDS * sr))
        for end, rms in reversed(self._frames):
            if end <= lo:
                break
            if end <= target and rms < _SILENCE_RMS:
                return end
        return target

    def _active_end(self, upto: int) -> Optional[int]:
        """[sealed_len, upto] 里最后一个活动帧的结束位置（带余量）。

        没有任何活动帧返回 None（整段静音，不该出预览）。
        """
        sr = self._engine.sample_rate
        for end, rms in reversed(self._frames):
            if end <= self._sealed_len:
                break
            if end <= upto and rms >= _SILENCE_RMS:
                return min(end + int(_TAIL_MARGIN_SECONDS * sr), upto)
        return None

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
