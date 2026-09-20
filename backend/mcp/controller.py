"""MCPController —— 全局单例，管理所有 MCP server 实例。

★ 借鉴 chatbox `controller.ts` (229 行) 的极简骨架：
  - `servers: Map<id, {instance, config}>`
  - bootstrap / start / stop / update / status 订阅
  - 工具聚合（多 server tools 合并到 CleanToolRegistry）

柳如烟独有：
  - ContextVar session_id 透传走 dispatch_batch 既有路径，本类不持有 per-call 状态
  - 状态变化广播到 sse_manager（前端订阅 'mcp.server.status'）
  - 角色级权限：get_available_tools(agent_id) 按 config.agents 过滤
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

from .config import load_config, save_config
from .stateful_client import StatefulMCPClient
from .types import (
    MCPRootConfig,
    MCPServerConfig,
    MCPServerState,
    MCPServerStatus,
    MCPToolInfo,
)

logger = logging.getLogger(__name__)

StatusListener = Callable[[str, MCPServerStatus], None]


class _ServerEntry:
    __slots__ = ("client", "config")

    def __init__(self, client: StatefulMCPClient, config: MCPServerConfig):
        self.client = client
        self.config = config


class MCPController:
    """所有 MCP server 的中枢。**仅在 asyncio loop 线程内调用方法**。"""

    def __init__(self) -> None:
        self._servers: dict[str, _ServerEntry] = {}
        self._listeners: set[StatusListener] = set()
        self._tool_bridge_register: Callable[[list[MCPToolInfo]], None] | None = None
        self._tool_bridge_unregister: Callable[[str], None] | None = None
        self._lock = asyncio.Lock()  # 串行化 add/remove/restart，避免 registry 撕裂

    # ── 注入点（main.py bootstrap 时调）───────────────────────────────────
    def set_tool_bridge(
        self,
        register: Callable[[list[MCPToolInfo]], None],
        unregister: Callable[[str], None],
    ) -> None:
        """注入 tool_bridge 回调；MCP server ready 时 register，stop 时 unregister。"""
        self._tool_bridge_register = register
        self._tool_bridge_unregister = unregister

    # ── 状态订阅（前端走 sse_manager，后端走这个）─────────────────────────
    def subscribe_status(self, listener: StatusListener) -> Callable[[], None]:
        self._listeners.add(listener)
        # 推一次当前状态
        for sid in self._servers:
            try:
                listener(sid, self.get_status(sid))
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-CTRL] listener initial push failed")

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def _emit_status(self, server_id: str) -> None:
        status = self.get_status(server_id)
        for ln in list(self._listeners):
            try:
                ln(server_id, status)
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-CTRL] listener notify failed")

    # ── 查询 ──────────────────────────────────────────────────────────────
    def list_servers(self) -> list[str]:
        return list(self._servers.keys())

    def get_status(self, server_id: str) -> MCPServerStatus:
        entry = self._servers.get(server_id)
        if entry is None:
            return MCPServerStatus(server_id=server_id, state=MCPServerState.IDLE)
        c = entry.client
        return MCPServerStatus(
            server_id=server_id,
            state=c.state,
            error=c.error,
            started_at=c.started_at,
            tool_count=len(c.tools),
        )

    def get_config(self, server_id: str) -> MCPServerConfig | None:
        entry = self._servers.get(server_id)
        return entry.config if entry else None

    def get_tools(self, server_id: str) -> list[MCPToolInfo]:
        entry = self._servers.get(server_id)
        return entry.client.tools if entry else []

    def get_all_tools(self) -> list[MCPToolInfo]:
        out: list[MCPToolInfo] = []
        for entry in self._servers.values():
            out.extend(entry.client.tools)
        return out

    def get_available_tools(self, agent_id: str | None) -> list[MCPToolInfo]:
        """按角色权限过滤后聚合。'*' 通配；agent_id None → 仅 '*'。"""
        from .config import visible_to_agent

        out: list[MCPToolInfo] = []
        for entry in self._servers.values():
            if entry.client.state != MCPServerState.RUNNING:
                continue
            if not visible_to_agent(entry.config, agent_id):
                continue
            out.extend(entry.client.tools)
        return out

    def get_client(self, server_id: str) -> StatefulMCPClient | None:
        entry = self._servers.get(server_id)
        return entry.client if entry else None

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def bootstrap(self) -> None:
        """从配置文件加载所有 enabled server 并启动。

        所有 server 启动并行，单个失败不阻塞其他。
        """
        root: MCPRootConfig = load_config()
        if not root.mcpServers:
            logger.info("[MCP-CTRL] no servers configured, skip bootstrap")
            return
        async with self._lock:
            tasks = []
            for sid, cfg in root.mcpServers.items():
                if not cfg.enabled:
                    logger.info("[MCP-CTRL] %s is disabled, skip", sid)
                    continue
                tasks.append(asyncio.create_task(self._start_one(sid, cfg)))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(
            "[MCP-CTRL] bootstrap done: %d server(s) running",
            sum(1 for e in self._servers.values() if e.client.state == MCPServerState.RUNNING),
        )

    async def shutdown(self) -> None:
        async with self._lock:
            tasks = [
                asyncio.create_task(self._stop_one(sid)) for sid in list(self._servers.keys())
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("[MCP-CTRL] shutdown done")

    async def add_or_update_server(self, server_id: str, config: MCPServerConfig) -> MCPServerStatus:
        """新增或更新 server，并持久化配置文件。

        - 已存在且 enabled：stop 旧 → start 新
        - 已存在且 disabled：仅更新配置不启动
        - 不存在且 enabled：start 新
        """
        from .config import upsert_server

        async with self._lock:
            upsert_server(server_id, config)
            existing = self._servers.get(server_id)
            if existing is not None:
                await self._stop_one_locked(server_id)
            if config.enabled:
                await self._start_one(server_id, config)
            else:
                # 仅记账，不启动
                self._servers[server_id] = _ServerEntry(
                    client=StatefulMCPClient(server_id, config),
                    config=config,
                )
        self._emit_status(server_id)
        return self.get_status(server_id)

    async def remove_server(self, server_id: str) -> None:
        from .config import remove_server as cfg_remove

        async with self._lock:
            await self._stop_one_locked(server_id)
            self._servers.pop(server_id, None)
            cfg_remove(server_id)
        self._emit_status(server_id)

    async def restart_server(self, server_id: str) -> MCPServerStatus:
        async with self._lock:
            entry = self._servers.get(server_id)
            if entry is None:
                return MCPServerStatus(server_id=server_id, state=MCPServerState.IDLE)
            cfg = entry.config
            await self._stop_one_locked(server_id)
            if cfg.enabled:
                await self._start_one(server_id, cfg)
        self._emit_status(server_id)
        return self.get_status(server_id)

    async def toggle_server(self, server_id: str, enabled: bool) -> MCPServerStatus:
        from .config import toggle_server as cfg_toggle

        async with self._lock:
            cfg = cfg_toggle(server_id, enabled)
            if cfg is None:
                return MCPServerStatus(server_id=server_id, state=MCPServerState.IDLE)
            new_cfg = cfg.mcpServers[server_id]
            existing = self._servers.get(server_id)
            if enabled:
                if existing is None or existing.client.state != MCPServerState.RUNNING:
                    if existing is not None:
                        await self._stop_one_locked(server_id)
                    await self._start_one(server_id, new_cfg)
                else:
                    existing.config = new_cfg
            else:
                if existing is not None:
                    await self._stop_one_locked(server_id)
                    # 保留配置实例占位（disabled 状态）
                    self._servers[server_id] = _ServerEntry(
                        client=StatefulMCPClient(server_id, new_cfg), config=new_cfg
                    )
        self._emit_status(server_id)
        return self.get_status(server_id)

    # ── 内部：单 server 启停 ─────────────────────────────────────────────
    async def _start_one(self, server_id: str, config: MCPServerConfig) -> None:
        client = StatefulMCPClient(
            server_id, config, on_notification=self._on_client_notification
        )
        self._servers[server_id] = _ServerEntry(client=client, config=config)
        try:
            await client.start()
        except Exception as e:  # noqa: BLE001
            logger.exception("[MCP-CTRL] start %s raised", server_id)
            client._error = f"{type(e).__name__}: {e}"  # type: ignore[attr-defined]
            client._state = MCPServerState.DEGRADED  # type: ignore[attr-defined]
        if client.state == MCPServerState.RUNNING and self._tool_bridge_register:
            try:
                self._tool_bridge_register(client.tools)
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-CTRL] tool_bridge register failed for %s", server_id)
        self._emit_status(server_id)

    # ── M3: server-push 通知回调 ────────────────────────────────────────
    def _on_client_notification(self, server_id: str, payload: Any) -> None:
        """stateful_client 在收到 ToolListChanged 并刷完工具后调用 payload='tools_refreshed'。

        策略：unregister 旧的 → register 新的 → emit_status（前端走 sse_bridge 拿到
        mcp.tools.changed 事件 + 完整 list 重拉）。
        """
        if payload != "tools_refreshed":
            return
        entry = self._servers.get(server_id)
        if entry is None:
            return
        if self._tool_bridge_unregister:
            try:
                self._tool_bridge_unregister(server_id)
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-CTRL] tool_bridge unregister failed (refresh) %s", server_id)
        if self._tool_bridge_register:
            try:
                self._tool_bridge_register(entry.client.tools)
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-CTRL] tool_bridge re-register failed %s", server_id)
        self._emit_status(server_id)
        # 显式触发一次 tools.changed SSE 事件（status 事件已经包含 tool_count，但前端
        # 也监听 tools.changed 做更精细的本地缓存失效）
        try:
            from .sse_bridge import broadcast_tools_changed

            broadcast_tools_changed(server_id, len(entry.client.tools))
        except Exception:  # noqa: BLE001
            logger.exception("[MCP-CTRL] broadcast_tools_changed failed %s", server_id)

    async def _stop_one(self, server_id: str) -> None:
        async with self._lock:
            await self._stop_one_locked(server_id)
        self._emit_status(server_id)

    async def _stop_one_locked(self, server_id: str) -> None:
        """已持锁版本。"""
        entry = self._servers.get(server_id)
        if entry is None:
            return
        if self._tool_bridge_unregister:
            try:
                self._tool_bridge_unregister(server_id)
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-CTRL] tool_bridge unregister failed for %s", server_id)
        # M4: 释放任何 pending elicitation Future，避免悬挂
        try:
            from .elicitation import cancel_all_for_server

            cancel_all_for_server(server_id)
        except Exception:  # noqa: BLE001
            logger.exception("[MCP-CTRL] elicitation cancel failed for %s", server_id)
        try:
            await entry.client.stop()
        except Exception:  # noqa: BLE001
            logger.exception("[MCP-CTRL] stop %s raised", server_id)


# ── 全局单例 ──────────────────────────────────────────────────────────────
_singleton_lock = threading.Lock()
_singleton: MCPController | None = None


def get_controller() -> MCPController:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = MCPController()
    return _singleton


def reset_controller_for_tests() -> None:
    """仅供测试用。"""
    global _singleton
    with _singleton_lock:
        _singleton = None


__all__ = ["MCPController", "get_controller", "reset_controller_for_tests"]
