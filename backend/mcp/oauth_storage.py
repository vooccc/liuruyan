"""OAuth token 文件存储（实现 mcp.client.auth.oauth2.TokenStorage Protocol）。

持久化路径：`~/.liuruyan/mcp/oauth/<server_id>.json`
权限：chmod 600（POSIX）；Windows 依赖 NTFS ACL 继承（用户目录默认即用户私有）。

文件结构：
{
  "tokens": {... OAuthToken model_dump ...} | null,
  "client_info": {... OAuthClientInformationFull model_dump ...} | null
}
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from pathlib import Path

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from backend.paths import mcp_dir

logger = logging.getLogger(__name__)


def _oauth_file(server_id: str) -> Path:
    base = mcp_dir() / "oauth"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{server_id}.json"


class FileTokenStorage:
    """Per-server 文件 token 存储。线程安全。"""

    def __init__(self, server_id: str):
        self.server_id = server_id
        self._path = _oauth_file(server_id)
        self._lock = threading.RLock()

    def _read_raw(self) -> dict:
        if not self._path.exists():
            return {"tokens": None, "client_info": None}
        try:
            with self._path.open("r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.warning("[MCP-OAUTH] read %s failed: %s", self._path, e)
            return {"tokens": None, "client_info": None}
        if not isinstance(data, dict):
            return {"tokens": None, "client_info": None}
        data.setdefault("tokens", None)
        data.setdefault("client_info", None)
        return data

    def _write_raw(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)
        # POSIX chmod 600；Windows 上 os.chmod 仅影响 read-only bit，依赖目录 ACL。
        try:
            os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:  # noqa: BLE001
            pass

    # ── TokenStorage Protocol 实现 ───────────────────────────────────────
    async def get_tokens(self) -> OAuthToken | None:
        with self._lock:
            raw = self._read_raw().get("tokens")
        if raw is None:
            return None
        try:
            return OAuthToken.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning("[MCP-OAUTH] invalid tokens for %s: %s", self.server_id, e)
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        with self._lock:
            raw = self._read_raw()
            raw["tokens"] = tokens.model_dump(mode="json")
            self._write_raw(raw)
        logger.info("[MCP-OAUTH] stored tokens for %s", self.server_id)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        with self._lock:
            raw = self._read_raw().get("client_info")
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            logger.warning("[MCP-OAUTH] invalid client_info for %s: %s", self.server_id, e)
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        with self._lock:
            raw = self._read_raw()
            raw["client_info"] = client_info.model_dump(mode="json")
            self._write_raw(raw)
        logger.info("[MCP-OAUTH] stored client_info for %s", self.server_id)

    # ── 辅助：清除（用于 re-auth / 删除 server）──────────────────────────
    def clear(self) -> None:
        with self._lock:
            if self._path.exists():
                try:
                    self._path.unlink()
                except Exception:  # noqa: BLE001
                    logger.exception("[MCP-OAUTH] clear failed")


__all__ = ["FileTokenStorage"]
