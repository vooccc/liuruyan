"""memory_audit：自我整理记忆系统。

价值定位：
    柳如烟用得越久，KV jsonl 里的记忆越多。没有人主动清理 → 几个月后变成
    "1500 条记忆，95% 是过期的实验"。Cascade 的 create_memory 没自带清理工具，
    全靠人肉。这个工具让记忆系统**自我整理**：

    - 找语义重复（向量相似 > 0.92）→ 报告 + 可选合并
    - 找冷记忆（超 90 天没被 recall + temp=cold + 非 locked）→ 报告 + 可选删除
    - 衰减温度（warm → cold / burning → warm，按 last_recalled_at）→ 应用
    - 输出汇总：建议 LLM 接下来该做什么（合并 X 条 / 删 Y 条 / N 条已自动衰减）

调用模式：
    - actions=[]               只检测不动手（report only）
    - actions=['decay']        应用温度衰减（默认 14 天阈值）
    - actions=['decay','dedupe'] 衰减 + 合并明显重复（保留较新）
    - actions=['decay','prune'] 衰减 + 删除超过 cutoff 天的 cold 记忆

设计原则：
    - 默认 dry-run：actions=[] 只返建议清单，让 LLM/用户审视后再决定
    - 删除/合并都不可逆，所以 dry-run 是安全网
    - dedupe 必须有 embedding（向量相似度判定）；没启用时跳过该 action

不在这里做的事：
    - 不主动跑 reindex（embedding 索引一致性由 reindex 工具维护）
    - 不修改 jsonl 之外的内容（MEMORY.md / 反思文件 等）
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# 默认参数
_DEFAULT_DEDUPE_THRESHOLD = 0.92   # 向量余弦相似度 ≥ 此值视为重复
_DEFAULT_PRUNE_DAYS = 90           # 超过 N 天没 recall + cold + 非 locked → 候选删除
_DEFAULT_DECAY_DAYS = 14           # 温度衰减阈值
_MAX_REPORT_ITEMS = 50             # 报告里的样本数上限（避免炸 prompt）

_VALID_ACTIONS = {"decay", "dedupe", "prune"}
_VALID_SCOPES = {"private", "shared", "all"}


def _scopes_to_buckets(
    scope: str,
    agent_id: Optional[str],
) -> List[Tuple[str, Path]]:
    """根据 scope 决定要处理哪些 bucket，返回 [(scope_name, kv_path), ...]"""
    from .agent_memory_tool import _resolve_kv_path

    if scope == "all":
        return [
            ("private", _resolve_kv_path(agent_id, scope="private")),
            ("shared", _resolve_kv_path(agent_id, scope="shared")),
        ]
    if scope in ("private", "shared"):
        return [(scope, _resolve_kv_path(agent_id, scope=scope))]
    raise ValueError(f"invalid scope: {scope!r}")


def _find_cold_records(
    records: List[Dict[str, Any]],
    cutoff_days: int,
    now_ts: float,
) -> List[Dict[str, Any]]:
    """找超过 cutoff_days 没被 recall + cold + 非 locked 的记忆。"""
    threshold = cutoff_days * 86400
    cold = []
    for r in records:
        if r.get("locked"):
            continue
        temp = r.get("temp") or 1
        if temp != 1:  # 非 cold（warm/burning）跳过
            continue
        last = r.get("last_recalled_at") or r.get("ts") or now_ts
        if now_ts - last >= threshold:
            cold.append(r)
    return cold


async def _find_duplicates_via_embedding(
    records: List[Dict[str, Any]],
    bucket_key: str,
    bucket_dir: Path,
    threshold: float,
) -> List[Tuple[str, str, float]]:
    """用向量索引找语义重复对，返回 [(id_a, id_b, similarity), ...]，按相似度降序。

    实现策略：
    - 对每条 record，用其向量在索引里找 topK=10 邻居
    - 如果邻居 sim ≥ threshold 且 id 不同，记录这对（去重 a<b 形式）
    - 没启用 embedding 时返空列表（调用方应当跳过 dedupe action）
    """
    try:
        from backend.embedding_index import get_index
    except Exception:
        return []

    idx = get_index(bucket_key, bucket_dir)
    if idx is None:
        return []

    # idx 没暴露按 id 取 vec 的接口；直接 search 每条记录的内容是个慢方法。
    # 但 audit 是低频操作（用户主动触发），N<1000 时几秒内能跑完，可接受。
    # 这里走"重新算每条记忆的 embedding 然后 search"——但这是 expensive。
    #
    # 替代方案：直接读 idx 的 _vectors 矩阵做对偶相似度（O(N²) 但纯 numpy 快）。
    try:
        idx._load_lazy()
        if idx._vectors is None or len(idx._ids) < 2:
            return []
        import numpy as np

        vectors = idx._vectors  # shape (N, dim)，已 L2 归一化
        # 对偶矩阵 N×N，对角线 = 1（自己跟自己）
        sim_matrix = vectors @ vectors.T
        # 取上三角 i<j 的 (i, j, sim) 满足 sim >= threshold
        n = sim_matrix.shape[0]
        ids = idx._ids
        pairs: List[Tuple[str, str, float]] = []
        for i in range(n):
            for j in range(i + 1, n):
                s = float(sim_matrix[i, j])
                if s >= threshold:
                    pairs.append((ids[i], ids[j], s))
        # 相似度降序
        pairs.sort(key=lambda x: x[2], reverse=True)
        return pairs
    except Exception as exc:
        logger.warning("duplicate detection failed: %s", exc)
        return []


def _summary_of_record(r: Dict[str, Any]) -> Dict[str, Any]:
    """提取 record 的精简摘要供报告（不带向量、内容截断）。"""
    return {
        "id": r.get("id"),
        "title": r.get("title"),
        "content_preview": (r.get("content", "") or "")[:120],
        "tags": r.get("tags", []),
        "ts": r.get("ts"),
        "last_recalled_at": r.get("last_recalled_at"),
        "recall_count": r.get("recall_count", 0),
        "temp": r.get("temp", 1),
        "locked": bool(r.get("locked")),
    }


async def memory_audit_tool(
    scope: str = "private",
    agent_id: Optional[str] = None,
    actions: Optional[List[str]] = None,
    decay_days: int = _DEFAULT_DECAY_DAYS,
    prune_days: int = _DEFAULT_PRUNE_DAYS,
    dedupe_threshold: float = _DEFAULT_DEDUPE_THRESHOLD,
) -> str:
    """对记忆系统做一次审计。

    Args:
        scope: 'private' / 'shared' / 'all'
        agent_id: scope='private' 时指定哪个 agent 的私有记忆
        actions: 要执行的写操作子集 ⊆ {'decay', 'dedupe', 'prune'}；
                 空列表 / 不传 → dry-run 只检测
        decay_days: 14 天没 recall 的 warm 记忆降级
        prune_days: 90 天没 recall 的 cold 记忆候选删除
        dedupe_threshold: 向量相似度阈值（默认 0.92）
    """
    actions_set = set(actions or [])
    invalid = actions_set - _VALID_ACTIONS
    if invalid:
        return json.dumps({
            "error": f"invalid actions: {sorted(invalid)}. Must be subset of {sorted(_VALID_ACTIONS)}."
        })

    if scope not in _VALID_SCOPES:
        return json.dumps({"error": f"invalid scope: {scope!r}"})

    # 延迟 import 避免循环依赖（agent_memory_tool 可能 import 我们）
    from .agent_memory_tool import (
        _decay_cold_records,
        _load_records,
        _resolve_embedding_bucket,
        _save_records,
    )

    try:
        buckets = _scopes_to_buckets(scope, agent_id)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    now_ts = time.time()
    report: Dict[str, Any] = {
        "scope": scope,
        "agent_id": agent_id,
        "actions_applied": sorted(actions_set),
        "buckets": [],
    }

    total_cold = 0
    total_decayed = 0
    total_pruned = 0
    total_duplicate_pairs = 0

    for scope_name, kv_path in buckets:
        records = _load_records(kv_path)
        bucket_summary: Dict[str, Any] = {
            "scope": scope_name,
            "kv_path": str(kv_path),
            "total_records": len(records),
        }

        if not records:
            bucket_summary["empty"] = True
            report["buckets"].append(bucket_summary)
            continue

        # ── 衰减（修改 records，不删条目） ──
        decayed_count = 0
        if "decay" in actions_set:
            decayed_count = _decay_cold_records(records, decay_days=decay_days)
            total_decayed += decayed_count
        bucket_summary["decay_candidates"] = sum(
            1 for r in records
            if not r.get("locked")
            and not r.get("temp_user_set")
            and (r.get("temp") or 1) > 1
            and (now_ts - (r.get("last_recalled_at") or r.get("ts") or now_ts)) >= decay_days * 86400
        )
        bucket_summary["decayed"] = decayed_count

        # ── 冷记忆候选（90 天没 recall + cold + 非 locked）──
        cold = _find_cold_records(records, cutoff_days=prune_days, now_ts=now_ts)
        bucket_summary["cold_count"] = len(cold)
        bucket_summary["cold_samples"] = [_summary_of_record(r) for r in cold[:_MAX_REPORT_ITEMS]]
        total_cold += len(cold)

        # ── 删除冷记忆 ──
        pruned_count = 0
        if "prune" in actions_set and cold:
            cold_ids: Set[str] = {r["id"] for r in cold if r.get("id")}
            kept = [r for r in records if r.get("id") not in cold_ids]
            pruned_count = len(records) - len(kept)
            records = kept
            total_pruned += pruned_count
            # 同时清 embedding 索引里对应条目
            try:
                from backend.embedding_index import get_index
                bucket_key, bucket_dir = _resolve_embedding_bucket(agent_id, scope_name)
                idx = get_index(bucket_key, bucket_dir)
                if idx is not None:
                    for cid in cold_ids:
                        idx.remove(cid)
            except Exception as exc:
                logger.warning("embedding cleanup after prune failed: %s", exc)
        bucket_summary["pruned"] = pruned_count

        # ── 重复检测（用向量索引）──
        duplicate_pairs: List[Tuple[str, str, float]] = []
        try:
            bucket_key, bucket_dir = _resolve_embedding_bucket(agent_id, scope_name)
            duplicate_pairs = await _find_duplicates_via_embedding(
                records, bucket_key, bucket_dir, dedupe_threshold,
            )
        except Exception as exc:
            logger.warning("dedupe detection failed: %s", exc)
        bucket_summary["duplicate_pairs_count"] = len(duplicate_pairs)
        bucket_summary["duplicate_pairs"] = [
            {"id_a": a, "id_b": b, "sim": round(s, 4)}
            for a, b, s in duplicate_pairs[:_MAX_REPORT_ITEMS]
        ]
        total_duplicate_pairs += len(duplicate_pairs)

        # ── 自动 dedupe（保留 recall_count 高的或较新的）──
        deduped_count = 0
        if "dedupe" in actions_set and duplicate_pairs:
            # 简单策略：每对里删 b（推荐方）保留 a。但用 recall_count + ts 做 tie-break：
            # - recall_count 高的留下（被想起更多次）
            # - 同 recall_count 时较新的（ts 大）留下
            records_by_id = {r["id"]: r for r in records if r.get("id")}
            to_delete: Set[str] = set()
            for a, b, _ in duplicate_pairs:
                ra = records_by_id.get(a)
                rb = records_by_id.get(b)
                if ra is None or rb is None or a in to_delete or b in to_delete:
                    continue
                # 选删除哪个
                if ra.get("locked") and not rb.get("locked"):
                    to_delete.add(b)
                elif rb.get("locked") and not ra.get("locked"):
                    to_delete.add(a)
                elif (ra.get("recall_count", 0) or 0) > (rb.get("recall_count", 0) or 0):
                    to_delete.add(b)
                elif (rb.get("recall_count", 0) or 0) > (ra.get("recall_count", 0) or 0):
                    to_delete.add(a)
                elif (ra.get("ts", 0) or 0) >= (rb.get("ts", 0) or 0):
                    to_delete.add(b)
                else:
                    to_delete.add(a)
            if to_delete:
                records = [r for r in records if r.get("id") not in to_delete]
                deduped_count = len(to_delete)
                # 同步清 embedding 索引
                try:
                    from backend.embedding_index import get_index
                    bucket_key, bucket_dir = _resolve_embedding_bucket(agent_id, scope_name)
                    idx = get_index(bucket_key, bucket_dir)
                    if idx is not None:
                        for did in to_delete:
                            idx.remove(did)
                except Exception as exc:
                    logger.warning("embedding cleanup after dedupe failed: %s", exc)
        bucket_summary["deduped"] = deduped_count

        # ── 写盘（仅当有 action 改动）──
        if actions_set & {"decay", "prune", "dedupe"} and (decayed_count or pruned_count or deduped_count):
            try:
                _save_records(kv_path, records)
            except Exception as exc:
                logger.warning("save records after audit failed (%s): %s", kv_path, exc)
                bucket_summary["save_error"] = str(exc)

        bucket_summary["records_after"] = len(records)
        report["buckets"].append(bucket_summary)

    # 顶层汇总
    report["summary"] = {
        "total_cold": total_cold,
        "total_decayed": total_decayed,
        "total_pruned": total_pruned,
        "total_duplicate_pairs": total_duplicate_pairs,
        "advice": _build_advice(actions_set, total_cold, total_duplicate_pairs, total_decayed),
    }

    return json.dumps(report, ensure_ascii=False)


def _build_advice(
    actions_applied: Set[str],
    total_cold: int,
    total_duplicate_pairs: int,
    total_decayed: int,
) -> str:
    """根据扫描结果生成一段建议文本，给 LLM 看。"""
    parts: List[str] = []
    if not actions_applied:
        # dry-run
        suggestions: List[str] = []
        if total_decayed > 0 or total_cold > 0:
            suggestions.append("call again with actions=['decay'] to cool down stale warm/burning records")
        if total_duplicate_pairs > 0:
            suggestions.append(f"call again with actions=['dedupe'] to merge {total_duplicate_pairs} duplicate pairs")
        if total_cold > 5:
            suggestions.append(
                f"call again with actions=['prune'] to delete {total_cold} cold records (90+ days untouched)"
            )
        if not suggestions:
            return "Memory looks healthy — no audit actions needed."
        parts.append("Dry-run completed. Suggested next steps:")
        for i, s in enumerate(suggestions, 1):
            parts.append(f"  {i}. {s}")
    else:
        applied = sorted(actions_applied)
        parts.append(f"Audit completed (applied: {', '.join(applied)}).")
        if "decay" in actions_applied:
            parts.append(f"  - Decayed {total_decayed} stale warm/burning records.")
        if "prune" in actions_applied:
            parts.append(f"  - Found {total_cold} cold records (90+ days idle).")
        if "dedupe" in actions_applied:
            parts.append(f"  - Found {total_duplicate_pairs} duplicate pairs (vector sim ≥ threshold).")
    return "\n".join(parts)


# ============================================================================
# handler（dispatch 入口）
# ============================================================================

_CONTEXT_KEYS = {"session_id", "llm_client", "trace_id", "agent_id"}


def _drop(kwargs):
    return {k: v for k, v in kwargs.items() if k not in _CONTEXT_KEYS}


async def _handle_memory_audit(**kwargs):
    args = _drop(kwargs)
    return await memory_audit_tool(
        scope=args.get("scope") or "private",
        agent_id=kwargs.get("agent_id"),
        actions=args.get("actions") or [],
        decay_days=int(args.get("decay_days", _DEFAULT_DECAY_DAYS)),
        prune_days=int(args.get("prune_days", _DEFAULT_PRUNE_DAYS)),
        dedupe_threshold=float(args.get("dedupe_threshold", _DEFAULT_DEDUPE_THRESHOLD)),
    )


__all__ = ["memory_audit_tool", "_handle_memory_audit"]
