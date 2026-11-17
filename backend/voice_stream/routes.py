"""voice_stream 路由（孤立包自带，不经 backend/routes/__init__.py）。

- GET  /voice/stream/config   前端降级判断的唯一依据
- PUT  /voice/stream/config   设置页 partial 更新（同 /voice/config PUT 的模式）
- POST /voice/stream/local/download  启动本地模型三件套后台下载（download.py）
- WS   /voice/stream/ws      浏览器 PCM 流中继

main.py 注册（2 行，删除本模块时还原这 2 行即可）：
    from backend.voice_stream.routes import router as _voice_stream_router
    app.include_router(_voice_stream_router)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, WebSocket
from pydantic import BaseModel

from .config import load_voice_stream_config, save_voice_stream_config, stream_status
from .service import VoiceStreamSession

router = APIRouter(prefix="/voice/stream", tags=["voice-stream"])


@router.get("/config")
async def get_stream_config() -> Dict[str, Any]:
    return stream_status()


class VoiceStreamConfigUpdate(BaseModel):
    """PUT /voice/stream/config 的 partial-update body。None 表示不动。

    ws_url / sample_rate 不暴露到 UI（高级字段，直接改 models.json）。
    """

    enabled: Optional[bool] = None
    provider_id: Optional[str] = None
    model: Optional[str] = None
    language_hints: Optional[List[str]] = None
    heartbeat: Optional[bool] = None
    max_session_seconds: Optional[int] = None
    local_model_dir: Optional[str] = None
    polish_enabled: Optional[bool] = None
    polish_provider_id: Optional[str] = None
    polish_model: Optional[str] = None


@router.put("/config")
async def update_stream_config(body: VoiceStreamConfigUpdate) -> Dict[str, Any]:
    cfg = load_voice_stream_config()
    data = cfg.model_dump()
    data.update(body.model_dump(exclude_none=True))
    try:
        from .config import VoiceStreamConfig

        new_cfg = VoiceStreamConfig.model_validate(data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid config: {e}")
    save_voice_stream_config(new_cfg)
    return {
        "status": "success",
        "config": new_cfg.model_dump(),
        "stream_status": stream_status(),
    }


@router.post("/local/download")
async def start_local_model_download() -> Dict[str, Any]:
    """启动本地模型三件套后台下载（已在下载中时幂等返回当前进度）。

    进度通过 GET /config 的 local.download 字段轮询；完成后 local.ready 变 true。
    """
    from .download import download_state, start_model_download

    started = start_model_download()
    return {
        "status": "success",
        "started": started,
        "download": download_state(),
        "stream_status": stream_status(),
    }


@router.get("/rules")
async def get_rules() -> Dict[str, Any]:
    """读取替换规则文本（首次访问自动写出带示例的模板文件）。"""
    from .polish import default_rules_path, ensure_rules_file

    ensure_rules_file()
    path = default_rules_path()
    try:
        content = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        content = ""
    return {"content": content}


@router.put("/rules")
async def update_rules(body: Dict[str, str]) -> Dict[str, Any]:
    """保存替换规则文本（body.content）。写入后强制重载缓存。"""
    from .polish import default_rules_path, load_rules

    content = body.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="body.content (string) required")
    path = default_rules_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"write rules failed: {e}")
    load_rules(force_reload=True)
    return {"status": "success", "count": len(load_rules(force_reload=True))}


@router.websocket("/ws")
async def stream_ws(ws: WebSocket) -> None:
    await VoiceStreamSession(ws).run()
