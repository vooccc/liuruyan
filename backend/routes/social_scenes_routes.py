"""场景编排 API（/social/scenes*，Phase 3.3）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
依赖均可直接 import，不反向依赖 main.py：
- get_orchestrator / VALID_SCENE_TYPES / SCENE_TYPE_LABELS / END_RESOLVED（backend.orchestrator）
- get_agent_manager（backend.agent_config）/ get_session_manager（backend.session）

不含 /social/collab-chat（SSE 协作对话流，深度耦合 chat 流式与多角色编排，
仍留在 main.py）。

端点：场景创建/列表/详情/推进/解决/取消/召唤。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.agent_config import get_agent_manager
from backend.orchestrator import (
    END_RESOLVED, SCENE_TYPE_LABELS, VALID_SCENE_TYPES, get_orchestrator,
)
from backend.session import get_session_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["social-scenes"])


class SceneCreateRequest(BaseModel):
    scene_type: str                         # 场景类型
    participants: List[str]                 # 参与角色
    trigger: str = ""                       # 触发描述
    shared_context: Optional[Dict] = None   # 场景上下文
    linked_task_id: Optional[str] = None    # 关联任务
    ttl: int = 12                           # 最大轮数
    force: bool = False                     # 跳过冷却检查


class SceneAdvanceRequest(BaseModel):
    speaker_response: Optional[str] = None  # 发言者回复内容


@router.post("/social/scenes")
async def create_scene(body: SceneCreateRequest):
    """创建叙事场景

    同时立即创建对应的 collab session（collab_type='scene'），保证场景一被创建
    就在 listSessions() 中可见 —— 否则前端议事厅侧栏看不到这个场景，直到用户
    在 collab-chat 流里发出第一条消息为止。
    """
    if body.scene_type not in VALID_SCENE_TYPES:
        raise HTTPException(status_code=400, detail=f"无效场景类型: {body.scene_type}，可选: {', '.join(VALID_SCENE_TYPES)}")

    orch = get_orchestrator()
    scene, reason = orch.create_scene(
        scene_type=body.scene_type,
        participants=body.participants,
        trigger=body.trigger,
        shared_context=body.shared_context,
        linked_task_id=body.linked_task_id,
        ttl=body.ttl,
        force=body.force,
    )
    if not scene:
        raise HTTPException(status_code=409, detail=reason)

    # 立即建对应 session（collab-chat 懒创建分支会因 metadata.scene_id 已有匹配 session 而复用）
    session_id = None
    try:
        agent_mgr = get_agent_manager()
        session_mgr = get_session_manager()
        participant_names = []
        for pid in scene.participants:
            info = agent_mgr.get_agent(pid)
            participant_names.append(info.get("name", pid) if info else pid)
        # 标题只放参与者列表；场景类型由前端 RoundtableList 卡片上的色条 + tag 表达，
        # 头像群已经显示参与者。重复在 title 里写 "场景：自由（柳如烟、风浪）" 既冗长
        # 又跟卡片下方信息重复。如有特殊命名需要，用户可手动改 title。
        _ = SCENE_TYPE_LABELS  # 保留 import 以防其它路径仍依赖
        title = '、'.join(participant_names) if participant_names else "协作"
        session = await session_mgr.create_session(
            agent_id=scene.participants[0] if scene.participants else "default",
            title=title,
            model=None,
            collab_type="scene",
            scene_id=scene.id,
            scene_type=scene.scene_type,
            participants=list(scene.participants or []),
        )
        session_id = session.id
    except Exception as exc:
        # session 建失败不影响场景创建本身；collab-chat 懒创建仍可作为 fallback
        logger.warning("[create_scene] failed to create companion session: %s", exc)

    return {"status": "created", "scene": scene.to_dict(), "session_id": session_id}


@router.get("/social/scenes/active")
async def get_active_scenes():
    """获取活跃场景列表"""
    orch = get_orchestrator()
    scenes = orch.get_active_scenes()
    return {"scenes": [s.to_dict() for s in scenes], "count": len(scenes)}


@router.get("/social/scenes/{scene_id}")
async def get_scene_detail(scene_id: str):
    """获取场景详情"""
    orch = get_orchestrator()
    scene = orch.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="场景不存在")
    return {"scene": scene.to_dict()}


@router.put("/social/scenes/{scene_id}/advance")
async def advance_scene(scene_id: str, body: SceneAdvanceRequest):
    """推进场景（下一轮）"""
    orch = get_orchestrator()
    scene, reason = orch.advance_scene(scene_id, body.speaker_response)
    if not scene:
        raise HTTPException(status_code=400, detail=reason)
    return {"status": "advanced", "scene": scene.to_dict()}


@router.put("/social/scenes/{scene_id}/resolve")
async def resolve_scene(scene_id: str, body: Optional[Dict] = None):
    """解决场景"""
    end_condition = (body or {}).get("end_condition", END_RESOLVED)
    orch = get_orchestrator()
    scene, reason = orch.resolve_scene(scene_id, end_condition)
    if not scene:
        raise HTTPException(status_code=400, detail=reason)
    return {"status": "resolved", "scene": scene.to_dict()}


@router.delete("/social/scenes/{scene_id}")
async def cancel_scene(scene_id: str):
    """取消场景"""
    orch = get_orchestrator()
    scene, reason = orch.cancel_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=400, detail=reason)
    return {"status": "cancelled", "scene": scene.to_dict()}


@router.post("/social/scenes/{scene_id}/summon")
async def summon_to_scene(scene_id: str, body: dict):
    """召唤角色加入场景"""
    agent_id = body.get("agent_id")
    if not agent_id:
        raise HTTPException(status_code=400, detail="缺少 agent_id")

    orch = get_orchestrator()
    scene, reason = orch.add_participant(scene_id, agent_id)
    if not scene:
        raise HTTPException(status_code=400, detail=reason)
    return {"status": "joined", "scene": scene.to_dict()}
