"""叙事编排引擎 API（/social/orchestrator/*，Phase 3.3）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
依赖来自 backend.orchestrator，可直接 import，不反向依赖 main.py。

端点：
- GET  /social/orchestrator/achievements 工作成就列表
- GET  /social/orchestrator/milestones   羁绊里程碑 + 专属事件定义
- GET  /social/orchestrator/energy        叙事能量（按 agent 或全量）
- POST /social/orchestrator/energy        增加叙事能量
- GET  /social/orchestrator/stats         编排引擎统计
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from backend.orchestrator import (
    BOND_MILESTONES, EXCLUSIVE_EVENTS, get_orchestrator,
)

router = APIRouter(tags=["social-orchestrator"])


@router.get("/social/orchestrator/achievements")
async def get_achievements():
    """获取工作成就列表"""
    orch = get_orchestrator()
    return {"achievements": orch.get_achievements()}


@router.get("/social/orchestrator/milestones")
async def get_milestones():
    """获取羁绊里程碑定义"""
    return {"milestones": BOND_MILESTONES, "exclusive_events": [
        {**e, "agents": list(e["agents"])} for e in EXCLUSIVE_EVENTS
    ]}


@router.get("/social/orchestrator/energy")
async def get_narrative_energy(agent_id: Optional[str] = None):
    """获取叙事能量"""
    orch = get_orchestrator()
    if agent_id:
        return {"agent_id": agent_id, "energy": orch.get_energy(agent_id)}
    return {"energy": {aid: orch.get_energy(aid) for aid in orch._narrative_energy}}


@router.post("/social/orchestrator/energy")
async def add_narrative_energy(body: dict):
    """为角色增加叙事能量"""
    agent_id = body.get("agent_id")
    amount = body.get("amount", 5.0)
    if not agent_id:
        raise HTTPException(status_code=400, detail="缺少 agent_id")
    orch = get_orchestrator()
    orch.add_energy(agent_id, amount)
    return {"status": "success", "agent_id": agent_id, "energy": orch.get_energy(agent_id)}


@router.get("/social/orchestrator/stats")
async def get_orchestrator_stats():
    """获取编排引擎统计"""
    orch = get_orchestrator()
    return orch.get_stats()
