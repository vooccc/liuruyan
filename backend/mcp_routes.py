"""MCP 子系统 REST API。

挂载点：FastAPI app.include_router(router) → /api/mcp/*
设计文档：`_docs/MCP_DESIGN.md`

M1 端点：
  GET    /api/mcp/servers                       列出所有 server（含 status + tools）
  GET    /api/mcp/servers/{server_id}           查单个
  POST   /api/mcp/servers                       新增/更新 server（body: AddServerRequest）
  PUT    /api/mcp/servers/{server_id}           更新单个 server config
  DELETE /api/mcp/servers/{server_id}           删除并停止
  POST   /api/mcp/servers/{server_id}/toggle    启停（body: {"enabled": true}）
  POST   /api/mcp/servers/{server_id}/restart   重启
  GET    /api/mcp/registry                      内置 catalog（spotlight 数据源）
  GET    /api/mcp/tools                         所有 server 当前可见工具（合并）

后续 milestone：OAuth callback、Elicitation reply 走 mcp_routes_oauth.py / mcp_routes_elicit.py。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from backend.mcp.controller import get_controller
from backend.mcp.registries import BUILTIN_CATALOG
from backend.mcp.types import (
    AddServerRequest,
    MCPServerConfig,
    ServerListItem,
    ToggleServerRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mcp", tags=["mcp"])


def _build_list_item(server_id: str) -> ServerListItem | None:
    ctrl = get_controller()
    cfg = ctrl.get_config(server_id)
    if cfg is None:
        return None
    return ServerListItem(
        server_id=server_id,
        config=cfg,
        status=ctrl.get_status(server_id),
        tools=ctrl.get_tools(server_id),
    )


@router.get("/servers")
async def list_servers() -> dict[str, Any]:
    ctrl = get_controller()
    items = []
    for sid in ctrl.list_servers():
        item = _build_list_item(sid)
        if item is not None:
            items.append(item.model_dump(mode="json"))
    return {"servers": items}


@router.get("/servers/{server_id}")
async def get_server(server_id: str) -> dict[str, Any]:
    item = _build_list_item(server_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"server '{server_id}' not found")
    return item.model_dump(mode="json")


@router.post("/servers")
async def add_or_update_server(req: AddServerRequest) -> dict[str, Any]:
    if not req.server_id or "/" in req.server_id or " " in req.server_id:
        raise HTTPException(status_code=400, detail="invalid server_id")
    ctrl = get_controller()
    status = await ctrl.add_or_update_server(req.server_id, req.config)
    return {
        "server_id": req.server_id,
        "status": status.model_dump(mode="json"),
    }


@router.put("/servers/{server_id}")
async def update_server(server_id: str, config: MCPServerConfig) -> dict[str, Any]:
    ctrl = get_controller()
    status = await ctrl.add_or_update_server(server_id, config)
    return {
        "server_id": server_id,
        "status": status.model_dump(mode="json"),
    }


@router.delete("/servers/{server_id}")
async def delete_server(server_id: str) -> dict[str, Any]:
    ctrl = get_controller()
    if server_id not in ctrl.list_servers():
        raise HTTPException(status_code=404, detail=f"server '{server_id}' not found")
    await ctrl.remove_server(server_id)
    return {"server_id": server_id, "removed": True}


@router.post("/servers/{server_id}/toggle")
async def toggle_server(server_id: str, req: ToggleServerRequest) -> dict[str, Any]:
    ctrl = get_controller()
    if server_id not in ctrl.list_servers():
        raise HTTPException(status_code=404, detail=f"server '{server_id}' not found")
    status = await ctrl.toggle_server(server_id, req.enabled)
    return {"server_id": server_id, "status": status.model_dump(mode="json")}


@router.post("/servers/{server_id}/restart")
async def restart_server(server_id: str) -> dict[str, Any]:
    ctrl = get_controller()
    if server_id not in ctrl.list_servers():
        raise HTTPException(status_code=404, detail=f"server '{server_id}' not found")
    status = await ctrl.restart_server(server_id)
    return {"server_id": server_id, "status": status.model_dump(mode="json")}


@router.get("/registry")
async def get_registry(force_refresh: bool = False) -> dict[str, Any]:
    """内置目录 + marketplace 订阅合并后的 catalog（spotlight 数据源）。

    - builtin 优先（防外部覆盖官方条目）
    - marketplace URL 拉取失败不影响 builtin 返回
    - 默认 1 小时缓存；前端要强刷传 ?force_refresh=1
    """
    from backend.mcp.marketplace import get_combined_catalog

    entries = await get_combined_catalog(force=force_refresh)
    return {"entries": [e.model_dump(mode="json") for e in entries]}


@router.get("/tools")
async def list_tools(agent_id: str | None = None) -> dict[str, Any]:
    """汇总所有 running server 的工具。可按 agent_id 过滤。"""
    ctrl = get_controller()
    tools = ctrl.get_available_tools(agent_id) if agent_id else ctrl.get_all_tools()
    return {"tools": [t.model_dump(mode="json") for t in tools]}


@router.post("/servers/{server_id}/reauth")
async def reauth_server(server_id: str) -> dict[str, Any]:
    """清空某 server 的 OAuth token 并触发重启，强制走完整授权流程。

    用户在前端点"重新授权"或 token refresh 失败跳 needs_reauth 时调。
    """
    ctrl = get_controller()
    if server_id not in ctrl.list_servers():
        raise HTTPException(status_code=404, detail=f"server '{server_id}' not found")
    from backend.mcp.oauth_manager import clear_tokens

    clear_tokens(server_id)
    status = await ctrl.restart_server(server_id)
    return {"server_id": server_id, "status": status.model_dump(mode="json")}


@router.get("/control-session-id")
async def get_control_session_id() -> dict[str, str]:
    """返回 MCP 状态广播用的 SSE pseudo-session id。

    前端用它订阅 `/sessions/<id>/events`，拿 `mcp.server.status` 等事件。
    """
    from backend.mcp.sse_bridge import MCP_CONTROL_SESSION_ID

    return {"session_id": MCP_CONTROL_SESSION_ID}


# ── M4.1: Elicitation 响应 ───────────────────────────────────────────────
class ElicitationResponse(BaseModel):
    action: str  # 'accept' / 'decline' / 'cancel'
    content: dict[str, Any] | None = None


@router.post("/elicitations/{request_id}/respond")
async def respond_elicitation(request_id: str, body: ElicitationResponse) -> dict[str, Any]:
    """前端 elicitation 弹窗用户填完表单后 POST 进来。"""
    from backend.mcp.elicitation import respond

    ok = respond(request_id, body.action, body.content)
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown or expired request_id: {request_id}")
    return {"ok": True, "request_id": request_id}


# ── M4.3: mcpb / dxt 包安装 ─────────────────────────────────────────────
@router.post("/install-mcpb")
async def install_mcpb(
    file: UploadFile = File(...),
    server_id: str | None = None,
) -> dict[str, Any]:
    """上传 .mcpb / .dxt 包安装一个 MCP server。

    body: multipart/form-data，字段 `file` 是 zip 文件。
    可选 query `?server_id=xxx` 强制指定 ID（默认 manifest.name）。
    """
    from backend.mcp.mcpb_installer import MCPBInstallError, install_from_bytes

    blob = await file.read()
    if not blob:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(blob) > 50 * 1024 * 1024:  # 50MB 上限——防 DOS
        raise HTTPException(status_code=413, detail="file too large (>50MB)")

    try:
        sid, cfg = install_from_bytes(blob, server_id_hint=server_id)
    except MCPBInstallError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ctrl = get_controller()
    status = await ctrl.add_or_update_server(sid, cfg)
    return {
        "server_id": sid,
        "status": status.model_dump(mode="json"),
        "config": cfg.model_dump(mode="json"),
    }


# ── M4.4: marketplace 订阅 ──────────────────────────────────────────────
class MarketplaceSubscriptions(BaseModel):
    urls: list[str]


@router.get("/marketplaces")
async def list_marketplaces() -> dict[str, Any]:
    from backend.mcp.marketplace import load_subscriptions

    return {"urls": load_subscriptions()}


@router.put("/marketplaces")
async def update_marketplaces(body: MarketplaceSubscriptions) -> dict[str, Any]:
    """全量替换订阅列表（保序去重）。"""
    from backend.mcp.marketplace import load_subscriptions, save_subscriptions

    save_subscriptions(body.urls)
    return {"urls": load_subscriptions()}
