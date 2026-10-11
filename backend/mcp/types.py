"""MCP 子系统数据模型。

兼容 Claude Desktop / Cursor 配置文件格式（`mcpServers` 字典），
扩展柳如烟独有字段：`agents`（角色级权限）、`concurrency_class`、`sampling`、`elicitation`。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ── transport ──────────────────────────────────────────────────────────────
TransportType = Literal["stdio", "streamable_http", "sse"]


class StdioTransport(BaseModel):
    type: Literal["stdio"] = "stdio"
    command: str = Field(..., description="子进程可执行命令，如 npx / uvx / python")
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict, description="子进程环境变量；支持 ${env:VAR} 占位符")
    cwd: str | None = None


class HttpTransport(BaseModel):
    type: Literal["streamable_http", "sse"] = "streamable_http"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


# ── OAuth（M1 占位，M2 实现）──────────────────────────────────────────────
class MCPOAuthConfig(BaseModel):
    enabled: bool = True
    token_url: str = ""
    authorize_url: str = ""
    client_id: str | None = None
    client_secret: str | None = None
    scopes: list[str] = Field(default_factory=list)
    auto_refresh: bool = True
    refresh_skew_seconds: int = 60


# ── per-server 子配置 ─────────────────────────────────────────────────────
class SamplingConfig(BaseModel):
    enabled: bool = False
    allowed_models: list[str] = Field(default_factory=list)
    max_tokens_cap: int = 4096
    max_rpm: int = 10


class ElicitationConfig(BaseModel):
    enabled: bool = True
    timeout_seconds: int = 60


# ── 主配置 ────────────────────────────────────────────────────────────────
class MCPServerConfig(BaseModel):
    """单个 MCP server 的配置。

    与 Claude Desktop 同名字段保持一致；柳如烟扩展字段加 `liuruyan_*` 前缀（避免污染兼容字段）。
    实际使用时既支持 Claude Desktop 风格的扁平 `command/args/url`，也支持嵌套 `transport`。
    """

    # 兼容字段（标准 MCP 客户端通用）
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    # 显式 transport（覆盖兼容字段的推断）
    transport: TransportType | None = None

    # 通用字段
    enabled: bool = True
    timeout: int = 120
    connect_timeout: int = 60
    description: str = ""
    cwd: str | None = None

    # 柳如烟扩展
    agents: list[str] = Field(
        default_factory=lambda: ["*"],
        description="允许使用此 server 的角色 id 列表；'*' 表示全部。",
    )
    concurrency_class: Literal["read", "write", "exclusive"] = "read"
    sampling: SamplingConfig = Field(default_factory=SamplingConfig)
    elicitation: ElicitationConfig = Field(default_factory=ElicitationConfig)
    oauth: MCPOAuthConfig | None = None

    def resolve_transport(self) -> TransportType:
        """从字段推断 transport 类型。"""
        if self.transport:
            return self.transport
        if self.url:
            return "streamable_http"
        if self.command:
            return "stdio"
        raise ValueError("MCPServerConfig 必须至少配置 command（stdio）或 url（http/sse）")


class MCPRootConfig(BaseModel):
    """配置文件根结构：`{"mcpServers": {...}}`，兼容 Claude Desktop。"""

    mcpServers: dict[str, MCPServerConfig] = Field(default_factory=dict)


# ── runtime 状态 ──────────────────────────────────────────────────────────
class MCPServerState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"  # 启动失败或运行中断；保留实例待 restart
    STOPPING = "stopping"
    NEEDS_OAUTH = "needs_oauth"
    NEEDS_REAUTH = "needs_reauth"


class MCPServerStatus(BaseModel):
    server_id: str
    state: MCPServerState
    error: str | None = None
    started_at: float | None = None
    last_error_at: float | None = None
    tool_count: int = 0


class MCPToolInfo(BaseModel):
    """从 server 拉到的工具元数据，用于前端展示和 tool_bridge 注册。"""

    server_id: str
    name: str  # MCP 协议原始名（不带前缀）
    qualified_name: str  # 注册到 CleanToolRegistry 的 mcp__<server>__<tool>
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


# ── REST API 请求/响应 ─────────────────────────────────────────────────────
class AddServerRequest(BaseModel):
    server_id: str
    config: MCPServerConfig


class UpdateServerRequest(BaseModel):
    config: MCPServerConfig


class ToggleServerRequest(BaseModel):
    enabled: bool


class ServerListItem(BaseModel):
    server_id: str
    config: MCPServerConfig
    status: MCPServerStatus
    tools: list[MCPToolInfo] = Field(default_factory=list)


class CatalogEntry(BaseModel):
    """spotlight 内置目录条目。"""

    name: str
    title: str
    description: str
    icon: str = ""
    homepage: str = ""
    configuration: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    requires_token: bool = False
