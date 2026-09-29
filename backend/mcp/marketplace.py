"""Marketplace 远程目录（M4.4）。

用户在配置里填一组 URL，每个 URL 返回 JSON 形式的 catalog 条目列表（schema 等同
`MCPCatalogEntry`）。我们：
  1. fetch + 1 小时缓存
  2. 容错：URL 拉失败不影响其它 URL 和 builtin
  3. 合并：builtin → 各 marketplace（按 name 去重，先到先得）

订阅配置文件：`~/.liuruyan/mcp/marketplaces.json`

```json
{ "urls": ["https://raw.githubusercontent.com/.../catalog.json", ...] }
```

为什么不直接订 raycast 自家格式？raycast catalog schema 跟我们的 `MCPCatalogEntry`
不完全一样。我们 v1 只接受我们自己的 schema，外部要订需要做 schema-shim。把
shim 放进各家 marketplace 自己的 repo 而不是塞进柳如烟，避免 catalog 格式扩散。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from backend.paths import mcp_dir

from .registries import BUILTIN_CATALOG
from .types import CatalogEntry as MCPCatalogEntry

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 3600.0
DEFAULT_TIMEOUT = 8.0


def _marketplace_path() -> Path:
    return mcp_dir() / "marketplaces.json"


def load_subscriptions() -> list[str]:
    p = _marketplace_path()
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        urls = data.get("urls") or []
        return [str(u) for u in urls if isinstance(u, str) and u.strip()]
    except Exception:  # noqa: BLE001
        logger.exception("[MCPMP] failed to load marketplaces.json")
        return []


def save_subscriptions(urls: list[str]) -> None:
    p = _marketplace_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    cleaned = [u.strip() for u in urls if u and u.strip()]
    # 去重保序
    seen: set[str] = set()
    deduped = []
    for u in cleaned:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"urls": deduped}, f, ensure_ascii=False, indent=2)
    import os as _os

    _os.replace(tmp, p)


# ── 缓存 ──────────────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, list[MCPCatalogEntry]]] = {}
_cache_lock = threading.Lock()


async def _fetch_one(url: str) -> list[MCPCatalogEntry]:
    """拉一个 URL，返回解析后的 catalog 条目；失败返空列表（不抛）。"""
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as cli:
            r = await cli.get(url, follow_redirects=True)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("[MCPMP] fetch %s failed: %s", url, e)
        return []

    # 兼容两种返回：纯数组 / {"entries": [...]}
    raw_entries: list[Any]
    if isinstance(data, list):
        raw_entries = data
    elif isinstance(data, dict) and isinstance(data.get("entries"), list):
        raw_entries = data["entries"]
    else:
        logger.warning("[MCPMP] %s returned unexpected shape", url)
        return []

    out: list[MCPCatalogEntry] = []
    for raw in raw_entries:
        try:
            out.append(MCPCatalogEntry.model_validate(raw))
        except Exception as e:  # noqa: BLE001
            logger.debug("[MCPMP] %s skipped invalid entry: %s", url, e)
    return out


async def fetch_all(force: bool = False) -> list[MCPCatalogEntry]:
    """拉所有订阅的 catalog（带 1 小时缓存）。**非阻塞**：单 URL 失败仅丢自己。"""
    urls = load_subscriptions()
    if not urls:
        return []
    now = time.time()
    coros: list[asyncio.Future | asyncio.Task] = []
    use_cache: list[list[MCPCatalogEntry]] = []
    fetch_targets: list[str] = []

    with _cache_lock:
        for u in urls:
            entry = _cache.get(u)
            if not force and entry and (now - entry[0] < DEFAULT_TTL_SECONDS):
                use_cache.append(entry[1])
            else:
                fetch_targets.append(u)

    fetched: list[list[MCPCatalogEntry]] = []
    if fetch_targets:
        # return_exceptions=True：单个 URL 抛异常不带崩 gather；后面统一替成空列表
        raw_results = await asyncio.gather(
            *(_fetch_one(u) for u in fetch_targets), return_exceptions=True
        )
        with _cache_lock:
            for u, items in zip(fetch_targets, raw_results):
                if isinstance(items, BaseException):
                    logger.warning("[MCPMP] fetch %s raised: %s", u, items)
                    items = []
                _cache[u] = (now, items)
                fetched.append(items)

    flat: list[MCPCatalogEntry] = []
    for chunk in use_cache + fetched:
        flat.extend(chunk)
    return flat


async def get_combined_catalog(force: bool = False) -> list[MCPCatalogEntry]:
    """builtin + marketplace 合并去重（按 name）。builtin 优先（防外部覆盖官方条目）。"""
    seen: set[str] = set()
    out: list[MCPCatalogEntry] = []
    for entry in BUILTIN_CATALOG:
        if entry.name in seen:
            continue
        seen.add(entry.name)
        out.append(entry)
    remote = await fetch_all(force=force)
    for entry in remote:
        if entry.name in seen:
            continue
        seen.add(entry.name)
        out.append(entry)
    return out


__all__ = [
    "load_subscriptions",
    "save_subscriptions",
    "fetch_all",
    "get_combined_catalog",
]
