"""Embedding API 客户端（OpenAI 兼容 /embeddings 协议）。

设计：
- 走 openai.AsyncOpenAI（柳如烟已在 requirements.txt 装了 openai==2.31.0）
- 99% 主流 provider（OpenAI/智谱/硅基流动/阿里 dashscope/Mistral/豆包/Cohere/Together/Fireworks）
  都提供 OpenAI 兼容 /embeddings 端点，零协议适配
- 单例 + LRU query 缓存：同一 query 反复检索不重算
- 失败兜底：返 None 不抛——save_memory 仍然写 jsonl，recall_memory 退化 BM25

不在这里做的事：
- 不维护"哪些 provider 支持 embedding"的 catalog——交给前端 UI + 用户自己选
  （capabilities.py 已经能识别 embedding 模型类型，前端勾选时能过滤）
- 不批量化：单条 embed 已经 < 500ms；批量在 reindex 路径里做
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from typing import List, Optional

logger = logging.getLogger(__name__)

# 单例：进程内复用 client + LRU 缓存。
# config 变更时 _reset_client_cache 把 _instance 置 None，下次访问重建。
_lock = threading.Lock()
_instance: Optional["EmbeddingClient"] = None


class EmbeddingClient:
    """OpenAI 兼容 /embeddings 端点的轻量异步客户端。

    实例化策略：从 embedding_config 单例读 base_url/api_key/model/dim/timeout，
    所以全进程一个 EmbeddingClient 就够。配置变更走 invalidate_cache → 重建。
    """

    def __init__(self) -> None:
        from .embedding_config import get_config

        cfg = get_config()
        if not cfg.is_ready():
            raise RuntimeError(
                "embedding_config not ready (enabled=%s, model=%r, base_url=%r, has_key=%s, dim=%d)"
                % (cfg.enabled, cfg.model, cfg.base_url, bool(cfg.api_key), cfg.dim)
            )
        # 延迟 import openai：未启用 embedding 的用户路径上不该被 openai 库拖累
        from openai import AsyncOpenAI
        from backend.utils.llm_headers import merge_default_llm_headers

        self.cfg = cfg
        # 注入项目级默认 headers：embedding 同样会撞聚合 router 反爬
        # （SiliconFlow / OpenRouter 的 /embeddings 端点也走同一套 UA 校验）。
        self.client = AsyncOpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.timeout,
            default_headers=merge_default_llm_headers(),
        )
        # query LRU 缓存：text → np.ndarray
        # 用 OrderedDict 实现：访问移到末尾，超容量 popitem(last=False) 淘汰最老
        self._cache: "OrderedDict[str, object]" = OrderedDict()
        self._cache_max = max(16, int(cfg.cache_size))

    @property
    def dim(self) -> int:
        return self.cfg.dim

    @property
    def model(self) -> str:
        return self.cfg.model

    @property
    def provider(self) -> str:
        return self.cfg.provider

    async def embed(self, text: str) -> Optional["object"]:
        """单条 embed。返回 numpy 1D 数组（float32, shape=(dim,)）或 None。

        失败原因：网络/鉴权错误、维度不匹配、文本为空。所有失败都返 None +
        logger.warning，不抛——上游 save/recall 都要能 graceful degrade。
        """
        if not text or not isinstance(text, str):
            return None
        text = text.strip()
        if not text:
            return None

        # 缓存命中
        with _lock:
            if text in self._cache:
                self._cache.move_to_end(text)
                return self._cache[text]

        # 调 API
        # 注意：OpenAI embeddings 端点接受 input=string 或 input=list[string]，
        # 这里走单条以简化错误处理；reindex 批量另走专门方法。
        try:
            resp = await self.client.embeddings.create(
                model=self.cfg.model,
                input=text[:8000],  # 多数 embedding 模型上下文 8K token；柳如烟单条记忆 ≤ 10K char 够用
            )
        except Exception as exc:
            logger.warning("embedding API call failed (model=%s): %s", self.cfg.model, exc)
            return None

        if not resp.data or len(resp.data) == 0:
            logger.warning("embedding response has no data")
            return None

        # 延迟 import numpy：跟 openai 同样思路，未启用路径不被 numpy 拖累
        import numpy as np

        try:
            vec = np.array(resp.data[0].embedding, dtype=np.float32)
        except Exception as exc:
            logger.warning("embedding response parse failed: %s", exc)
            return None

        if vec.shape != (self.cfg.dim,):
            logger.warning(
                "embedding dim mismatch: got %s want (%d,) — config dim wrong? model=%s",
                vec.shape, self.cfg.dim, self.cfg.model,
            )
            return None

        # 写入缓存
        with _lock:
            self._cache[text] = vec
            if len(self._cache) > self._cache_max:
                self._cache.popitem(last=False)
        return vec

    async def embed_batch(self, texts: List[str]) -> List[Optional["object"]]:
        """批量 embed（reindex 全量重算用）。简单并发 gather；失败的那条返 None。"""
        return await asyncio.gather(*[self.embed(t) for t in texts])

    async def test_connection(self) -> dict:
        """连通性测试：随便 embed 一个短句，验证 base_url + api_key + model + dim 都对。

        返回：
            {
                "ok": bool,
                "dim": int,                 # 实际拿到的向量维度
                "expected_dim": int,        # 配置里写的维度
                "elapsed_ms": int,
                "error": str | None,
            }
        """
        import time as _time

        start = _time.time()
        try:
            resp = await self.client.embeddings.create(
                model=self.cfg.model,
                input="hello",
            )
            actual_dim = len(resp.data[0].embedding) if resp.data else 0
            elapsed = int((_time.time() - start) * 1000)
            return {
                "ok": actual_dim == self.cfg.dim,
                "dim": actual_dim,
                "expected_dim": self.cfg.dim,
                "elapsed_ms": elapsed,
                "error": None if actual_dim == self.cfg.dim
                         else f"dim mismatch: actual={actual_dim} expected={self.cfg.dim}",
            }
        except Exception as exc:
            return {
                "ok": False,
                "dim": 0,
                "expected_dim": self.cfg.dim,
                "elapsed_ms": int((_time.time() - start) * 1000),
                "error": str(exc),
            }


# ============================================================================
# 单例工厂 + 缓存失效
# ============================================================================

def get_client() -> Optional[EmbeddingClient]:
    """拿当前 EmbeddingClient 单例。配置未就绪时返 None（让上游短路）。"""
    global _instance
    with _lock:
        if _instance is not None:
            return _instance
    # config 检查放在锁外避免阻塞
    from .embedding_config import get_config
    cfg = get_config()
    if not cfg.is_ready():
        return None
    with _lock:
        if _instance is None:
            try:
                _instance = EmbeddingClient()
            except Exception as exc:
                logger.warning("EmbeddingClient init failed: %s", exc)
                return None
        return _instance


def _reset_client_cache() -> None:
    """配置变更时调，让 get_client 下次重建。embedding_config.invalidate_cache 会调这个。"""
    global _instance
    with _lock:
        _instance = None


__all__ = ["EmbeddingClient", "get_client"]
