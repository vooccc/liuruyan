"""
save_memory / recall_memory：跨会话经验累积的轻量 KV 工具

定位（和现有 memory 系统的关系）：
- 现有 `memory_dir() / *.md` 是"日记 / 长期记忆 / 反思"风格——人写的、半结构化
- 现有 `MEMORY.md` / 反思文件 → 角色启动时整篇加载到 prompt
- 这套 KV 工具是"角色按需主动 save/recall 的事实型经验"——结构化、按需查询、不进 prompt

为啥分两套：
- prompt 注入版（MEMORY.md）容量小 / 全局污染大；适合人格设定 + 关键背景
- 工具调用版（这套）容量大 / 按需检索；适合 "上次跑 frontend tsc 的命令是 X" 这类琐事

存储：
- 每 agent 一份 JSONL：<agent_dir>/memory/kv_memories.jsonl
  没 agent_id（脚本场景）→ paths.memory_dir() / kv_memories.jsonl
- 每行：{id, ts, content, tags: [str], updated_at?}
- 检索：query 走子串 + tags 子集匹配；按 ts 倒序，topK 默认 8

设计取舍：
- sync 版本（save_memory_tool / recall_memory_tool）保持原 BM25 兜底语义
- async 版本（save_memory_async / recall_memory_async）在 sync 之上叠加 embedding：
  embedding_config 启用 + provider 就绪 → 向量化 + 混合检索；否则跟 sync 等价
- 没做 update-by-id：先 delete + save，简单可靠（虽然多一次 I/O）
- 软上限 1000 条/agent；超了警告但不删（角色看到提示自己清）

v2 升级（embedding + scope + 可解释性，2026-05-14）：
- scope='private' / 'shared' / 'all'：跨角色私有 ✕ 共享 双轨
  → ./backend\paths.py:222 shared_memory_dir()
- mode='auto' / 'bm25' / 'vector'：检索模式（auto = 自动混合）
- recall 结果带 match_reason：vector_sim / tag_match / temp_boost / time_decay
  让 LLM 知道"为什么这条被推上来"
- embedding 失败兜底：API 挂了 → save 仍写 jsonl，recall 退化 BM25
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_KV_FILE_NAME = "kv_memories.jsonl"
_DEFAULT_TOP_K = 8
_MAX_TOP_K = 50
_SOFT_LIMIT = 1000


# ============================================================================
# 记忆温度系统（v3 升级，2026-05-14）
# ============================================================================
#
# 跟 memory_manager.py:74-89 三档温度系统语义对齐，但实现独立——
# memory_manager 处理的是 MEMORY.md（人写的半结构化），靠 <!-- temp:xx --> 标记；
# 这里处理的是 KV jsonl 的结构化记录，靠 record.temp 整数字段。
#
# 升温自动规则（每次 recall 命中触发）：
#   recall_count: 0..2 → cold (1)
#   recall_count: 3..9 → warm (2)
#   recall_count: 10+ → burning (3)
# 用户/LLM 显式传 temp 时跳过自动规则（尊重用户判断）。
#
# 衰减：在 memory_audit 工具里集中跑，不在 recall 路径里实时算（性能 + 简单）。
#
# locked=True 不参与衰减（重要笔记永不冷却），但仍能升温。

_TEMP_COLD = 1        # ❄️ 冷存：默认状态
_TEMP_WARM = 2        # 🌡️ 温热：被反复想起
_TEMP_BURNING = 3     # 🔥 燃烧：执念级别

_TEMP_LABELS = {_TEMP_COLD: "❄️", _TEMP_WARM: "🌡️", _TEMP_BURNING: "🔥"}
_TEMP_NAMES = {_TEMP_COLD: "cold", _TEMP_WARM: "warm", _TEMP_BURNING: "burning"}
_TEMP_PARSE = {  # 接受用户/LLM 传的多种写法
    "cold": _TEMP_COLD, "❄️": _TEMP_COLD, "1": _TEMP_COLD,
    "warm": _TEMP_WARM, "🌡️": _TEMP_WARM, "2": _TEMP_WARM,
    "burning": _TEMP_BURNING, "🔥": _TEMP_BURNING, "3": _TEMP_BURNING,
}

# 自动升温阈值
_AUTO_WARM_AT = 3      # recall_count >= 3 → warm
_AUTO_BURNING_AT = 10  # recall_count >= 10 → burning

# 召回评分加权：温度高 → 排前面
# 注意：vector_sim ∈ [-1, 1]、bm25_score ∈ N+，rrf_score ∈ (0, 1)
# 温度权重作用在 RRF 融合后的最终 score 上，所以乘性放大（1.0/1.2/1.5）
_TEMP_WEIGHT = {_TEMP_COLD: 1.0, _TEMP_WARM: 1.2, _TEMP_BURNING: 1.5}


def _parse_temp(value: Any) -> Optional[int]:
    """把用户传的温度值（int / str / emoji）规范化为 int 1/2/3，无效返 None。"""
    if value is None:
        return None
    if isinstance(value, int):
        return value if value in (_TEMP_COLD, _TEMP_WARM, _TEMP_BURNING) else None
    if isinstance(value, str):
        return _TEMP_PARSE.get(value.strip().lower())
    return None


def _auto_temp_from_count(recall_count: int) -> int:
    """根据被命中次数自动推导温度。"""
    if recall_count >= _AUTO_BURNING_AT:
        return _TEMP_BURNING
    if recall_count >= _AUTO_WARM_AT:
        return _TEMP_WARM
    return _TEMP_COLD


# ============================================================================
# 路径解析
# ============================================================================

def _resolve_kv_path(agent_id: Optional[str], scope: str = "private") -> Path:
    """决定写哪个 kv 文件。

    scope:
        'private' (默认) — agent 私有，落 <agent_dir>/memory/kv_memories.jsonl
        'shared'         — 跨角色共享，落 paths.shared_memory_dir()/kv_memories.jsonl

    'all' 不在这里处理（'all' 是 recall 时的多 bucket 读取语义，写入必须明确 scope）。
    """
    try:
        from backend import paths
        from backend.agent_config import get_agent_manager
    except Exception:
        # 项目上下文不可用 → 临时目录兜底
        from tempfile import gettempdir
        sub = scope if scope in ("private", "shared") else "private"
        return Path(gettempdir()) / "liuruyan_kv" / sub / (agent_id or "_global") / _KV_FILE_NAME

    if scope == "shared":
        return paths.shared_memory_dir() / _KV_FILE_NAME

    if agent_id:
        try:
            manager = get_agent_manager()
            cfg = manager.get_agent_config(agent_id)
            if cfg:
                memory_dir = cfg.memory_dir
                memory_dir.mkdir(parents=True, exist_ok=True)
                return memory_dir / _KV_FILE_NAME
        except Exception as exc:
            logger.warning(f"resolve agent memory dir failed for {agent_id}: {exc}")

    # 全局兜底
    base = paths.memory_dir() / "kv"
    base.mkdir(parents=True, exist_ok=True)
    return base / _KV_FILE_NAME


def _resolve_embedding_bucket(agent_id: Optional[str], scope: str) -> tuple:
    """返回 (bucket_key, bucket_dir)，给 EmbeddingIndex 单例缓存用。

    bucket_key 是 (scope, agent_id) 复合键，shared 的所有 agent 共用一个 bucket，
    private 每个 agent 独立。
    """
    try:
        from backend import paths
    except Exception:
        # 项目上下文不可用 → 走临时目录（embedding 在测试沙盒里也能跑）
        from tempfile import gettempdir
        sub = "shared" if scope == "shared" else f"private:{agent_id or '_global'}"
        return sub, Path(gettempdir()) / "liuruyan_embeddings" / sub

    if scope == "shared":
        return "shared", paths.shared_embedding_dir()

    # private
    aid = agent_id or "_global"
    if agent_id:
        return f"private:{aid}", paths.agent_embedding_dir(aid)
    # 没 agent_id → 用 memory_dir 下的 _global 子目录
    base = paths.memory_dir() / "embeddings_global"
    base.mkdir(parents=True, exist_ok=True)
    return "private:_global", base


def _load_records(p: Path) -> List[Dict[str, Any]]:
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict) and rec.get("id"):
                        out.append(rec)
                except (ValueError, TypeError):
                    continue
    except Exception as exc:
        logger.warning(f"load kv memories failed: {exc}")
    return out


def _save_records(p: Path, records: List[Dict[str, Any]]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(p)


# ============================================================================
# save_memory
# ============================================================================

def save_memory_tool(
    content: str,
    tags: Optional[List[str]] = None,
    agent_id: Optional[str] = None,
    memory_id: Optional[str] = None,
    title: Optional[str] = None,
    user_triggered: Optional[bool] = None,
    scope: str = "private",
    temp: Optional[Any] = None,       # 'cold'/'warm'/'burning'/1/2/3/emoji
    locked: Optional[bool] = None,    # True = 不参与温度衰减
) -> str:
    if not content or not isinstance(content, str):
        return json.dumps({"error": "content must be a non-empty string"})
    content = content.strip()
    if not content:
        return json.dumps({"error": "content is empty after strip"})
    if len(content) > 10_000:
        return json.dumps({"error": "content too long (>10000 chars). Split into smaller memories."})

    if tags is not None and not isinstance(tags, list):
        return json.dumps({"error": "tags must be a list of strings"})
    norm_tags = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]
    norm_title = str(title).strip() if isinstance(title, str) and title.strip() else None

    if scope not in ("private", "shared"):
        return json.dumps({"error": f"invalid scope: {scope!r}. Use 'private' or 'shared'."})

    norm_temp = _parse_temp(temp)
    if temp is not None and norm_temp is None:
        return json.dumps({
            "error": f"invalid temp: {temp!r}. Use 'cold'/'warm'/'burning' or 1/2/3."
        })

    p = _resolve_kv_path(agent_id, scope=scope)
    records = _load_records(p)

    now_ts = time.time()
    if memory_id:
        # 更新现有
        existing = next((r for r in records if r.get("id") == memory_id), None)
        if existing is None:
            return json.dumps({"error": f"memory not found: {memory_id}"})
        existing["content"] = content
        existing["tags"] = norm_tags or existing.get("tags", [])
        if norm_title is not None:
            existing["title"] = norm_title
        if isinstance(user_triggered, bool):
            existing["user_triggered"] = user_triggered
        if norm_temp is not None:
            existing["temp"] = norm_temp
            existing["temp_user_set"] = True  # 用户显式设过 → 后续 auto-promote 不动
        if isinstance(locked, bool):
            existing["locked"] = locked
        existing["updated_at"] = now_ts
        rec = existing
        action = "updated"
    else:
        rec = {
            "id": uuid.uuid4().hex[:12],
            "ts": now_ts,
            "content": content,
            "tags": norm_tags,
            "temp": norm_temp if norm_temp is not None else _TEMP_COLD,
            "recall_count": 0,
            "last_recalled_at": None,
            "locked": bool(locked) if locked is not None else False,
        }
        if norm_temp is not None:
            rec["temp_user_set"] = True
        if norm_title is not None:
            rec["title"] = norm_title
        if isinstance(user_triggered, bool):
            rec["user_triggered"] = user_triggered
        records.append(rec)
        action = "created"

    soft_warning = None
    if len(records) > _SOFT_LIMIT:
        soft_warning = (
            f"You have {len(records)} memories — exceeds soft limit {_SOFT_LIMIT}. "
            "Consider deleting stale entries via create_memory(action='delete', id=...)."
        )

    _save_records(p, records)

    return json.dumps({
        "success": True,
        "action": action,
        "memory": rec,
        "store": str(p),
        "scope": scope,
        "total": len(records),
        "warning": soft_warning,
    }, ensure_ascii=False)


# ============================================================================
# recall_memory
# ============================================================================

def _score(rec: Dict[str, Any], query_tokens: List[str], required_tags: List[str]) -> int:
    """简单子串/标签打分。0 = 不匹配，>0 越大越相关。

    注意：这是基础分（int），不含温度权重。温度加权在 _apply_temp_weight 里做（float）——
    保持 int 基础分让 RRF 排名稳定（rank 是排序后位置，不依赖绝对值），温度只放大最终输出排序。
    """
    content_lc = rec.get("content", "").lower()
    rec_tags = set(rec.get("tags", []) or [])
    score = 0
    # 必备 tag 全命中才算上场
    for t in required_tags:
        if t not in rec_tags:
            return 0
        score += 5
    # query token 子串命中
    for tok in query_tokens:
        if tok in content_lc:
            score += 3
        if tok in rec_tags:
            score += 4
    return score


def _temp_weight(rec: Dict[str, Any]) -> float:
    """读 record 的温度权重；缺失或非法值默认 cold (1.0)。"""
    temp = rec.get("temp") or _TEMP_COLD
    return _TEMP_WEIGHT.get(temp, 1.0)


def _load_namespace_records(agent_id: Optional[str]) -> List[Dict[str, Any]]:
    """v5 命名空间记忆 → recall 伪记录（2026-10-24）。

    让 recall_memory 不只搜 create_memory 的 KV 存储：全局层条目、项目状态卡、
    timeline 事件全部参与 BM25/混合检索——"让她回想一下"能想起迁移后的分层记忆。
    伪记录只读不写（recall_count/温度升温只作用于 KV 记录，id 前缀隔离天然跳过）。
    """
    try:
        from backend.memory_manager import get_memory_manager

        ns = get_memory_manager(agent_id=agent_id).namespace_manager
        if not ns.is_active():
            return []
    except Exception:
        return []

    def _ts_from_date(value: Any) -> int:
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(str(value)[:10]).timestamp())
        except Exception:
            return 0

    records: List[Dict[str, Any]] = []

    # 全局层：### [date] title 条目
    global_text = ns.read_global()
    for i, m in enumerate(re.finditer(r"^### \[(\d{4}-\d{2}-\d{2})\]\s*(.+)$", global_text, re.M)):
        start = m.end()
        nxt = global_text.find("\n### [", start)
        body = global_text[start: nxt if nxt != -1 else len(global_text)].strip()
        records.append({
            "id": f"ns_global_{i}",
            "content": (m.group(2).strip() + ("\n" + body if body else "")).strip(),
            "tags": ["global", "relationship"],
            "ts": _ts_from_date(m.group(1)),
            "temp": _TEMP_WARM,
            "source": "namespace_global",
        })

    # 项目状态卡（每项目一条）+ timeline 事件（每条一记录）
    for entry in ns.list_namespaces():
        if entry.get("status") == "archived":
            continue
        ns_id = entry.get("id") or ""
        tags = [entry.get("name", ns_id)] + list(entry.get("aliases") or [])
        card = ns.read_card(ns_id).strip()
        if card:
            records.append({
                "id": f"ns_card_{ns_id}",
                "content": card,
                "tags": tags,
                "ts": int(time.time()),
                "temp": _TEMP_WARM,
                "source": "namespace_card",
            })
        for j, tl in enumerate(ns.read_timeline(ns_id, limit=200)):
            content = "。".join(
                str(tl.get(k, "")) for k in ("event", "decision", "cognition") if tl.get(k)
            )
            if not content:
                continue
            records.append({
                "id": f"ns_tl_{ns_id}_{j}",
                "content": content,
                "tags": tags,
                "ts": _ts_from_date(tl.get("ts")),
                "temp": _TEMP_COLD,
                "source": "namespace_timeline",
            })

    return records


def _record_recall_hits(
    hit_records: List[Dict[str, Any]],
    bucket_paths: Dict[str, Path],
) -> None:
    """记录召回命中：更新 recall_count + last_recalled_at + 自动 promote 温度，按 scope 分桶写盘。

    Args:
        hit_records: 本次返给 LLM 的记忆列表（dict，每个含 _scope 标注来源 bucket）。
        bucket_paths: scope 名 → 该 bucket 的 jsonl 路径。

    设计：
    - 一次 recall 多条命中 → 按 scope 分组 → 每个 bucket 仅 load+save 一次（IO 最小化）
    - 用户显式设过 temp（temp_user_set=True）的记忆不参与自动升温
    - 写盘失败 logger.warning 不抛——recall 主流程不能因为温度统计写不进去就破
    """
    if not hit_records:
        return

    now = time.time()
    by_scope: Dict[str, set] = {}
    for r in hit_records:
        s = r.get("_scope") or "private"
        mid = r.get("id")
        if not mid:
            continue
        by_scope.setdefault(s, set()).add(mid)

    for scope_name, hit_ids in by_scope.items():
        path = bucket_paths.get(scope_name)
        if not path:
            continue
        try:
            records = _load_records(path)
        except Exception as exc:
            logger.warning("load records for recall hit update failed (%s): %s", path, exc)
            continue
        changed = False
        for rec in records:
            if rec.get("id") not in hit_ids:
                continue
            rec["recall_count"] = int(rec.get("recall_count", 0) or 0) + 1
            rec["last_recalled_at"] = now
            # 自动升温（用户显式 set 过的不动）
            if not rec.get("temp_user_set"):
                cur = rec.get("temp", _TEMP_COLD) or _TEMP_COLD
                target = _auto_temp_from_count(rec["recall_count"])
                if target > cur:
                    rec["temp"] = target
            changed = True
        if changed:
            try:
                _save_records(path, records)
            except Exception as exc:
                logger.warning("save records for recall hit update failed (%s): %s", path, exc)


def _decay_cold_records(
    records: List[Dict[str, Any]],
    decay_days: int = 14,
) -> int:
    """对长时间没被 recall 的 warm/burning 记录降一档。返回被改动的条目数。

    跟 memory_manager.py:178-205 同语义（每 14 天降一档），但作用在 KV jsonl 而非 MEMORY.md。
    locked=True 跳过；temp_user_set=True 也跳过（用户显式标的执念不衰减）。

    被 memory_audit 工具调用（不在 recall 路径里跑，避免无关开销）。
    """
    now = time.time()
    threshold = decay_days * 86400
    changed = 0
    for r in records:
        if r.get("locked"):
            continue
        if r.get("temp_user_set"):
            continue
        last = r.get("last_recalled_at") or r.get("ts") or now
        if now - last < threshold:
            continue
        cur = r.get("temp", _TEMP_COLD) or _TEMP_COLD
        if cur > _TEMP_COLD:
            r["temp"] = cur - 1
            changed += 1
    return changed


def recall_memory_tool(
    query: Optional[str] = None,
    tags: Optional[List[str]] = None,
    top_k: int = _DEFAULT_TOP_K,
    agent_id: Optional[str] = None,
    scope: str = "private",
) -> str:
    """BM25 检索（单 bucket）。scope='all' 请走 recall_memory_async。"""
    top_k = max(1, min(int(top_k or _DEFAULT_TOP_K), _MAX_TOP_K))
    query_tokens = [t for t in (query or "").lower().split() if t]
    required_tags = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]
    list_mode = not query_tokens and not required_tags

    if scope not in ("private", "shared"):
        # sync 路径不支持 'all'（多 bucket 读需要联动 RRF，走 async）
        return json.dumps({
            "error": f"sync recall_memory_tool requires scope in (private, shared); got {scope!r}. Use recall_memory_async for scope='all'."
        })

    p = _resolve_kv_path(agent_id, scope=scope)
    records = _load_records(p)
    if not records:
        return json.dumps({"results": [], "total": 0, "returned": 0, "store": str(p), "scope": scope})

    if list_mode:
        # 无过滤 → 按时间倒序列出 (替代旧 list_memories)
        records.sort(key=lambda r: r.get("ts", 0), reverse=True)
        top = records[:top_k]
        return json.dumps({
            "results": top,
            "total": len(records),
            "returned": len(top),
            "mode": "list",
            "store": str(p),
            "scope": scope,
        }, ensure_ascii=False)

    scored: List[tuple[float, Dict[str, Any]]] = []
    for rec in records:
        s = _score(rec, query_tokens, required_tags)
        if s > 0:
            # 温度加权：burning 记忆排前面
            weighted = float(s) * _temp_weight(rec)
            scored.append((weighted, rec))

    # score 降序，相同分数下 ts 降序（最近的优先）
    scored.sort(key=lambda x: (x[0], x[1].get("ts", 0)), reverse=True)
    top_pairs = scored[:top_k]
    top = [dict(rec) for _, rec in top_pairs]

    # 写盘：被命中的记忆 recall_count++ + last_recalled_at + 可能升温
    # sync 路径单 bucket 简单：所有 hit 都来自 scope 这个 bucket
    for r in top:
        r["_scope"] = scope
    _record_recall_hits(top, {scope: p})

    return json.dumps({
        "results": top,
        "total": len(scored),
        "returned": len(top),
        "store": str(p),
        "scope": scope,
        "mode": "bm25",
    }, ensure_ascii=False)


# ============================================================================
# list_memories / delete_memory（合并到一个工具签名内）
# ============================================================================

def list_memories_tool(agent_id: Optional[str] = None, limit: int = 20) -> str:
    limit = max(1, min(int(limit or 20), 100))
    p = _resolve_kv_path(agent_id)
    records = _load_records(p)
    records.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return json.dumps({
        "results": records[:limit],
        "total": len(records),
        "returned": min(len(records), limit),
        "store": str(p),
    }, ensure_ascii=False)


def delete_memory_tool(
    memory_id: str,
    agent_id: Optional[str] = None,
    scope: str = "private",
) -> str:
    """删除记忆。scope='private'/'shared'：在指定 bucket 找；'auto'：先 private 再 shared。

    embedding 启用时会顺带从向量索引里清理对应条目，避免"jsonl 删了但向量还在"的孤儿。
    """
    if not memory_id:
        return json.dumps({"error": "memory_id required"})

    if scope == "auto":
        scopes_to_try = ["private", "shared"]
    elif scope in ("private", "shared"):
        scopes_to_try = [scope]
    else:
        return json.dumps({"error": f"invalid scope: {scope!r}"})

    last_err = None
    for s in scopes_to_try:
        p = _resolve_kv_path(agent_id, scope=s)
        records = _load_records(p)
        new_records = [r for r in records if r.get("id") != memory_id]
        if len(new_records) == len(records):
            last_err = f"memory not found in scope={s}: {memory_id}"
            continue
        _save_records(p, new_records)

        # 同步清理 embedding 索引（如果启用了）
        try:
            from backend.embedding_index import get_index
            bucket_key, bucket_dir = _resolve_embedding_bucket(agent_id, s)
            idx = get_index(bucket_key, bucket_dir)
            if idx is not None:
                idx.remove(memory_id)
        except Exception as exc:
            logger.warning("embedding index cleanup failed for %s: %s", memory_id, exc)

        return json.dumps({
            "success": True,
            "deleted_id": memory_id,
            "scope": s,
            "remaining": len(new_records),
        }, ensure_ascii=False)

    return json.dumps({"error": last_err or f"memory not found: {memory_id}"})


# ============================================================================
# handler
# ============================================================================

_CONTEXT_KEYS = {"session_id", "llm_client", "trace_id", "agent_id"}


def _drop(kwargs):
    return {k: v for k, v in kwargs.items() if k not in _CONTEXT_KEYS}


async def _handle_save_memory(**kwargs):
    args = _drop(kwargs)
    return await save_memory_async(
        content=args.get("content", ""),
        tags=args.get("tags"),
        agent_id=kwargs.get("agent_id"),
        memory_id=args.get("memory_id") or args.get("id"),
        title=args.get("title"),
        user_triggered=args.get("user_triggered"),
        scope=args.get("scope") or "private",
        temp=args.get("temp"),
        locked=args.get("locked"),
    )


async def _handle_recall_memory(**kwargs):
    args = _drop(kwargs)
    return await recall_memory_async(
        query=args.get("query"),
        tags=args.get("tags"),
        top_k=args.get("top_k", _DEFAULT_TOP_K),
        agent_id=kwargs.get("agent_id"),
        scope=args.get("scope") or "private",
        mode=args.get("mode") or "auto",
    )


def _handle_list_memories(**kwargs):
    args = _drop(kwargs)
    return list_memories_tool(
        agent_id=kwargs.get("agent_id"),
        limit=args.get("limit", 20),
    )


async def _handle_delete_memory(**kwargs):
    args = _drop(kwargs)
    # delete 不需要 await（无网络调用），但为了跟 save/recall handler 签名一致改 async
    return delete_memory_tool(
        memory_id=args.get("memory_id") or args.get("id", ""),
        agent_id=kwargs.get("agent_id"),
        scope=args.get("scope") or "auto",  # 默认 auto：先 private 找不到再试 shared
    )


# ============================================================================
# create_memory: 对齐 Cascade create_memory(Action) 接口的统一写工具
# ============================================================================

_CREATE_MEMORY_ACTIONS = {"create", "update", "delete"}


async def _handle_create_memory(**kwargs):
    args = _drop(kwargs)
    action = (args.get("action") or "").strip().lower()
    if action not in _CREATE_MEMORY_ACTIONS:
        return json.dumps({
            "error": f"action must be one of {sorted(_CREATE_MEMORY_ACTIONS)}, got: {action!r}"
        })

    agent_id = kwargs.get("agent_id")
    scope = args.get("scope") or "private"

    if action == "delete":
        mid = args.get("id") or args.get("memory_id") or ""
        if not mid:
            return json.dumps({"error": "id required for action='delete'"})
        # delete 默认 auto：用户/LLM 不知道在 private 还是 shared 时自动两边找
        return delete_memory_tool(memory_id=mid, agent_id=agent_id, scope=args.get("scope") or "auto")

    # create / update
    content = args.get("content") or ""
    if not content:
        return json.dumps({"error": "content required for action='create'/'update'"})

    if action == "update":
        mid = args.get("id") or args.get("memory_id")
        if not mid:
            return json.dumps({"error": "id required for action='update'"})
    else:
        mid = None  # create: 新建

    return await save_memory_async(
        content=content,
        tags=args.get("tags"),
        agent_id=agent_id,
        memory_id=mid,
        title=args.get("title"),
        user_triggered=args.get("user_triggered"),
        scope=scope,
        temp=args.get("temp"),
        locked=args.get("locked"),
    )


# ============================================================================
# v2: async 路径 — sync 之上叠加 embedding + 多 bucket + 可解释 match_reason
# ============================================================================

def _embed_text_for_memory(rec: Dict[str, Any]) -> str:
    """构造 embedding 输入文本：title + content + tags 拼接。

    标题往往是高质量摘要，tag 提供领域信号；空 title 时只用 content + tags。
    """
    parts = []
    title = (rec.get("title") or "").strip()
    if title:
        parts.append(title)
    parts.append(rec.get("content", ""))
    tags = rec.get("tags") or []
    if tags:
        parts.append(" ".join(tags))
    return "\n".join(parts).strip()


async def save_memory_async(
    content: str,
    tags: Optional[List[str]] = None,
    agent_id: Optional[str] = None,
    memory_id: Optional[str] = None,
    title: Optional[str] = None,
    user_triggered: Optional[bool] = None,
    scope: str = "private",
    temp: Optional[Any] = None,
    locked: Optional[bool] = None,
) -> str:
    """async save：sync 之上叠加 embedding 向量化。

    embedding 失败兜底：jsonl 已经写入，下次 recall BM25 仍然能命中；
    重新启用 embedding 后 reindex 路径会把这条补上向量。
    """
    # 先调 sync 写 jsonl（这一步必须成功，否则 embedding 也没意义）
    sync_result = save_memory_tool(
        content=content,
        tags=tags,
        agent_id=agent_id,
        memory_id=memory_id,
        title=title,
        user_triggered=user_triggered,
        scope=scope,
        temp=temp,
        locked=locked,
    )

    # 解析返回，拿 memory_id；error 路径直接透传
    try:
        result = json.loads(sync_result)
    except Exception:
        return sync_result
    if not result.get("success"):
        return sync_result

    rec = result.get("memory") or {}
    mid = rec.get("id")
    if not mid:
        return sync_result

    # 算 embedding 写索引（embedding 未启用时短路）
    embedding_status = "skipped (not enabled)"
    try:
        from backend.embedding_client import get_client
        from backend.embedding_index import get_index

        client = get_client()
        if client is not None:
            text = _embed_text_for_memory(rec)
            vec = await client.embed(text)
            if vec is not None:
                bucket_key, bucket_dir = _resolve_embedding_bucket(agent_id, scope)
                idx = get_index(bucket_key, bucket_dir)
                if idx is not None:
                    idx.add(mid, vec)
                    embedding_status = "indexed"
                else:
                    embedding_status = "skipped (index unavailable)"
            else:
                embedding_status = "skipped (embed returned None)"
    except Exception as exc:
        # 任何 embedding 异常都不能影响 save 主流程
        logger.warning("embedding indexing failed for %s: %s", mid, exc)
        embedding_status = f"failed: {exc}"

    # 把 embedding 状态注回返回 json
    result["embedding"] = embedding_status
    return json.dumps(result, ensure_ascii=False)


def _rrf_fuse(
    bm25_ranked: List[Dict[str, Any]],
    vector_ranked: List[tuple],
    records_by_id: Dict[str, Dict[str, Any]],
    k: int = 60,
) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion：融合 BM25 排名和 vector 排名。

    对每个候选 id：
        rrf_score = sum(1 / (k + rank_in_each_list))
    rank 从 1 开始；不在某个列表里 → 该项贡献为 0。
    业界默认 k=60，让靠后的 rank 权重快速衰减。

    bm25_ranked: List[record dict]，已按 BM25 score 降序
    vector_ranked: List[(memory_id, similarity)]，已按 sim 降序
    """
    fused: Dict[str, Dict[str, Any]] = {}

    for rank, rec in enumerate(bm25_ranked, start=1):
        mid = rec.get("id")
        if not mid:
            continue
        entry = fused.setdefault(mid, {
            "record": rec,
            "rrf_score": 0.0,
            "bm25_rank": None,
            "vector_rank": None,
            "vector_sim": None,
        })
        entry["bm25_rank"] = rank
        entry["rrf_score"] += 1.0 / (k + rank)

    for rank, (mid, sim) in enumerate(vector_ranked, start=1):
        if not mid:
            continue
        rec = records_by_id.get(mid)
        if rec is None:
            # vector 索引里有但 jsonl 已删 → 跳过（孤儿）
            continue
        entry = fused.setdefault(mid, {
            "record": rec,
            "rrf_score": 0.0,
            "bm25_rank": None,
            "vector_rank": None,
            "vector_sim": None,
        })
        entry["vector_rank"] = rank
        entry["vector_sim"] = float(sim)
        entry["rrf_score"] += 1.0 / (k + rank)

    # 按 rrf_score 降序
    return sorted(fused.values(), key=lambda x: x["rrf_score"], reverse=True)


