"""导入 / 导出 API（/export/* · /import/archive）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
把 agent / 全局记忆打包成 ZIP 给用户下载，或从 ZIP 合并回本机。
同 ID agent 走"合并"语义（详见 backend/export_import.py 模块 docstring）：
角色设定文件以本机为准，session 同 sid 跳过、新 sid 补入，日记同名跳过。

依赖：export_import（惰性 import）、paths（backend.paths）。

端点：
- GET  /export/agents/list  枚举可导出 agent
- POST /export/agents       打包指定 agent 为 ZIP
- POST /export/memory       打包全局 memory 为 ZIP
- POST /export/open-dir     打开导出目录
- POST /import/archive      上传 ZIP 合并
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from fastapi import APIRouter, Body, File, HTTPException, UploadFile

from backend import paths

router = APIRouter(tags=["export-import"])


@router.get("/export/agents/list")
async def list_agents_for_export():
    """枚举本机所有 agent（id + 名字 + session 数 + last_active），供前端勾选 UI。"""
    from backend import export_import as ei
    return {"agents": ei.list_agents_summary()}


@router.post("/export/agents")
async def export_agents_endpoint(payload: Dict = Body(...)):
    """打包指定 agent_ids 为 ZIP，写入 ~/.liuruyan/exports/。

    Body: {"agent_ids": ["agent_xxx", "agent_yyy"]}
    Returns: {"path": "...", "filename": "...", "size_bytes": int, "agents": [...]}

    设计动机：Tauri webview 默认拦截 blob URL 下载，<a download> click 不触发。
    改为后端直接落盘 ~/.liuruyan/exports/，前端拿路径后调 /export/open-dir
    用 explorer 打开文件夹给用户。dev / Tauri 都通，不依赖 Tauri plugin。
    """
    from backend import export_import as ei
    from datetime import datetime as _dt

    agent_ids = payload.get("agent_ids") or []
    if not isinstance(agent_ids, list) or not agent_ids:
        raise HTTPException(status_code=400, detail="agent_ids 必传且非空")

    exports_dir = paths.data_dir() / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    # 文件名带时分秒，避免同一天多次导出覆盖
    stamp = _dt.now().strftime("%Y-%m-%d_%H%M%S")
    out_path = exports_dir / f"liuruyan-agents-{stamp}.zip"

    try:
        result = ei.export_agents(agent_ids, out_path)
        if not result["agents"]:
            out_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail=f"none of the agent_ids exist: {agent_ids}")
        return {
            "path": str(out_path),
            "filename": out_path.name,
            "size_bytes": out_path.stat().st_size,
            "agents": result["agents"],
            "skipped": result.get("skipped") or [],
        }
    except HTTPException:
        raise
    except Exception as e:
        out_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"export failed: {e}")


@router.post("/export/memory")
async def export_memory_endpoint():
    """打包全局 memory/* 为 ZIP，写入 ~/.liuruyan/exports/。"""
    from backend import export_import as ei
    from datetime import datetime as _dt

    exports_dir = paths.data_dir() / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.now().strftime("%Y-%m-%d_%H%M%S")
    out_path = exports_dir / f"liuruyan-memory-{stamp}.zip"

    try:
        result = ei.export_memory(out_path)
        return {
            "path": str(out_path),
            "filename": out_path.name,
            "size_bytes": out_path.stat().st_size,
            "total_files": result["total_files"],
        }
    except Exception as e:
        out_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"export failed: {e}")


@router.post("/export/open-dir")
async def open_exports_dir():
    """用系统文件浏览器打开 ~/.liuruyan/exports/。"""
    import subprocess
    import platform

    exports_dir = paths.data_dir() / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    try:
        if platform.system() == "Windows":
            subprocess.Popen(["explorer", str(exports_dir)])
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(exports_dir)])
        else:
            subprocess.Popen(["xdg-open", str(exports_dir)])
        return {"success": True, "path": str(exports_dir)}
    except Exception as e:
        return {"success": False, "error": str(e), "path": str(exports_dir)}


@router.post("/import/archive")
async def import_archive_endpoint(file: UploadFile = File(...)):
    """上传 ZIP，自动识别 manifest.kind 后合并。

    Returns:
        {"kind", "new_agents", "merged_agents", "added_sessions", "skipped_sessions",
         "added_files_count", "skipped_files_count", "errors"}
    """
    from backend import export_import as ei
    import tempfile

    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="只接受 .zip 文件")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    try:
        # 上传到临时盘（流式，避免大文件一次性读入内存）
        while chunk := await file.read(1024 * 1024):
            tmp.write(chunk)
            if tmp.tell() > ei.ARCHIVE_MAX_BYTES:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"archive 超过 {ei.ARCHIVE_MAX_BYTES // (1024 * 1024)}MB 上限",
                )
        tmp.close()
        tmp_path = Path(tmp.name)

        report = ei.import_archive(tmp_path)
        return report.to_dict()
    finally:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except OSError:
            pass
