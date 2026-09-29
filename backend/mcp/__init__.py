"""柳如烟 MCP（Model Context Protocol）子系统。

设计文档：`_docs/MCP_DESIGN.md`。
M1 范围：stdio + streamable_http 双 transport，无 OAuth，工具桥接到现有 CleanToolRegistry。

使用入口：
    from backend.mcp import get_controller
    ctrl = get_controller()
    await ctrl.bootstrap()
"""

from .controller import get_controller, MCPController  # noqa: F401
from .types import (  # noqa: F401
    MCPServerConfig,
    MCPServerState,
    MCPServerStatus,
    MCPToolInfo,
    MCPRootConfig,
)

__all__ = [
    "get_controller",
    "MCPController",
    "MCPServerConfig",
    "MCPServerState",
    "MCPServerStatus",
    "MCPToolInfo",
    "MCPRootConfig",
]
