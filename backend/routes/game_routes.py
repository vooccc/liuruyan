"""游戏化打磨 API（Phase 3.4）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致：
- 关注值 attention（角色被关注程度）
- 吃醋 jealousy（偏向检测 + 行为修饰）
- 成就 achievements（解锁状态 + 统计）
- 通知 notifications（成就/演化推送队列）
- 总览 stats

所有 handler 只依赖游戏化门面单例 ``get_game_mechanics``；数据目录与
main.py 一致（``paths.memory_dir()``）。门面是进程级单例，首次调用定目录，
此后忽略入参——故此处与 main.py 各自获取同一实例，互不冲突。
"""
from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import paths
from backend.game_mechanics import get_game_mechanics

router = APIRouter(prefix="/game", tags=["game"])


def _ensure_game_mechanics():
    """确保游戏化门面使用固定持久化目录。"""
    return get_game_mechanics(str(paths.memory_dir()))


class AttentionUpdateRequest(BaseModel):
    event: str                              # 事件类型（见 ATTENTION_EVENTS）
    intensity: float = 1.0                  # 强度 0~1.5
    context: str = ""                       # 上下文


class JealousyTriggerRequest(BaseModel):
    favored_agent: str                      # 被偏向的角色
    context: str = ""                       # 上下文


class JealousyCalmRequest(BaseModel):
    amount: float = 0.3                     # 平复量


class AchievementCheckRequest(BaseModel):
    stats: Dict                             # 游戏状态数据
    context: str = ""


@router.get("/attention/{agent_id}")
async def get_attention(agent_id: str):
    """获取角色关注值"""
    gm = _ensure_game_mechanics()
    return gm.get_attention(agent_id)


@router.get("/attention")
async def get_all_attention():
    """获取所有角色关注值"""
    gm = _ensure_game_mechanics()
    return {"attention": gm.get_all_attention()}


@router.put("/attention/{agent_id}")
async def update_attention(agent_id: str, body: AttentionUpdateRequest):
    """更新角色关注值"""
    gm = _ensure_game_mechanics()
    result = gm.update_attention(agent_id, body.event, body.intensity, body.context)
    return result


@router.post("/jealousy/trigger")
async def trigger_jealousy(body: JealousyTriggerRequest):
    """触发吃醋检测"""
    gm = _ensure_game_mechanics()
    events = gm.trigger_jealousy(body.favored_agent, body.context)
    return {"jealousy_events": events, "count": len(events)}


@router.get("/jealousy/active")
async def get_active_jealousies():
    """获取当前吃醋状态"""
    gm = _ensure_game_mechanics()
    return {"jealousies": gm.get_jealousy_status()}


@router.get("/jealousy/{agent_id}/modifier")
async def get_jealousy_modifier(agent_id: str):
    """获取角色吃醋行为修饰（注入 system prompt 用）"""
    gm = _ensure_game_mechanics()
    modifier = gm.get_jealousy_modifier(agent_id)
    if not modifier:
        return {"active": False, "modifier": None}
    return {"active": True, "modifier": modifier}


@router.put("/jealousy/{agent_id}/calm")
async def calm_jealousy(agent_id: str, body: JealousyCalmRequest):
    """平复角色吃醋"""
    gm = _ensure_game_mechanics()
    result = gm.calm_jealousy(agent_id, body.amount)
    if not result:
        raise HTTPException(status_code=404, detail="该角色没有在吃醋")
    return result


@router.get("/achievements")
async def get_achievements():
    """获取成就列表（含解锁状态）"""
    gm = _ensure_game_mechanics()
    return {"achievements": gm.get_achievements()}


@router.get("/achievements/stats")
async def get_achievement_stats():
    """获取成就统计"""
    gm = _ensure_game_mechanics()
    return gm.get_achievement_stats()


@router.post("/achievements/check")
async def check_achievements(body: AchievementCheckRequest):
    """手动检查成就（通常由系统自动调用）"""
    gm = _ensure_game_mechanics()
    newly = gm.check_achievements(body.stats, body.context)
    return {"newly_unlocked": newly, "count": len(newly)}


@router.get("/notifications")
async def get_game_notifications():
    """获取待推送的游戏通知（成就+演化）"""
    gm = _ensure_game_mechanics()
    notifs = gm.pop_all_notifications()
    return {"notifications": notifs, "count": len(notifs)}


@router.get("/notifications/history")
async def get_notification_history(limit: int = 50):
    """获取通知历史"""
    gm = _ensure_game_mechanics()
    return {"history": gm.evolution.get_history(limit)}


@router.get("/stats")
async def get_game_stats():
    """获取游戏化系统总览统计"""
    gm = _ensure_game_mechanics()
    return gm.get_stats()
