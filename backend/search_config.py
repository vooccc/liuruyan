"""搜索引擎配置（设置 → 辅助功能 → 搜索引擎）。

支持三大 AI 搜索引擎（Devin / Exa / Tavily）的 API Key 配置、启用开关与自由优先级排序。
持久化到 ``search_config.json``，并在「保存时」「启动时」灌进 ``os.environ``：
一处配置，柳如烟全局 web_search 与内容模块两处都能用，无需重启即时生效。

优先级机制：
- 用户界面配置的优先顺序排在前列（例如 [devin, exa, tavily]）；
- 免费安全气囊（SearXNG / 百度 / Bing / Firecrawl）自动挂在队列末尾兜底，确保永远出结果。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
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
FALLBACK_ENGINES_CJK = ["searxng", "baidu", "bing", "ddg", "firecrawl"]
FALLBACK_ENGINES_EN = ["searxng", "bing", "ddg", "firecrawl"]

_TOKEN_PREFIXES = ("devin-session-token$", "sk-ws-")

# Devin 3.14+ 独立认证扩展 (codeium.windsurf-auth) 为当前主存储；旧版 codeium.windsurf 仅作兜底
_SECRET_AUTH_KEYS_PRIMARY = (
    'secret://{"extensionId":"codeium.windsurf-auth","key":"windsurf_auth.sessions"}',
    'secret://{"extensionId":"codeium.windsurf-auth","key":"devin_auth1_token"}',
    'secret://{"extensionId":"codeium.devin-auth","key":"windsurf_auth.sessions"}',
    'secret://{"extensionId":"codeium.devin-auth","key":"devin_auth1_token"}',
)
_PLAINTEXT_AUTH_KEYS = ("windsurfAuthStatus", "devinAuthStatus", "authStatus")
_SECRET_AUTH_KEYS_LEGACY = (
    'secret://{"extensionId":"codeium.windsurf","key":"devin_auth1_token"}',
    'secret://{"extensionId":"codeium.windsurf","key":"windsurf_auth.sessions"}',
)


def _devin_vscdb_candidates() -> List[Path]:
    """返回本机 Devin 客户端 state.vscdb 候选路径列表（去重保序）。"""
    raw_candidates: List[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "").strip()
        roaming_base = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
        for app_dir in ("Devin", "devin"):
            raw_candidates.append(roaming_base / app_dir / "User" / "globalStorage" / "state.vscdb")
    elif sys.platform == "darwin":
        app_support = Path.home() / "Library" / "Application Support"
        for app_dir in ("Devin", "devin"):
            raw_candidates.append(app_support / app_dir / "User" / "globalStorage" / "state.vscdb")
    else:
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        cfg_base = Path(xdg_cfg) if xdg_cfg else (Path.home() / ".config")
        for app_dir in ("Devin", "devin"):
            raw_candidates.append(cfg_base / app_dir / "User" / "globalStorage" / "state.vscdb")

    deduped: List[Path] = []
    seen: set[str] = set()
    for p in raw_candidates:
        try:
            norm = os.path.normcase(str(p.resolve()))
        except Exception:
            norm = os.path.normcase(str(p))
        if norm not in seen:
            seen.add(norm)
            deduped.append(p)
    return deduped


def _dpapi_decrypt(encrypted: bytes) -> bytes:
    """使用 Windows DPAPI 解密 Electron os_crypt 主密钥。"""
    if sys.platform != "win32":
        raise RuntimeError("DPAPI is only supported on Windows")
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    buf = ctypes.create_string_buffer(encrypted, len(encrypted))
    blob_in = DATA_BLOB(len(encrypted), buf)
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _load_electron_os_crypt_key(local_state_path: Path) -> Optional[bytes]:
    """读取并解密 Electron Local State 中的 os_crypt.encrypted_key。"""
    if sys.platform != "win32" or not local_state_path.is_file():
        return None
    try:
        data = json.loads(local_state_path.read_text(encoding="utf-8-sig"))
        enc_b64 = (data.get("os_crypt") or {}).get("encrypted_key")
        if not enc_b64:
            return None
        raw = base64.b64decode(enc_b64)
        if not raw.startswith(b"DPAPI"):
            return None
        return _dpapi_decrypt(raw[5:])
    except Exception as e:
        logger.debug("[SearchConfig] load os_crypt key from %s failed: %s", local_state_path, e)
        return None


def _decrypt_electron_secret_value(raw_value: str, master_key: Optional[bytes]) -> Optional[str]:
    """解密 Electron SecretStorage 条目（Buffer + v10/v11 AES-256-GCM）。"""
    if not raw_value:
        return None
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return raw_value

    if isinstance(parsed, dict) and parsed.get("type") == "Buffer" and isinstance(parsed.get("data"), list):
        enc = bytes(parsed["data"])
        if enc[:3] in (b"v10", b"v11"):
            if not master_key or len(enc) <= 15:
                return None
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM

                nonce = enc[3:15]
                ct = enc[15:]
                return AESGCM(master_key).decrypt(nonce, ct, None).decode("utf-8")
            except Exception as e:
                logger.debug("[SearchConfig] decrypt secret buffer failed: %s", e)
                return None
        return enc.decode("utf-8", errors="ignore")

    return raw_value


def _is_valid_devin_token(token: Any) -> bool:
    return isinstance(token, str) and token.strip().startswith(_TOKEN_PREFIXES)


def _extract_token_from_payload(payload: Any) -> str:
    """从会话 JSON（数组/对象）或裸字符串中提取有效的 Devin sessionToken。"""
    if payload is None:
        return ""
    if isinstance(payload, str):
        text = payload.strip()
        if _is_valid_devin_token(text):
            return text
        try:
            decoded = json.loads(text)
        except Exception:
            return ""
        return _extract_token_from_payload(decoded)

    if isinstance(payload, list):
        for item in payload:
            tok = _extract_token_from_payload(item)
            if tok:
                return tok
        return ""

    if isinstance(payload, dict):
        for field in ("accessToken", "sessionToken", "apiKey", "token"):
            val = payload.get(field)
            if _is_valid_devin_token(val):
                return val.strip()
    return ""


def _query_item_table(cur: Any, key: str) -> Optional[str]:
    cur.execute("SELECT value FROM ItemTable WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def extract_local_devin_token() -> str:
    """尝试从本机 Devin 客户端 globalStorage/state.vscdb 中读取已登录会话凭据。"""
    import sqlite3

    for db_path in _devin_vscdb_candidates():
        if not db_path.is_file():
            continue
        conn: Optional[sqlite3.Connection] = None
        try:
            try:
                uri = f"file:///{db_path.resolve().as_posix()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=3.0)
            except sqlite3.Error:
                conn = sqlite3.connect(str(db_path), timeout=3.0)
            cur = conn.cursor()

            local_state_path = db_path.parent.parent.parent / "Local State"
            master_key = _load_electron_os_crypt_key(local_state_path)

            auth_ext_explicitly_empty = False
            for secret_key in _SECRET_AUTH_KEYS_PRIMARY:
                raw_val = _query_item_table(cur, secret_key)
                if not raw_val:
                    continue
                plain = _decrypt_electron_secret_value(raw_val, master_key)
                if plain is None:
                    continue
                token = _extract_token_from_payload(plain)
                if token:
                    return token
                # 若 codeium.windsurf-auth 的 sessions 已明确清空为 []，说明当前处于登出态
                if "windsurf_auth.sessions" in secret_key:
                    try:
                        if json.loads(plain) == []:
                            auth_ext_explicitly_empty = True
                    except Exception:
                        pass

            for plain_key in _PLAINTEXT_AUTH_KEYS:
                raw_val = _query_item_table(cur, plain_key)
                if not raw_val:
                    continue
                token = _extract_token_from_payload(raw_val)
                if token:
                    return token

            if not auth_ext_explicitly_empty:
                for secret_key in _SECRET_AUTH_KEYS_LEGACY:
                    raw_val = _query_item_table(cur, secret_key)
                    if not raw_val:
                        continue
                    plain = _decrypt_electron_secret_value(raw_val, master_key)
                    if plain is None:
                        continue
                    token = _extract_token_from_payload(plain)
                    if token:
                        return token
        except Exception as e:
            logger.debug("[SearchConfig] extract token from %s failed: %s", db_path, e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return ""


def detect_local_devin() -> Dict[str, Any]:
    """探测本机安装的 Devin 客户端及其版本（轻量检测，不访问本地 SQLite 数据库）。"""
    candidates: List[Path] = [Path(r"D:\Programs\Devin\Devin.exe")]
    for env_name, subpath in (
        ("LOCALAPPDATA", Path("Programs") / "Devin" / "Devin.exe"),
        ("ProgramFiles", Path("Devin") / "Devin.exe"),
        ("ProgramFiles(x86)", Path("Devin") / "Devin.exe"),
    ):
        base = os.environ.get(env_name, "").strip()
        if base:
            candidates.append(Path(base) / subpath)

    for exe in candidates:
        if exe.is_file():
            product_json = exe.parent / "resources" / "app" / "product.json"
            ide_version = "1.126.0"
            ext_version = "3.10.27"
            if product_json.is_file():
                try:
                    data = json.loads(product_json.read_text(encoding="utf-8"))
                    ide_version = data.get("version") or ide_version
                    ext_version = (
                        data.get("extensionVersion")
                        or data.get("windsurfVersion")
                        or data.get("codeiumVersion")
                        or ext_version
                    )
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
