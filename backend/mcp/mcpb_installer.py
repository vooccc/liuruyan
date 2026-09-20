"""mcpb / dxt 包安装器（M4.3）。

参考 Anthropic Desktop Extensions 规范：
https://github.com/anthropics/dxt

包结构：一个 zip 文件，根目录有 `manifest.json`：
{
    "dxt_version": "0.1",
    "name": "github-server",
    "version": "1.2.0",
    "description": "...",
    "server": {
        "type": "node" | "python" | "binary",
        "entry_point": "server/index.js",
        "mcp_config": {
            "command": "node",
            "args": ["${__dirname}/server/index.js"],
            "env": {"GITHUB_TOKEN": "${user_config.github_token}"}
        }
    },
    "user_config": [...]   // 可选，安装时引导用户填字段（v1 不实现，留 raw 表单）
}

我们做最小可用实现：
1. 用户上传 .mcpb / .dxt（其实就是 zip）
2. 校验：必须是 zip、含 manifest.json、关键字段齐
3. 解压到 `~/.liuruyan/mcp/extensions/<name>/`
4. 把 mcp_config.command/args/env 转成 MCPServerConfig
5. 把 ${__dirname} 占位符替换成真实安装目录
6. 调 controller.add_or_update_server 启起来

★ 安全门：解压前校验 zip 路径不含 `..` / 绝对路径（防 zipslip）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any

from backend.paths import mcp_dir

from .types import MCPServerConfig

logger = logging.getLogger(__name__)


class MCPBInstallError(Exception):
    """mcpb 安装错误。"""


def extensions_dir() -> Path:
    p = mcp_dir() / "extensions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """防 zipslip 的解压。"""
    dest_resolved = dest.resolve()
    for member in zf.namelist():
        if not member or member.endswith("/"):
            continue
        # 拒绝绝对路径 + .. 上跳
        if member.startswith(("/", "\\")) or ".." in Path(member).parts:
            raise MCPBInstallError(f"unsafe path in archive: {member}")
        target = (dest / member).resolve()
        # 解析后必须仍在 dest 下
        try:
            target.relative_to(dest_resolved)
        except ValueError as e:
            raise MCPBInstallError(f"unsafe path resolve: {member}") from e
    zf.extractall(dest)


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if not isinstance(manifest, dict):
        raise MCPBInstallError("manifest must be a JSON object")
    if not manifest.get("name"):
        raise MCPBInstallError("manifest.name missing")
    server = manifest.get("server")
    if not isinstance(server, dict):
        raise MCPBInstallError("manifest.server missing or invalid")
    cfg = server.get("mcp_config")
    if not isinstance(cfg, dict):
        raise MCPBInstallError("manifest.server.mcp_config missing or invalid")
    if not cfg.get("command") and not cfg.get("url"):
        raise MCPBInstallError("manifest.server.mcp_config needs `command` or `url`")


def _resolve_placeholders(value: Any, install_dir: Path) -> Any:
    """递归把 ${__dirname} 替换成真实安装目录。

    其它占位符（${user_config.xxx} / ${env:XXX}）暂不在这里处理：
    - ${env:XXX} 由 config.resolve_env_vars 在运行时处理
    - ${user_config.xxx} v1 留空，由用户在 UI 表单里直接填
    """
    if isinstance(value, str):
        return value.replace("${__dirname}", str(install_dir))
    if isinstance(value, list):
        return [_resolve_placeholders(v, install_dir) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_placeholders(v, install_dir) for k, v in value.items()}
    return value


def install_from_bytes(blob: bytes, *, server_id_hint: str | None = None) -> tuple[str, MCPServerConfig]:
    """把上传的 zip bytes 安装成一个 MCP server config。

    返回 `(server_id, config)`。调用方负责 `controller.add_or_update_server(...)`。
    server_id 优先用 hint，否则用 manifest.name 做 slugify。
    """
    import io

    if not zipfile.is_zipfile(io.BytesIO(blob)):
        raise MCPBInstallError("uploaded file is not a valid zip / mcpb / dxt")

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        try:
            with zf.open("manifest.json") as f:
                manifest = json.loads(f.read().decode("utf-8-sig"))
        except KeyError as e:
            raise MCPBInstallError("manifest.json missing in archive") from e
        except json.JSONDecodeError as e:
            raise MCPBInstallError(f"manifest.json invalid: {e}") from e

        _validate_manifest(manifest)

        name = (server_id_hint or manifest["name"]).strip()
        # slugify：只留字母数字下划线短横
        name = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name)
        if not name:
            raise MCPBInstallError("server_id ends up empty after slugify")

        target = extensions_dir() / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        try:
            _safe_extract(zf, target)
        except MCPBInstallError:
            shutil.rmtree(target, ignore_errors=True)
            raise

    server_block = manifest["server"]
    raw_cfg = _resolve_placeholders(server_block["mcp_config"], target)
    description = manifest.get("description") or ""
    version = manifest.get("version") or ""

    cfg = MCPServerConfig(
        command=raw_cfg.get("command"),
        args=list(raw_cfg.get("args") or []),
        env=dict(raw_cfg.get("env") or {}),
        url=raw_cfg.get("url"),
        headers=dict(raw_cfg.get("headers") or {}),
        cwd=raw_cfg.get("cwd"),
        description=(description + (f" (v{version})" if version else "")).strip(),
        enabled=True,
    )
    logger.info("[MCPB] installed %s → %s (transport inferred=%s)", name, target, cfg.resolve_transport())
    return name, cfg


def uninstall(server_id: str) -> bool:
    """删 server 同时清掉其 extensions 目录。"""
    p = extensions_dir() / server_id
    if not p.exists():
        return False
    shutil.rmtree(p, ignore_errors=True)
    logger.info("[MCPB] removed extension dir %s", p)
    return True


__all__ = [
    "MCPBInstallError",
    "extensions_dir",
    "install_from_bytes",
    "uninstall",
]
