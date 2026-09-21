"""搜索引擎配置（设置 → 辅助功能 → 搜索引擎）。

支持三大 AI 搜索引擎（Devin / Exa / Tavily）的 API Key 配置、启用开关与自由优先级排序。
持久化到 ``search_config.json``，并在「保存时」「启动时」灌进 ``os.environ``：
一处配置，柳如烟全局 web_search 与内容模块两处都能用，无需重启即时生效。

优先级机制：
- 用户界面配置的优先顺序排在前列（例如 [devin, exa, tavily]）；
- 免费安全气囊（SearXNG / 百度 / Bing / Firecrawl）自动挂在队列末尾兜底，确保永远出结果。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from . import paths

logger = logging.getLogger(__name__)

ENV_KEYS = {
    "tavily": "TAVILY_API_KEY",
    "exa": "EXA_API_KEY",
    "devin": "DEVIN_API_KEY",
}

SUPPORTED_AI_ENGINES = ["devin", "exa", "tavily"]
DEFAULT_PRIORITY_ORDER = ["devin", "exa", "tavily"]

# 默认免 Key 兜底底座
FALLBACK_ENGINES_CJK = ["searxng", "baidu", "bing", "firecrawl"]
FALLBACK_ENGINES_EN = ["searxng", "bing", "firecrawl"]


def extract_local_devin_token() -> str:
    """尝试从本机 Devin/Windsurf 的 globalStorage/state.vscdb 中直接提取已登录的 sessionToken"""
    import sqlite3
    db_candidates = [
        Path(os.environ.get("APPDATA", "")) / "Devin" / "User" / "globalStorage" / "state.vscdb",
        Path(os.environ.get("APPDATA", "")) / "Windsurf" / "User" / "globalStorage" / "state.vscdb",
    ]
    for db_path in db_candidates:
        if not db_path.is_file():
            continue
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("SELECT value FROM ItemTable WHERE key = 'windsurfAuthStatus'")
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                data = json.loads(row[0])
                token = data.get("sessionToken") or data.get("apiKey")
                if token and str(token).startswith("devin-session-token$"):
                    return str(token).strip()
        except Exception as e:
            logger.debug("[SearchConfig] extract token from %s failed: %s", db_path, e)
    return ""


def detect_local_devin() -> Dict[str, Any]:
    """探测本机安装的 Devin 客户端及其版本（轻量检测，不访问本地 SQLite 数据库）。"""
    candidates = [
        Path(r"D:\Programs\Devin\Devin.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Devin" / "Devin.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Devin" / "Devin.exe",
    ]
    for exe in candidates:
        if exe.is_file():
            product_json = exe.parent / "resources" / "app" / "product.json"
            ide_version = "1.126.0"
            ext_version = "3.10.27"
            if product_json.is_file():
                try:
                    data = json.loads(product_json.read_text(encoding="utf-8"))
                    ide_version = data.get("version", ide_version)
                    ext_version = data.get("windsurfVersion", ext_version)
                except Exception as e:
                    logger.warning("[SearchConfig] Read Devin product.json failed: %s", e)
            return {
                "installed": True,
                "path": str(exe),
                "ideName": "devin",
                "ideVersion": ide_version,
                "extensionName": "devin",
                "extensionVersion": ext_version,
            }

    # 兜底默认值（与最新已对齐版本一致）
    return {
        "installed": False,
        "path": "",
        "ideName": "devin",
        "ideVersion": "1.126.0",
        "extensionName": "devin",
        "extensionVersion": "3.10.27",
    }


class SearchConfig(BaseModel):
    """搜索引擎配置（用户可改）。"""

    tavily_api_key: str = ""
    """Tavily 搜索 API key（形如 tvly-xxxx）。留空则回退到环境变量 / .env。"""

    exa_api_key: str = ""
    """Exa 搜索 API key。留空则回退到环境变量 / .env。"""

    devin_api_key: str = ""
    """Devin 个人 sessionToken（形如 devin-session-token$xxxx）。"""

    enabled_engines: Dict[str, bool] = Field(
        default_factory=lambda: {"devin": True, "exa": True, "tavily": True}
    )
    """各 AI 引擎的启用状态开关。"""

    priority_order: List[str] = Field(
        default_factory=lambda: list(DEFAULT_PRIORITY_ORDER)
    )
    """AI 引擎调用优先级顺序（前面的先尝试，失败/无Key自动降级到下一个）。"""


def load_search_config() -> SearchConfig:
    """从 search_config.json 读配置。缺失 / 损坏返默认（不阻断）。"""
    p = paths.search_config_path()
    if not p.exists():
        return SearchConfig()
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return SearchConfig()
    try:
        cfg = SearchConfig.model_validate(raw)
        # 补齐未包含的引擎
        for eng in SUPPORTED_AI_ENGINES:
            if eng not in cfg.enabled_engines:
                cfg.enabled_engines[eng] = True
            if eng not in cfg.priority_order:
                cfg.priority_order.append(eng)
        return cfg
    except Exception:
        return SearchConfig()


def save_search_config(cfg: SearchConfig) -> None:
    """写回 search_config.json，并立即把 key 灌进 os.environ（无需重启即生效）。"""
    p = paths.search_config_path()
    p.write_text(
        json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    apply_search_config_to_env(cfg)


def apply_search_config_to_env(cfg: SearchConfig | None = None) -> None:
    """把 UI 配的 key 灌进 os.environ，让 web_tools 的 os.getenv 读得到。

    仅当 UI key 非空时覆盖；为空则不动环境变量（保留 .env / 外部 export 的值）。
    在 FastAPI 启动钩子里调用一次，并在每次保存后调用。
    """
    cfg = cfg or load_search_config()
    pairs = [
        ("tavily", cfg.tavily_api_key),
        ("exa", cfg.exa_api_key),
        ("devin", cfg.devin_api_key),
    ]
    for eng, key_val in pairs:
        key = (key_val or "").strip()
        env_var = ENV_KEYS.get(eng)
        if env_var:
            if key:
                os.environ[env_var] = key
            elif env_var in os.environ and not key:
                # 留空且没有其它来源时不覆盖
                pass


def get_effective_priority(is_cjk: bool = False) -> List[str]:
    """计算当前生效的后端调用列表。

    1. 用户在 UI 中排在前列的已启用 AI 引擎（devin / exa / tavily）
    2. 系统免 Key 兜底底座（searxng / 百度 / bing / firecrawl）
    """
    cfg = load_search_config()
    effective_ai = [
        eng for eng in cfg.priority_order
        if eng in SUPPORTED_AI_ENGINES and cfg.enabled_engines.get(eng, True)
    ]
    fallback = FALLBACK_ENGINES_CJK if is_cjk else FALLBACK_ENGINES_EN

    res = list(effective_ai)
    for fb in fallback:
        if fb not in res:
            res.append(fb)
    return res
