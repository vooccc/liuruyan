"""Computer Use 控制面 HTTP API（FastAPI）。

挂在 backend/main.py 的 app.include_router() 里：

    GET  /api/computer_use/policy      → 读策略（含动作白名单）
    PUT  /api/computer_use/policy      → 改策略（持久化到 ~/.liuruyan/computer_use_policy.json）
    POST /api/computer_use/release     → 强制释放单实例锁（前端"立即停止"按钮）
    GET  /api/computer_use/status      → 实时状态：lock holder / 最近 N 秒 action 数 / 平台支持
    GET  /api/computer_use/audit       → 读最近 N 条审计

设计原则：
- 改策略的同时落盘——下次启动自动 load_from_disk
- "立即停止"=释放锁；正在跑的 sidecar 进程有 timeout 兜底自然结束
- 不挂 v1 / v2 channels 标志位，是基础功能；但默认 policy.enabled=False，前端必须显式开
"""
from __future__ import annotations

import logging
import platform
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.computer_use import audit as _audit
from backend.computer_use import lock as _lock
from backend.computer_use import safety as _safety
from backend.computer_use import schema as _cu_schema
from backend.computer_use.sidecar import is_platform_supported

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/computer_use", tags=["computer-use"])


# ============================================================================
# Pydantic models
# ============================================================================
class PolicyPayload(BaseModel):
    """PUT /policy 入参。所有字段可选——只传想改的。"""
    enabled: Optional[bool] = None
    allowed_actions: Optional[List[str]] = Field(
        default=None,
        description="None = 允许全部；非空 list = 白名单（其他 action 全拒）",
    )
    blocked_actions: Optional[List[str]] = Field(
        default=None,
        description="黑名单：永远拒绝这些 action（即使 enabled=true）",
    )
    # 不在 pydantic 层硬限——让 policy_from_dict 把超界的值 clamp 到合法范围
    # （前端 slider 拖到 max 也能成功保存而不是 422）
    max_per_second: Optional[int] = None
    max_per_minute: Optional[int] = None
    # M5 视觉闭环开关
    auto_screenshot_after_mutate: Optional[bool] = None


# ============================================================================
# 路由
# ============================================================================
@router.get("/policy")
async def get_policy() -> Dict[str, Any]:
    """读当前策略 + meta（已知 action 列表）。"""
    return {
        "policy": _safety.policy_to_dict(),
        "meta": {
            "all_actions": sorted(_cu_schema.ALLOWED_ACTIONS),
            "platform": platform.system(),
            "platform_supported": is_platform_supported(),
        },
    }


@router.put("/policy")
async def put_policy(payload: PolicyPayload) -> Dict[str, Any]:
    """改策略 + 落盘。返回应用后的最新策略。"""
    raw: Dict[str, Any] = payload.model_dump(exclude_unset=True)
    if not raw:
        raise HTTPException(status_code=400, detail="empty body; nothing to update")

    # 验 action 名都在 schema 里（防 typo / LLM 幻造）
    for key in ("allowed_actions", "blocked_actions"):
        v = raw.get(key)
        if isinstance(v, list):
            unknown = [a for a in v if a not in _cu_schema.ALLOWED_ACTIONS]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"{key} contains unknown actions: {unknown}",
                )

    try:
        kwargs = _safety.policy_from_dict(raw)
        _safety.set_policy(**kwargs)
        _safety.save_to_disk()
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "ok": True,
        "policy": _safety.policy_to_dict(),
    }


@router.post("/release")
async def force_release(session_id: Optional[str] = None) -> Dict[str, Any]:
    """强制释放单实例锁。

    前端"立即停止"按钮命中这里。当前正在执行的 sidecar 调用有 timeout 兜底，
    不会卡死；释放锁后下一个 action 必须重新 acquire。

    - 不传 session_id → 强制释放（无视谁占着）
    - 传 session_id → 只释放自己占的锁
    """
    holder = _lock.read_holder()
    if not holder:
        return {"ok": True, "released": False, "reason": "no_lock_held"}

    if session_id is not None and holder.session_id != session_id:
        return {
            "ok": False,
            "released": False,
            "reason": "owned_by_other_session",
            "holder": {
                "session_id": holder.session_id,
                "pid": holder.pid,
            },
        }

    # release(holder.session_id) 只删自己的；这里要强制 → 直接删 lock 文件
    try:
        _lock._lock_path().unlink(missing_ok=True)  # noqa: SLF001 admin override 路径
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"lock unlink failed: {exc}") from exc

    return {
        "ok": True,
        "released": True,
        "former_holder": {
            "session_id": holder.session_id,
            "pid": holder.pid,
        },
    }


