"""session_search：跨会话搜索历史对话。

价值定位：
    柳如烟用户经常问："那个上次的 bug 怎么修的？" / "你说过 X 是什么意思来着？"
    当前柳如烟只能在当前 session 里走 transcript.recall_snippets，跨 session 无能为力。
    本工具是 LLM 的"会话考古铲"——能在所有 session 的 transcript 里 BM25 检索。

定位 vs 其它工具：
    - recall_memory：搜 KV jsonl 长期记忆（agent 主动存的"我应该记住"）
    - session_search（本工具）：搜原始对话历史（"我们当时是怎么说的"）
    - 两者互补：recall_memory 是经过提炼的，session_search 是原始上下文

实现策略（读取层）：
    - scope='self': 只搜当前 agent 的 main sessions（默认）
    - scope='all': 所有 agent main sessions + 议事厅 rooms
    - scope='rooms': 只搜议事厅 sessions（多角色协作历史）
    - 不需要 SessionManager 全部 load 到内存——直接扫盘读 transcript.jsonl
      （避免触发 SessionManager._load_all_sessions 改全局 state）
    - 自动跳过 worker session（noisy 中间产物）

评分（BM25-like）：
    - exact substring match: +3 (短词 <4 char) / +6 (长词 ≥4 char)
    - 用户 query 多个关键词时各自累加
    - 最近 N 天加权 +2（recency bias）
    - 同 session 内取 top max_per_session 条避免 dump

不做的事：
    - 不走 embedding（首版）：跨 session 量级大，BM25 + recency 加权已 covers 90%
      如果未来需要可叠加 embedding（embedding_index 索引每个 transcript）
    - 不修改 session 状态（read-only，CONCURRENCY_READ）
    - 不返回 system 消息 / tool_call / tool_result（只人话片段）
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 默认参数（2026-05-14 token 克制调整）
#   max_results: 10→5（避免 dump 太多 session）
#   max_per_session: 保持 2（一问一答的对话颗粒度，LLM 一次看完不追问）
#   content preview: 240→160（结合智能截取信息密度提高）
_DEFAULT_MAX_RESULTS = 5
_DEFAULT_MAX_PER_SESSION = 2
_DEFAULT_RECENCY_DAYS = 7
_RECENCY_BONUS = 2
_MAX_CONTENT_PREVIEW = 160
_SMART_TRUNCATE_RADIUS = 80  # 命中位置前后各取 N 字
_MAX_QUERY_LENGTH = 200
_VALID_SCOPES = {"self", "all", "rooms"}
_VALID_ROLES = {"user", "assistant"}


# 人话文本提取的唯一真源在 transcript_embedding.extract_text —— session_search
# （BM25 片段 + 向量索引）与自动回忆（语义召回）共用，保证"可索引消息"切分口径
# 一致，否则共享同一向量索引会序号错位。这里 re-export 保持本模块内旧调用名不变。
from backend.transcript_embedding import extract_text as _safe_extract_text


def _tokenize_query(query: str) -> List[str]:
    """简单 tokenize：保留 ≥2 char 的连续字符或汉字串。"""
    if not query:
        return []
    # 拆为 unicode 词块；中文按 1-3 字 n-gram 也不做（用户原词命中率足够）
    raw = re.findall(r"[\w\u4e00-\u9fff]+", query.lower())
    seen: List[str] = []
    seen_set = set()
    for token in raw:
        if len(token) >= 2 and token not in seen_set:
            seen.append(token)
            seen_set.add(token)
    return seen


def _score_message(
    text: str,
    keywords: List[str],
    ts: Optional[float],
    now_ts: float,
    recency_days: int,
) -> int:
    """对一条 transcript 消息打分。"""
    if not text or not keywords:
        return 0
    text_low = text.lower()
    score = 0
    for kw in keywords:
        if kw in text_low:
            score += 6 if len(kw) >= 4 else 3
    if score == 0:
        return 0
    # 最近 N 天加 recency bonus
    if ts and recency_days > 0:
        if (now_ts - ts) <= recency_days * 86400:
            score += _RECENCY_BONUS
    return score


def _ts_to_epoch(raw: Any) -> Optional[float]:
    """ISO 时间戳 → epoch float。失败返 None。"""
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return None
    return None


def _read_session_messages(transcript_path: Path) -> List[Dict[str, Any]]:
    """读 transcript.jsonl 的 message 记录（不抛错）。"""
    if not transcript_path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "message":
                    out.append(rec)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("session_search read failed (%s): %s", transcript_path, exc)
    return out


def _read_session_meta(session_dir: Path) -> Optional[Dict[str, Any]]:
    """读 session.json 元数据；失败返 None。"""
    meta_path = session_dir / "session.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _enumerate_session_dirs(
    scope: str,
    agent_id: Optional[str],
) -> List[Tuple[Path, str]]:
    """枚举要扫的 session 目录，返回 [(session_dir, kind), ...]。

    kind: 'agent_main' / 'room' （worker / nested 不在跨会话搜索范围）
    """
    from backend import paths

    dirs: List[Tuple[Path, str]] = []

    if scope in ("self", "all"):
        agents_root = paths.agents_dir()
        if agents_root.exists():
            target_agents: List[Path]
            if scope == "self" and agent_id:
                # 只扫该 agent 的目录
                aroot = agents_root / agent_id
                target_agents = [aroot] if aroot.exists() else []
            else:
                # all 或 self 没指定 agent_id（兜底也只扫所有 agent main，不含 room）
                target_agents = [d for d in agents_root.iterdir() if d.is_dir()]

            for agent_dir in target_agents:
                sessions_dir = agent_dir / "sessions"
                if not sessions_dir.exists():
                    continue
                for sdir in sessions_dir.iterdir():
                    if not sdir.is_dir():
                        continue
                    # main session（直系子目录），不下钻 workers/
                    if (sdir / "session.json").exists():
                        dirs.append((sdir, "agent_main"))

    if scope in ("rooms", "all"):
        rooms_root = paths.council_rooms_dir()
        if rooms_root.exists():
            for room_dir in rooms_root.iterdir():
                if not room_dir.is_dir():
                    continue
                sessions_dir = room_dir / "sessions"
                if not sessions_dir.exists():
                    continue
                for sdir in sessions_dir.iterdir():
                    if not sdir.is_dir():
                        continue
                    if (sdir / "session.json").exists():
                        dirs.append((sdir, "room"))

    return dirs


def _smart_truncate(text: str, keywords: List[str]) -> str:
    """
    智能截取：围绕首个命中关键词位置取前后 _SMART_TRUNCATE_RADIUS 字。

    对比粗暴 text[:160]：保证命中词能被 LLM 看到 + 信息密度高。
    命中词用 ** 高亮让 LLM 一眼定位。
    """
    if not text:
        return ""
    text_low = text.lower()
    # 找首个命中位置（取最小 offset）
    first_hit = -1
    hit_kw = ""
    for kw in keywords:
        idx = text_low.find(kw)
        if idx >= 0 and (first_hit == -1 or idx < first_hit):
            first_hit = idx
            hit_kw = kw

    if first_hit == -1:
        # 没有命中（理论上 不应该走到这，但入初后防御性 fallback）
        return text[:_MAX_CONTENT_PREVIEW] + ("…" if len(text) > _MAX_CONTENT_PREVIEW else "")

    # 计算窗口
    start = max(0, first_hit - _SMART_TRUNCATE_RADIUS)
    end = min(len(text), first_hit + len(hit_kw) + _SMART_TRUNCATE_RADIUS)
    snippet = text[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""

    # 高亮所有命中 keyword（不只首个）
    # 不用正则免得有 escape 坑，手动 case-insensitive replace。
    for kw in keywords:
        if not kw:
            continue
        idx = 0
        snippet_low = snippet.lower()
        out = []
        while idx < len(snippet):
            found = snippet_low.find(kw, idx)
            if found == -1:
                out.append(snippet[idx:])
                break
            out.append(snippet[idx:found])
            out.append("**" + snippet[found:found + len(kw)] + "**")
            idx = found + len(kw)
        snippet = "".join(out)

    return prefix + snippet + suffix


def _format_snippet(
    msg: Dict[str, Any],
    score: int,
    keywords: List[str],
) -> Optional[Dict[str, Any]]:
    """
    把 transcript message 转成 LLM-friendly snippet。

    2026-05-14 token 克制改造：
      - 采用 _smart_truncate（命中位置 ±N 字 + 高亮）
      - 删除 score 字段（LLM 看不懂分数绝对意义，排序在服务端做完即可）
    """
    role = msg.get("role")
    if role not in _VALID_ROLES:
        return None
    text = _safe_extract_text(msg.get("content")).strip()
    if not text:
        return None
    return {
        "role": role,
        "content": _smart_truncate(text, keywords),
        "timestamp": msg.get("timestamp"),
    }


async def _bm25_only(
    query: str,
    scope: str = "self",
    agent_id: Optional[str] = None,
    max_results: int = _DEFAULT_MAX_RESULTS,
    max_per_session: int = _DEFAULT_MAX_PER_SESSION,
    recency_days: int = _DEFAULT_RECENCY_DAYS,
    days_back: Optional[int] = None,
    role: Optional[str] = None,
) -> str:
    """纯 BM25（词法）跨会话检索 —— embedding 未就绪时的默认路径。

    P4 前这就是 session_search_tool 的全部实现；P4 把它原样保留为降级路径，
    embedding 就绪时由 ``session_search_tool`` 入口改走 ``_hybrid_search``。
    含完整输入校验，所以入口可以放心把未就绪/降级的请求直接转给它。

    搜索跨会话的历史对话。

    Args:
        query: 关键词（必填）
        scope: 'self' / 'all' / 'rooms'
        agent_id: scope='self' 时绑定的角色（context 自动注入）
        max_results: 最多返回多少个 session（默认 10）
        max_per_session: 同一 session 内最多 N 条片段（默认 2）
        recency_days: 给最近 N 天的命中加 +2 分（默认 7）
        days_back: 只搜最近 N 天创建的 session（默认 None 不限）
        role: 只匹配某 role 的消息（'user' / 'assistant'，None 都搜）
    """
    query = (query or "").strip()
    if not query:
        return json.dumps({"error": "query is required (non-empty string)"})
    if len(query) > _MAX_QUERY_LENGTH:
        return json.dumps({"error": f"query too long (max {_MAX_QUERY_LENGTH} chars)"})

    if scope not in _VALID_SCOPES:
        return json.dumps({"error": f"invalid scope: {scope!r}. Must be one of {sorted(_VALID_SCOPES)}"})

    if role and role not in _VALID_ROLES:
        return json.dumps({"error": f"invalid role: {role!r}. Must be one of {sorted(_VALID_ROLES)}"})

    keywords = _tokenize_query(query)
    if not keywords:
        return json.dumps({"error": "query has no usable tokens (need ≥2 char words)"})

    now_ts = time.time()
    cutoff_ts: Optional[float] = (now_ts - days_back * 86400) if days_back else None

    session_dirs = _enumerate_session_dirs(scope, agent_id)
    total_sessions = len(session_dirs)

    # session_id → {meta, scored_messages}
    per_session_hits: List[Dict[str, Any]] = []

    for session_dir, kind in session_dirs:
        meta = _read_session_meta(session_dir)
        if meta is None:
            continue

        # session_type 过滤：跳过 worker
        if meta.get("session_type") == "worker":
            continue

        # days_back 过滤：用 last_active_at 或 updated_at
        if cutoff_ts is not None:
            last_active = _ts_to_epoch(meta.get("last_active_at") or meta.get("updated_at"))
            if last_active is not None and last_active < cutoff_ts:
                continue

        msgs = _read_session_messages(session_dir / "transcript.jsonl")
        if not msgs:
            continue

        scored_in_session: List[Dict[str, Any]] = []
        for m in msgs:
            mrole = m.get("role")
            if mrole not in _VALID_ROLES:
                continue
            if role and mrole != role:
                continue
            text = _safe_extract_text(m.get("content"))
            ts_epoch = _ts_to_epoch(m.get("timestamp"))
            score = _score_message(text, keywords, ts_epoch, now_ts, recency_days)
            if score <= 0:
                continue
            snippet = _format_snippet(m, score, keywords)
            if snippet is None:
                continue
            # 临时携带 score 用于排序，输出前在 _strip_internal 里去掉
            snippet["__score"] = score
            scored_in_session.append(snippet)

        if not scored_in_session:
            continue

        # 取 session 内 top N（默认 1）
        scored_in_session.sort(key=lambda s: s["__score"], reverse=True)
        top_snippets = scored_in_session[:max_per_session]
        session_top_score = top_snippets[0]["__score"]

        # 合并 source 字段："agent:liuruyan" / "room:tech_review"
        if kind == "room":
            source = f"room:{session_dir.parent.parent.name}"
        else:
            source = f"agent:{meta.get('agent_id') or session_dir.parent.parent.name}"

        per_session_hits.append({
            "session_id": meta.get("id") or session_dir.name,
            "title": meta.get("title") or "未命名会话",
            "source": source,
            "last_active_at": meta.get("last_active_at"),
            "__session_score": session_top_score,
            "snippets": top_snippets,
        })

    # 跨 session 排序（按 __session_score 降序，tie-break 用 last_active_at 较新优先）
    per_session_hits.sort(
        key=lambda h: (
            -h["__session_score"],
            -(_ts_to_epoch(h.get("last_active_at")) or 0),
        )
    )

    results = per_session_hits[:max_results]

    # 去掉内部排序字段，输出给 LLM 的东西越精越好
    for r in results:
        r.pop("__session_score", None)
        for s in r.get("snippets", []):
            s.pop("__score", None)

    # 输出顶层字段瘦身：只留 returned/results + 空结果时的 advice。
    # 省掉 query/scope/keywords/total_sessions_searched/total_sessions_with_hits
    # （LLM 看不到上下文本身反而加噪音）
    payload: Dict[str, Any] = {
        "returned": len(results),
        "results": results,
    }
    advice = _build_advice(query, scope, len(results), len(per_session_hits))
    if advice:
        payload["advice"] = advice
    return json.dumps(payload, ensure_ascii=False)


async def session_search_tool(
    query: str,
    scope: str = "self",
    agent_id: Optional[str] = None,
    max_results: int = _DEFAULT_MAX_RESULTS,
    max_per_session: int = _DEFAULT_MAX_PER_SESSION,
    recency_days: int = _DEFAULT_RECENCY_DAYS,
    days_back: Optional[int] = None,
    role: Optional[str] = None,
) -> str:
    """跨会话历史对话检索入口（P4）。

    - embedding 就绪 + 基础参数合法 → 走 ``_hybrid_search``（BM25×向量 RRF 融合，
      能召回"换义/转述"的语义近似项）；混合内部任何失败（query 无法 embed 等）
      → 返回 None，回退纯 BM25。
    - embedding 未就绪 → 直接走 ``_bm25_only``（行为与 P4 前完全一致）。

    校验权威仍在 ``_bm25_only``：未就绪/降级/参数非法都由它统一处理并返错误，
    入口只在"明确合法"时才尝试混合，避免重复校验逻辑。
    """
    q = (query or "").strip()
    try:
        from backend.transcript_embedding import embedding_ready

        ready = embedding_ready()
    except Exception:  # noqa: BLE001
        ready = False

    if ready:
        keywords = _tokenize_query(q) if q else []
        params_ok = (
            bool(q)
            and len(q) <= _MAX_QUERY_LENGTH
            and scope in _VALID_SCOPES
            and (not role or role in _VALID_ROLES)
            and bool(keywords)
        )
        if params_ok:
            hybrid = await _hybrid_search(
                q, keywords, scope, agent_id,
                max_results, max_per_session, recency_days, days_back, role,
            )
            if hybrid is not None:
                return hybrid

    return await _bm25_only(
        query, scope, agent_id,
        max_results, max_per_session, recency_days, days_back, role,
    )


async def _hybrid_search(
    query: str,
    keywords: List[str],
    scope: str,
    agent_id: Optional[str],
    max_results: int,
    max_per_session: int,
    recency_days: int,
    days_back: Optional[int],
    role: Optional[str],
) -> Optional[str]:
    """BM25 × 向量 的 RRF 混合检索。embedding 不可用/query 无法 embed → 返 None（让入口降级）。

    懒索引：每个 session 第一次被搜到时把它的人话消息 embed 入库（单次调用有
    MAX_EMBED_PER_CALL 预算上限，跨会话首搜不卡死，剩余后续渐进补齐）。
    """
    from backend.transcript_embedding import (
        MAX_EMBED_PER_CALL,
        MAX_INDEX_MESSAGES_PER_SESSION,
        ensure_indexed,
        get_embedding_client,
        get_transcript_index,
        search_index,
    )

    client = await get_embedding_client()
    if client is None:
        return None
    try:
        query_vec = await client.embed(query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("session_search query embed failed: %s", exc)
        return None
    if query_vec is None:
        return None

    now_ts = time.time()
    cutoff_ts: Optional[float] = (now_ts - days_back * 86400) if days_back else None
    session_dirs = _enumerate_session_dirs(scope, agent_id)

    # 消息级候选注册表，key = (session_uid, ordinal)
    cand: Dict[Tuple[str, int], Dict[str, Any]] = {}
    bm25_ranked: List[Tuple[Tuple[str, int], int]] = []
    vec_ranked: List[Tuple[Tuple[str, int], float]] = []
    embed_budget = MAX_EMBED_PER_CALL
    vec_top_k = max(max_per_session * 3, 6)

    for session_dir, kind in session_dirs:
        meta = _read_session_meta(session_dir)
        if meta is None:
            continue
        if meta.get("session_type") == "worker":
            continue
        if cutoff_ts is not None:
            last_active = _ts_to_epoch(meta.get("last_active_at") or meta.get("updated_at"))
            if last_active is not None and last_active < cutoff_ts:
                continue

        msgs = _read_session_messages(session_dir / "transcript.jsonl")
        if not msgs:
            continue

        # 可索引人话消息（ordinal 稳定：transcript 追加写、老序号不变）
        human: List[Tuple[int, Dict[str, Any], str]] = []
        ord_i = 0
        for m in msgs:
            if m.get("role") not in _VALID_ROLES:
                continue
            text = _safe_extract_text(m.get("content"))
            if not text.strip():
                continue
            human.append((ord_i, m, text))
            ord_i += 1
        if not human:
            continue

        session_uid = str(session_dir)
        if kind == "room":
            source = f"room:{session_dir.parent.parent.name}"
        else:
            source = f"agent:{meta.get('agent_id') or session_dir.parent.parent.name}"
        sinfo = {
            "session_id": meta.get("id") or session_dir.name,
            "title": meta.get("title") or "未命名会话",
            "source": source,
            "last_active_at": meta.get("last_active_at"),
        }

        # ── BM25 分支 ──
        for ordinal, m, text in human:
            if role and m.get("role") != role:
                continue
            score = _score_message(text, keywords, _ts_to_epoch(m.get("timestamp")), now_ts, recency_days)
            if score > 0:
                key = (session_uid, ordinal)
                cand.setdefault(key, {"msg": m, "sinfo": sinfo})
                bm25_ranked.append((key, score))

        # ── 向量分支（懒索引 + 检索）──
        index = get_transcript_index(session_dir, sinfo["session_id"])
        if index is not None:
            items = [(str(o), t) for (o, _m, t) in human[:MAX_INDEX_MESSAGES_PER_SESSION]]
            used = await ensure_indexed(index, items, client, max(embed_budget, 0))
            embed_budget -= used
            for ordinal, sim in search_index(index, query_vec, vec_top_k):
                if ordinal < 0 or ordinal >= len(human):
                    continue
                _o, m, _t = human[ordinal]
                if role and m.get("role") != role:
                    continue
                key = (session_uid, ordinal)
                cand.setdefault(key, {"msg": m, "sinfo": sinfo})
                vec_ranked.append((key, sim))

    # ── RRF 融合（业界默认 k=60）──
    _K = 60
    bm25_ranked.sort(key=lambda x: x[1], reverse=True)
    vec_ranked.sort(key=lambda x: x[1], reverse=True)
    fused: Dict[Tuple[str, int], float] = {}
    for rank, (key, _s) in enumerate(bm25_ranked, start=1):
        fused[key] = fused.get(key, 0.0) + 1.0 / (_K + rank)
    for rank, (key, _sim) in enumerate(vec_ranked, start=1):
        fused[key] = fused.get(key, 0.0) + 1.0 / (_K + rank)

    ordered = sorted(fused.items(), key=lambda x: x[1], reverse=True)

    # ── 分组成 session（按融合分顺序，受 max_per_session / max_results 约束）──
    by_session: Dict[str, Dict[str, Any]] = {}
    results: List[Dict[str, Any]] = []
    for (session_uid, ordinal), _fscore in ordered:
        if session_uid not in by_session and len(by_session) >= max_results:
            continue
        info = cand.get((session_uid, ordinal))
        if info is None:
            continue
        snippet = _format_snippet(info["msg"], 0, keywords)
        if snippet is None:
            continue
        bucket = by_session.get(session_uid)
        if bucket is None:
            sinfo = info["sinfo"]
            bucket = {
                "session_id": sinfo["session_id"],
                "title": sinfo["title"],
                "source": sinfo["source"],
                "last_active_at": sinfo["last_active_at"],
                "snippets": [],
            }
            by_session[session_uid] = bucket
            results.append(bucket)
        if len(bucket["snippets"]) < max_per_session:
            bucket["snippets"].append(snippet)

    results = [s for s in results if s["snippets"]][:max_results]
    total_with_hits = len({k[0] for k in cand})

    payload: Dict[str, Any] = {"returned": len(results), "results": results}
    advice = _build_advice(query, scope, len(results), total_with_hits)
    if advice:
        payload["advice"] = advice
    return json.dumps(payload, ensure_ascii=False)


def _build_advice(query: str, scope: str, returned: int, total_with_hits: int) -> Optional[str]:
    """
    只在需要引导 LLM 改变行为时返 advice，避免口水话咣噪。

    仅两种场景产 advice：
      1. 空结果 → 建议换 scope / 简化关键词
      2. 还有更多未返 → 说明 top N，调 max_results
    """
    if returned == 0:
        if scope == "self":
            return "No matches in your own sessions. Try scope='all' or simplify keywords."
        return f"No matches in scope={scope}. Try simpler keywords."
    if total_with_hits > returned:
        return f"{total_with_hits} sessions hit, showing top {returned}. Bump max_results to see more."
    return None


# ============================================================================
# handler
# ============================================================================

_CONTEXT_KEYS = {"session_id", "llm_client", "trace_id", "agent_id"}


def _drop(kwargs):
    return {k: v for k, v in kwargs.items() if k not in _CONTEXT_KEYS}


async def _handle_session_search(**kwargs):
    """
    入口 handler：接入 60s session 级缓存。

    缓存策略（参 @session_search_cache.py）：
      - cache key = sha1(normalized params)
      - sid 缺失 → 跳过缓存，直接打盘（脚本调用无害）
      - 命中 → 零 IO 零重复计算
      - miss → 跳原 logic 后写回
    """
    from backend.tools.session_search_cache import get_search_cache, compute_cache_key

    args = _drop(kwargs)
    session_id = kwargs.get("session_id")
    agent_id = kwargs.get("agent_id")

    # 规范化参数（为了 cache key 稳定）
    norm_payload = {
        "query": args.get("query", ""),
        "scope": args.get("scope") or "self",
        "agent_id": agent_id,
        "max_results": int(args.get("max_results", _DEFAULT_MAX_RESULTS)),
        "max_per_session": int(args.get("max_per_session", _DEFAULT_MAX_PER_SESSION)),
        "recency_days": int(args.get("recency_days", _DEFAULT_RECENCY_DAYS)),
        "days_back": int(args["days_back"]) if args.get("days_back") not in (None, "") else None,
        "role": args.get("role"),
    }

    cache = get_search_cache()
    cache_key = compute_cache_key(norm_payload)

    # 仅 sid 存在时走缓存（脚本/后台调用跳过）
    if session_id:
        cached = cache.get(session_id, cache_key)
        if cached is not None:
            return cached

    result_json = await session_search_tool(
        query=norm_payload["query"],
        scope=norm_payload["scope"],
        agent_id=norm_payload["agent_id"],
        max_results=norm_payload["max_results"],
        max_per_session=norm_payload["max_per_session"],
        recency_days=norm_payload["recency_days"],
        days_back=norm_payload["days_back"],
        role=norm_payload["role"],
    )

    # 只有有效结果才写缓存（error 结果不入缓，避免占着位子）
    if session_id and result_json and '"error"' not in result_json[:64]:
        cache.set(session_id, cache_key, result_json)

    return result_json


__all__ = ["session_search_tool", "_handle_session_search"]
