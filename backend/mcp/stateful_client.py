"""MCP StatefulClient —— 跨 task lifecycle 安全实现。

★ 借鉴 ACP `acp/compat` 的核心 patch：

问题（柳如烟 FastAPI + uvicorn 必踩）：
  - connect() 进入 AsyncExitStack 在 task A（如 startup task / 配置热更新 task）
  - close() 退出 AsyncExitStack 在 task B（如 shutdown task / reload task）
  - anyio.CancelScope 要求 enter/exit 在同一 task，跨 task 时报 RuntimeError 被静默吞掉
  - 子进程不被回收，每次 reload 泄漏一份 → CPU 100% / 僵尸进程

解：
  - 单 background task 内 `async with AsyncExitStack`，进入 transport 后等 stop_event.wait()
  - start() / stop() 是 thin wrapper，只跨 task 发信号（asyncio.Event），不跨 task 走 ExitStack
  - 即使父 task 被 cancel，lifecycle task 也能在自己 task 内干净退出 stack

支持 transport：
  - stdio (StdioServerParameters + stdio_client)
  - streamable_http (mcp.client.streamable_http.streamablehttp_client)
  - sse (mcp.client.sse.sse_client) —— 兼容遗留 server
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from .config import resolve_env_dict, resolve_env_vars
from .types import MCPServerConfig, MCPServerState, MCPToolInfo

# server-push notification 类型（M3）。callback 入参就是 ServerNotification.root（具体 model）
NotificationListener = Callable[[str, Any], None]

logger = logging.getLogger(__name__)


class StatefulClientError(Exception):
    """StatefulClient 内部错误。"""


class StatefulMCPClient:
    """单个 MCP server 的 stateful 连接客户端。

    生命周期：
        client = StatefulMCPClient(server_id, config)
        await client.start()        # 阻塞到 ClientSession.initialize() 完成或超时
        tools = client.tools         # list[MCPToolInfo]
        result = await client.call_tool(name, args, timeout=...)
        await client.stop()          # 优雅关闭

    线程安全：方法只能在 asyncio loop 里调；多任务并发调 call_tool 安全（mcp ClientSession 自带锁）。
    """

    def __init__(
        self,
        server_id: str,
        config: MCPServerConfig,
        on_notification: NotificationListener | None = None,
    ):
        self.server_id = server_id
        self.config = config
        self._lifecycle_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._ready_event: asyncio.Event = asyncio.Event()
        self._session: Any = None  # mcp.ClientSession
        self._tools: list[MCPToolInfo] = []
        self._state: MCPServerState = MCPServerState.IDLE
        self._error: str | None = None
        self._started_at: float | None = None
        # M3: server → client 通知回调（ToolListChanged / ResourceListChanged 等）
        self._on_notification = on_notification

    # ── public state accessors ────────────────────────────────────────────
    @property
    def state(self) -> MCPServerState:
        return self._state

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def started_at(self) -> float | None:
        return self._started_at

    @property
    def tools(self) -> list[MCPToolInfo]:
        return list(self._tools)

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        """启动 server 并等待 initialize 完成。

        失败时 state 标 DEGRADED，error 字段记录原因，不抛——让 controller 决定是否重试。
        """
        if self._state in (MCPServerState.STARTING, MCPServerState.RUNNING):
            return
        self._state = MCPServerState.STARTING
        self._error = None
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()

        # 在当前 loop 里 spawn lifecycle task —— enter/exit 都在它内部，跨 task 安全
        self._lifecycle_task = asyncio.create_task(
            self._lifecycle(), name=f"mcp-lifecycle-{self.server_id}"
        )

        # 等 ready 或 lifecycle 提前结束（错误情况）
        connect_timeout = max(1, int(self.config.connect_timeout))
        ready_wait = asyncio.create_task(self._ready_event.wait())
        done, pending = await asyncio.wait(
            {ready_wait, self._lifecycle_task},
            timeout=connect_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            if t is ready_wait:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        if ready_wait not in done and self._lifecycle_task not in done:
            # 超时
            self._error = f"connect timeout after {connect_timeout}s"
            self._state = MCPServerState.DEGRADED
            await self._signal_stop_and_join()
            return
        if self._lifecycle_task in done and not self._ready_event.is_set():
            # lifecycle task 提前结束（启动失败）；error 已由 _lifecycle 写入
            return
        # 正常 ready
        self._state = MCPServerState.RUNNING
        self._started_at = time.time()

    async def stop(self) -> None:
        """优雅关闭。失败也不抛——shutdown 路径要尽可能完成。"""
        if self._state == MCPServerState.IDLE:
            return
        self._state = MCPServerState.STOPPING
        await self._signal_stop_and_join()
        self._state = MCPServerState.IDLE
        self._session = None
        self._tools = []
        self._started_at = None

    async def _signal_stop_and_join(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._lifecycle_task and not self._lifecycle_task.done():
            try:
                # 给 lifecycle task 一个合理的 join 窗口；超时也不强 cancel——
                # cancel 会跨 task 触发 anyio CancelScope，正是 ACP patch 要避免的事。
                await asyncio.wait_for(self._lifecycle_task, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("[MCP-LIFECYCLE] %s did not exit within 10s", self.server_id)
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-LIFECYCLE] %s join error", self.server_id)

    # ── lifecycle body —— 必须在单 task 内完成 enter/exit AsyncExitStack ──
    async def _lifecycle(self) -> None:
        """Long-running task：进入 transport context → 等 stop_event → 退出。

        关键：enter / exit AsyncExitStack 都在**这个 task** 里发生，
        anyio.CancelScope 的 task affinity 要求满足，子进程能干净回收。
        """
        try:
            async with AsyncExitStack() as stack:
                read_stream, write_stream = await self._enter_transport(stack)

                # mcp 1.x: ClientSession 是 async context manager
                from mcp import ClientSession
                from mcp.types import ServerNotification

                read_timeout = timedelta(seconds=max(1, int(self.config.timeout)))

                async def _message_handler(message):  # noqa: ANN001
                    """处理 server → client 推送：通知 / 请求 / 异常。

                    我们只关心 ServerNotification（其中 ToolListChanged 是核心 M3 用例）。
                    Request 走 SDK 注册的 callback（sampling/elicitation 由 ClientSession 字段
                    分发，本 handler 不接管）。Exception 让 SDK 自身抛回 lifecycle。
                    """
                    if isinstance(message, ServerNotification):
                        try:
                            self._handle_server_notification(message)
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "[MCP-NOTIF] %s on_notification listener failed",
                                self.server_id,
                            )

                # M4: server-push request callbacks
                from .elicitation import elicitation_callback_for
                from .sampling import sampling_callback_for

                async def _elicit_cb(_ctx, params):  # noqa: ANN001
                    return await elicitation_callback_for(self.server_id, params)

                async def _sampling_cb(_ctx, params):  # noqa: ANN001
                    return await sampling_callback_for(self.server_id, params)

                session = await stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=read_timeout,
                        message_handler=_message_handler,
                        elicitation_callback=_elicit_cb,
                        sampling_callback=_sampling_cb,
                    )
                )
                self._session = session

                # initialize
                await session.initialize()

                # 拉一次 tools list
                await self._refresh_tools_locked()

                # 标 ready，等 stop
                self._ready_event.set()
                await self._stop_event.wait()
        except asyncio.CancelledError:
            # 父 task / wait_for 触发 cancel —— 让 stack 在本 task 内自然退出。
            # 注意：只在 error 未被上层显式设置时记一笔（避免 timeout 等更具体的错被覆盖）。
            if not self._error:
                self._error = "lifecycle cancelled"
            raise
        except Exception as e:  # noqa: BLE001
            self._error = f"{type(e).__name__}: {e}"
            self._state = MCPServerState.DEGRADED
            logger.exception("[MCP-LIFECYCLE] %s lifecycle failed", self.server_id)
        finally:
            # ready_event 即便没 set 也要解锁，避免 start() 永久等
            if not self._ready_event.is_set():
                self._ready_event.set()

    async def _enter_transport(self, stack: AsyncExitStack):
        """根据 config 选 transport，进入 async context，返回 (read_stream, write_stream)。"""
        transport_type = self.config.resolve_transport()

        if transport_type == "stdio":
            return await self._enter_stdio(stack)
        if transport_type == "streamable_http":
            return await self._enter_streamable_http(stack)
        if transport_type == "sse":
            return await self._enter_sse(stack)
        raise StatefulClientError(f"unsupported transport: {transport_type}")

    async def _enter_stdio(self, stack: AsyncExitStack):
        import shutil

        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not self.config.command:
            raise StatefulClientError("stdio transport requires `command`")
        # env：白名单 + ${env:} 展开，避免暴露宿主全部环境
        # 关键 PATH 等保留，其余按用户配置 + 展开占位符
        merged_env = self._build_subprocess_env()

        # Windows asyncio.create_subprocess_exec 不做 PATH 查找（直接 CreateProcess），
        # 必须传绝对路径。用 shutil.which 在我们刚构造的 PATH（含 venv\Scripts）里解析命令。
        # 这样 npx / uvx / node / python 等都能定位到。
        cmd = self.config.command
        resolved = shutil.which(cmd, path=merged_env.get("PATH"))
        if resolved is None and os.name == "nt":
            # Windows 上 .exe / .cmd / .bat 后缀候选都试一遍（npx 在 Windows 通常是 npx.cmd）
            for ext in (".cmd", ".bat", ".exe", ".ps1"):
                resolved = shutil.which(cmd + ext, path=merged_env.get("PATH"))
                if resolved:
                    break
        final_cmd = resolved or cmd  # 解析失败也别强吞，让原 SDK 报更详细的错

        params = StdioServerParameters(
            command=final_cmd,
            args=list(self.config.args or []),
            env=merged_env,
            cwd=self.config.cwd or None,
        )
        ctx = stdio_client(params)
        streams = await stack.enter_async_context(ctx)
        # mcp 1.x stdio_client 返回 (read, write)
        return streams[0], streams[1]

    async def _enter_streamable_http(self, stack: AsyncExitStack):
        from mcp.client.streamable_http import streamablehttp_client

        if not self.config.url:
            raise StatefulClientError("streamable_http transport requires `url`")
        url = resolve_env_vars(self.config.url)
        headers = {k: resolve_env_vars(v) for k, v in (self.config.headers or {}).items()}

        # M2: OAuth 2.1——如果配置了 oauth，构造 OAuthClientProvider 给 SDK
        auth = None
        if self.config.oauth is not None and self.config.oauth.enabled:
            try:
                from .oauth_manager import resolve_oauth_provider

                auth, cleanup = await resolve_oauth_provider(self.server_id, self.config)
                if cleanup is not None:
                    # 确保 lifecycle 退出时清理 callback server（闭包里的 aiohttp runner）
                    async def _do_cleanup():
                        try:
                            await cleanup()
                        except Exception:  # noqa: BLE001
                            logger.exception("[MCP-OAUTH] cleanup failed")

                    stack.push_async_callback(_do_cleanup)
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-OAUTH] build provider failed for %s", self.server_id)
                auth = None

        ctx = streamablehttp_client(url, headers=headers or None, auth=auth)
        streams = await stack.enter_async_context(ctx)
        # streamablehttp_client 返回 (read, write, _session_id_callback)
        return streams[0], streams[1]

    async def _enter_sse(self, stack: AsyncExitStack):
        from mcp.client.sse import sse_client

        if not self.config.url:
            raise StatefulClientError("sse transport requires `url`")
        url = resolve_env_vars(self.config.url)
        headers = {k: resolve_env_vars(v) for k, v in (self.config.headers or {}).items()}
        ctx = sse_client(url, headers=headers or None)
        streams = await stack.enter_async_context(ctx)
        return streams[0], streams[1]

    def _build_subprocess_env(self) -> dict[str, str]:
        """构造 stdio 子进程的环境变量。

        策略：保留宿主基础 env（PATH / HOME / TEMP 等），叠加用户配置（已展开 ${env:}）。
        """
        base_keep = {
            "PATH",
            "HOME",
            "USERPROFILE",
            "TEMP",
            "TMP",
            "TMPDIR",
            "SystemRoot",
            "APPDATA",
            "LOCALAPPDATA",
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "PROGRAMDATA",
            "LANG",
            "LC_ALL",
        }
        env: dict[str, str] = {}
        for k in base_keep:
            v = os.environ.get(k)
            if v is not None:
                env[k] = v
        # 把当前 Python 同目录 prepend 进 PATH。这样 venv 里 pip 装的命令
        # （uvx / pip-installed CLI tools）能被子进程 spawn 找到。
        # 启动 backend 时即使没显式 activate venv，sys.executable 已经指向 venv\python.exe，
        # 它的同目录就是 venv\Scripts（Windows）或 venv/bin（Unix），里面有 uvx 等。
        import sys as _sys
        _venv_scripts = str(Path(_sys.executable).parent)
        existing_path = env.get("PATH", "")
        sep = os.pathsep
        if _venv_scripts and _venv_scripts not in existing_path.split(sep):
            env["PATH"] = _venv_scripts + (sep + existing_path if existing_path else "")

        if _sys.platform == "win32":
            # 注入 Windows 防黑窗 hook：为所有 Node 子进程及其派生命令（git / rg / tar 等）
            # 默认补齐 windowsHide: true，彻底根治 MCP Server 内部频繁调 git 时疯狂弹黑窗口。
            quiet_node_script = Path(__file__).parent / "win_quiet_node.cjs"
            if quiet_node_script.exists():
                script_path = str(quiet_node_script.resolve())
                if " " in script_path:
                    try:
                        import ctypes
                        buf = ctypes.create_unicode_buffer(1024)
                        if ctypes.windll.kernel32.GetShortPathNameW(script_path, buf, 1024) > 0:
                            script_path = buf.value
                    except Exception:
                        pass
                node_opt_flag = f'--require "{script_path}"' if " " in script_path else f"--require {script_path}"
                curr_node_opts = env.get("NODE_OPTIONS", "")
                if script_path not in curr_node_opts:
                    env["NODE_OPTIONS"] = (
                        f"{curr_node_opts} {node_opt_flag}".strip()
                        if curr_node_opts
                        else node_opt_flag
                    )

        # 用户配置的 env（含 ${env:} 展开）覆盖
        env.update(resolve_env_dict(self.config.env or {}))
        return env

    # ── tool calls ────────────────────────────────────────────────────────
    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        """调一个 MCP tool。返回 mcp `CallToolResult`（含 content / isError）。"""
        if self._state != MCPServerState.RUNNING or self._session is None:
            raise StatefulClientError(
                f"server {self.server_id} not running (state={self._state.value})"
            )
        effective_timeout = timeout if timeout is not None else float(self.config.timeout)
        coro = self._session.call_tool(tool_name, arguments=arguments)
        try:
            return await asyncio.wait_for(coro, timeout=effective_timeout)
        except asyncio.TimeoutError as e:
            raise StatefulClientError(
                f"tool call {tool_name} timeout after {effective_timeout}s"
            ) from e

    async def list_tools(self) -> list[MCPToolInfo]:
        await self._refresh_tools_locked()
        return list(self._tools)

    # ── M3: server-push 通知处理 ──────────────────────────────────────────
    def _handle_server_notification(self, message: Any) -> None:
        """同步分发 ServerNotification → 触发对应处理。

        ToolListChanged 是核心：spawn 一个后台 task 重新 list_tools，刷完
        通过 on_notification 回调让 controller 重新注册到 tool_bridge + 推 SSE。
        其它通知先打日志预留，等具体使用场景再实现。
        """
        from mcp.types import (
            PromptListChangedNotification,
            ResourceListChangedNotification,
            ResourceUpdatedNotification,
            ToolListChangedNotification,
        )

        root = getattr(message, "root", message)

        if isinstance(root, ToolListChangedNotification):
            logger.info("[MCP-NOTIF] %s tools/list_changed", self.server_id)
            # 异步刷新——不能在 message_handler 同步路径里 await list_tools
            # （SDK 已经在 ClientSession 同 task 内调它，重入会死锁）
            asyncio.get_event_loop().create_task(
                self._async_refresh_and_notify(),
                name=f"mcp-tools-refresh-{self.server_id}",
            )
        elif isinstance(root, (ResourceListChangedNotification, ResourceUpdatedNotification)):
            logger.info("[MCP-NOTIF] %s %s", self.server_id, type(root).__name__)
        elif isinstance(root, PromptListChangedNotification):
            logger.info("[MCP-NOTIF] %s prompts/list_changed", self.server_id)
        else:
            logger.debug("[MCP-NOTIF] %s untracked: %s", self.server_id, type(root).__name__)

        if self._on_notification is not None:
            try:
                self._on_notification(self.server_id, root)
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-NOTIF] external listener failed")

    async def _async_refresh_and_notify(self) -> None:
        """ToolListChanged 后台刷新 + 通知 controller。"""
        try:
            await self._refresh_tools_locked()
        except Exception:  # noqa: BLE001
            logger.exception("[MCP-NOTIF] %s refresh after list_changed failed", self.server_id)
            return
        # 通过同一个 on_notification 钩子告知 controller "刷完了，请重新注册工具"
        if self._on_notification is not None:
            try:
                self._on_notification(self.server_id, "tools_refreshed")
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-NOTIF] tools_refreshed listener failed")

    async def _refresh_tools_locked(self) -> None:
        if self._session is None:
            return
        result = await self._session.list_tools()
        tools: list[MCPToolInfo] = []
        for t in getattr(result, "tools", []) or []:
            qname = qualify_tool_name(self.server_id, t.name)
            tools.append(
                MCPToolInfo(
                    server_id=self.server_id,
                    name=t.name,
                    qualified_name=qname,
                    description=t.description or "",
                    input_schema=getattr(t, "inputSchema", {}) or {},
                )
            )
        self._tools = tools


# ── 工具名规范（标准命名空间格式）────────────────────────────────────────
import re as _re

_SERVER_NAME_RE = _re.compile(r"^[A-Za-z0-9_-]+$")


def qualify_tool_name(server_name: str, tool_name: str) -> str:
    """`mcp__<server>__<tool>`；server_name 不合规时降级为 `mcp__<tool>`。

    采用标准的 MCP 工具命名空间前缀，保障跨客户端兼容。
    """
    sn = (server_name or "").replace(" ", "_")
    if _SERVER_NAME_RE.match(sn):
        return f"mcp__{sn.lower()}__{tool_name}"
    return f"mcp__{tool_name}"


__all__ = [
    "StatefulMCPClient",
    "StatefulClientError",
    "qualify_tool_name",
]
