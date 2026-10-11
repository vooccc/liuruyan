"""前端遥测收集端点（路径 A 落地）。

端点：
- POST /debug/frontend_events  —— 接收前端批量上报的 console/network/error/unhandledrejection 事件

设计契约：
- 每条事件落 ~/.liuruyan/frontend_events.jsonl（一行一 JSON）
- 全局速率限制：进程内令牌桶，最高 200 events/sec，超出 429（防 console.log 风暴自爆）
- 单事件字段截断：msg ≤ 4KB / stack ≤ 16KB，超出标 _truncated:true
- 文件单文件 ~50MB 自动 rotate 一次（保留 .jsonl.1）
- 完全不影响业务路由——失败也只 print，不抛
- 故意不验证 schema（上游什么乱七八糟前端框架都可能 patch 进来）；只做尺寸 + 类型守卫

为什么不用 /api/* 前缀：跟 /health /chat 同款根路径直挂。
为什么不打 SSE：上行单向，HTTP POST 已经够。
为什么不上 starlette middleware：太重，单个 path 不值当。

设计参考：backend/paths.py:frontend_events_jsonl_path
工具配套：backend/tools/builtin/frontend_events_tool.py
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.dev_features import gene_editor_status, unlock_gene_editor_for_dev_session
from backend.paths import frontend_events_jsonl_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])


# ============================================================================
# 限制常量
# ============================================================================

# 单事件字段上限
_MAX_MSG_BYTES = 4 * 1024
_MAX_STACK_BYTES = 16 * 1024
_MAX_URL_BYTES = 2 * 1024

# 单批上报事件数上限（防一次性塞 1MB）
_MAX_EVENTS_PER_REQUEST = 200

# 速率限制：进程内令牌桶
_RATE_LIMIT_PER_SEC = 200
_RATE_BURST = 400

# 文件 rotate 阈值（rotate 后变 .jsonl.1）
_ROTATE_THRESHOLD_BYTES = 50 * 1024 * 1024

# 允许的 type / level 集合（预防胡传）
_ALLOWED_TYPES = {"console", "network", "error", "unhandledrejection", "perf", "custom"}
_ALLOWED_LEVELS = {"DEBUG", "INFO", "WARN", "ERROR"}


# ============================================================================
# 速率限制（令牌桶）
# ============================================================================

class _TokenBucket:
    """简易令牌桶。线程安全。"""

    def __init__(self, rate_per_sec: float, burst: float):
        self.rate = float(rate_per_sec)
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, n: int = 1) -> bool:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last
            self.last = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False


_bucket = _TokenBucket(_RATE_LIMIT_PER_SEC, _RATE_BURST)


# ============================================================================
# 写入与 rotate
# ============================================================================

_write_lock = threading.Lock()  # 多请求并发写同一文件需要串行化


def _truncate_str(value: Any, max_bytes: int) -> tuple[Any, bool]:
    """超长字符串截断；非字符串原样返。返回 (value, truncated)."""
    if not isinstance(value, str):
        return value, False
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "…<TRUNC>", True


def _sanitize_event(raw: Any, server_ts: float) -> Optional[Dict[str, Any]]:
    """规范化单条事件。无效返 None。

    规则：
    - 必须是 dict
    - type 字段不在白名单 → 直接 'custom'
    - level 字段大写后不在白名单 → 'INFO'
    - 长字段（msg/stack/url）超阈值截断 + 标 _truncated
    - 添加服务端 server_ts（毫秒），保留前端 ts
    """
    if not isinstance(raw, dict):
        return None

    out: Dict[str, Any] = {}
    truncated = False

    # type
    typ = raw.get("type")
    if not isinstance(typ, str) or typ not in _ALLOWED_TYPES:
        typ = "custom"
    out["type"] = typ

    # level
    lvl = raw.get("level")
    if isinstance(lvl, str):
        up = lvl.strip().upper()
        if up == "WARNING":
            up = "WARN"
        if up not in _ALLOWED_LEVELS:
            up = "INFO"
    else:
        up = "INFO"
    out["level"] = up

    # 时间戳：前端 ts 保留 + 服务端 server_ts 兜底（毫秒）
    fe_ts = raw.get("ts")
    if isinstance(fe_ts, (int, float)):
        out["ts"] = float(fe_ts)
    out["server_ts"] = int(server_ts * 1000)

    # 长字段截断
    for key, max_bytes in (
        ("msg", _MAX_MSG_BYTES),
        ("stack", _MAX_STACK_BYTES),
        ("url", _MAX_URL_BYTES),
    ):
        if key in raw:
            v, t = _truncate_str(raw[key], max_bytes)
            out[key] = v
            if t:
                truncated = True

    # 其他白名单透传字段（短的，不需截断）
    for key in (
        "method", "status", "duration_ms", "source",
        "lineno", "colno", "name", "route",
        "agent_id", "session_id", "page",
    ):
        if key in raw:
            v = raw[key]
            # 防止巨型 object 漏进来：限定基本类型
            if isinstance(v, (str, int, float, bool)) or v is None:
                # 字符串再截短一点
                if isinstance(v, str) and len(v.encode("utf-8")) > 1024:
                    out[key] = v.encode("utf-8")[:1024].decode("utf-8", errors="replace") + "…"
                    truncated = True
                else:
                    out[key] = v

    if truncated:
        out["_truncated"] = True
    return out


def _maybe_rotate(path: Path) -> None:
    """超阈值时旋转一次。失败静默——遥测自身错不应该影响业务。"""
    try:
        if not path.exists():
            return
        if path.stat().st_size < _ROTATE_THRESHOLD_BYTES:
            return
        backup = path.with_suffix(path.suffix + ".1")
        try:
            if backup.exists():
                backup.unlink()
        except OSError:
            pass
        path.rename(backup)
    except OSError:
        pass


def _append_events(events: List[Dict[str, Any]]) -> int:
    """追加批量事件到 jsonl。返回写成功条数。"""
    if not events:
        return 0
    path = frontend_events_jsonl_path()
    with _write_lock:
        _maybe_rotate(path)
        written = 0
        try:
            with path.open("a", encoding="utf-8") as fp:
                for ev in events:
                    try:
                        line = json.dumps(ev, ensure_ascii=False)
                    except (TypeError, ValueError):
                        continue
                    fp.write(line + "\n")
                    written += 1
        except OSError as exc:
            # 磁盘满 / 权限挂 —— 静默吃掉，不能让前端 bug 排查工具自己变成事故源
            logger.warning("[FRONTEND-EVENTS] write failed: %s", exc)
        return written


# ============================================================================
# 路由
# ============================================================================

class _EventsBatch(BaseModel):
    # 故意用 List[Any]：Pydantic 收到非 dict 会 422 拒整批，但设计是"尽量收 + sanitize 过滤"。
    # 后续 _sanitize_event 自己会丢非 dict 项。
    events: List[Any] = Field(default_factory=list)


@router.post("/frontend_events")
async def post_frontend_events(payload: _EventsBatch, request: Request):
    """接收前端遥测批量上报。

    Body: {"events": [{type, level, ts, msg, ...}, ...]}
    Returns: {"accepted": N, "dropped": M, "rate_limited": bool}

    并不验 schema 严格性——前端任何乱传都尽量收下并 sanitize。
    速率超限直接 429（前端 telemetry 客户端会指数退避，不会丢得很惨）。
    """
    raw_events = payload.events or []
    if len(raw_events) > _MAX_EVENTS_PER_REQUEST:
        raw_events = raw_events[:_MAX_EVENTS_PER_REQUEST]

    if not raw_events:
        return {"accepted": 0, "dropped": 0, "rate_limited": False}

    if not _bucket.consume(len(raw_events)):
        # 全批拒，让客户端攒大一点再来
        raise HTTPException(status_code=429, detail="rate limit exceeded; back off")

    server_ts = time.time()
    sanitized: List[Dict[str, Any]] = []
    dropped = 0
    for raw in raw_events:
        ev = _sanitize_event(raw, server_ts)
        if ev is None:
            dropped += 1
        else:
            sanitized.append(ev)

    written = _append_events(sanitized)
    return {
        "accepted": written,
        "dropped": dropped + (len(sanitized) - written),
        "rate_limited": False,
    }


@router.get("/frontend_events/_meta")
async def frontend_events_meta():
    """返当前 jsonl 大小与路径，给设置面板/调试用。"""
    p = frontend_events_jsonl_path()
    size = p.stat().st_size if p.exists() else 0
    return {
        "path": str(p),
        "exists": p.exists(),
        "size_bytes": size,
        "rotate_threshold_bytes": _ROTATE_THRESHOLD_BYTES,
    }


@router.get("/features/gene-editor")
async def get_gene_editor_feature():
    return gene_editor_status()


@router.post("/features/gene-editor/unlock")
async def unlock_gene_editor_feature():
    status = unlock_gene_editor_for_dev_session()
    if not status.get("enabled"):
        raise HTTPException(status_code=404, detail="feature not available")
    return status


__all__ = ["router"]
