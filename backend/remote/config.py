"""Remote Access 配置读写。

文件：``~/.liuruyan/remote.json``

schema:
    {
        "version": 1,
        "enabled": bool,        # 总开关，默认 false
        "host": str,            # 监听 host，默认 "127.0.0.1"；用户开启远程访问后切 "0.0.0.0"
        "pwa_enabled": bool,    # 是否注册 service worker（默认 true）；预留给未来回退用
        "updated_at": str,      # ISO-8601 时间戳，审计字段
    }

⚠️ host 字段实际生效路径：``backend/main.py`` 启动段读 ``_remote_host_default()``
   → 该 helper 调本模块 ``load_config()`` → 返回 host 字段或默认 "127.0.0.1"。
   也就是说，**改了 host 必须重启进程才生效**。UI 上有明确提示。

设计文档：``_docs/REMOTE_M8A.md`` §4.3
隔离契约：``.windsurf/rules/remote-isolation.md``
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict

from .. import paths

logger = logging.getLogger(__name__)


CONFIG_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
ALLOWED_HOSTS = {"127.0.0.1", "0.0.0.0"}  # M8a 只暴露这两个；其他特殊监听地址用环境变量手动配


@dataclass
class RemoteConfig:
    version: int = CONFIG_VERSION
    enabled: bool = False
    host: str = DEFAULT_HOST
    pwa_enabled: bool = True
    updated_at: str = ""

    def to_public_dict(self) -> Dict[str, Any]:
        """给 UI 显示用的 dict。当前无敏感字段，等价于 asdict。"""
        return asdict(self)


_DEFAULT = RemoteConfig()


def load_config() -> RemoteConfig:
    """读 ``~/.liuruyan/remote.json``。文件不存在 / 损坏 → 返回默认（disabled, 127.0.0.1）。

    保险默认：任何异常路径都返回"未启用 + loopback"，不会让远程访问"莫名其妙就开了"。
    """
    p = paths.remote_config_path()
    if not p.exists():
        return RemoteConfig()
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Remote] config 损坏，按默认 disabled 处理：%s", exc)
        return RemoteConfig()
    host = str(raw.get("host", _DEFAULT.host)) or _DEFAULT.host
    if host not in ALLOWED_HOSTS:
        # 防呆：用户手编了奇怪 host，回退到 loopback 避免意外暴露
        logger.warning("[Remote] config host=%r 不在白名单，回退为 127.0.0.1", host)
        host = DEFAULT_HOST
    return RemoteConfig(
        version=int(raw.get("version", CONFIG_VERSION)),
        enabled=bool(raw.get("enabled", _DEFAULT.enabled)),
        host=host,
        pwa_enabled=bool(raw.get("pwa_enabled", _DEFAULT.pwa_enabled)),
        updated_at=str(raw.get("updated_at", "") or ""),
    )


def save_config(cfg: RemoteConfig) -> None:
    """原子写回。会创建文件（不存在时）+ 自动盖时间戳。

    写入流程：
        tmp = path + ".tmp"
        tmp.write(json)
        tmp.replace(path)   # POSIX/Windows 都原子
    避免半写崩盘损坏配置。
    """
    if cfg.host not in ALLOWED_HOSTS:
        raise ValueError(f"host={cfg.host!r} 不允许；只接受 {sorted(ALLOWED_HOSTS)}")
    cfg.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    p = paths.remote_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)


__all__ = [
    "CONFIG_VERSION",
    "DEFAULT_HOST",
    "ALLOWED_HOSTS",
    "RemoteConfig",
    "load_config",
    "save_config",
]
