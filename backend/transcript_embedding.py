"""跨会话对话的 transcript 语义索引（P4，B-lazy 懒索引）。

定位（见 _docs/handover_context_defense_parity_2026_06_09.md 第八节）：
    session_search 原本是纯 BM25（词法）检索，用户"换义/转述提问"（与当年原话
    用词不同）时会漏召回。本模块给每个 session 的 transcript 建**向量索引**，让
    session_search 能在 BM25 之外叠加语义召回（RRF 融合）。

为什么 B-lazy（懒索引）而不是写盘即 embed：
    柳如烟是桌面单人应用，绝大多数老会话永远不会被回忆。写盘即 embed 会为"从不
    被搜的会话"白花 embedding 调用。懒索引只在某会话**第一次被 session_search 命中**
    时才把它的人话消息 embed 入库（并缓存），之后走索引。单次调用还有 embed 预算
    上限，避免 scope='all' 首搜时一次性把所有会话 embed 爆掉——剩余的在后续调用里
    渐进补齐，稳态后全部已索引、检索快。

硬约束（与 recall_memory 同口径）：
    - embedding 未配置/未就绪 → 本模块所有入口短路、返回空，session_search 退回纯 BM25。
    - 索引是衍生缓存：transcript.jsonl 是真相源，索引丢了能 rebuild（重新搜即重建）。
    - 只索引 user/assistant 的人话文本（tool_call/tool_result/system 不进索引）。

存储：<session_dir>/embeddings/{meta,vectors,ids}（复用 EmbeddingIndex），
     bucket_key = "transcript:<session_id>"。id = 人话消息在「可索引列表」里的序号
     （transcript 追加写、老序号稳定，所以序号可作稳定主键）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_INDEXABLE_ROLES = ("user", "assistant")

# 单 session 最多索引多少条人话消息（防超大会话一次性爆 embed；超出部分不索引，
# 仍可被 BM25 命中）。
MAX_INDEX_MESSAGES_PER_SESSION = 600
# 单次 session_search 调用最多 embed 多少条（懒索引摊销闸，跨会话首搜不卡死）。
MAX_EMBED_PER_CALL = 400


def extract_text(content: Any) -> str:
    """从 transcript content（str 或 list[block]）提取人话文本（唯一真源）。

    被 session_search（BM25 片段提取 + 索引）和自动回忆（语义召回）共用，保证
    "可索引消息"的切分口径完全一致 —— 否则两边算出的消息序号会错位、共享同一个
    向量索引时互相读错条目。

    兼容：纯字符串、老格式 assistant content=json.dumps(segments)、Anthropic
    content blocks（text / thinking 算人话；tool_use/tool_result/image 跳过）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    segs = obj.get("segments")
                    if isinstance(segs, list):
                        parts = [
                            s.get("content", "") for s in segs
                            if isinstance(s, dict) and s.get("type") in ("text", "thinking")
                        ]
                        joined = "\n".join(p for p in parts if p)
                        if joined:
                            return joined
                    if isinstance(obj.get("content"), str):
                        return obj["content"]
            except (json.JSONDecodeError, TypeError):
                pass
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "thinking":
                parts.append(str(block.get("thinking", "")))
        return "\n".join(p for p in parts if p)
    return ""


def _read_message_records(transcript_path: Path) -> List[Dict[str, Any]]:
    """读 transcript.jsonl 的 message 记录（不抛错）。"""
    p = Path(transcript_path)
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
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "message":
                    out.append(rec)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("read transcript failed (%s): %s", p, exc)
    return out


