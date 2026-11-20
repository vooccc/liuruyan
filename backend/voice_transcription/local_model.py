"""本地 SenseVoice 模型定位与自包含后台下载器。

设计要点：
- 默认优先复用 ``<data_dir>/models/voice_stream/``（与流式语音共享同一套 230MB 模型，不重复下载）；
  如果用户删掉了 voice_stream 模块，本模块仍独立可用（不 import backend.voice_stream）。
- 仅需 ``model.int8.onnx`` + ``tokens.txt`` 即可执行离线转写；``silero_vad.onnx`` 存在时自动启用 VAD 切句。
- 后台下载沿用 hf-mirror + gh-proxy 源与 ``.part`` 原子写入校验。
"""
from __future__ import annotations

import shutil
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import paths
from .config import VoiceTranscriptionConfig, load_transcription_config

_RELEASE_DIR = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

_HF_BASE = (
    "https://hf-mirror.com/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
)
_SILERO_FALLBACKS = [
    "https://gh-proxy.com/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "https://ghproxy.net/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
]

_FILES: List[Dict[str, Any]] = [
    {
        "name": "model.int8.onnx",
        "label": "SenseVoice 识别模型（约 228MB）",
        "urls": [f"{_HF_BASE}/model.int8.onnx"],
        "min_bytes": 200_000_000,
    },
    {
        "name": "tokens.txt",
        "label": "词表 tokens.txt",
        "urls": [f"{_HF_BASE}/tokens.txt"],
        "min_bytes": 100_000,
    },
    {
        "name": "silero_vad.onnx",
        "label": "VAD 切句模型 silero_vad.onnx",
        "urls": list(_SILERO_FALLBACKS),
        "min_bytes": 500_000,
    },
]

_CHUNK = 1024 * 1024
_TIMEOUT = 60


def default_shared_model_dir() -> Path:
    """默认共享模型目录（与流式语音共用，避免重复占用 230MB 磁盘）。"""
    return paths.data_dir() / "models" / "voice_stream"


def fallback_transcription_model_dir() -> Path:
    """独立备用模型目录。"""
    return paths.data_dir() / "models" / "voice_transcription"


def _candidate_roots(cfg: VoiceTranscriptionConfig) -> List[Path]:
    custom = (cfg.local_model_dir or "").strip()
    if custom:
        return [Path(custom).expanduser()]
    return [default_shared_model_dir(), fallback_transcription_model_dir()]


def resolve_local_model_paths(cfg: VoiceTranscriptionConfig) -> Optional[Dict[str, Optional[Path]]]:
    """查找本地 SenseVoice 模型文件。

    Returns:
        ``{"model": Path, "tokens": Path, "vad": Optional[Path]}``；
        缺少 model.int8.onnx 或 tokens.txt 时返回 None。
    """
    for root in _candidate_roots(cfg):
        if not root.is_dir():
            continue
        for base in (root, root / _RELEASE_DIR):
            model = base / "model.int8.onnx"
            tokens = base / "tokens.txt"
            vad = base / "silero_vad.onnx"
            if model.is_file() and tokens.is_file():
                return {
                    "model": model,
                    "tokens": tokens,
                    "vad": vad if vad.is_file() else None,
                }
    return None


def local_model_status(cfg: VoiceTranscriptionConfig) -> Dict[str, Any]:
    """返回本地模型就绪状态与下载进度。"""
    p = resolve_local_model_paths(cfg)
    custom = (cfg.local_model_dir or "").strip()
    display_dir = str(Path(custom).expanduser()) if custom else str(default_shared_model_dir())
    return {
        "dir": str(p["model"].parent) if p else display_dir,
        "ready": p is not None,
        "vad_ready": bool(p and p["vad"] is not None),
        "model": str(p["model"]) if p else "",
        "download": download_state(),
    }


# ── 后台下载状态机 ─────────────────────────────────────────────────────

_state: Dict[str, Any] = {
    "phase": "idle",  # idle | downloading | done | error
    "file": "",
    "label": "",
    "downloaded": 0,
    "total": 0,
    "percent": 0,
    "files_done": 0,
    "files_total": len(_FILES),
    "error": "",
    "started_at": 0.0,
}
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


def download_state() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def _set(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)


def start_model_download() -> bool:
    """启动后台下载（幂等；已在下载中返回 False）。"""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _state.update(
            phase="downloading",
            file="",
            label="",
            downloaded=0,
            total=0,
            percent=-1,
            files_done=0,
            error="",
            started_at=time.time(),
        )
        cfg = load_transcription_config()
        custom = (cfg.local_model_dir or "").strip()
        target_dir = Path(custom).expanduser() if custom else default_shared_model_dir()
        _thread = threading.Thread(
            target=_worker,
            args=(target_dir,),
            name="voice-transcription-model-dl",
            daemon=True,
        )
        _thread.start()
        return True


def _fetch(url: str, dest: Path, min_bytes: int, on_progress: Any) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    downloaded = 0
    total = 0
    req = urllib.request.Request(url, headers={"User-Agent": "liuruyan-voice-transcription/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
        total = int(resp.headers.get("Content-Length") or 0)
        if total and dest.name == "model.int8.onnx" and total < min_bytes:
            raise ValueError(f"content-length {total} < min {min_bytes}")
        _set(file=dest.name, downloaded=0, total=total, percent=0 if total else -1)
        with open(part, "wb") as f:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                on_progress(downloaded, total)
    if total and downloaded != total:
        part.unlink(missing_ok=True)
        raise ValueError(f"downloaded {downloaded} bytes != content-length {total}")
    if downloaded < min_bytes:
        part.unlink(missing_ok=True)
        raise ValueError(f"downloaded {downloaded} bytes < min {min_bytes}")
    shutil.move(str(part), str(dest))


def _worker(target_dir: Path) -> None:
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        for i, spec in enumerate(_FILES):
            dest = target_dir / spec["name"]
            if dest.is_file() and dest.stat().st_size >= spec["min_bytes"]:
                _set(file=spec["name"], label=spec["label"], files_done=i + 1)
                continue
            _set(file=spec["name"], label=spec["label"], downloaded=0, total=0, percent=-1)
            last_err: Optional[Exception] = None
            for url in spec["urls"]:
                try:
                    _fetch(
                        url,
                        dest,
                        spec["min_bytes"],
                        lambda d, t: _set(
                            downloaded=d,
                            total=t,
                            percent=min(100, int(d * 100 / t)) if t else -1,
                        ),
                    )
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
            if last_err is not None:
                raise RuntimeError(f"{spec['name']} 下载失败（所有源均不可达）: {last_err}")
            _set(files_done=i + 1)
        _set(phase="done", percent=100, error="")
    except Exception as e:  # noqa: BLE001
        _set(phase="error", error=str(e))
