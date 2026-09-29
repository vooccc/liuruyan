"""v2 · E-5 · channels admin routes — IM 凭据 CRUD + 启停

设计：MEMORY project_v2_e5_implementation_plan.md Step 2

把 ``ChannelStore`` + ``ChannelManager.{add_and_start, stop_and_remove,
restart}`` 包成 HTTP 端点，让前端 SettingsView 能配置钉钉等 IM 凭据。

未启用 v2 channels 整套（``LIURUYAN_V2_CHANNELS=0``）时所有端点返 503。

端点：

```
GET    /v2/channels/configs                列所有（secret 字段脱敏成 ••••）
GET    /v2/channels/configs/{config_id}    单个详情（同样脱敏）
POST   /v2/channels/configs                创建 + 启动
PUT    /v2/channels/configs/{config_id}    更新（•••• 不覆盖原值）+ restart
DELETE /v2/channels/configs/{config_id}    停 + 删
POST   /v2/channels/configs/{id}/enable    enabled=True + restart
POST   /v2/channels/configs/{id}/disable   enabled=False + restart（停）
```

**安全机制**（双向 ••••）：

- 后端 GET 永远把 secret 类字段（名字含 ``secret``/``token``/``password``）
  改成 ``"••••••••"`` 再返；前端拿到 mask 字符串就当"密文存在"展示
- 前端 PUT 时如果某 secret 字段值就是 ``"••••••••"``，后端识别 → 不覆盖
  store 里的原值（实现"半填"语义）

漏一边即 → 泄密 / 凭据丢失。所以两边契约必须对齐 ``MASK_VALUE``。

**lazy factory 注册**：

POST 创建新 channel 时如果对应 factory 还没注册（如系统启动时 store 里
没 dingtalk 配置 → bootstrap 跳过 lazy import），admin route 这层在 add_and_start
之前 lazy 注册一次。这样用户能"从零创建第一个 dingtalk 配置"。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.v2_bootstrap import get_v2_channels_handle
from liuruyan.channels import ChannelConfig

log = logging.getLogger(__name__)


router = APIRouter(prefix="/v2/channels", tags=["v2-channels-admin"])


MASK_VALUE = "••••••••"

# 字段名包含其中之一（不区分大小写）→ secret，GET 时脱敏
_SECRET_KEY_NEEDLES = ("secret", "token", "password", "passwd")


def _is_secret_field(field_name: str) -> bool:
    lo = field_name.lower()
    return any(needle in lo for needle in _SECRET_KEY_NEEDLES)


def _redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """copy + 把所有 secret 字段值替换成 MASK_VALUE（保留空字符串）。"""
    out: dict[str, Any] = {}
    for k, v in config.items():
        if _is_secret_field(k) and isinstance(v, str) and v:
            out[k] = MASK_VALUE
        else:
            out[k] = v
    return out


def _merge_config_keep_secrets(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """合并 incoming → existing；incoming 里 secret=MASK 时保留 existing 的原值。

    PUT 半填语义：前端展示时拿到的是 ••••，用户没改的话原样传回，后端识别
    并不覆盖。
    """
    merged = dict(existing)
    for k, v in incoming.items():
        if _is_secret_field(k) and v == MASK_VALUE:
            continue  # 保留原值
        merged[k] = v
    return merged


# ── lazy factory 注册（POST 时按需注册 backend.channels.* adapter）────


def _ensure_factory_registered(manager: Any, channel: str) -> None:
    """如 manager 还没注册该 channel 的 factory，lazy import 并注册。

    失败时不抛——让 add_and_start 走"no factory → ERROR"分支让用户感知。
    """
    if channel in manager.registered_channels():
        return
    try:
        if channel == "dingtalk":
            from backend.channels import _lazy_dingtalk
            manager.register_factory("dingtalk", _lazy_dingtalk())
        elif channel == "matrix":
            from backend.channels import _lazy_matrix
            manager.register_factory("matrix", _lazy_matrix())
        elif channel == "feishu":
            from backend.channels import _lazy_feishu
            manager.register_factory("feishu", _lazy_feishu())
        elif channel == "wechat":
            from backend.channels import _lazy_wechat
            manager.register_factory("wechat", _lazy_wechat())
        elif channel == "web":
            from backend.channels import WebChannel
            manager.register_factory("web", WebChannel)
        else:
            log.warning(
                "[v2:channels:admin] no known backend factory for channel=%r; "
                "config will be stored but adapter won't start",
                channel,
            )
    except Exception:
        log.exception(
            "[v2:channels:admin] failed to lazy-register factory for channel=%r",
            channel,
        )


# ── pydantic schema ───────────────────────────────────────────────────


class ChannelConfigCreate(BaseModel):
    id: str = Field(..., min_length=1, max_length=128)
    channel: str = Field(..., min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ChannelConfigUpdate(BaseModel):
    config: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None


class ChannelConfigResponse(BaseModel):
    id: str
    channel: str
    config: dict[str, Any]   # secret 字段已脱敏
    enabled: bool
    created_at: float
    status: str              # ChannelManager 当前状态（running / error / stopped / ...）
    last_error: str = ""


def _build_response(cfg: ChannelConfig, manager: Any) -> ChannelConfigResponse:
    info = manager._info_for(cfg.id)
    if info is not None:
        status = info.status.value if hasattr(info.status, "value") else str(info.status)
        last_error = info.last_error
    else:
        status = "stopped"
        last_error = ""
    return ChannelConfigResponse(
        id=cfg.id,
        channel=cfg.channel,
        config=_redact_config(cfg.config),
        enabled=cfg.enabled,
        created_at=cfg.created_at,
        status=status,
        last_error=last_error,
    )


def _require_handle():
    """取 handle；未启用 → 503。"""
    handle = get_v2_channels_handle()
    if handle is None:
        raise HTTPException(
            status_code=503,
            detail="v2 channels not enabled (LIURUYAN_V2_CHANNELS=1)",
        )
    return handle


# ── 端点 ───────────────────────────────────────────────────────────────


@router.get("/configs", response_model=list[ChannelConfigResponse])
async def list_configs(channel: Optional[str] = None):
    """列所有 channel 配置（secret 已脱敏）。可选按 ``channel`` 过滤。"""
    handle = _require_handle()
    configs = handle.store.list_configs()
    if channel:
        configs = [c for c in configs if c.channel == channel]
    return [_build_response(c, handle.manager) for c in configs]


@router.get("/configs/{config_id}", response_model=ChannelConfigResponse)
async def get_config(config_id: str):
    handle = _require_handle()
    cfg = handle.store.get(config_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"config {config_id!r} not found")
    return _build_response(cfg, handle.manager)


@router.post("/configs", response_model=ChannelConfigResponse, status_code=201)
async def create_config(payload: ChannelConfigCreate):
    handle = _require_handle()
    if handle.store.get(payload.id) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"config {payload.id!r} already exists; use PUT to update",
        )

    _ensure_factory_registered(handle.manager, payload.channel)

    cfg = ChannelConfig(
        id=payload.id,
        channel=payload.channel,
        config=dict(payload.config),
        enabled=payload.enabled,
    )
    await handle.manager.add_and_start(cfg)
    # add_and_start 已 upsert 进 store；重读拿正典 created_at
    persisted = handle.store.get(payload.id)
    if persisted is None:
        # 理论不可能（add_and_start 内部 upsert 了）；防御性
        raise HTTPException(status_code=500, detail="config disappeared after upsert")
    return _build_response(persisted, handle.manager)


@router.put("/configs/{config_id}", response_model=ChannelConfigResponse)
async def update_config(config_id: str, payload: ChannelConfigUpdate):
    handle = _require_handle()
    existing = handle.store.get(config_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"config {config_id!r} not found")

    new_config = existing.config
    if payload.config is not None:
        new_config = _merge_config_keep_secrets(existing.config, payload.config)
    new_enabled = existing.enabled if payload.enabled is None else payload.enabled

    _ensure_factory_registered(handle.manager, existing.channel)

    updated = ChannelConfig(
        id=existing.id,
        channel=existing.channel,
        config=new_config,
        enabled=new_enabled,
        created_at=existing.created_at,
    )
    # add_and_start 走覆盖语义（先 stop 旧 + upsert + 启动新）
    await handle.manager.add_and_start(updated)
    persisted = handle.store.get(config_id)
    if persisted is None:
        raise HTTPException(status_code=500, detail="config disappeared after upsert")
    return _build_response(persisted, handle.manager)


@router.delete("/configs/{config_id}", status_code=204)
async def delete_config(config_id: str):
    handle = _require_handle()
    ok = await handle.manager.stop_and_remove(config_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"config {config_id!r} not found")
    return None


@router.post("/configs/{config_id}/enable", response_model=ChannelConfigResponse)
async def enable_config(config_id: str):
    handle = _require_handle()
    existing = handle.store.get(config_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"config {config_id!r} not found")
    handle.store.set_enabled(config_id, True)
    _ensure_factory_registered(handle.manager, existing.channel)
    await handle.manager.restart(config_id)
    persisted = handle.store.get(config_id)
    return _build_response(persisted, handle.manager)


@router.post("/configs/{config_id}/disable", response_model=ChannelConfigResponse)
async def disable_config(config_id: str):
    handle = _require_handle()
    existing = handle.store.get(config_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"config {config_id!r} not found")
    handle.store.set_enabled(config_id, False)
    # restart with enabled=False → just stop
    await handle.manager.restart(config_id)
    persisted = handle.store.get(config_id)
    return _build_response(persisted, handle.manager)


class WeChatTestRequest(BaseModel):
    corp_id: str = Field(..., description="企业 ID")
    corp_secret: str = Field(..., description="应用 Secret")


@router.post("/wechat/test")
async def test_wechat_credentials(body: WeChatTestRequest):
    """测试企业微信 CorpID 与 Secret 连通性。"""
    from backend.channels.wechat import WeChatChannel
    res = await WeChatChannel.verify_credentials(body.corp_id, body.corp_secret)
    return res


__all__ = ["router", "MASK_VALUE"]