def build_indexable_messages(transcript_path: Path) -> List[Dict[str, Any]]:
    """构建 transcript 的「可索引人话消息」列表（向量索引的序号真源）。

    过滤：role ∈ {user, assistant} 且 extract_text 非空。``ordinal`` 是该消息在
    本列表里的位置 —— transcript 追加写、老消息位置不变，所以 ordinal 是稳定主键，
    可直接当 EmbeddingIndex 的 id。session_search 与自动回忆共用本函数 → 共用同一
    向量索引不会错位。

    Returns:
        [{"ordinal": int, "role": str, "text": str, "timestamp": Any, "raw": dict}, ...]
    """
    out: List[Dict[str, Any]] = []
    ordinal = 0
    for m in _read_message_records(transcript_path):
        if m.get("role") not in _INDEXABLE_ROLES:
            continue
        text = extract_text(m.get("content"))
        if not text.strip():
            continue
        out.append({
            "ordinal": ordinal,
            "role": m.get("role"),
            "text": text,
            "timestamp": m.get("timestamp"),
            "raw": m,
        })
        ordinal += 1
    return out


def embedding_ready() -> bool:
    """embedding 是否配置就绪（未就绪时 session_search 走纯 BM25）。"""
    try:
        from backend.embedding_config import get_config

        return get_config().is_ready()
    except Exception:  # noqa: BLE001 — 配置读失败一律当未就绪，绝不阻断检索
        return False


def get_transcript_index(session_dir: Path, session_id: str):
    """拿该 session 的 transcript 向量索引（embedding 未就绪返 None）。"""
    try:
        from backend.embedding_index import get_index

        bucket_key = f"transcript:{session_id}"
        bucket_dir = Path(session_dir) / "embeddings"
        return get_index(bucket_key, bucket_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_transcript_index failed (%s): %s", session_id, exc)
        return None


async def ensure_indexed(
    index,
    items: List[Tuple[str, str]],
    client,
    budget: int,
) -> int:
    """对 items=[(id, text)] 里尚未入库的，按 ``budget`` 上限 embed + add。

    Args:
        index: EmbeddingIndex（None 时直接返 0）。
        items: (id_str, text) 列表，id 须稳定（人话消息序号）。
        client: EmbeddingClient（None 时返 0）。
        budget: 本次最多 embed 多少条（API 调用预算）。

    Returns:
        本次实际成功入库（拿到向量）的条数。budget 只在真正发起 embed 调用时消耗。
    """
    if index is None or client is None or budget <= 0 or not items:
        return 0
    embedded = 0
    remaining = budget
    for id_str, text in items:
        if remaining <= 0:
            break
        try:
            if index.has(id_str):
                continue
        except Exception:  # noqa: BLE001
            continue
        if not text or not text.strip():
            continue
        try:
            vec = await client.embed(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("transcript embed failed (%s): %s", id_str, exc)
            remaining -= 1
            continue
        remaining -= 1  # 只要发起了 embed 调用就计预算（不论成功）
        if vec is None:
            continue
        try:
            index.add(id_str, vec)
            embedded += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("transcript index add failed (%s): %s", id_str, exc)
    return embedded


def search_index(index, query_vec, top_k: int) -> List[Tuple[int, float]]:
    """对单 session 索引做向量检索，返回 [(ordinal:int, sim:float), ...]。

    id 在索引里是字符串序号，这里解析回 int 序号给调用方映射到消息。
    无法解析的 id 跳过（防脏数据）。
    """
    if index is None or query_vec is None or top_k <= 0:
        return []
    try:
        hits = index.search(query_vec, top_k)
    except Exception as exc:  # noqa: BLE001
        logger.warning("transcript index search failed: %s", exc)
        return []
    out: List[Tuple[int, float]] = []
    for id_str, sim in hits:
        try:
            out.append((int(id_str), float(sim)))
        except (ValueError, TypeError):
            continue
    return out


async def get_embedding_client():
    """拿 EmbeddingClient（未就绪返 None）。薄封装，便于测试 monkeypatch。"""
    try:
        from backend.embedding_client import get_client

        return get_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_embedding_client failed: %s", exc)
        return None


__all__ = [
    "MAX_INDEX_MESSAGES_PER_SESSION",
    "MAX_EMBED_PER_CALL",
    "extract_text",
    "build_indexable_messages",
    "embedding_ready",
    "get_transcript_index",
    "ensure_indexed",
    "search_index",
    "get_embedding_client",
]
