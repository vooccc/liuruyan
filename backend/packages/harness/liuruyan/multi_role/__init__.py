"""liuruyan.multi_role — 多角色 / 多 IM 路由层。

落地：
- E-0a:  session_mapper（IM 消息 → 柳如烟 (user_id, agent_id, session_id)）
"""

from .session_mapper import SessionKey, map_session

__all__ = ["SessionKey", "map_session"]
