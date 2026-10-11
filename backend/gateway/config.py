"""Gateway 配置读写。

文件：`~/.liuruyan/gateway_config.json`
默认：disabled。用户必须在 UI 显式打开（或手工编辑此文件）后 Gateway 才真生效。

schema:
    {
        "enabled": bool,           # 总开关，默认 false
        "host": str,               # 监听 host；M1 固定 "127.0.0.1"，M6 可切 "0.0.0.0"
        "cors": bool,              # 是否开 CORS（默认 true，让浏览器扩展类客户端能用）
        "require_token": bool,     # 是否强制 token 校验（默认 true，绝不开匿名后门）
        "idle_timeout_secs": int,  # 流式 chunk 间隔超时秒；默认 180
        "log_request_body": bool,  # 审计里是否记请求体（默认 false，防 prompt 泄漏）
    }

⚠️ `host` / `port` 字段仅作展示与文档用途——Gateway router 是挂在主 FastAPI 进程上的，
   实际监听 host/port 由 `backend/main.py` 启动配置决定，不在这里独立监听。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict

from .. import paths

logger = logging.getLogger(__name__)


@dataclass
class GatewayConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    cors: bool = True
    require_token: bool = True
    idle_timeout_secs: int = 180
    log_request_body: bool = False

    def to_public_dict(self) -> Dict[str, Any]:
        """用于返回给前端 UI 的安全 dict（同 to dict，目前无敏感字段）。"""
        return asdict(self)


_DEFAULT = GatewayConfig()


def load_config() -> GatewayConfig:
    """读 ~/.liuruyan/gateway_config.json。不存在或损坏返回默认 disabled 配置。"""
    p = paths.gateway_config_path()
    if not p.exists():
        return GatewayConfig()
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Gateway] config 损坏，按默认处理：%s", exc)
        return GatewayConfig()
    return GatewayConfig(
        enabled=bool(raw.get("enabled", _DEFAULT.enabled)),
        host=str(raw.get("host", _DEFAULT.host)) or _DEFAULT.host,
        cors=bool(raw.get("cors", _DEFAULT.cors)),
        require_token=bool(raw.get("require_token", _DEFAULT.require_token)),
        idle_timeout_secs=int(raw.get("idle_timeout_secs", _DEFAULT.idle_timeout_secs)),
        log_request_body=bool(raw.get("log_request_body", _DEFAULT.log_request_body)),
    )


def save_config(cfg: GatewayConfig) -> None:
    """写回。会创建文件（不存在时）。"""
    p = paths.gateway_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = ["GatewayConfig", "load_config", "save_config"]
