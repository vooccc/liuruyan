"""Embedding 配置 API。

端点：
- GET  /embedding/config           — 当前配置 + 可选 provider 列表
- PUT  /embedding/config           — 更新配置（partial）+ 自动失效缓存
- POST /embedding/test             — 临时配置测试连通性（不落盘）
- GET  /embedding/stats            — 各 bucket 索引大小统计
- POST /embedding/reindex          — 同步全量重算（向后兼容，N<50 走这个）
- POST /embedding/reindex/stream   — NDJSON 流式重算，每条记录 yield 一行 JSON

设计参考：./backend\routes\voice_routes.py
（同样的"基础配置 + 可选 provider 列表 + 测试连通性"三段式）
"""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.embedding_config import (
    EmbeddingConfig,
    get_config,
    save_config,
)

router = APIRouter(prefix="/embedding", tags=["embedding"])


# ============================================================================
# GET /embedding/config — 返回当前配置 + 可选 provider 列表（让前端 UI 自动填 base_url）
# ============================================================================


# 不支持 embedding endpoint 的 provider —— 让前端 UI 直接过滤掉，避免用户配错
_NOT_SUPPORT_EMBEDDING = {
    "deepseek",     # 官方文档明确不提供 /embeddings
    "kimi",         # 月之暗面只支持 chat
    "moonshot",     # kimi 别名
    "openrouter",   # 只做 chat 路由
    "groq",         # 只做推理加速，无 embedding
}

# 各 provider 推荐的 embedding 模型 + 默认维度（让前端默认勾选）
# 维度信息靠模型名硬编码——provider 公开 API 没有"返回模型维度"的标准方式
_RECOMMENDED: Dict[str, Dict[str, Any]] = {
    "openai": {"model": "text-embedding-3-small", "dim": 1536},
    "siliconflow": {"model": "BAAI/bge-m3", "dim": 1024},          # 免费、中文友好、强烈推荐
    "zhipu": {"model": "embedding-3", "dim": 2048},
    "glm": {"model": "embedding-3", "dim": 2048},                  # zhipu 别名
    "dashscope": {"model": "text-embedding-v3", "dim": 1024},      # 阿里通义
    "doubao": {"model": "doubao-embedding-text-240715", "dim": 2560},
    "mistral": {"model": "mistral-embed", "dim": 1024},
    "cohere": {"model": "embed-multilingual-v3.0", "dim": 1024},
    "together": {"model": "BAAI/bge-base-en-v1.5", "dim": 768},
    "fireworks": {"model": "thenlper/gte-large", "dim": 1024},
    "xunfei": {"model": "embedding-3", "dim": 2560},               # 讯飞星火
}


@router.get("/config")
async def get_embedding_config() -> Dict[str, Any]:
    """返当前 embedding 配置 + 可选 provider 列表 + 推荐模型映射。"""
    from backend.providers.store import load_providers_config_cached

    cfg = get_config()

    eligible: List[Dict[str, Any]] = []
    try:
        providers_cfg = load_providers_config_cached()
        for pid, pc in providers_cfg["providers"].items():
            if pid in _NOT_SUPPORT_EMBEDDING:
                continue
            # 只有 OpenAI 兼容协议能直接走 /embeddings 端点
            if pc.transport != "openai_chat":
                continue
            if not pc.enabled:
                continue
            eligible.append({
                "id": pc.id,
                "name": pc.name,
                "base_url": pc.base_url,
                "has_api_key": bool(pc.api_key),
                "is_aggregator": pc.is_aggregator,
                "recommended": _RECOMMENDED.get(pid),  # None 表示无推荐
            })
    except Exception as exc:
        # providers 配置加载失败不影响 embedding 自己的配置查看
        eligible = []

    return {
        "config": cfg.public_dict(),
        "eligible_providers": eligible,
        "recommended_models": _RECOMMENDED,
    }


# ============================================================================
# PUT /embedding/config — 更新配置（partial）
# ============================================================================


class EmbeddingConfigUpdate(BaseModel):
    """PUT /embedding/config 的 partial body。None 字段不动。"""

    enabled: Optional[bool] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    dim: Optional[int] = None
    timeout: Optional[float] = None
    cache_size: Optional[int] = None


