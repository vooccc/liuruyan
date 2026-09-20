"""v2 · E-1a · ChannelStore — channel 实例配置原子存储

设计：_docs/architecture_v2/02_runtime_core.md § 6.1（``channels/store.json``）+
01_master_plan.md Phase E §1（store.py + manager.py）

存储位置：``{base}/channels/store.json``，base 走 ``LIURUYAN_DATA_DIR`` env
（默认 cwd）。原子写：tempfile + rename（atomic_write_json）。

每个 ChannelConfig 描述一个"channel 实例"——同一平台可能配多账号（如钉钉
企业 A 和企业 B 各装一份柳如烟 bot），所以一个 channel name 可能有多个
ChannelConfig（不同 ``id``）。

数据形态：

```json
{
    "_schema_version": 1,
    "channels": [
        {
            "id": "dingtalk-corp-a",
            "channel": "dingtalk",
            "config": {"client_id": "...", "client_secret": "...", "default_agent": "liuruyan"},
            "enabled": true,
            "created_at": 1714521600.0
        }
    ]
}
```

API 层：CRUD + 查询。不做生命周期（启停 adapter 是 ChannelManager 的事）。

异常隔离：读不到文件 / 损坏 → 返空 channel 列表（启动时不阻断）；写失败抛
（数据完整性优先于"配置写不进去也能跑"）。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..persistence.atomic import atomic_write_json
from ..sandbox.virtual_path import get_default_base

log = logging.getLogger(__name__)


CHANNEL_STORE_SCHEMA_VERSION = 1


@dataclass(slots=True)
class ChannelConfig:
    """一个 channel 实例的完整配置。

    ``id`` 唯一标识；同一 ``channel`` 平台可有多个 ``id``（多账号）。
    """

    id: str                                 # 唯一 ID（如 "dingtalk-corp-a"）
    channel: str                            # 平台名（"dingtalk" / "feishu" / ...）
    config: dict[str, Any]                  # 平台特定配置（client_id/secret/default_agent/...）
    enabled: bool = True                    # 启动时是否自动 start
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelConfig":
        # 兼容缺字段 / 多字段
        return cls(
            id=str(data.get("id") or ""),
            channel=str(data.get("channel") or ""),
            config=dict(data.get("config") or {}),
            enabled=bool(data.get("enabled", True)),
            created_at=float(data.get("created_at") or time.time()),
        )


def channels_dir() -> Path:
    """``{base}/channels``"""
    return get_default_base() / "channels"


def store_path() -> Path:
    """``{base}/channels/store.json``"""
    return channels_dir() / "store.json"


class ChannelStore:
    """channel 实例配置 CRUD（in-memory + 原子持久化）。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or store_path()
        self._configs: dict[str, ChannelConfig] = {}  # id → ChannelConfig
        self._loaded = False

    # ── 读 ──────────────────────────────────────────────────────────────

    def load(self) -> None:
        """从磁盘读；文件不存在 / 损坏 → 空 dict（不抛）。"""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._configs = {}
            self._loaded = True
            return
        except Exception:
            log.exception("ChannelStore.load failed at %s; using empty config", self._path)
            self._configs = {}
            self._loaded = True
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.exception("ChannelStore.load JSON parse failed at %s; using empty config", self._path)
            self._configs = {}
            self._loaded = True
            return

        configs = data.get("channels") or []
        if not isinstance(configs, list):
            log.warning(
                "ChannelStore: invalid 'channels' field type=%s, using empty",
                type(configs).__name__,
            )
            self._configs = {}
        else:
            self._configs = {}
            for item in configs:
                if not isinstance(item, dict):
                    continue
                cfg = ChannelConfig.from_dict(item)
                if not cfg.id or not cfg.channel:
                    log.warning(
                        "ChannelStore: skipping config with missing id/channel: %r",
                        item,
                    )
                    continue
                if cfg.id in self._configs:
                    log.warning(
                        "ChannelStore: duplicate id %r, last one wins", cfg.id,
                    )
                self._configs[cfg.id] = cfg

        self._loaded = True

    def list_configs(self) -> list[ChannelConfig]:
        if not self._loaded:
            self.load()
        return list(self._configs.values())

    def get(self, config_id: str) -> ChannelConfig | None:
        if not self._loaded:
            self.load()
        return self._configs.get(config_id)

    def list_enabled(self) -> list[ChannelConfig]:
        return [c for c in self.list_configs() if c.enabled]

    def list_by_channel(self, channel: str) -> list[ChannelConfig]:
        return [c for c in self.list_configs() if c.channel == channel]

    # ── 写 ──────────────────────────────────────────────────────────────

    def upsert(self, config: ChannelConfig) -> None:
        """新增或覆盖（按 id）。立即持久化。"""
        if not config.id or not config.channel:
            raise ValueError(
                f"ChannelConfig must have non-empty id/channel: {config!r}"
            )
        if not self._loaded:
            self.load()
        self._configs[config.id] = config
        self._persist()

    def remove(self, config_id: str) -> bool:
        """按 id 删；返 True 如果命中并删除。"""
        if not self._loaded:
            self.load()
        if config_id not in self._configs:
            return False
        del self._configs[config_id]
        self._persist()
        return True

    def set_enabled(self, config_id: str, enabled: bool) -> bool:
        """开关单个 channel；返 True 如果命中并更新。"""
        if not self._loaded:
            self.load()
        cfg = self._configs.get(config_id)
        if cfg is None:
            return False
        if cfg.enabled == enabled:
            return True  # no-op，不重写盘
        # dataclass slots=True 直接赋字段
        cfg.enabled = enabled
        self._persist()
        return True

    # ── 内部 ────────────────────────────────────────────────────────────

    def _persist(self) -> None:
        payload = {
            "_schema_version": CHANNEL_STORE_SCHEMA_VERSION,
            "channels": [c.to_dict() for c in self._configs.values()],
        }
        atomic_write_json(self._path, payload)


__all__ = [
    "ChannelConfig",
    "ChannelStore",
    "CHANNEL_STORE_SCHEMA_VERSION",
    "channels_dir",
    "store_path",
]
