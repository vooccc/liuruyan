"""Remote Access REST 路由。

端点：
    GET  /remote/status              ——  无需 loopback。返当前状态（enabled / host /
                                          dist_mounted）给 UI 显示用。
    GET  /remote/admin/config        ——  loopback-only。返完整 RemoteConfig。
    PUT  /remote/admin/config        ——  loopback-only。写回 + 返 needs_restart 标记。

⚠️ admin 端点仅接受 loopback (127.0.0.1 / ::1) 来源。
   理由：远程访问的总开关绝不能被远程端点自己关闭/篡改——chicken-and-egg 安全洞。
   攻击者拿到任意 token 也无法"关掉远程访问自我抹除痕迹"。

设计文档：``_docs/REMOTE_M8A.md`` §4.4 / §5 Step 2
隔离契约：``.windsurf/rules/remote-isolation.md`` §一·禁区第 4 条 + §二·内部纪律第 4 条
"""
from __future__ import annotations

import ipaddress
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import config as _config
from . import static as _static


router = APIRouter(prefix="/remote", tags=["remote"])


# ──────────────────────── loopback 守卫 ────────────────────────


_LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),  # IPv4 loopback 全段
    ipaddress.ip_network("::1/128"),
)


def _is_loopback(client_host: Optional[str]) -> bool:
    if not client_host:
        return False
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    return any(ip in net for net in _LOOPBACK_NETWORKS)


def _require_loopback(request: Request) -> None:
    """admin 端点 dependency：非 loopback 来源 → 403。"""
    client_host = request.client.host if request.client else None
    if not _is_loopback(client_host):
        raise HTTPException(
            status_code=403,
            detail={"error": "not_loopback", "client_ip": client_host or "unknown"},
        )


# ──────────────────────── 请求/响应模型 ────────────────────────


class UpdateConfigPayload(BaseModel):
    """PUT /remote/admin/config 请求体。

    所有字段都是可选——只传想改的字段，未传字段保留原值（增量更新语义，
    避免前端 PUT 时漏字段被默认值覆盖）。
    """
    enabled: Optional[bool] = Field(default=None)
    host: Optional[str] = Field(default=None)
    pwa_enabled: Optional[bool] = Field(default=None)


class StatusResponse(BaseModel):
    enabled: bool
    host: str
    pwa_enabled: bool
    updated_at: str
    # 运行时信息（不直接来自 config）
    dist_mounted: bool
    dist_path: Optional[str]
    mount_skip_reason: Optional[str]


# ──────────────────────── 端点实现 ────────────────────────


@router.get("/status", response_model=StatusResponse)
async def get_status() -> StatusResponse:
    """无需 loopback。返当前状态供 UI 显示。

    包含运行时信息（dist_mounted）让前端能直接在 UI 上提示
    "前端 dist 已挂载 ✓" / "未找到 dist，需要先 npm run build"。
    """
    cfg = _config.load_config()
    mount = _static.get_mount_state()
    return StatusResponse(
        enabled=cfg.enabled,
        host=cfg.host,
        pwa_enabled=cfg.pwa_enabled,
        updated_at=cfg.updated_at,
        dist_mounted=bool(mount.get("mounted")),
        dist_path=mount.get("dist_path"),
        mount_skip_reason=mount.get("skip_reason"),
    )


@router.get("/admin/config", dependencies=[Depends(_require_loopback)])
async def get_admin_config() -> Dict[str, Any]:
    """loopback-only。返完整配置。"""
    return _config.load_config().to_public_dict()


@router.put("/admin/config", dependencies=[Depends(_require_loopback)])
async def update_admin_config(payload: UpdateConfigPayload) -> Dict[str, Any]:
    """loopback-only。增量更新。

    返 ``needs_restart=True`` 提示用户重启柳如烟生效——uvicorn 监听 host 必须重启进程
    才能切换；M8a 不做热重启（参见隔离契约 §二·内部纪律第 2 条）。
    """
    cfg = _config.load_config()

    # 增量合并：只动 payload 显式给的字段
    if payload.enabled is not None:
        cfg.enabled = payload.enabled
    if payload.host is not None:
        if payload.host not in _config.ALLOWED_HOSTS:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_host",
                    "allowed": sorted(_config.ALLOWED_HOSTS),
                },
            )
        cfg.host = payload.host
    if payload.pwa_enabled is not None:
        cfg.pwa_enabled = payload.pwa_enabled

    # 自动联动：enabled=true 且 host 还是 loopback → 默认切 0.0.0.0
    # （拯救用户少点一次：他启用远程访问大概率就是要 0.0.0.0）
    if cfg.enabled and cfg.host == "127.0.0.1" and payload.host is None:
        cfg.host = "0.0.0.0"

    try:
        _config.save_config(cfg)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail={"error": "save_failed", "message": str(exc)},
        ) from exc

    return {
        "config": cfg.to_public_dict(),
        "needs_restart": True,
    }


__all__ = ["router"]
