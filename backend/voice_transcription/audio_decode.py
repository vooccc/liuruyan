"""音频解码与大文件切片工具（零硬依赖优先，存在 ffmpeg 时自动解锁全格式）。

职责：
1. ``decode_to_pcm16k(audio_bytes, filename)``：
   将音频解码为 16kHz 单声道 float32（[-1.0, 1.0]）供本地 SenseVoice 使用。
   - WAV（PCM 8/16/24/32-bit 与 IEEE float32/float64）：纯 Python + numpy 原生解码，零外部依赖；
   - 其他格式（mp3 / m4a / ogg / flac / aac / webm / mp4 等）：优先调用本机 ffmpeg；
     若无 ffmpeg 则尝试可选库（soundfile / av），均不可用时给出清晰指引。
2. ``split_audio_for_api(audio_bytes, filename, max_bytes)``：
   OpenAI 兼容接口通常限制单文件 25MB；超限时优先用 ffmpeg（或纯 Python WAV 切片）自动分段，
   返回 ``[(chunk_bytes, chunk_filename, offset_sec), ...]``。
"""
from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

TARGET_SAMPLE_RATE = 16000
DEFAULT_API_MAX_BYTES = 24 * 1024 * 1024  # 留 1MB 余量对齐 OpenAI 25MB 限制

API_SUPPORTED_EXTENSIONS = (
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".m4a",
    ".wav",
    ".webm",
    ".ogg",
    ".flac",
)

# Windows 10/11 原生 WinRT MediaTranscoder 常见可解码格式（免装 ffmpeg / 第三方库）
WINRT_NATIVE_EXTENSIONS = (
    ".mp3",
    ".m4a",
    ".aac",
    ".wma",
    ".mp4",
    ".m4v",
    ".mov",
    ".wav",
    ".flac",
)
_WINRT_SUPPORTED_EXTENSIONS = frozenset(WINRT_NATIVE_EXTENSIONS)


class AudioDecodeError(RuntimeError):
    """音频解码或切片失败（消息面向用户与 LLM）。"""


