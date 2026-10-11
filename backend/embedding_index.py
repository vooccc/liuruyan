"""轻量 embedding 向量索引（per-bucket）。

设计取舍：
- 不上 chromadb / faiss / sqlite-vec：单人桌面应用，N < 10K 时纯 numpy 余弦
  相似度 < 5ms；引入向量库会让 PyInstaller 打包多 100+MB
- 向量是衍生缓存：jsonl 是真相源，索引文件丢了能 rebuild（reindex 路径里实现）
- 维度 + provider + model 写进 meta.json，换模型时拒绝混搭——强制用户重算
- 文件级锁：写盘前 .tmp + replace，避免读到半截文件
- 进程内单例缓存：每个 (scope, agent_id) 一个 EmbeddingIndex 复用

存储布局：
    <bucket_dir>/embeddings.meta.json    {dim, provider, model, count, updated_at}
    <bucket_dir>/embeddings.vectors.npy  shape=(N, dim) float32 已 L2 归一化
    <bucket_dir>/embeddings.ids.json     ["mem_id_1", "mem_id_2", ...]  跟 vectors 同序

bucket_dir 路径来自 paths.py：
    private: paths.agent_embedding_dir(agent_id)
    shared:  paths.shared_embedding_dir()
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_HEADER_NAME = "embeddings.meta.json"
_VECTORS_NAME = "embeddings.vectors.npy"
_IDS_NAME = "embeddings.ids.json"


class EmbeddingIndex:
    """单 bucket 的向量索引 + 余弦检索。

    不持有 EmbeddingClient——index 只管"id ↔ 向量"映射，向量化在外面做。
    这样 reindex / 测试 mock / 离线场景都好处理。
    """

    def __init__(self, bucket_dir: Path, dim: int, provider: str, model: str) -> None:
        self.base = bucket_dir
        self.dim = dim
        self.provider = provider
        self.model = model
        self._lock = threading.Lock()
        self._vectors = None  # numpy.ndarray (N, dim) float32
        self._ids: List[str] = []
        self._loaded = False

    # ------------------------------------------------------------------
    # 懒加载 + 持久化
    # ------------------------------------------------------------------

    def _load_lazy(self) -> None:
        """首次访问时把磁盘上的 npy + ids + meta 读进来。空索引也算"已加载"。"""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:  # double-check
                return
            self._do_load()
            self._loaded = True

    def _do_load(self) -> None:
        import numpy as np

        meta_path = self.base / _HEADER_NAME
        vec_path = self.base / _VECTORS_NAME
        ids_path = self.base / _IDS_NAME

        if not (meta_path.exists() and vec_path.exists() and ids_path.exists()):
            self._vectors = np.zeros((0, self.dim), dtype=np.float32)
            self._ids = []
            return

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("read embedding meta failed (%s): %s", meta_path, exc)
            self._vectors = np.zeros((0, self.dim), dtype=np.float32)
            self._ids = []
            return

        # 维度 / 模型不一致 → 拒绝加载（让用户走 reindex 流程）
        if meta.get("dim") != self.dim or meta.get("model") != self.model:
            logger.warning(
                "embedding meta mismatch at %s: stored=(dim=%s, model=%r) want=(dim=%d, model=%r) "
                "→ index treated as empty; run reindex to rebuild",
                self.base, meta.get("dim"), meta.get("model"), self.dim, self.model,
            )
            self._vectors = np.zeros((0, self.dim), dtype=np.float32)
            self._ids = []
            return

        try:
            vectors = np.load(vec_path).astype(np.float32, copy=False)
            ids = json.loads(ids_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("load embedding vectors failed (%s): %s", self.base, exc)
            self._vectors = np.zeros((0, self.dim), dtype=np.float32)
            self._ids = []
            return

        if not isinstance(ids, list) or len(ids) != vectors.shape[0]:
            logger.warning(
                "embedding ids/vectors size mismatch at %s: ids=%d vectors=%d",
                self.base, len(ids) if isinstance(ids, list) else -1, vectors.shape[0],
            )
            self._vectors = np.zeros((0, self.dim), dtype=np.float32)
            self._ids = []
            return

        self._vectors = vectors
        self._ids = list(ids)

    def _persist(self) -> None:
        """写盘：.tmp + replace 原子化，避免读到半截。

        必须在持有 self._lock 时调用。

        坑：np.save("foo.tmp") 会自动加 ".npy" 变成 "foo.tmp.npy"。
        用 file handle 模式（np.save(open_file, arr)）规避自动加扩展名。
        """
        import numpy as np

        self.base.mkdir(parents=True, exist_ok=True)
        # vectors：用 file handle 写避免 numpy 自动加 .npy
        vec_tmp = self.base / (_VECTORS_NAME + ".tmp")
        with vec_tmp.open("wb") as fh:
            np.save(fh, self._vectors)
        vec_tmp.replace(self.base / _VECTORS_NAME)

        # ids
        ids_tmp = self.base / (_IDS_NAME + ".tmp")
        ids_tmp.write_text(json.dumps(self._ids, ensure_ascii=False), encoding="utf-8")
        ids_tmp.replace(self.base / _IDS_NAME)

        # meta
        meta = {
            "dim": self.dim,
            "provider": self.provider,
            "model": self.model,
            "count": len(self._ids),
            "updated_at": time.time(),
        }
        meta_tmp = self.base / (_HEADER_NAME + ".tmp")
        meta_tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        meta_tmp.replace(self.base / _HEADER_NAME)

    # ------------------------------------------------------------------
    # 写操作：add / update / remove
    # ------------------------------------------------------------------

    def add(self, memory_id: str, vec) -> None:
        """新增或替换一条向量。vec 是 numpy 1D array shape=(dim,)。

        内部会做 L2 归一化，所以检索阶段直接点积 = 余弦相似度（省一个除法）。
        """
        import numpy as np

        if vec is None:
            return
        if vec.shape != (self.dim,):
            raise ValueError(f"vec shape {vec.shape} != ({self.dim},)")

        # 归一化（零向量保护）
        norm = float(np.linalg.norm(vec))
        if norm < 1e-12:
            logger.warning("zero-norm vector for memory_id=%s, skipped", memory_id)
            return
        nvec = (vec / norm).astype(np.float32, copy=False)

        self._load_lazy()
        with self._lock:
            if memory_id in self._ids:
                i = self._ids.index(memory_id)
                self._vectors[i] = nvec
            else:
                self._vectors = np.vstack([self._vectors, nvec[None, :]])
                self._ids.append(memory_id)
            self._persist()

    def remove(self, memory_id: str) -> bool:
        """删除一条向量。不存在时返 False，存在并删掉返 True。"""
        import numpy as np

        self._load_lazy()
        with self._lock:
            if memory_id not in self._ids:
                return False
            i = self._ids.index(memory_id)
            self._vectors = np.delete(self._vectors, i, axis=0)
            self._ids.pop(i)
            self._persist()
            return True

    def clear(self) -> None:
        """清空整个 bucket（reindex 全量重算前调）。"""
        import numpy as np

        with self._lock:
            self._vectors = np.zeros((0, self.dim), dtype=np.float32)
            self._ids = []
            self._loaded = True
            self._persist()

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search(self, query_vec, top_k: int) -> List[Tuple[str, float]]:
        """余弦相似度 topK。

        返回 [(memory_id, score), ...]，score ∈ [-1, 1]，越大越相似。
        空索引返空列表。query_vec 内部会归一化。
        """
        import numpy as np

        self._load_lazy()
        if self._vectors is None or len(self._ids) == 0:
            return []

        if query_vec is None or query_vec.shape != (self.dim,):
            return []

        norm = float(np.linalg.norm(query_vec))
        if norm < 1e-12:
            return []
        nq = (query_vec / norm).astype(np.float32, copy=False)

        # 点积（双方都已归一化 = 余弦相似度）
        sims = self._vectors @ nq
        n = sims.shape[0]
        k = min(int(top_k), n)
        if k <= 0:
            return []
        # argpartition 取 topK 索引（无序），再对这 K 个排序
        if k < n:
            idx = np.argpartition(-sims, k - 1)[:k]
            idx = idx[np.argsort(-sims[idx])]
        else:
            idx = np.argsort(-sims)
        return [(self._ids[int(i)], float(sims[int(i)])) for i in idx]

    def has(self, memory_id: str) -> bool:
        self._load_lazy()
        return memory_id in self._ids

    def size(self) -> int:
        self._load_lazy()
        return len(self._ids)

    def stats(self) -> dict:
        """返回索引状态摘要（用于 /embedding/stats 接口 + 调试）。"""
        self._load_lazy()
        return {
            "count": len(self._ids),
            "dim": self.dim,
            "provider": self.provider,
            "model": self.model,
            "base_dir": str(self.base),
        }


# ============================================================================
# 单例缓存：避免每次 save/recall 都重建 EmbeddingIndex 实例
# ============================================================================

_cache_lock = threading.Lock()
_index_cache: dict = {}


def get_index(bucket_key: str, bucket_dir: Path) -> Optional[EmbeddingIndex]:
    """拿 (scope, agent_id) 对应 bucket 的 EmbeddingIndex 实例。

    bucket_key 通常是 "private:<agent_id>" 或 "shared"，作为缓存键。
    embedding_config 未就绪时返 None。
    """
    from .embedding_config import get_config

    cfg = get_config()
    if not cfg.is_ready():
        return None

    cache_key = f"{bucket_key}|{cfg.model}|{cfg.dim}"
    with _cache_lock:
        if cache_key in _index_cache:
            return _index_cache[cache_key]
        idx = EmbeddingIndex(
            bucket_dir=bucket_dir,
            dim=cfg.dim,
            provider=cfg.provider,
            model=cfg.model,
        )
        _index_cache[cache_key] = idx
        return idx


def _reset_index_cache() -> None:
    """配置变更时调，让 get_index 下次重建（避免 dim/model 变了仍用旧实例）。"""
    with _cache_lock:
        _index_cache.clear()


__all__ = ["EmbeddingIndex", "get_index"]
