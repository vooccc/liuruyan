"""MCP 配置加载/保存。

文件位置：`paths.mcp_config_path()` → `~/.liuruyan/mcp_servers.json`
**该文件兼容 Claude Desktop / Cursor 同名格式**——用户可直接拷贝过来。

env 占位 `${env:VAR}` 在加载时不解析，保持原样存盘；只在 `resolve_env_vars()`
里运行时按需展开（避免明文 token 落盘）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from backend.paths import mcp_config_path

from .types import MCPRootConfig, MCPServerConfig

logger = logging.getLogger(__name__)

_ENV_PLACEHOLDER_RE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_lock = threading.RLock()


def load_config(path: Path | None = None) -> MCPRootConfig:
    """读配置文件。文件不存在 → 返回空配置（不报错，让首次使用零摩擦）。"""
    fp = path or mcp_config_path()
    if not fp.exists():
        return MCPRootConfig()
    try:
        with fp.open("r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        logger.error("[MCP-CONFIG] failed to parse %s: %s", fp, e)
        return MCPRootConfig()
    if not isinstance(raw, dict):
        logger.warning("[MCP-CONFIG] root must be object, got %s", type(raw).__name__)
        return MCPRootConfig()
    # 兼容两种顶层 key：mcpServers（标准）/ servers（部分变种）
    if "mcpServers" not in raw and "servers" in raw:
        raw = {"mcpServers": raw["servers"]}
    try:
        return MCPRootConfig.model_validate(raw)
    except Exception as e:
        logger.error("[MCP-CONFIG] validation failed: %s", e)
        return MCPRootConfig()


def save_config(cfg: MCPRootConfig, path: Path | None = None) -> None:
    """原子写入配置文件。永远不持久化解析后的 env 值。"""
    fp = path or mcp_config_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = cfg.model_dump(mode="json", exclude_defaults=False)
    tmp = fp.with_suffix(fp.suffix + ".tmp")
    with _lock:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, fp)


def resolve_env_vars(value: str) -> str:
    """把 `${env:VAR}` 展开为运行时环境变量值；缺失时保留原样并打 warn。"""
    if not isinstance(value, str):
        return value

    def _sub(m: re.Match) -> str:
        var = m.group(1)
        v = os.environ.get(var)
        if v is None:
            logger.warning("[MCP-CONFIG] env var %s not set; keeping placeholder", var)
            return m.group(0)
        return v

    return _ENV_PLACEHOLDER_RE.sub(_sub, value)


def resolve_env_dict(env: dict[str, str]) -> dict[str, str]:
    return {k: resolve_env_vars(v) for k, v in env.items()}


def upsert_server(server_id: str, config: MCPServerConfig, path: Path | None = None) -> MCPRootConfig:
    """新增/更新一个 server 配置；返回写入后的完整 root。"""
    with _lock:
        root = load_config(path)
        root.mcpServers[server_id] = config
        save_config(root, path)
        return root


def remove_server(server_id: str, path: Path | None = None) -> MCPRootConfig:
    with _lock:
        root = load_config(path)
        root.mcpServers.pop(server_id, None)
        save_config(root, path)
        return root


def toggle_server(server_id: str, enabled: bool, path: Path | None = None) -> MCPRootConfig | None:
    with _lock:
        root = load_config(path)
        if server_id not in root.mcpServers:
            return None
        root.mcpServers[server_id].enabled = enabled
        save_config(root, path)
        return root


def visible_to_agent(cfg: MCPServerConfig, agent_id: str | None) -> bool:
    """该 server 是否对指定角色可见。'*' 通配；agent_id None → 仅 '*' 可见。"""
    if "*" in cfg.agents:
        return True
    if agent_id is None:
        return False
    return agent_id in cfg.agents


__all__ = [
    "load_config",
    "save_config",
    "resolve_env_vars",
    "resolve_env_dict",
    "upsert_server",
    "remove_server",
    "toggle_server",
    "visible_to_agent",
]