@router.get("/status")
async def status() -> Dict[str, Any]:
    """实时状态快照：锁、平台、近期活动。"""
    holder = _lock.read_holder()
    recent = _audit.read_recent(limit=20)

    # 最近 60 秒 action 数（粗算，不读 _safety._rate 内部状态）
    import time as _time
    now = _time.time()
    last_60s = sum(1 for r in recent if (now - r.get("ts", 0)) < 60)
    last_5s = sum(1 for r in recent if (now - r.get("ts", 0)) < 5)

    return {
        "platform": platform.system(),
        "platform_supported": is_platform_supported(),
        "policy_enabled": _safety.get_policy().enabled,
        "lock_holder": (
            None if holder is None
            else {
                "session_id": holder.session_id,
                "pid": holder.pid,
                "acquired_at": holder.acquired_at,
                "held_seconds": now - holder.acquired_at,
            }
        ),
        "activity": {
            "actions_last_5s": last_5s,
            "actions_last_60s": last_60s,
            "last_action": (recent[-1] if recent else None),
        },
    }


@router.get("/audit")
async def get_audit(
    limit: int = Query(50, ge=1, le=500),
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """读最近 limit 条审计；可按 session_id 过滤（应用侧过滤，不索引）。"""
    items = _audit.read_recent(limit=limit if session_id is None else limit * 4)
    if session_id is not None:
        items = [r for r in items if r.get("session_id") == session_id][-limit:]
    return {"items": items, "count": len(items)}


# ============================================================================
# M7: Replay
# ============================================================================
class ReplayRequest(BaseModel):
    """回放请求。"""
    session_id: Optional[str] = Field(default=None, description="按 session 过滤 audit；None=全局")
    last_n: int = Field(default=50, ge=1, le=500)
    max_steps: int = Field(default=100, ge=1, le=500)
    dry_run: bool = Field(default=True, description="默认 True：只 preview 计划，不真执行")
    speed_multiplier: float = Field(default=1.0, gt=0, le=10.0)
    abort_on_error: bool = Field(default=True)
    replay_session_id: Optional[str] = Field(
        default=None,
        description="执行 replay 时占用的 session_id（lock 取持）；dry_run=True 时不需要"
    )


@router.post("/replay")
async def post_replay(payload: ReplayRequest) -> Dict[str, Any]:
    """从 audit 构建 replay 计划；dry_run=False 时真执行。

    安全模型：
    - 即使 dry_run=False，每步仍走 executor._guarded_call → safety/audit 都生效
    - replay 自己也写 audit（action 字段保留，session_id 是当时的 replay_session_id）
    """
    from backend.computer_use import replay as _replay
    from backend.computer_use.executor import (
        ComputerUseBlocked, ComputerUseDenied, ComputerUseUnsupported, get_executor,
    )

    try:
        plan = _replay.build_plan(
            session_id=payload.session_id,
            last_n=payload.last_n,
            max_steps=payload.max_steps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    plan_dict = plan.to_dict()
    if payload.dry_run:
        return {"dry_run": True, "plan": plan_dict, "execution": None}

    # 真执行：必须给 replay_session_id（用于 lock）
    if not payload.replay_session_id:
        raise HTTPException(
            status_code=400,
            detail="replay_session_id is required when dry_run=False",
        )
    if not plan.steps:
        return {"dry_run": False, "plan": plan_dict, "execution": {
            "completed": True, "results": [], "step_count": 0,
            "executed_count": 0, "aborted_at": None,
        }}

    executor = get_executor(payload.replay_session_id)
    try:
        execution = await _replay.run_plan(
            plan,
            executor=executor,
            speed_multiplier=payload.speed_multiplier,
            dry_run=False,
            abort_on_error=payload.abort_on_error,
        )
    except ComputerUseDenied as exc:
        raise HTTPException(status_code=403, detail=f"replay blocked by safety: {exc}")
    except ComputerUseUnsupported as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except ComputerUseBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return {"dry_run": False, "plan": plan_dict, "execution": execution}