@router.put("/config")
async def update_embedding_config(body: EmbeddingConfigUpdate) -> Dict[str, Any]:
    """更新配置。落盘后自动失效进程内 client + index 缓存。

    注意：dim 变更但 enabled=true 时不主动 reindex——避免大量旧记忆突然消失；
    用户应当在前端点"全量重算"按钮触发 /embedding/reindex。
    """
    cfg = get_config()
    update = body.model_dump(exclude_none=True)
    if not update:
        return {"status": "noop", "config": cfg.public_dict()}

    # 应用 update
    new_data = {
        "enabled": cfg.enabled,
        "provider": cfg.provider,
        "model": cfg.model,
        "base_url": cfg.base_url,
        "api_key": cfg.api_key,
        "dim": cfg.dim,
        "timeout": cfg.timeout,
        "cache_size": cfg.cache_size,
        "updated_at": cfg.updated_at,
    }
    new_data.update(update)

    try:
        new_cfg = EmbeddingConfig(**new_data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid config: {exc}")

    # enabled=True 但字段不全 → 拒绝（避免半启用状态）
    if new_cfg.enabled and not new_cfg.is_ready():
        raise HTTPException(
            status_code=400,
            detail=(
                "enabled=true requires all of: model, base_url, api_key, dim>0. "
                f"Got model={new_cfg.model!r}, base_url={new_cfg.base_url!r}, "
                f"has_api_key={bool(new_cfg.api_key)}, dim={new_cfg.dim}."
            ),
        )

    save_config(new_cfg)
    return {"status": "success", "config": new_cfg.public_dict()}


# ============================================================================
# POST /embedding/test — 测试连通性（不落盘）
# ============================================================================


class EmbeddingTestRequest(BaseModel):
    """POST /embedding/test body。

    可以传完整临时配置（用户在 UI 点"测试"按钮，但还没保存）；
    不传则用当前已落盘的配置。
    """
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    dim: Optional[int] = None
    timeout: Optional[float] = None


@router.post("/test")
async def test_embedding(body: EmbeddingTestRequest) -> Dict[str, Any]:
    """临时配置测试连通性。返 {ok, dim, expected_dim, elapsed_ms, error}。

    实现方式：用 body 字段覆盖当前 cfg 临时构造一个 EmbeddingClient，调用
    test_connection()。完成后 client 实例丢弃（不污染单例缓存）。
    """
    # 临时构造配置
    cfg = get_config()
    test_cfg_data = {
        "enabled": True,  # 临时强制启用以让 EmbeddingClient 能初始化
        "provider": cfg.provider or "test",
        "model": body.model or cfg.model,
        "base_url": body.base_url or cfg.base_url,
        "api_key": body.api_key or cfg.api_key,
        "dim": body.dim or cfg.dim,
        "timeout": body.timeout or cfg.timeout,
        "cache_size": 1,  # 测试用不到缓存
    }
    test_cfg = EmbeddingConfig(**test_cfg_data)
    if not test_cfg.is_ready():
        raise HTTPException(
            status_code=400,
            detail=(
                "test requires model + base_url + api_key + dim. "
                f"Got: model={test_cfg.model!r}, base_url={test_cfg.base_url!r}, "
                f"has_key={bool(test_cfg.api_key)}, dim={test_cfg.dim}."
            ),
        )

    # 临时 client：不走 get_client 单例工厂避免污染
    try:
        from openai import AsyncOpenAI
        from backend.utils.llm_headers import merge_default_llm_headers
        import time as _time

        client = AsyncOpenAI(
            api_key=test_cfg.api_key,
            base_url=test_cfg.base_url,
            timeout=test_cfg.timeout,
            default_headers=merge_default_llm_headers(),
        )
        start = _time.time()
        try:
            resp = await client.embeddings.create(model=test_cfg.model, input="hello")
            actual_dim = len(resp.data[0].embedding) if resp.data else 0
            elapsed = int((_time.time() - start) * 1000)
            ok = actual_dim == test_cfg.dim
            return {
                "ok": ok,
                "dim": actual_dim,
                "expected_dim": test_cfg.dim,
                "elapsed_ms": elapsed,
                "error": None if ok
                         else f"dim mismatch: actual={actual_dim} expected={test_cfg.dim}. Update `dim` to {actual_dim}.",
            }
        except Exception as exc:
            return {
                "ok": False,
                "dim": 0,
                "expected_dim": test_cfg.dim,
                "elapsed_ms": int((_time.time() - start) * 1000),
                "error": str(exc),
            }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"client init failed: {exc}")


# ============================================================================
# GET /embedding/stats — 各 bucket 索引大小统计（诊断 + 前端"我有多少向量"展示）
# ============================================================================


