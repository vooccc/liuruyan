"""把 MCPController 的状态变化广播到 sse_manager。

前端通过 `/sessions/__mcp_control__/events` 订阅，拿到：
  - mcp.server.status （每次 server 状态变化）
  - mcp.tools.changed （工具列表更新，M3 用）
  - mcp.oauth.needed  （OAuth 流程需用户介入）

与现有 sse_manager 复用断线续传 + ring buffer + bounded queue，不新增总线。
使用固定 pseudo session_id `__mcp_control__`，不跟真实会话混淆。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.sse_manager import get_sse_manager

from .controller import get_controller
from .types import MCPServerStatus

logger = logging.getLogger(__name__)

MCP_CONTROL_SESSION_ID = "__mcp_control__"


def _send_nonblocking(event: dict[str, Any]) -> None:
    """把 event 发到 sse_manager，跨 sync/async 通用。

    send_event 是 async，但主体只是 put_nowait；用 create_task fire-and-forget。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 不在 event loop 里（测试 / 启动期）——跳过，不崩
        return
    sse = get_sse_manager()
    loop.create_task(sse.send_event(MCP_CONTROL_SESSION_ID, event))


def _status_listener(server_id: str, status: MCPServerStatus) -> None:
    _send_nonblocking(
        {
            "type": "mcp.server.status",
            "server_id": server_id,
            "state": status.state.value,
            "error": status.error,
            "started_at": status.started_at,
            "tool_count": status.tool_count,
        }
    )


_attached = False


def attach_to_controller() -> None:
    """注入 listener 一次。重复调用安全（幂等）。"""
    global _attached
    if _attached:
        return
    ctrl = get_controller()
    ctrl.subscribe_status(_status_listener)
    _attached = True
    logger.info("[MCP-SSE] attached controller → sse_manager")


def broadcast_tools_changed(server_id: str, tool_count: int) -> None:
    """工具列表刷新事件（M3 通知用；M2 stateful_client 启动后也触发一次）。"""
    _send_nonblocking(
        {
            "type": "mcp.tools.changed",
            "server_id": server_id,
            "tool_count": tool_count,
        }
    )


def broadcast_oauth_needed(server_id: str, authorize_url: str) -> None:
    _send_nonblocking(
        {
            "type": "mcp.oauth.needed",
            "server_id": server_id,
            "authorize_url": authorize_url,
        }
    )


__all__ = [
    "MCP_CONTROL_SESSION_ID",
    "attach_to_controller",
    "broadcast_tools_changed",
    "broadcast_oauth_needed",
]