def find_ffmpeg() -> Optional[str]:
    """查找可用 ffmpeg 可执行文件路径。"""
    env_bin = os.getenv("FFMPEG_BINARY", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:
        pass
    return None


def has_ffmpeg() -> bool:
    return find_ffmpeg() is not None


def has_winrt_audio_decoder() -> bool:
    """Windows 10/11 是否可通过系统原生 WinRT MediaTranscoder 解码常见音视频。"""
    return sys.platform == "win32" and shutil.which("powershell") is not None


def _subprocess_kwargs() -> dict:
    kw: dict = {"capture_output": True}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW，防 Windows 弹黑窗
    return kw


def _resample_to_16k(mono: np.ndarray, orig_sr: int) -> np.ndarray:
    if orig_sr <= 0:
        raise AudioDecodeError(f"无效采样率: {orig_sr}")
    if mono.size == 0 or orig_sr == TARGET_SAMPLE_RATE:
        return mono.astype(np.float32, copy=False)
    new_len = max(1, int(round(mono.shape[0] * TARGET_SAMPLE_RATE / orig_sr)))
    x_old = np.linspace(0.0, 1.0, num=mono.shape[0], endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
    return np.interp(x_new, x_old, mono).astype(np.float32)


def _decode_pcm_bytes(
    raw: bytes,
    *,
    audio_format: int,
    channels: int,
    sample_rate: int,
    bits_per_sample: int,
) -> Tuple[np.ndarray, float]:
    if channels <= 0 or sample_rate <= 0:
        raise AudioDecodeError("WAV 头部声道数或采样率无效")
    if audio_format == 1:  # PCM
        if bits_per_sample == 8:
            arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif bits_per_sample == 16:
            usable = len(raw) - (len(raw) % 2)
            arr = np.frombuffer(raw[:usable], dtype="<i2").astype(np.float32) / 32768.0
        elif bits_per_sample == 24:
            usable = len(raw) - (len(raw) % 3)
            b = np.frombuffer(raw[:usable], dtype=np.uint8).reshape(-1, 3)
            # 24-bit little-endian signed → int32
            i32 = (
                b[:, 0].astype(np.int32)
                | (b[:, 1].astype(np.int32) << 8)
                | (b[:, 2].astype(np.int32) << 16)
            )
            i32 = np.where(i32 & 0x800000, i32 - 0x1000000, i32)
            arr = i32.astype(np.float32) / 8388608.0
        elif bits_per_sample == 32:
            usable = len(raw) - (len(raw) % 4)
            arr = np.frombuffer(raw[:usable], dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            raise AudioDecodeError(f"不支持的 WAV PCM 位深: {bits_per_sample}-bit")
    elif audio_format == 3:  # IEEE float
        if bits_per_sample == 32:
            usable = len(raw) - (len(raw) % 4)
            arr = np.frombuffer(raw[:usable], dtype="<f4").astype(np.float32)
        elif bits_per_sample == 64:
            usable = len(raw) - (len(raw) % 8)
            arr = np.frombuffer(raw[:usable], dtype="<f8").astype(np.float32)
        else:
            raise AudioDecodeError(f"不支持的 WAV IEEE float 位深: {bits_per_sample}-bit")
    else:
        raise AudioDecodeError(f"不支持的 WAV 编码格式 code={audio_format}（需安装 ffmpeg 解码）")

    if channels > 1:
        usable_frames = (arr.size // channels) * channels
        arr = arr[:usable_frames].reshape(-1, channels).mean(axis=1)

    duration_sec = float(arr.shape[0]) / float(sample_rate)
    pcm16k = _resample_to_16k(np.clip(arr, -1.0, 1.0), sample_rate)
    return pcm16k, duration_sec


def _try_decode_wav_bytes(audio_bytes: bytes) -> Optional[Tuple[np.ndarray, float]]:
    """纯 Python 解析 RIFF/WAVE（兼容 PCM 与 IEEE float32）。非 WAV 返回 None。"""
    if len(audio_bytes) < 12 or audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
        return None

    pos = 12
    fmt_info: Optional[Tuple[int, int, int, int]] = None
    data_chunks: List[bytes] = []

    while pos + 8 <= len(audio_bytes):
        chunk_id = audio_bytes[pos : pos + 4]
        chunk_size = struct.unpack_from("<I", audio_bytes, pos + 4)[0]
        chunk_start = pos + 8
        chunk_end = min(len(audio_bytes), chunk_start + chunk_size)
        if chunk_id == b"fmt " and chunk_end - chunk_start >= 16:
            audio_format, channels, sample_rate, _, _, bits_per_sample = struct.unpack_from(
                "<HHIIHH", audio_bytes, chunk_start
            )
            if audio_format == 0xFFFE and chunk_end - chunk_start >= 40:
                # WAVE_FORMAT_EXTENSIBLE: SubFormat 前 2 字节即真实 format tag
                audio_format = struct.unpack_from("<H", audio_bytes, chunk_start + 24)[0]
            fmt_info = (audio_format, channels, sample_rate, bits_per_sample)
        elif chunk_id == b"data":
            data_chunks.append(audio_bytes[chunk_start:chunk_end])
        # RIFF chunk 按偶数字节对齐
        pos = chunk_start + chunk_size + (chunk_size & 1)

    if fmt_info is None or not data_chunks:
        raise AudioDecodeError("WAV 文件损坏：缺少 fmt 或 data 块")

    audio_format, channels, sample_rate, bits_per_sample = fmt_info
    return _decode_pcm_bytes(
        b"".join(data_chunks),
        audio_format=audio_format,
        channels=channels,
        sample_rate=sample_rate,
        bits_per_sample=bits_per_sample,
    )


def _decode_via_ffmpeg(audio_bytes: bytes, ext: str, ffmpeg_bin: str) -> Tuple[np.ndarray, float]:
    suffix = ext if ext.startswith(".") else f".{ext}" if ext else ".bin"
    with tempfile.TemporaryDirectory(prefix="lry_asr_") as tmpdir:
        in_path = Path(tmpdir) / f"input{suffix}"
        in_path.write_bytes(audio_bytes)
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(in_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(TARGET_SAMPLE_RATE),
            "-f",
            "s16le",
            "pipe:1",
        ]
        proc = subprocess.run(cmd, timeout=300, **_subprocess_kwargs())
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise AudioDecodeError(f"ffmpeg 解码失败: {err or f'exit={proc.returncode}'}")
        pcm_raw = proc.stdout
        if not pcm_raw:
            raise AudioDecodeError("音频文件不包含可解码的音轨")
        usable = len(pcm_raw) - (len(pcm_raw) % 2)
        samples = np.frombuffer(pcm_raw[:usable], dtype="<i2").astype(np.float32) / 32768.0
        duration = float(samples.shape[0]) / float(TARGET_SAMPLE_RATE)
        return samples, duration


def _try_decode_optional_libs(audio_bytes: bytes, ext: str) -> Optional[Tuple[np.ndarray, float]]:
    # 1) soundfile (libsndfile: flac/ogg/aiff/wav)
    try:
        import soundfile as sf  # type: ignore

        data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=True)
        mono = data.mean(axis=1)
        duration = float(mono.shape[0]) / float(sr)
        return _resample_to_16k(mono, int(sr)), duration
    except ImportError:
        pass
    except Exception:
        pass

    # 2) PyAV (av: mp3/m4a/mp4/aac/webm/ogg)
    try:
        import av  # type: ignore

        container = av.open(io.BytesIO(audio_bytes))
        resampler = av.AudioResampler(format="flt", layout="mono", rate=TARGET_SAMPLE_RATE)
        chunks: List[np.ndarray] = []
        for frame in container.decode(audio=0):
            for out_frame in resampler.resample(frame):
                arr = out_frame.to_ndarray().reshape(-1).astype(np.float32)
                if arr.size:
                    chunks.append(arr)
        container.close()
        if not chunks:
            raise AudioDecodeError("PyAV 未解码出音频帧")
        samples = np.concatenate(chunks)
        return samples, float(samples.shape[0]) / float(TARGET_SAMPLE_RATE)
    except ImportError:
        pass
    except AudioDecodeError:
        raise
    except Exception:
        pass

    return None


_WINRT_TRANSCODE_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskOp = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
$asTaskProg = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncActionWithProgress`1'
})[0]
function AwaitOp($op, $t) {
    $m = $asTaskOp.MakeGenericMethod($t)
    $task = $m.Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    return $task.Result
}
function AwaitProg($op) {
    $m = $asTaskProg.MakeGenericMethod([double])
    $task = $m.Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
}
[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.MediaProperties.MediaEncodingProfile, Windows.Media.MediaProperties, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Transcoding.MediaTranscoder, Windows.Media.Transcoding, ContentType=WindowsRuntime] | Out-Null

$srcPath = $env:LRY_WINRT_SRC
$dstPath = $env:LRY_WINRT_DST
$srcFile = AwaitOp ([Windows.Storage.StorageFile]::GetFileFromPathAsync($srcPath)) ([Windows.Storage.StorageFile])
$dstFile = AwaitOp ([Windows.Storage.StorageFile]::GetFileFromPathAsync($dstPath)) ([Windows.Storage.StorageFile])
$profile = [Windows.Media.MediaProperties.MediaEncodingProfile]::CreateWav([Windows.Media.MediaProperties.AudioEncodingQuality]::Low)
$profile.Audio = [Windows.Media.MediaProperties.AudioEncodingProperties]::CreatePcm(16000, 1, 16)
$transcoder = New-Object Windows.Media.Transcoding.MediaTranscoder
$prep = AwaitOp ($transcoder.PrepareFileTranscodeAsync($srcFile, $dstFile, $profile)) ([Windows.Media.Transcoding.PrepareTranscodeResult])
if (-not $prep.CanTranscode) {
    throw "CanTranscode=False: $($prep.FailureReason)"
}
AwaitProg ($prep.TranscodeAsync())
"""


def _try_decode_via_winrt(audio_bytes: bytes, ext: str) -> Optional[Tuple[np.ndarray, float]]:
    """Windows 原生 WinRT MediaTranscoder 兜底：免 ffmpeg 解码 .mp3/.m4a/.aac/.wma/.mp4。

    转码为 16kHz 16-bit mono PCM WAV 后交由纯 Python WAV 解析器读取；
    非 Windows、格式不受支持或损坏字节时返回 None，由调用方继续走统一报错指引。
    """
    if sys.platform != "win32":
        return None
    norm_ext = (ext if ext.startswith(".") else f".{ext}" if ext else "").lower()
    if norm_ext not in _WINRT_SUPPORTED_EXTENSIONS:
        return None
    ps_bin = shutil.which("powershell")
    if not ps_bin:
        return None

    try:
        with tempfile.TemporaryDirectory(prefix="lry_winrt_") as tmpdir:
            in_path = Path(tmpdir) / f"input{norm_ext}"
            out_path = Path(tmpdir) / "output.wav"
            ps_path = Path(tmpdir) / "transcode.ps1"
            in_path.write_bytes(audio_bytes)
            out_path.write_bytes(b"")
            ps_path.write_text(_WINRT_TRANSCODE_PS, encoding="utf-8")

            env = os.environ.copy()
            env["LRY_WINRT_SRC"] = str(in_path)
            env["LRY_WINRT_DST"] = str(out_path)

            kw = _subprocess_kwargs()
            proc = subprocess.run(
                [ps_bin, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ps_path)],
                env=env,
                timeout=180,
                **kw,
            )
            if proc.returncode != 0 or not out_path.is_file():
                return None
            wav_bytes = out_path.read_bytes()
            if not wav_bytes:
                return None
            return _try_decode_wav_bytes(wav_bytes)
    except Exception:
        return None


def decode_to_pcm16k(audio_bytes: bytes, filename: str = "audio.wav") -> Tuple[np.ndarray, float]:
    """将任意支持的音频字节解码为 16kHz 单声道 float32 数组及原始时长（秒）。"""
    if not audio_bytes:
        raise AudioDecodeError("音频文件为空")

    ext = Path(filename).suffix.lower()

    # 1. 优先尝试纯 Python WAV 解码（即使扩展名错写，只要头部是 RIFF/WAVE 也能解）
    try:
        wav_res = _try_decode_wav_bytes(audio_bytes)
        if wav_res is not None:
            return wav_res
    except AudioDecodeError as wav_err:
        # 压缩编码的 WAV（非 PCM/float）继续尝试 ffmpeg
        ffmpeg_bin = find_ffmpeg()
        if ffmpeg_bin:
            return _decode_via_ffmpeg(audio_bytes, ext or ".wav", ffmpeg_bin)
        raise wav_err

    # 2. 非 WAV：优先走本机 ffmpeg
    ffmpeg_bin = find_ffmpeg()
    if ffmpeg_bin:
        return _decode_via_ffmpeg(audio_bytes, ext or ".bin", ffmpeg_bin)

    # 3. 尝试可选 Python 音频库
    opt_res = _try_decode_optional_libs(audio_bytes, ext)
    if opt_res is not None:
        return opt_res

    # 4. Windows 原生 WinRT MediaTranscoder 兜底（零第三方依赖直解 mp3/m4a/aac/wma/mp4）
    winrt_res = _try_decode_via_winrt(audio_bytes, ext)
    if winrt_res is not None:
        return winrt_res

    # 5. 明确报错并给出可操作方案
    raise AudioDecodeError(
        f"本地 SenseVoice 暂无法直接解码 {ext or '未知格式'} 文件（当前环境未检测到 ffmpeg）。\n"
        "解决方式：\n"
        "1. 安装 ffmpeg 并加入 PATH（如 `winget install Gyan.FFmpeg`），即可本地离线支持 ogg/opus/webm 等全格式；\n"
        "2. 或将音频转为 .wav / .mp3 / .m4a 格式后再试；\n"
        "3. 或在「设置 → 辅助功能 → 语音转写」切换为 OpenAI 兼容接口（原生支持 mp3/m4a/mp4/webm/ogg/flac）。"
    )


def _split_wav_pure_python(
    audio_bytes: bytes,
    max_bytes: int,
) -> Optional[List[Tuple[bytes, str, float]]]:
    """纯 Python 按帧切分 PCM WAV，确保每片 <= max_bytes。"""
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            params = wf.getparams()
            nframes = wf.getnframes()
            frame_bytes = params.nchannels * params.sampwidth
            if frame_bytes <= 0 or params.framerate <= 0:
                return None
            # 留 1KB 给 WAV 头
            max_frames_per_chunk = max(params.framerate, (max_bytes - 1024) // frame_bytes)
            chunks: List[Tuple[bytes, str, float]] = []
            offset_frames = 0
            idx = 0
            while offset_frames < nframes:
                frames_to_read = min(max_frames_per_chunk, nframes - offset_frames)
                raw = wf.readframes(frames_to_read)
                buf = io.BytesIO()
                with wave.open(buf, "wb") as out_wf:
                    out_wf.setnchannels(params.nchannels)
                    out_wf.setsampwidth(params.sampwidth)
                    out_wf.setframerate(params.framerate)
                    out_wf.writeframes(raw)
                chunks.append(
                    (
                        buf.getvalue(),
                        f"chunk_{idx:03d}.wav",
                        float(offset_frames) / float(params.framerate),
                    )
                )
                offset_frames += frames_to_read
                idx += 1
            return chunks
    except Exception:
        return None


def split_audio_for_api(
    audio_bytes: bytes,
    filename: str,
    max_bytes: int = DEFAULT_API_MAX_BYTES,
) -> List[Tuple[bytes, str, float]]:
    """当音频超过 API 单文件上限（25MB）时自动分段。

    Returns:
        ``[(chunk_bytes, chunk_filename, offset_seconds), ...]``
    """
    if len(audio_bytes) <= max_bytes:
        return [(audio_bytes, filename, 0.0)]

    ext = Path(filename).suffix.lower()
    if ext == ".wav":
        wav_chunks = _split_wav_pure_python(audio_bytes, max_bytes)
        if wav_chunks:
            return wav_chunks

    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        size_mb = len(audio_bytes) / (1024 * 1024)
        raise AudioDecodeError(
            f"音频文件体积 {size_mb:.1f} MB 超过 OpenAI 兼容接口 25MB 上限，且未检测到 ffmpeg 无法自动分段。\n"
            "请安装 ffmpeg，或切换为「本地 SenseVoice」离线引擎处理大文件。"
        )

    segment_sec = 600  # 10 分钟一片，16k mono 64kbps mp3 约 4.8MB，远小于 24MB
    with tempfile.TemporaryDirectory(prefix="lry_split_") as tmpdir:
        in_path = Path(tmpdir) / f"input{ext or '.bin'}"
        in_path.write_bytes(audio_bytes)
        out_pattern = str(Path(tmpdir) / "chunk_%03d.mp3")
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(in_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            "-f",
            "segment",
            "-segment_time",
            str(segment_sec),
            "-reset_timestamps",
            "1",
            out_pattern,
        ]
        proc = subprocess.run(cmd, timeout=600, **_subprocess_kwargs())
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise AudioDecodeError(f"ffmpeg 分段切片失败: {err or f'exit={proc.returncode}'}")

        chunk_files = sorted(Path(tmpdir).glob("chunk_*.mp3"))
        if not chunk_files:
            raise AudioDecodeError("ffmpeg 未生成任何音频切片")

        out: List[Tuple[bytes, str, float]] = []
        for idx, cf in enumerate(chunk_files):
            data = cf.read_bytes()
            if len(data) > max_bytes:
                raise AudioDecodeError(f"分段切片 {cf.name} 仍超过体积上限")
            out.append((data, cf.name, float(idx * segment_sec)))
        return out
