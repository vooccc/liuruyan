"""项目记忆绑定工具（v5 · 2026-10-24）。

`set_project_binding`：把**当前会话**的项目记忆上下文切到指定项目（或解绑）。
配合开篇自动识别（namespace_manager.autobind_project）使用：
- 常规路径：会话开头聊到项目 → 自动绑定，用户无感；
- 修正路径：用户在聊天里说"这个会话换到 XX 项目/别带项目记忆" → 模型调本工具切换。

只动当前会话（session_id 来自工具调用的 ctx），不碰其他会话。
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_UNBIND_TOKENS = {"", "none", "null", "全局", "不绑定", "解绑"}


async def _handle_set_project_binding(**kwargs: Any) -> str:
    from backend.memory_manager import get_memory_manager
    from backend.session import get_session_manager

    session_id = kwargs.get("session_id") or ""
    agent_id = kwargs.get("agent_id")
    raw_project = str(kwargs.get("project") or "").strip()

    def reply(ok: bool, message: str) -> str:
        return json.dumps({"ok": ok, "message": message}, ensure_ascii=False)

    ns_manager = get_memory_manager(agent_id=agent_id).namespace_manager
    if not ns_manager.is_active():
        return reply(False, "该角色未启用项目记忆分层，无需绑定。")

    if not session_id:
        return reply(False, "缺少会话上下文（session_id），无法定位当前会话。")
    session = get_session_manager()._sessions.get(session_id)
    if session is None:
        return reply(False, "当前会话不在内存态，请让用户在记忆面板「项目档案」里手动绑定。")

    # 解绑
    if raw_project.lower() in _UNBIND_TOKENS:
        previous = session.project_namespace
        session.set_project_namespace(None)
        label = ns_manager.get_namespace(previous)["name"] if previous else ""
        return reply(True, f"已解绑项目记忆（原：{label or '无'}）。下一轮起回到全局视角。")

    # 解析项目：id 精确 > name 精确 > 别名/名称子串
    ns_id = None
    if ns_manager.get_namespace(raw_project):
        ns_id = raw_project
    else:
        for entry in ns_manager.list_namespaces():
            if entry.get("name") == raw_project:
                ns_id = entry["id"]
                break
        if ns_id is None:
            ns_id = ns_manager.resolve_from_text(raw_project)

    if ns_id is None:
        available = "、".join(e["name"] for e in ns_manager.list_namespaces() if e.get("status") != "archived")
        return reply(False, f"未找到项目「{raw_project}」。已注册的项目：{available}")

    entry = ns_manager.get_namespace(ns_id) or {}
    session.set_project_namespace(ns_id)
    logger.info("[set_project_binding] session=%s -> %s", session_id, ns_id)
    return reply(
        True,
        f"当前会话已切换到「{entry.get('name', ns_id)}」的项目记忆，下一轮生效；"
        "其他项目的记忆已被隔离，如需跨项目参考请用 recall_memory 检索。",
    )


__all__ = ["_handle_set_project_binding"]