@router.get("/stats")
async def get_stats() -> Dict[str, Any]:
    """扫描所有 agent embedding 目录 + shared，返每个 bucket 的索引大小 + meta。

    扫描方式：直接读各 agent 目录下的 embeddings.meta.json（如果存在），
    不强制实例化 EmbeddingIndex（避免给空目录创建 numpy 数组浪费内存）。
    """
    import json
    from backend import paths

    cfg = get_config()
    out: Dict[str, Any] = {
        "enabled": cfg.enabled,
        "current_model": cfg.model,
        "current_dim": cfg.dim,
        "buckets": [],
    }

    def _scan_meta(scope_label: str, agent_id: Optional[str], bucket_dir):
        meta_path = bucket_dir / "embeddings.meta.json"
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return {
            "scope": scope_label,
            "agent_id": agent_id,
            "dir": str(bucket_dir),
            **meta,
        }

    # shared bucket
    shared_meta = _scan_meta("shared", None, paths.shared_embedding_dir())
    if shared_meta:
        out["buckets"].append(shared_meta)

    # 每个 agent 的 private bucket
    agents_root = paths.agents_dir()
    if agents_root.exists():
        for agent_path in agents_root.iterdir():
            if not agent_path.is_dir():
                continue
            agent_id = agent_path.name
            bucket_dir = agent_path / "memory" / "embeddings"
            if not bucket_dir.exists():
                continue
            m = _scan_meta(f"private:{agent_id}", agent_id, bucket_dir)
            if m:
                out["buckets"].append(m)

    out["total_vectors"] = sum(b.get("count", 0) for b in out["buckets"])
    return out


# ============================================================================
# POST /embedding/reindex — 全量重算（V1：单 bucket 同步版；V2 加 SSE 进度）
# ============================================================================


class ReindexRequest(BaseModel):
    scope: str = "private"
    agent_id: Optional[str] = None
    """scope='shared' 时 agent_id 应为 None；scope='private' 时必填。"""


@router.post("/reindex")
async def reindex_bucket(body: ReindexRequest) -> Dict[str, Any]:
    """全量重算指定 bucket 的向量索引。

    流程：
    1. 校验 embedding 已启用
    2. 加载该 bucket 的 jsonl
    3. 清空现有索引
    4. 批量算 embedding（失败的条目跳过 + 计数）
    5. 全部 add 进 index
    6. 返 {processed, succeeded, failed, elapsed_ms}

    V1 限制：同步阻塞，N>1000 时前端会卡。V2 改 SSE 进度推送。
    """
    import time as _time
    from backend.embedding_client import get_client
    from backend.embedding_index import get_index
    from backend.tools.builtin.agent_memory_tool import (
        _resolve_kv_path,
        _resolve_embedding_bucket,
        _embed_text_for_memory,
        _load_records,
    )

    if body.scope not in ("private", "shared"):
        raise HTTPException(status_code=400, detail=f"invalid scope: {body.scope!r}")

    client = get_client()
    if client is None:
        raise HTTPException(status_code=400, detail="embedding not enabled or not configured")

    kv_path = _resolve_kv_path(body.agent_id, scope=body.scope)
    records = _load_records(kv_path)

    bucket_key, bucket_dir = _resolve_embedding_bucket(body.agent_id, body.scope)
    idx = get_index(bucket_key, bucket_dir)
    if idx is None:
        raise HTTPException(status_code=500, detail="failed to get embedding index")

    start = _time.time()
    idx.clear()  # 全量重算 → 先清空

    succeeded = 0
    failed = 0
    for rec in records:
        mid = rec.get("id")
        if not mid:
            continue
        text = _embed_text_for_memory(rec)
        if not text:
            continue
        try:
            vec = await client.embed(text)
        except Exception:
            failed += 1
            continue
        if vec is None:
            failed += 1
            continue
        try:
            idx.add(mid, vec)
            succeeded += 1
        except Exception:
            failed += 1

    elapsed_ms = int((_time.time() - start) * 1000)
    return {
        "scope": body.scope,
        "agent_id": body.agent_id,
        "processed": len(records),
        "succeeded": succeeded,
        "failed": failed,
        "elapsed_ms": elapsed_ms,
        "kv_path": str(kv_path),
        "bucket_dir": str(bucket_dir),
    }


