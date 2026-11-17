"""本地 SenseVoice 模型三件套下载器（voice_stream 孤岛模块自带，删模块无残留）。

动机：本地流式引擎（providers/local.py）需要 model.int8.onnx + tokens.txt +
silero_vad.onnx 三件套（共约 230MB）。新装用户首次使用本地档必然缺文件，
设置页提供「下载模型」按钮 → POST /voice/stream/local/download → 本模块后台
线程逐个下载，进度通过 GET /voice/stream/config 的 local.download 字段暴露，
前端轮询画进度条；全部完成后 local.ready 变 true，广播 voice-config-updated
麦克风按钮即时出现。

下载源（2026-11-16 逐个 curl 实测）：
- model.int8.onnx（239,233,841B）/ tokens.txt（315,894B）：
    hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
    （hf-mirror 302 → CDN，国内直连稳定）
- silero_vad.onnx（643,854B）：该 HF 仓库没有这个文件，走 GitHub release 加速
    代理 gh-proxy.com / ghproxy.net，最后兜底直连 GitHub。

健壮性：
- 大文件先写 <name>.part 临时文件，完整后原子 rename，中断不留半个真名文件；
- 每个文件下完做最小体积校验（防 CDN 错误页存成 .onnx）；
- 同一 URL 失败自动换下一个 fallback；所有 URL 都挂 → phase=error，
  保留 error 信息，可再次 POST 重试（从第一个未完成的文件继续）。
"""
from __future__ import annotations

import shutil
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from .providers.local import default_local_model_dir

# ── 下载源定义 ────────────────────────────────────────────────────────

_HF_BASE = (
    "https://hf-mirror.com/csukuangfj/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
)
_SILERO_FALLBACKS = [
    "https://gh-proxy.com/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "https://ghproxy.net/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
]

_FILES: list[dict] = [
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

_CHUNK = 1024 * 1024  # 1MB/块
_TIMEOUT = 60

# ── 进程级状态（单线程下载，锁保护读写） ──────────────────────────────

_state: Dict[str, Any] = {
    "phase": "idle",  # idle | downloading | done | error
    "file": "",  # 当前文件名
    "label": "",  # 当前文件中文描述
    "downloaded": 0,  # 当前文件已下载字节
    "total": 0,  # 当前文件总字节（0 = 未知）
    "percent": 0,  # 当前文件百分比（total 未知时 -1）
    "files_done": 0,
    "files_total": len(_FILES),
    "error": "",
    "started_at": 0.0,
}
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


def download_state() -> Dict[str, Any]:
    """当前下载状态快照（GET config 的 local.download 字段）。"""
    with _lock:
        return dict(_state)


def _set(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)


def start_model_download() -> bool:
    """启动后台下载。已在下载中返回 False；其余情况（重试/续传）启动新线程。"""
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
        _thread = threading.Thread(target=_worker, name="voice-stream-model-dl", daemon=True)
        _thread.start()
        return True


def _fetch(url: str, dest: Path, min_bytes: int, on_progress: Any) -> None:
    """单 URL 流式下载到 dest（.part → rename）。失败抛异常由调用方换源。"""
    part = dest.with_suffix(dest.suffix + ".part")
    downloaded = 0
    total = 0
    req = urllib.request.Request(url, headers={"User-Agent": "liuruyan-voice-stream/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 源白名单
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
    if downloaded < min_bytes:
        part.unlink(missing_ok=True)
        raise ValueError(f"downloaded {downloaded} bytes < min {min_bytes}")
    shutil.move(str(part), str(dest))


def _worker() -> None:
    target_dir = default_local_model_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        for i, spec in enumerate(_FILES):
            dest = target_dir / spec["name"]
            if dest.is_file() and dest.stat().st_size >= spec["min_bytes"]:
                # 上次下载已留下完整文件（断点重试）→ 跳过
                _set(file=spec["name"], label=spec["label"], files_done=i + 1)
                continue
            _set(file=spec["name"], label=spec["label"], downloaded=0, total=0, percent=-1)
            last_err: Optional[Exception] = None
            for url in spec["urls"]:
                try:
                    _fetch(url, dest, spec["min_bytes"], lambda d, t: _set(downloaded=d, total=t))
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001 逐源回退
                    last_err = e
                    dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
            if last_err is not None:
                raise RuntimeError(f"{spec['name']} 下载失败（所有源均不可达）: {last_err}")
            _set(files_done=i + 1)
        _set(phase="done", percent=100, error="")
    except Exception as e:  # noqa: BLE001 对外只留消息
        _set(phase="error", error=str(e))
