"""社交图共享服务（从 main.py 抽出的跨层 helper）。

背景：``_ensure_social_graph`` 原本是 main.py 的模块级 helper，同时被
chat 流（_apply_user_relation_update / _apply_pair_affinity_update）和
/social/graph/* 路由使用。把 social 路由拆到 routes/ 模块后，这个 helper
必须有一个不依赖 main.py 的归宿，否则会形成 routes → main 的反向循环依赖。

本模块即该归宿：纯函数 + 仅依赖 social_graph / agent_config / paths /
game_mechanics / orchestrator，main.py 与各 social 路由模块都 import 它。
"""
from __future__ import annotations

from typing import Optional

from backend import paths
from backend.agent_config import get_agent_manager
from backend.game_mechanics import get_game_mechanics
from backend.orchestrator import get_orchestrator
from backend.social_graph import SocialGraph, get_social_graph

# 与 main.py 一致：进程启动时固定一次记忆数据目录（游戏化门面持久化用）。
_MEMORY_DATA_DIR = paths.memory_dir()


def ensure_social_graph() -> SocialGraph:
    """确保社交图已初始化（首次调用用当前角色 + 模板 ID 列表 initialize）。"""
    graph = get_social_graph(str(paths.memory_dir()))
    if not graph._agent_ids:
        # 首次调用，用当前角色列表初始化
        manager = get_agent_manager()
        agent_ids = [a["id"] for a in manager.list_agents()]
        # 同时加入模板ID（用于出厂关系）
        for tmpl in manager.list_templates():
            tid = tmpl.get("template_id", "")
            if tid and tid not in agent_ids:
                agent_ids.append(tid)
        graph.initialize(agent_ids)
    return graph


def ensure_game_mechanics():
    """确保游戏化门面使用固定持久化目录。"""
    return get_game_mechanics(str(_MEMORY_DATA_DIR))


def is_deep_user_message(message: str, response: str = "") -> bool:
    text = f"{message or ''}\n{response or ''}".strip()
    if not text:
        return False
    if len(text) >= 120:
        return True
    if text.count("\n") >= 2:
        return True
    deep_keywords = (
        "为什么", "怎么想", "害怕", "难过", "喜欢", "想你", "未来",
        "关系", "秘密", "真心", "信任", "陪我", "认真", "重要",
    )
    return any(keyword in text for keyword in deep_keywords)


def get_agent_name(agent_id: str) -> str:
    info = get_agent_manager().get_agent(agent_id) or {}
    return info.get("name", agent_id)


def apply_user_relation_update(agent_id: str, user_message: str, assistant_response: str) -> None:
    graph = ensure_social_graph()
    gm = ensure_game_mechanics()
    relation = graph.get_user_relation(agent_id)
    if not relation:
        return

    old_line = relation.current_line
    old_stage = relation.current_stage

    is_deep = is_deep_user_message(user_message, assistant_response)
    affinity_delta = 0.03 if is_deep else 0.015

    result = graph.update_user_relation(
        agent_id,
        line=None,
        affinity_delta=affinity_delta,
        is_deep=is_deep,
        is_crisis=False,
    )
    if not result:
        return

    gm.update_attention(
        agent_id,
        event="chat",
        intensity=1.15 if is_deep else 0.75,
        context=(user_message or "")[:80],
    )
    gm.notify_user_relation_change(
        agent_id,
        get_agent_name(agent_id),
        old_line,
        relation.current_line,
        old_stage,
        relation.current_stage,
    )
    graph.save_to_disk()


def classify_collab_event(scene_type: Optional[str]) -> str:
    if scene_type in ("cooperation", "crisis"):
        return "task_success"
    if scene_type == "conflict":
        return "conflict"
    return "cooperation"


def apply_pair_affinity_update(agent_a: str, agent_b: str, event_type: str, description: str = "") -> None:
    graph = ensure_social_graph()
    gm = ensure_game_mechanics()
    orch = get_orchestrator()

    rel = graph.get_relationship(agent_a, agent_b)
    if not rel:
        return

    old_affinity = rel.affinity
    old_label = rel.current_label
    old_bond_depth = rel.bond_depth

    result = graph.update_affinity(
        agent_a,
        agent_b,
        event_type=event_type,
        intensity=1.0,
        description=description,
    )
    if not result:
        return

    new_affinity = rel.affinity
    new_label = rel.current_label
    new_bond_depth = rel.bond_depth
    a_name = get_agent_name(agent_a)
    b_name = get_agent_name(agent_b)

    if new_affinity > old_affinity:
        attention_intensity = min((new_affinity - old_affinity) * 5, 1.5)
        gm.update_attention(agent_a, "chat", attention_intensity, description)
        gm.update_attention(agent_b, "chat", attention_intensity, description)

    gm.evolution.notify_affinity_change(
        agent_a,
        f"{a_name} ↔ {b_name}",
        old_affinity,
        new_affinity,
        event_type,
    )
    gm.achievements.check_and_unlock({
        "any_affinity": new_affinity,
        "high_affinity_count": sum(
            1 for s in gm.attention._states.values()
            if s.attention_score >= 0.6
        ),
    })
    gm._save()

    if old_label != new_label:
        gm.on_label_change(agent_a, a_name, agent_b, b_name, old_label, new_label)

    if new_bond_depth > old_bond_depth:
        milestone = orch.check_bond_milestone(agent_a, agent_b, new_bond_depth)
        if milestone:
            gm.on_bond_milestone(
                agent_a,
                a_name,
                agent_b,
                b_name,
                milestone["depth"],
                milestone["title"],
                milestone["desc"],
            )


def flush_social_and_game_state() -> None:
    ensure_social_graph().save_to_disk()


__all__ = [
    "ensure_social_graph",
    "ensure_game_mechanics",
    "is_deep_user_message",
    "get_agent_name",
    "apply_user_relation_update",
    "classify_collab_event",
    "apply_pair_affinity_update",
    "flush_social_and_game_state",
]
