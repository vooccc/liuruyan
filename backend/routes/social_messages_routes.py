"""跨角色消息通道 API（/social/messages*，Phase 3.1）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
全部依赖来自 backend.social_bus，可直接 import，不反向依赖 main.py。

端点：
- POST   /social/messages                  发送跨角色消息（隐私过滤预检）
- GET    /social/messages/{agent_id}       获取消息（默认仅未读）
- PUT    /social/messages/{message_id}/read 标记已读
- PUT    /social/messages/{agent_id}/read-all 全部标记已读
- DELETE /social/messages/{message_id}     丢弃消息
"""
from __future__ import annotations

from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.social_bus import (
    get_message_bus, CrossAgentMessage, PrivacyFilter,
    MSG_INFORM, PRIORITY_NORMAL,
)

router = APIRouter(tags=["social-messages"])


class CrossMessageSend(BaseModel):
    """发送跨角色消息请求"""
    from_agent: str
    from_session: str
    to_agent: str
    msg_type: str = MSG_INFORM
    content: str
    emotion_tone: str = "平淡"
    context_bundle: Optional[Dict] = None
    priority: int = PRIORITY_NORMAL
    ttl: int = 3
    task_id: Optional[str] = None
    task_role: Optional[str] = None


@router.post("/social/messages")
async def send_cross_message(body: CrossMessageSend):
    """发送跨角色消息"""
    bus = get_message_bus()

    # 隐私过滤预检
    if PrivacyFilter.should_filter(body.content, body.msg_type):
        return {
            "status": "filtered",
            "reason": "消息包含敏感内容，已被隐私过滤器拦截"
        }

    msg = CrossAgentMessage.create(
        from_agent=body.from_agent,
        from_session=body.from_session,
        to_agent=body.to_agent,
        msg_type=body.msg_type,
        content=body.content,
        emotion_tone=body.emotion_tone,
        context_bundle=body.context_bundle,
        priority=body.priority,
        ttl=body.ttl,
        task_id=body.task_id,
        task_role=body.task_role,
    )

    result = await bus.send(msg)
    return result


@router.get("/social/messages/{agent_id}")
async def get_agent_messages(agent_id: str, unread_only: bool = True):
    """获取角色的消息（默认只返回未读）"""
    bus = get_message_bus()
    if unread_only:
        messages = bus.get_unread(agent_id)
    else:
        messages = bus.get_all_messages(agent_id)
    return {"agent_id": agent_id, "messages": messages, "count": len(messages)}


@router.put("/social/messages/{message_id}/read")
async def mark_message_read(message_id: str):
    """标记消息已读"""
    bus = get_message_bus()
    if bus.mark_read(message_id):
        return {"status": "success", "message_id": message_id}
    raise HTTPException(status_code=404, detail="消息不存在")


@router.put("/social/messages/{agent_id}/read-all")
async def mark_all_messages_read(agent_id: str):
    """标记角色所有消息已读"""
    bus = get_message_bus()
    count = bus.mark_all_read(agent_id)
    return {"status": "success", "agent_id": agent_id, "marked_count": count}


@router.delete("/social/messages/{message_id}")
async def delete_cross_message(message_id: str):
    """丢弃消息"""
    bus = get_message_bus()
    if bus.delete_message(message_id):
        return {"status": "success", "message_id": message_id}
    raise HTTPException(status_code=404, detail="消息不存在")


@router.post("/social/corridor")
async def send_corridor_message(body: dict):
    """走廊模式：角色用自己的话转述给另一角色（走收件箱）"""
    from_agent = body.get("from_agent")
    from_session = body.get("from_session")
    to_agent = body.get("to_agent")
    content = body.get("content", "")
    msg_type = body.get("msg_type", "inform")
    emotion_tone = body.get("emotion_tone", "平淡")

    if not from_agent or not to_agent or not content:
        raise HTTPException(status_code=400, detail="缺少 from_agent / to_agent / content")

    bus = get_message_bus()
    result = await bus.send_corridor_message(
        from_agent=from_agent,
        from_session=from_session or "",
        to_agent=to_agent,
        content=content,
        msg_type=msg_type,
        emotion_tone=emotion_tone,
    )
    return result


@router.get("/social/bus/stats")
async def get_bus_stats():
    """获取消息总线统计"""
    bus = get_message_bus()
    return bus.get_stats()