async def recall_memory_async(
    query: Optional[str] = None,
    tags: Optional[List[str]] = None,
    top_k: int = _DEFAULT_TOP_K,
    agent_id: Optional[str] = None,
    scope: str = "private",
    mode: str = "auto",
) -> str:
    """async recall：可选向量召回 + RRF 融合 + match_reason。

    scope:
        'private' (默认) — 仅查 agent 私有
        'shared'         — 仅查跨角色共享
        'all'            — 私有 + 共享 RRF 融合

    mode:
        'auto'   (默认) — embedding 启用 → 混合；未启用 → BM25
        'bm25'         — 强制 BM25（调试用）
        'vector'       — 强制向量；未启用 → 空结果 + 提示
    """
    top_k = max(1, min(int(top_k or _DEFAULT_TOP_K), _MAX_TOP_K))
    query_tokens = [t for t in (query or "").lower().split() if t]
    required_tags = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]
    list_mode = not query_tokens and not required_tags

    if mode not in ("auto", "bm25", "vector"):
        return json.dumps({"error": f"invalid mode: {mode!r}. Use 'auto'/'bm25'/'vector'."})
    if scope not in ("private", "shared", "all"):
        return json.dumps({"error": f"invalid scope: {scope!r}. Use 'private'/'shared'/'all'."})

    # 决定要查哪些 bucket
    scopes = ["private", "shared"] if scope == "all" else [scope]

    # 多 bucket 加载 + 聚合：records_by_id 以 id 去重（理论上 private/shared 不会撞 id）
    all_records: List[Dict[str, Any]] = []
    records_by_id: Dict[str, Dict[str, Any]] = {}
    stores: List[str] = []
    for s in scopes:
        p = _resolve_kv_path(agent_id, scope=s)
        stores.append(str(p))
        for r in _load_records(p):
            mid = r.get("id")
            if not mid or mid in records_by_id:
                continue
            r = dict(r)  # 浅拷贝避免修改原始 jsonl 行
            r["_scope"] = s  # 标注来源 bucket，给 match_reason 用
            records_by_id[mid] = r
            all_records.append(r)

    # v5 (2026-10-24)：并入命名空间伪记录（全局层/项目卡/timeline）。
    # 只进 private 视角；id 前缀隔离不会与 KV 撞车；BM25 参与排名，
    # 向量索引里没有这些 id → vector 腿自然跳过（auto 模式靠 RRF 合并）。
    if scope in ("private", "all"):
        for r in _load_namespace_records(agent_id):
            if r["id"] in records_by_id:
                continue
            r["_scope"] = "private"
            records_by_id[r["id"]] = r
            all_records.append(r)
        stores.append("namespaces(global+cards+timeline)")

    if not all_records:
        return json.dumps({
            "results": [],
            "total": 0,
            "returned": 0,
            "stores": stores,
            "scope": scope,
            "mode": mode,
        }, ensure_ascii=False)

    # 空 query+tags → list 模式（按时间倒序）
    if list_mode:
        all_records.sort(key=lambda r: r.get("ts", 0), reverse=True)
        top = all_records[:top_k]
        return json.dumps({
            "results": top,
            "total": len(all_records),
            "returned": len(top),
            "mode": "list",
            "stores": stores,
            "scope": scope,
        }, ensure_ascii=False)

    # ── BM25 排名（永远算，作为 vector 兜底 + RRF 输入）──
    bm25_scored: List[tuple] = []
    for rec in all_records:
        s = _score(rec, query_tokens, required_tags)
        if s > 0:
            bm25_scored.append((s, rec))
    bm25_scored.sort(key=lambda x: (x[0], x[1].get("ts", 0)), reverse=True)
    bm25_ranked: List[Dict[str, Any]] = [rec for _, rec in bm25_scored]

    # ── vector 召回（只在 mode=auto/vector 且 embedding 就绪时跑）──
    vector_ranked: List[tuple] = []
    vector_status = "not_attempted"
    if mode in ("auto", "vector") and query:
        try:
            from backend.embedding_client import get_client
            from backend.embedding_index import get_index

            client = get_client()
            if client is None:
                vector_status = "embedding_disabled"
            else:
                qvec = await client.embed(query)
                if qvec is None:
                    vector_status = "embed_query_failed"
                else:
                    # 多 bucket 时分别 search 后合并
                    merged: Dict[str, float] = {}
                    for s in scopes:
                        bucket_key, bucket_dir = _resolve_embedding_bucket(agent_id, s)
                        idx = get_index(bucket_key, bucket_dir)
                        if idx is None:
                            continue
                        # 多取一倍候选给 RRF
                        for mid, sim in idx.search(qvec, top_k=top_k * 2):
                            # 同 id 取最高分（理论不会重）
                            if mid not in merged or sim > merged[mid]:
                                merged[mid] = sim
                    vector_ranked = sorted(merged.items(), key=lambda x: x[1], reverse=True)
                    vector_status = f"hits={len(vector_ranked)}"
        except Exception as exc:
            logger.warning("vector recall failed: %s", exc)
            vector_status = f"failed: {exc}"

    # 准备 bucket_paths 给 _record_recall_hits 写盘用
    bucket_paths = {s: _resolve_kv_path(agent_id, scope=s) for s in scopes}

    def _temp_meta(rec: Dict[str, Any]) -> Dict[str, Any]:
        """match_reason 里要展示的温度元信息。"""
        t = rec.get("temp", _TEMP_COLD) or _TEMP_COLD
        return {
            "temp": _TEMP_NAMES.get(t, "cold"),
            "temp_icon": _TEMP_LABELS.get(t, "❄️"),
            "recall_count": int(rec.get("recall_count", 0) or 0),
            "locked": bool(rec.get("locked")),
        }

    # ── 决定最终输出 ──
    if mode == "vector":
        if not vector_ranked:
            return json.dumps({
                "results": [],
                "total": 0,
                "returned": 0,
                "mode": "vector",
                "vector_status": vector_status,
                "scope": scope,
                "stores": stores,
                "hint": "vector mode requires embedding enabled and configured. Use mode='auto' for graceful fallback.",
            }, ensure_ascii=False)
        # 温度加权重排：burning 记忆排前
        vector_weighted = sorted(
            ((mid, sim, sim * _temp_weight(records_by_id.get(mid, {}))) for mid, sim in vector_ranked),
            key=lambda x: x[2],
            reverse=True,
        )
        out = []
        for rank, (mid, sim, _) in enumerate(vector_weighted[:top_k], start=1):
            rec = records_by_id.get(mid)
            if rec is None:
                continue
            r = dict(rec)
            r["match_reason"] = {
                "vector_sim": round(sim, 4),
                "vector_rank": rank,
                "tag_match": [t for t in (rec.get("tags") or []) if t in required_tags] if required_tags else [],
                "scope": rec.get("_scope"),
                **_temp_meta(rec),
            }
            out.append(r)
        _record_recall_hits(out, bucket_paths)
        return json.dumps({
            "results": out,
            "total": len(vector_ranked),
            "returned": len(out),
            "mode": "vector",
            "vector_status": vector_status,
            "scope": scope,
            "stores": stores,
        }, ensure_ascii=False)

    if mode == "bm25" or not vector_ranked:
        # 纯 BM25：温度加权重排
        # bm25_ranked 是按 score 降序的 List[record]，这里用 weighted score 重排
        bm25_weighted = sorted(
            (
                (rec, _score(rec, query_tokens, required_tags) * _temp_weight(rec))
                for rec in bm25_ranked
            ),
            key=lambda x: x[1],
            reverse=True,
        )
        top = []
        for rank, (rec, _) in enumerate(bm25_weighted[:top_k], start=1):
            r = dict(rec)
            r["match_reason"] = {
                "bm25_rank": rank,
                "tag_match": [t for t in (rec.get("tags") or []) if t in required_tags] if required_tags else [],
                "scope": rec.get("_scope"),
                "vector_status": vector_status,
                **_temp_meta(rec),
            }
            top.append(r)
        _record_recall_hits(top, bucket_paths)
        return json.dumps({
            "results": top,
            "total": len(bm25_ranked),
            "returned": len(top),
            "mode": "bm25" if mode == "bm25" else "bm25_fallback",
            "vector_status": vector_status,
            "scope": scope,
            "stores": stores,
        }, ensure_ascii=False)

    # mode='auto' 且 vector + bm25 都有 → RRF 融合 + 温度加权
    fused = _rrf_fuse(bm25_ranked, vector_ranked, records_by_id, k=60)
    # 温度加权：rrf_score *= temp_weight，重排
    for entry in fused:
        entry["weighted_rrf"] = entry["rrf_score"] * _temp_weight(entry["record"])
    fused.sort(key=lambda e: e["weighted_rrf"], reverse=True)
    out = []
    for entry in fused[:top_k]:
        rec = entry["record"]
        r = dict(rec)
        r["match_reason"] = {
            "bm25_rank": entry["bm25_rank"],
            "vector_rank": entry["vector_rank"],
            "vector_sim": round(entry["vector_sim"], 4) if entry["vector_sim"] is not None else None,
            "rrf_score": round(entry["rrf_score"], 6),
            "weighted_rrf": round(entry["weighted_rrf"], 6),
            "tag_match": [t for t in (rec.get("tags") or []) if t in required_tags] if required_tags else [],
            "scope": rec.get("_scope"),
            **_temp_meta(rec),
        }
        out.append(r)
    _record_recall_hits(out, bucket_paths)

    return json.dumps({
        "results": out,
        "total": len(fused),
        "returned": len(out),
        "mode": "hybrid_rrf",
        "vector_status": vector_status,
        "scope": scope,
        "stores": stores,
    }, ensure_ascii=False)
