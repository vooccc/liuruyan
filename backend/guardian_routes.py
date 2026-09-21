"""Guardian 权限配置 HTTP 路由。

抖出独立文件的目的是把这个模块加入 Guardian 红区——main.py 允许角色改（自进化），
但 guardian_routes.py 改不了，避免角色通过 main.py 加路由偷换 Guardian。

红区清单维护在 backend/tools/defense/guardian.py 的 _RED_ZONE_PATHS。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException


router = APIRouter()


@router.get("/guardian/config")
async def get_guardian_config():
    """读 guardian_config.json + 硬编码 defaults + 可配 agent 列表。

    前端用 effective_defaults（合并后）渲染 defaults 列；用 agents 列表渲染 overrides 矩阵行。
    config 字段保留用户当前落盘的子集，前端用来反向判断"是否被改过"。
    """
    from backend.guardian_config import (
        HARDCODED_DEFAULTS,
        load_guardian_config,
        merge_with_defaults,
    )
    from backend.agent_config import get_agent_manager
    cfg = load_guardian_config()
    manager = get_agent_manager()
    agents = [
        {"id": agent.get("id"), "name": agent.get("name") or agent.get("id")}
        for agent in manager.list_agents()
        if agent.get("id")
    ]
    return {
        "config": cfg,
        "hardcoded_defaults": HARDCODED_DEFAULTS,
        "effective_defaults": merge_with_defaults(cfg),
        "agents": agents,
    }


@router.put("/guardian/config")
async def set_guardian_config(body: dict):
    """整套覆盖 guardian_config.json，调用 Guardian.reload_config() 让单例立刻生效。"""
    from backend.guardian_config import (
        HARDCODED_DEFAULTS,
        load_guardian_config,
        merge_with_defaults,
        save_guardian_config,
        validate_guardian_payload,
    )
    from backend.agent_config import get_agent_manager
    from backend.tools.defense.guardian import get_guardian
    try:
        validated = validate_guardian_payload(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    manager = get_agent_manager()
    known_ids = {agent.get("id") for agent in manager.list_agents() if agent.get("id")}
    overrides = validated.get("overrides") or {}
    for agent_id in overrides.keys():
        if agent_id not in known_ids:
            raise HTTPException(
                status_code=400,
                detail=f"overrides 中的 agent_id '{agent_id}' 不存在",
            )
    save_guardian_config(validated)
    get_guardian().reload_config()
    cfg = load_guardian_config()
    agents = [
        {"id": agent.get("id"), "name": agent.get("name") or agent.get("id")}
        for agent in manager.list_agents()
        if agent.get("id")
    ]
    return {
        "status": "success",
        "config": cfg,
        "hardcoded_defaults": HARDCODED_DEFAULTS,
        "effective_defaults": merge_with_defaults(cfg),
        "agents": agents,
    }
