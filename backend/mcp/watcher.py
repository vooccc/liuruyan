"""配置文件热更新 watcher。

触发条件：`~/.liuruyan/mcp_servers.json` 的 mtime 变化。
策略：
  - mtime 轮询（2s）——没有 watchfiles 依赖，跨平台稳
  - diff old/new config → reconcile controller
      - 新增：add_or_update_server（会启动）
      - 修改：add_or_update_server（内部会 stop → update → start）
      - 删除：remove_server

★ 借鉴 ACP watcher.py 的 diff 策略，但换成 mtime 轮询（watchfiles 在 Win 下
偶尔丢事件，mtime 更稳；2s 延迟对人操作完全够）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from backend.paths import mcp_config_path

from .config import load_config
from .controller import get_controller

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 2.0


class MCPConfigWatcher:
    def __init__(self, path: Path | None = None, poll_interval: float = DEFAULT_POLL_INTERVAL):
        self._path = path or mcp_config_path()
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._last_mtime: float | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        # 记录启动时的 mtime，避免启动瞬间立即 reconcile（bootstrap 已加载过）
        self._last_mtime = self._current_mtime()
        self._task = asyncio.create_task(self._loop(), name="mcp-config-watcher")
        logger.info("[MCP-WATCHER] started, polling %s every %.1fs", self._path, self._poll_interval)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("[MCP-WATCHER] stop timeout, cancelling")
                self._task.cancel()
        self._task = None
        logger.info("[MCP-WATCHER] stopped")

    def _current_mtime(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except FileNotFoundError:
            return None

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )
                return  # stop_event set
            except asyncio.TimeoutError:
                pass
            try:
                mtime = self._current_mtime()
                if mtime is not None and mtime != self._last_mtime:
                    self._last_mtime = mtime
                    logger.info("[MCP-WATCHER] config changed, reconciling")
                    await self.reconcile_once()
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-WATCHER] poll error")

    async def reconcile_once(self) -> None:
        """读取当前磁盘配置，diff 当前 controller 状态，应用变更。

        独立函数，方便测试直接调用。
        """
        ctrl = get_controller()
        root = load_config(self._path)
        new_ids = set(root.mcpServers.keys())
        current_ids = set(ctrl.list_servers())

        # 删除：磁盘上没有了
        for sid in current_ids - new_ids:
            logger.info("[MCP-WATCHER] remove %s", sid)
            try:
                await ctrl.remove_server(sid)
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-WATCHER] remove %s failed", sid)

        # 新增 / 修改
        for sid, new_cfg in root.mcpServers.items():
            old_cfg = ctrl.get_config(sid)
            if old_cfg is None:
                logger.info("[MCP-WATCHER] add %s", sid)
            elif old_cfg.model_dump() != new_cfg.model_dump():
                logger.info("[MCP-WATCHER] update %s", sid)
            else:
                continue  # 无变化
            try:
                await ctrl.add_or_update_server(sid, new_cfg)
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-WATCHER] add/update %s failed", sid)


# ── 全局单例（main.py 用）─────────────────────────────────────────────────
_singleton: MCPConfigWatcher | None = None


def get_watcher() -> MCPConfigWatcher:
    global _singleton
    if _singleton is None:
        _singleton = MCPConfigWatcher()
    return _singleton


def reset_watcher_for_tests() -> None:
    global _singleton
    _singleton = None


__all__ = [
    "MCPConfigWatcher",
    "get_watcher",
    "reset_watcher_for_tests",
]
