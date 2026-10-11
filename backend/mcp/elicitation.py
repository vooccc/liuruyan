"""Elicitation 桥接（M4.1）。

server 调 elicit/create 时 → 经 SDK callback 到这里 → 经 SSE 推到前端
（事件 type=`mcp.elicitation.request`）→ 前端弹窗等用户填表 →
POST /api/mcp/elicitations/{id}/respond → 唤醒 Future → 返回给 SDK。

并发：每个 elicit 请求一个 UUID + 一个 Future；存全局 dict。timeout 用
config.elicitation.timeout_seconds 兜底，超时后 Future cancel 给 SDK 一个 ErrorData。

★ 不阻塞主 lifecycle task：elicitation_callback 是 async，await Future 期间
ClientSession 自己排队后续请求。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from mcp.types import ElicitResult, ErrorData

from .controller import get_controller

logger = logging.getLogger(__name__)


# pending_id → (future, server_id)
_pending: dict[str, tuple[asyncio.Future[ElicitResult | ErrorData], str]] = {}


async def elicitation_callback_for(server_id: str, params: Any) -> ElicitResult | ErrorData:
    """SDK 调进来；我们打事件给前端，await Future。"""
    request_id = uuid.uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[ElicitResult | ErrorData] = loop.create_future()
    _pending[request_id] = (fut, server_id)

    # 推 SSE
    try:
        from .sse_bridge import _send_nonblocking

        _send_nonblocking(
            {
                "type": "mcp.elicitation.request",
                "server_id": server_id,
                "request_id": request_id,
                # params 既可能是 form 也可能是 url；前端按字段分支
                "params": params.model_dump(mode="json") if hasattr(params, "model_dump") else dict(params),
            }
        )
    except Exception:  # noqa: BLE001
        logger.exception("[MCP-ELICIT] broadcast failed")

    # 取超时
    ctrl = get_controller()
    cfg = ctrl.get_config(server_id)
    timeout = (cfg.elicitation.timeout_seconds if cfg and cfg.elicitation else 60) or 60

    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        return ErrorData(code=-32001, message=f"elicitation timeout after {timeout}s")
    finally:
        _pending.pop(request_id, None)


def respond(request_id: str, action: str, content: dict | None = None) -> bool:
    """前端 POST 响应进来调本函数；返回是否找到对应请求。"""
    entry = _pending.get(request_id)
    if entry is None:
        return False
    fut, _ = entry
    if fut.done():
        return False
    if action in ("accept", "decline", "cancel"):
        fut.set_result(ElicitResult(action=action, content=content))
    else:
        fut.set_result(ErrorData(code=-32602, message=f"unknown action: {action}"))
    return True


def cancel_all_for_server(server_id: str) -> None:
    """server 重启 / 删除时调，避免悬挂 Future。"""
    for rid, (fut, sid) in list(_pending.items()):
        if sid == server_id and not fut.done():
            fut.set_result(ErrorData(code=-32000, message="server stopped"))


__all__ = ["elicitation_callback_for", "respond", "cancel_all_for_server"]
