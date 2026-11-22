"""项目档案与会话绑定工具（v5 记忆分层）。

1. ``set_project_binding``：让模型能按用户口头指令切换/解绑当前会话的项目上下文；
2. ``project_archive``：让模型按在场闭环纪律，通过 NamespaceManager 线程锁安全读写
   项目状态卡（FACTS.md）、追加事件溯源（timeline.jsonl）及更新项目元数据，
   避免裸改文件与后台 MemoryRefinerV2 发生并发覆盖。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _resolve_session_and_manager(kwargs: Dict[str, Any]):
    session_id = kwargs.get("session_id")
    agent_id = kwargs.get("agent_id")
    if not session_id or not agent_id:
        try:
            from backend.llm_client import _current_agent_id_var, _current_session_id_var

            session_id = session_id or _current_session_id_var.get()
            agent_id = agent_id or _current_agent_id_var.get()
        except Exception:
            pass

    from backend.memory_manager import get_memory_manager
    from backend.session import get_session_manager

    mm = get_memory_manager(agent_id=agent_id or "default")
    ns_manager = getattr(mm, "namespace_manager", None)
    session = get_session_manager()._sessions.get(session_id) if session_id else None
    return session_id, agent_id or "default", ns_manager, session


async def _handle_set_project_binding(
    project: str = "",
    **kwargs,
) -> str:
    """绑定或解绑当前会话的项目命名空间。"""
    session_id, _agent_id, ns_manager, session = _resolve_session_and_manager(kwargs)

    if not session_id:
        return json.dumps({"ok": False, "error": "当前不在有效会话上下文中"}, ensure_ascii=False)
    if session is None:
        return json.dumps({"ok": False, "error": f"会话 {session_id} 未加载"}, ensure_ascii=False)
    if ns_manager is None or not ns_manager.is_active():
        return json.dumps({"ok": False, "error": "该角色尚未启用项目记忆分层"}, ensure_ascii=False)

    target = (project or "").strip()
    if not target or target.lower() in ("none", "null", "解绑", "取消"):
        session.set_project_namespace(None)
        return json.dumps(
            {"ok": True, "project_namespace": None, "message": "已解绑当前会话的项目记忆，切回全局视角"},
            ensure_ascii=False,
        )

    matched = ns_manager.resolve_id_or_alias(target, include_archived=False)
    if matched is None:
        archived_hit = ns_manager.resolve_id_or_alias(target, include_archived=True)
        if archived_hit is not None:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"项目「{archived_hit.get('name')}」已归档，请先在记忆浏览器中恢复后再绑定",
                },
                ensure_ascii=False,
            )
        available = [e.get("name") for e in ns_manager.list_namespaces(include_archived=False)]
        return json.dumps(
            {"ok": False, "error": f"未找到项目 {target!r}", "available_projects": available},
            ensure_ascii=False,
        )

    session.set_project_namespace(matched["id"])
    return json.dumps(
        {
            "ok": True,
            "project_namespace": matched["id"],
            "project_name": matched.get("name"),
            "message": f"当前会话已绑定到项目「{matched.get('name')}」，后续仅注入该项目记忆",
        },
        ensure_ascii=False,
    )


async def _handle_project_archive(
    action: str = "list",
    project: Optional[str] = None,
    event: Optional[str] = None,
    decision: Optional[str] = None,
    cognition: Optional[str] = None,
    detail: Optional[str] = None,
    tags: Optional[List[str]] = None,
    temperature: str = "warm",
    card_content: Optional[str] = None,
    append_line: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    workspace_path: Optional[str] = None,
    summary: Optional[str] = None,
    topic: Optional[str] = None,
    topic_content: Optional[str] = None,
    target_topic: Optional[str] = None,
    **kwargs,
) -> str:
    """结构化读写项目档案（FACTS.md / topics/*.md / timeline.jsonl / REGISTRY.json）。"""
    from backend.namespace_manager import extract_card_summary

    _session_id, _agent_id, ns_manager, session = _resolve_session_and_manager(kwargs)
    if ns_manager is None:
        return json.dumps({"ok": False, "error": "NamespaceManager 不可用"}, ensure_ascii=False)

    act = (action or "").strip().lower()
    if act == "list":
        entries = ns_manager.list_namespaces(include_archived=False)
        bound = getattr(session, "project_namespace", None) if session is not None else None
        return json.dumps(
            {
                "ok": True,
                "active": ns_manager.is_active(),
                "current_session_project": bound,
                "projects": [
                    {
                        "id": e["id"],
                        "name": e["name"],
                        "aliases": e.get("aliases") or [],
                        "workspace_path": e.get("workspace_path"),
                        "summary": e.get("summary") or "",
                    }
                    for e in entries
                ],
            },
            ensure_ascii=False,
        )

    # 解析目标项目：显式 project > 当前会话绑定项目
    target_name = (project or "").strip()
    if not target_name and session is not None:
        target_name = getattr(session, "project_namespace", None) or ""

    if act == "create":
        if not target_name:
            return json.dumps({"ok": False, "error": "create 需要提供 project 名称"}, ensure_ascii=False)
        ns_manager.ensure_registry()
        existing = ns_manager.resolve_id_or_alias(target_name, include_archived=False)
        if existing is not None:
            return json.dumps(
                {"ok": True, "created": False, "namespace": existing, "message": "项目已存在"},
                ensure_ascii=False,
            )
        created = ns_manager.create_namespace(
            name=target_name,
            aliases=aliases,
            workspace_path=workspace_path,
        )
        return json.dumps({"ok": True, "created": True, "namespace": created}, ensure_ascii=False)

    if not ns_manager.is_active():
        return json.dumps({"ok": False, "error": "项目记忆分层尚未启用"}, ensure_ascii=False)
    if not target_name:
        return json.dumps(
            {"ok": False, "error": "未指定 project 且当前会话尚未绑定项目"},
            ensure_ascii=False,
        )

    entry = ns_manager.resolve_id_or_alias(target_name, include_archived=True)
    if entry is None:
        return json.dumps({"ok": False, "error": f"未找到项目 {target_name!r}"}, ensure_ascii=False)
    ns_id = entry["id"]

    if act == "read":
        return json.dumps(
            {
                "ok": True,
                "namespace": entry,
                "card": ns_manager.read_card(ns_id),
                "topics": ns_manager.list_topics(ns_id),
                "recent_timeline": ns_manager.read_timeline(ns_id, limit=15),
            },
            ensure_ascii=False,
        )

    if act == "list_topics":
        return json.dumps(
            {
                "ok": True,
                "ns_id": ns_id,
                "topics": ns_manager.list_topics(ns_id),
            },
            ensure_ascii=False,
        )

    if act == "read_topic":
        t_name = (topic or "").strip()
        if not t_name:
            return json.dumps({"ok": False, "error": "read_topic 需要提供 topic 名称"}, ensure_ascii=False)
        content = ns_manager.read_topic(ns_id, t_name)
        if not content:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"树叉「{t_name}」不存在或为空",
                    "available_topics": [t["name"] for t in ns_manager.list_topics(ns_id)],
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"ok": True, "ns_id": ns_id, "topic": t_name, "content": content},
            ensure_ascii=False,
        )

    if act == "update_topic":
        t_name = (topic or "").strip()
        if not t_name:
            return json.dumps({"ok": False, "error": "update_topic 需要提供 topic 名称"}, ensure_ascii=False)
        if topic_content is not None and topic_content.strip():
            saved_name = ns_manager.write_topic(ns_id, t_name, topic_content)
            return json.dumps(
                {"ok": True, "ns_id": ns_id, "topic": saved_name, "mode": "replace"},
                ensure_ascii=False,
            )
        if append_line is not None and append_line.strip():
            existing = ns_manager.read_topic(ns_id, t_name).rstrip()
            if not existing:
                existing = f"# {t_name}"
            saved_name = ns_manager.write_topic(ns_id, t_name, existing + "\n\n- " + append_line.strip())
            return json.dumps(
                {"ok": True, "ns_id": ns_id, "topic": saved_name, "mode": "append"},
                ensure_ascii=False,
            )
        return json.dumps(
            {"ok": False, "error": "update_topic 需要提供 topic_content 或 append_line"},
            ensure_ascii=False,
        )

    if act == "delete_topic":
        t_name = (topic or "").strip()
        if not t_name:
            return json.dumps({"ok": False, "error": "delete_topic 需要提供 topic 名称"}, ensure_ascii=False)
        deleted = ns_manager.delete_topic(ns_id, t_name)
        return json.dumps({"ok": deleted, "ns_id": ns_id, "topic": t_name}, ensure_ascii=False)

    if act == "merge_topics":
        src = (topic or "").strip()
        tgt = (target_topic or "").strip()
        if not src or not tgt:
            return json.dumps(
                {"ok": False, "error": "merge_topics 需要同时提供 topic（源）与 target_topic（目标）"},
                ensure_ascii=False,
            )
        try:
            merged_name = ns_manager.merge_topics(ns_id, src, tgt, merged_content=topic_content)
            return json.dumps(
                {"ok": True, "ns_id": ns_id, "source": src, "target": merged_name},
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    if act == "append_timeline":
        ev = (event or "").strip()
        if not ev:
            return json.dumps({"ok": False, "error": "append_timeline 需要提供 event 描述"}, ensure_ascii=False)
        payload: Dict[str, Any] = {
            "event": ev,
            "decision": (decision or "").strip() or "无",
            "cognition": (cognition or "").strip(),
            "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
            "temperature": temperature if temperature in ("burning", "warm", "cold") else "warm",
            "session_id": _session_id or "",
        }
        if topic and topic.strip():
            payload["topic"] = topic.strip()
        if detail and detail.strip():
            payload["detail"] = detail.strip()
        ns_manager.append_timeline(ns_id, payload)
        return json.dumps(
            {"ok": True, "ns_id": ns_id, "appended": payload},
            ensure_ascii=False,
        )

    if act == "update_card":
        if card_content is not None and card_content.strip():
            text = card_content.rstrip() + "\n"
            ns_manager.write_card(ns_id, text)
            new_summary = extract_card_summary(text, max_len=100)
            if new_summary:
                ns_manager.update_namespace(ns_id, summary=new_summary)
            return json.dumps(
                {"ok": True, "ns_id": ns_id, "mode": "replace", "summary": new_summary},
                ensure_ascii=False,
            )
        if append_line is not None and append_line.strip():
            ns_manager.append_card_line(ns_id, append_line.strip())
            new_summary = extract_card_summary(ns_manager.read_card(ns_id), max_len=100)
            if new_summary:
                ns_manager.update_namespace(ns_id, summary=new_summary)
            return json.dumps(
                {"ok": True, "ns_id": ns_id, "mode": "append", "summary": new_summary},
                ensure_ascii=False,
            )
        return json.dumps(
            {"ok": False, "error": "update_card 需要提供 card_content 或 append_line"},
            ensure_ascii=False,
        )

    if act == "update_meta":
        updated = ns_manager.update_namespace(
            ns_id,
            aliases=aliases,
            workspace_path=workspace_path,
            summary=summary,
        )
        return json.dumps({"ok": True, "namespace": updated}, ensure_ascii=False)

    return json.dumps(
        {
            "ok": False,
            "error": (
                f"不支持的 action: {action!r}，可选: list / create / read / list_topics / "
                "read_topic / update_topic / delete_topic / merge_topics / "
                "append_timeline / update_card / update_meta"
            ),
        },
        ensure_ascii=False,
    )
