"""社交网络图 API（/social/graph/*，Phase 3.2）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
依赖均可直接 import，不反向依赖 main.py：
- ensure_social_graph（backend.social_service）—— 跨层共享 helper
- social_graph 常量/类型（AFFINITY_EVENTS / LINE_* 等）

端点：角色关系查询/更新、用户三线关系、消息态度、团队推荐、社交图统计。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.social_graph import (
    AFFINITY_EVENTS, LINE_BOND, LINE_GUARDIAN, LINE_LOVER,
)
from backend.social_service import ensure_social_graph as _ensure_social_graph

router = APIRouter(tags=["social-graph"])


@router.get("/social/graph/relationships")
async def get_all_relationships(agent_id: Optional[str] = None):
    """获取关系列表（可选按角色过滤）"""
    graph = _ensure_social_graph()
    rels = graph.get_all_relationships(agent_id)
    return {
        "relationships": [
            {
                "key": graph.make_pair_key(r.agent_a, r.agent_b),
                "agent_a": r.agent_a,
                "agent_b": r.agent_b,
                "bond_types": r.bond_types,
                "affinity": round(r.affinity, 3),
                "bond_depth": r.bond_depth,
                "current_label": r.current_label,
                "interaction_count": r.interaction_count,
                "last_interaction": r.last_interaction,
            }
            for r in rels
        ]
    }


@router.get("/social/graph/relationships/{agent_a}/{agent_b}")
async def get_relationship_detail(agent_a: str, agent_b: str):
    """获取两个角色间的关系详情"""
    graph = _ensure_social_graph()
    rel = graph.get_relationship(agent_a, agent_b)
    if not rel:
        raise HTTPException(status_code=404, detail="关系不存在")
    return {"relationship": rel.to_dict()}


@router.get("/social/graph/summary/{agent_id}")
async def get_agent_relation_summary(agent_id: str):
    """获取某角色的关系摘要（前端展示用）"""
    graph = _ensure_social_graph()
    summary = graph.get_relationships_summary(agent_id)
    return {"agent_id": agent_id, "relationships": summary}


class AffinityUpdateRequest(BaseModel):
    """好感度更新请求"""
    agent_a: str
    agent_b: str
    event_type: str                     # 事件类型
    intensity: float = 1.0              # 事件强度
    description: str = ""               # 事件描述


@router.put("/social/graph/affinity")
async def update_affinity(body: AffinityUpdateRequest):
    """更新两个角色间的好感度"""
    if body.event_type not in AFFINITY_EVENTS:
        raise HTTPException(status_code=400, detail=f"无效事件类型: {body.event_type}，有效值: {list(AFFINITY_EVENTS.keys())}")
    if body.intensity < 0 or body.intensity > 1.0:
        raise HTTPException(status_code=400, detail="intensity 必须在 0~1.0 之间")

    graph = _ensure_social_graph()
    result = graph.update_affinity(
        body.agent_a, body.agent_b,
        body.event_type, body.intensity, body.description,
    )
    if not result:
        raise HTTPException(status_code=404, detail="关系不存在")

    # 持久化
    graph.save_to_disk()
    return result


# --- 用户关系三线 ---

@router.get("/social/graph/user-relations")
async def get_all_user_relations():
    """获取所有角色与用户的关系"""
    graph = _ensure_social_graph()
    return {
        "user_relations": {
            aid: ur.to_dict()
            for aid, ur in graph._user_relations.items()
        }
    }


@router.get("/social/graph/user-relations/{agent_id}")
async def get_user_relation(agent_id: str):
    """获取某角色与用户的关系"""
    graph = _ensure_social_graph()
    ur = graph.get_user_relation(agent_id)
    if not ur:
        raise HTTPException(status_code=404, detail="用户关系不存在")
    return {"user_relation": ur.to_dict()}


class UserRelationUpdateRequest(BaseModel):
    """用户关系更新请求"""
    line: Optional[str] = None          # 指定关系线
    affinity_delta: float = 0.0         # 好感度变化
    is_deep: bool = False               # 是否深度互动
    is_crisis: bool = False             # 是否危机互动


@router.put("/social/graph/user-relations/{agent_id}")
async def update_user_relation(agent_id: str, body: UserRelationUpdateRequest):
    """更新角色与用户的关系"""
    if body.line and body.line not in (LINE_LOVER, LINE_BOND, LINE_GUARDIAN):
        raise HTTPException(status_code=400, detail=f"无效关系线: {body.line}")

    graph = _ensure_social_graph()
    result = graph.update_user_relation(
        agent_id,
        line=body.line,
        affinity_delta=body.affinity_delta,
        is_deep=body.is_deep,
        is_crisis=body.is_crisis,
    )
    if not result:
        raise HTTPException(status_code=404, detail="用户关系不存在")

    graph.save_to_disk()
    return result


# --- 关系态度查询（给消息系统用）---

@router.get("/social/graph/attitude/{receiver_id}/{sender_id}")
async def get_message_attitude(receiver_id: str, sender_id: str):
    """获取角色收到跨角色消息时的态度"""
    graph = _ensure_social_graph()
    return graph.get_message_attitude(receiver_id, sender_id)


# --- 团队组建 ---

class TeamSuggestRequest(BaseModel):
    """团队推荐请求"""
    task_description: str
    available_agents: List[str]
    max_team_size: int = 4


@router.post("/social/graph/suggest-team")
async def suggest_team(body: TeamSuggestRequest):
    """根据关系和技能推荐团队组合"""
    graph = _ensure_social_graph()
    suggestions = graph.suggest_team_for_task(
        body.task_description,
        body.available_agents,
        body.max_team_size,
    )
    return {"suggestions": suggestions}


# --- 社交图统计 ---

@router.get("/social/graph/stats")
async def get_graph_stats():
    """获取社交图统计"""
    graph = _ensure_social_graph()
    return graph.get_stats()
