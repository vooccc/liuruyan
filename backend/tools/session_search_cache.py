"""
session_search 工具结果缓存：60s TTL，session 维度隔离。

设计参考 `backend/tools/session_read_cache.py`
（同款 Singleton + threading.Lock + sid 维度 + 延迟清理 范式）。

目的：
- LLM 在同一对话内可能反复调 session_search（review 上一轮 → 再确认 → 再扩展）。
- 同样 query/scope 参数下，transcript.jsonl 60s 内不会变（写入低频）。
- 命中缓存：零 IO、零 token 重复计算（result_json 直接复用）。

设计要点（照搬 session_read_cache）：
- Singleton：进程级 cache，跨 session 共享内存（不是 ContextVar——cache 不是请求维度状态）
- threading.Lock：dict 写入加锁，避免并发覆盖
- session_id 维度隔离：避免角色 A 看到角色 B 的缓存结果
- 延迟清理：写入时如果 session 总数 > 阈值才扫一遍清旧（不开后台 task）
- TTL=60s：transcript 写入低频，60s 信息几乎不变；超时直接丢弃
- LRU 限项：每 session 最多 16 项（防同 session 大量不同 query 把内存撑爆）
- session_id 缺失：不走 cache（脚本调用直接打盘，无害）

不做的事：
- 不持久化（进程退出即忘）
- 不开后台清理线程（按需触发足够）
- 不做精确的 LRU（按 expire 时间替换最旧的——简单且效果接近）
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Dict, Optional, Tuple


# TTL：缓存命中窗口，超过即视为过期
_CACHE_TTL_SECONDS = 60.0
# 每 session 缓存的最大条目数（超过按 expire_ts 替换最旧的）
_PER_SESSION_LRU = 16
# 全局 session 数触发延迟清理的阈值
_CLEANUP_THRESHOLD = 100
# 整个 session 超过多久没动 → 整体丢弃
_SESSION_IDLE_SECONDS = 30 * 60  # 30 分钟


def compute_cache_key(payload: Dict[str, Any]) -> str:
    """
    把任意 dict 参数稳定哈希为 cache key。

    使用 sha1（足够低碰撞、足够快），key 排序后 json.dumps 序列化。
    None / 空字符串归一化为相同 key（避免 max_results=None vs 不传 出现两份缓存）。
    """
    # 归一化：剔掉值是 None 或空串的键，避免歧义
    normalized = {
        k: v
        for k, v in payload.items()
        if v is not None and v != ""
    }
    # query 单独 lower + strip（搜索语义上大小写不敏感、首尾空白不影响命中）
    if "query" in normalized and isinstance(normalized["query"], str):
        normalized["query"] = normalized["query"].strip().lower()
    serialized = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()


class SessionSearchCache:
    """
    每个 session 一份 {cache_key: (result_json, expire_ts)} dict。

    Result 是已经序列化好的 JSON 字符串，避免每次命中重新 dumps。
    """

    _instance: Optional["SessionSearchCache"] = None
    _singleton_lock = threading.Lock()

    def __new__(cls) -> "SessionSearchCache":
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._lock = threading.Lock()
        # session_id -> {cache_key: (result_json_str, expire_ts)}
        self._cache: Dict[str, Dict[str, Tuple[str, float]]] = {}
        # session_id -> last_touch_ts，用于 session 级过期清理
        self._touch: Dict[str, float] = {}
        # 观测：命中/未命中计数器（用于 stats / 调试）
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------ API
    def get(self, session_id: Optional[str], cache_key: str) -> Optional[str]:
        """
        查询缓存。

        返回 None 表示未命中或已过期。
        命中时返回原始 result_json 字符串。
        """
        if not session_id or not cache_key:
            return None
        now = time.time()
        with self._lock:
            session_dict = self._cache.get(session_id)
            if not session_dict:
                self._misses += 1
                return None
            entry = session_dict.get(cache_key)
            if entry is None:
                self._misses += 1
                return None
            result_json, expire_ts = entry
            if expire_ts < now:
                # 过期，主动清理这条
                session_dict.pop(cache_key, None)
                self._misses += 1
                return None
            self._hits += 1
            # touch session 让它"活着"
            self._touch[session_id] = now
            return result_json

    def set(
        self,
        session_id: Optional[str],
        cache_key: str,
        result_json: str,
        ttl_seconds: float = _CACHE_TTL_SECONDS,
    ) -> None:
        """
        写入缓存。

        session_id 缺失或 cache_key 空 → 不写（无害降级）。
        超过 _PER_SESSION_LRU 时按 expire_ts 最旧的替换。
        """
        if not session_id or not cache_key or not result_json:
            return
        now = time.time()
        # floor 0.001 仅防御负数 / 0；正常生产 TTL=60s，测试可传 < 1s
        expire_ts = now + max(0.001, float(ttl_seconds))
        with self._lock:
            session_dict = self._cache.setdefault(session_id, {})

            # LRU 上限：满了就替换 expire_ts 最小（最早过期）的那条
            if len(session_dict) >= _PER_SESSION_LRU and cache_key not in session_dict:
                # 找最旧的 cache_key 替换
                oldest_key = min(session_dict.keys(), key=lambda k: session_dict[k][1])
                session_dict.pop(oldest_key, None)

            session_dict[cache_key] = (result_json, expire_ts)
            self._touch[session_id] = now
            self._maybe_cleanup_locked(now)

    def forget_session(self, session_id: str) -> None:
        """
        显式清掉某个 session 的全部缓存。
        （chat handler 在 session 关闭 / 切换时可调用）
        """
        if not session_id:
            return
        with self._lock:
            self._cache.pop(session_id, None)
            self._touch.pop(session_id, None)

    def stats(self) -> Dict[str, Any]:
        """暴露观测指标（命中率/总条目数/活跃 session 数）。"""
        with self._lock:
            total_entries = sum(len(v) for v in self._cache.values())
            return {
                "sessions": len(self._cache),
                "entries": total_entries,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": (
                    self._hits / (self._hits + self._misses)
                    if (self._hits + self._misses) > 0
                    else 0.0
                ),
            }

    def reset_for_testing(self) -> None:
        """测试钩子：清空全部缓存 + 重置计数器。"""
        with self._lock:
            self._cache.clear()
            self._touch.clear()
            self._hits = 0
            self._misses = 0

    # ------------------------------------------------------------ internal
    def _maybe_cleanup_locked(self, now: float) -> None:
        """
        延迟清理：必须持有 self._lock 调用。

        - session 数超过 _CLEANUP_THRESHOLD 才扫
        - 扫的内容：
          1) session_idle 超过 _SESSION_IDLE_SECONDS → 整个 session 丢
          2) 同 session 内已过期的 cache 条目 → 单条丢
        """
        if len(self._touch) < _CLEANUP_THRESHOLD:
            return
        idle_cutoff = now - _SESSION_IDLE_SECONDS
        expired_sids = [sid for sid, ts in self._touch.items() if ts < idle_cutoff]
        for sid in expired_sids:
            self._cache.pop(sid, None)
            self._touch.pop(sid, None)
        # 顺便扫一遍 expired 条目
        for sid, session_dict in list(self._cache.items()):
            stale = [k for k, (_, expire_ts) in session_dict.items() if expire_ts < now]
            for k in stale:
                session_dict.pop(k, None)
            if not session_dict:
                self._cache.pop(sid, None)
                self._touch.pop(sid, None)


_cache_singleton: Optional[SessionSearchCache] = None


def get_search_cache() -> SessionSearchCache:
    """单例 helper。线程安全。"""
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = SessionSearchCache()
    return _cache_singleton


__all__ = [
    "SessionSearchCache",
    "get_search_cache",
    "compute_cache_key",
]