# ============================================================================
# POST /embedding/reindex/stream — NDJSON 流式重算（每条记忆推一行进度）
# ============================================================================
#
# 协议：application/x-ndjson，按行 yield JSON：
#   {"event": "start",    "total": N, "scope": ..., "agent_id": ..., "kv_path": ..., "bucket_dir": ...}
#   {"event": "progress", "processed": k, "succeeded": s, "failed": f,
#                         "current_id": "...", "current_title": "...",
#                         "ok": true|false, "error": null|"..."}
#   ...每条记录一行...
#   {"event": "done",     "processed": N, "succeeded": S, "failed": F, "elapsed_ms": ...}
#
# 失败模式：
#   {"event": "error", "error": "embedding not enabled", "stage": "init"}
#
# 不再像同步版那样先 idx.clear()——idx.add() 本身覆盖同 id，所以增量更新就够了。
# clear+add 的语义只在"换 model 但不重启进程"时才必要，此时 EmbeddingIndex 加载会
# 走 dim/model mismatch 检测自动清空（embedding_index.py:_do_load）。
# 这样设计的额外好处：用户可以中途中断（断网/取消 fetch），已处理的部分有效。


async def _reindex_stream_generator(
    scope: str,
    agent_id: Optional[str],
) -> AsyncGenerator[bytes, None]:
    """逐条算 embedding 并 yield NDJSON 行。

    错误处理策略：
    - init 阶段错（未启用 / 配置不全 / index 拿不到）→ yield {event:error,stage:init} 后终止
    - 单条 embed 失败 → 仍 yield {event:progress, ok:false, error:...} 继续下一条
    - 整个生成器异常 → 上层 StreamingResponse 会断开连接，前端的 reader 收 done=true
    """
    import time as _time
    from backend.embedding_client import get_client
    from backend.embedding_index import get_index
    from backend.tools.builtin.agent_memory_tool import (
        _embed_text_for_memory,
        _load_records,
        _resolve_embedding_bucket,
        _resolve_kv_path,
    )

    def _line(payload: Dict[str, Any]) -> bytes:
        # ensure_ascii=False 让中文 title 直接保留原字
        return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    if scope not in ("private", "shared"):
        yield _line({"event": "error", "stage": "init", "error": f"invalid scope: {scope!r}"})
        return

    client = get_client()
    if client is None:
        yield _line({
            "event": "error",
            "stage": "init",
            "error": "embedding not enabled or not configured",
        })
        return

    kv_path = _resolve_kv_path(agent_id, scope=scope)
    records = _load_records(kv_path)

    bucket_key, bucket_dir = _resolve_embedding_bucket(agent_id, scope)
    idx = get_index(bucket_key, bucket_dir)
    if idx is None:
        yield _line({
            "event": "error",
            "stage": "init",
            "error": "failed to acquire embedding index",
        })
        return

    start_ts = _time.time()
    yield _line({
        "event": "start",
        "total": len(records),
        "scope": scope,
        "agent_id": agent_id,
        "kv_path": str(kv_path),
        "bucket_dir": str(bucket_dir),
    })

    processed = succeeded = failed = 0
    for rec in records:
        mid = rec.get("id")
        if not mid:
            continue
        title = (rec.get("title") or rec.get("content") or "")[:60]
        text = _embed_text_for_memory(rec)
        processed += 1
        if not text:
            failed += 1
            yield _line({
                "event": "progress",
                "processed": processed,
                "succeeded": succeeded,
                "failed": failed,
                "current_id": mid,
                "current_title": title,
                "ok": False,
                "error": "empty embedding text",
            })
            continue
        ok = True
        err: Optional[str] = None
        try:
            vec = await client.embed(text)
            if vec is None:
                ok = False
                err = "embed returned None"
            else:
                idx.add(mid, vec)
        except Exception as exc:
            ok = False
            err = str(exc)
        if ok:
            succeeded += 1
        else:
            failed += 1
        yield _line({
            "event": "progress",
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "current_id": mid,
            "current_title": title,
            "ok": ok,
            "error": err,
        })

    elapsed_ms = int((_time.time() - start_ts) * 1000)
    yield _line({
        "event": "done",
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "elapsed_ms": elapsed_ms,
    })


@router.post("/reindex/stream")
async def reindex_stream(body: ReindexRequest) -> StreamingResponse:
    """NDJSON 流式重算。前端用 fetch + ReadableStream.getReader() 逐行解析。

    设计取舍：
    - 不用 SSE（text/event-stream）：SSE 有 retry / id / event-name 一堆复杂语义，
      reindex 是单向 server→client 数据流，NDJSON 更朴素直接
    - 不用 chunked JSON 数组：JSON 数组直到流结束才是合法 JSON，前端无法增量解析
    - 走 application/x-ndjson：标准 mime type，前端 fetch streaming 直接配 TextDecoder + split('\\n')
    """
    # X-Accel-Buffering: no 阻止 nginx 反代时缓冲整个响应（柳如烟现在没反代，
    # 但 Tauri 内嵌 webview 有时也有类似缓冲，加上保险）
    return StreamingResponse(
        _reindex_stream_generator(body.scope, body.agent_id),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
