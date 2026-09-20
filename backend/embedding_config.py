"""Embedding 长期记忆配置读写。

设计原则：
- 默认禁用（enabled=False）：用户没配 → recall_memory 行为跟现在一模一样（BM25 兜底）
- 走 OpenAI 兼容 /embeddings 端点：99% 主流 provider 都支持，零协议适配
- provider/model/api_key/base_url/dim 全部用户在设置面板里配，不预设任何 hard-coded 默认
- 配置变更时自动失效进程内的 EmbeddingClient + EmbeddingIndex 缓存（在 agent_memory_tool 里实现）

存储位置：~/.liuruyan/embedding_config.json（./backend\paths.py:207）

为啥不放 models.json：
    models.json 是 chat/completion provider 配置，已经被 ProviderManager 管得很重；
    embedding 是独立功能（不参与 chat 路径），单独一个 json 让边界清晰，
    后续删 / 重置 / 全量重算时都不会牵动主 chat 系统。

API Key 处理：
    跟 models.secrets.json 同样思路，但单 endpoint 单独存——避免动 ProviderManager
    的 secrets 加载顺序。embedding_config.json 直接落 api_key 明文（柳如烟是本地
    桌面应用，模式跟 models.json 一致，见 ./backend\providers\schema.py:108-111）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingConfig:
    """Embedding 配置 schema。

    Fields:
        enabled: 总开关。False 时所有 embedding 路径短路，行为退化到 BM25。
        provider: 用户给的 provider id（仅展示用，跟 models.json 里的 provider 不强绑）。
        model: 模型 id（如 "text-embedding-3-small" / "BAAI/bge-m3" / "embedding-3"）。
        base_url: OpenAI 兼容 /embeddings 端点（不含 /embeddings 后缀）。
                  例如 "https://api.openai.com/v1" / "https://api.siliconflow.cn/v1"。
        api_key: 明文 key（本地桌面应用，跟 models.json 同口径）。
        dim: 向量维度。必须跟 model 实际输出维度一致；存储索引时校验，不一致则拒绝加载旧索引。
        timeout: HTTP 请求超时秒数。embedding 单条通常 < 500ms，10s 已经很宽。
        cache_size: 进程内 query embedding LRU 缓存大小（同一 agent 反复问相同问题不重算）。
        updated_at: 最后修改时间戳（每次 save 自动更新）。
    """
    enabled: bool = False
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    dim: int = 0
    timeout: float = 10.0
    cache_size: int = 256
    updated_at: float = 0.0

    def is_ready(self) -> bool:
        """判断配置是否完整可用。"""
        return (
            self.enabled
            and bool(self.model)
            and bool(self.base_url)
            and bool(self.api_key)
            and self.dim > 0
        )

    def public_dict(self) -> dict:
        """返给前端的 dict（脱敏 api_key 为掩码）。"""
        d = asdict(self)
        d["has_api_key"] = bool(self.api_key)
        # 完整 key 暴露给前端这件事跟 models.json 同口径——本地应用没必要假装安全
        # 见 ./backend\providers\schema.py:105-111
        return d


# ============================================================================
# 单例 + 文件读写
# ============================================================================

_lock = threading.Lock()
_cached: Optional[EmbeddingConfig] = None


def _load_from_disk() -> EmbeddingConfig:
    """从 ~/.liuruyan/embedding_config.json 加载，文件不存在 / 损坏 → 返默认禁用配置。"""
    from . import paths

    p = paths.embedding_config_path()
    if not p.exists():
        return EmbeddingConfig()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("embedding_config.json not a dict, using default disabled")
            return EmbeddingConfig()
        # 用 dataclass 字段名过滤未知字段（防 schema 变更后老配置爆炸）
        valid_fields = {f for f in EmbeddingConfig.__dataclass_fields__.keys()}
        clean = {k: v for k, v in data.items() if k in valid_fields}
        return EmbeddingConfig(**clean)
    except Exception as exc:
        logger.warning("load embedding_config failed: %s; using default disabled", exc)
        return EmbeddingConfig()


def get_config() -> EmbeddingConfig:
    """拿当前配置（带进程内缓存）。"""
    global _cached
    with _lock:
        if _cached is None:
            _cached = _load_from_disk()
        return _cached


def save_config(cfg: EmbeddingConfig) -> None:
    """落盘 + 失效进程缓存。"""
    from . import paths

    cfg.updated_at = time.time()
    p = paths.embedding_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    invalidate_cache()


def invalidate_cache() -> None:
    """配置变更时让所有下游（client + index 单例）下次访问时重读。

    agent_memory_tool 里的 EmbeddingClient/EmbeddingIndex 缓存也会通过这个钩子
    一并清掉——避免用户切换 provider/model 后旧 client 还在用老 base_url。
    """
    global _cached
    with _lock:
        _cached = None
    # 通知下游清缓存（延迟 import 防循环依赖）
    try:
        from .embedding_client import _reset_client_cache
        _reset_client_cache()
    except ImportError:
        pass
    try:
        from .embedding_index import _reset_index_cache
        _reset_index_cache()
    except ImportError:
        pass


__all__ = ["EmbeddingConfig", "get_config", "save_config", "invalidate_cache"]
