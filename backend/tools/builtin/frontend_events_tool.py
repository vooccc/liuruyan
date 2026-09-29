"""read_frontend_events：让 AI 自己看前端遥测（路径 A 配套工具）。

数据来源：~/.liuruyan/frontend_events.jsonl，由 backend/routes/debug_routes.py 写入，
内容是前端 main.tsx -> initDevTelemetry() 收的：
- console.error/warn
- fetch / XHR 请求（method/url/status/duration）
- 全局 window 'error' 事件（未捕获 JS 异常 + stack）
- unhandledrejection（Promise rejection）

跟 read_logs 区别：read_logs 是通用日志读取器，read_frontend_events 是前端遥测**语义化**
快捷入口——AI 不需要记 jsonl 路径，按 since_min/event_type/level/grep 过滤即可。
内部不复用 read_logs（它没法以"最近 N 分钟"为单位过滤）。

输出契约：跟 read_logs 形似但精简——给 AI 看的是结构化事件而非原始 line。

设计参考：@d:\\AI_Project\\智能体\\柳如烟\\backend\\routes\\debug_routes.py
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 输出硬上限（与 read_logs 对齐）
_MAX_RETURNED_EVENTS = 500
_MAX_OUTPUT_BYTES = 200_000
_DEFAULT_MAX = 100
_DEFAULT_SINCE_MIN = 30


def _resolve_path() -> Path:
    """懒 import paths 避免循环。"""
    from backend.paths import frontend_events_jsonl_path
    return frontend_events_jsonl_path()


def _parse_events_tail(path: Path, max_lines_to_scan: int = 5000) -> List[Dict[str, Any]]:
    """读 jsonl 尾部最多 max_lines_to_scan 行，返成功解析的 dict 列表。

    简单实现：用 read_logs 的反向读思路，但目前文件大小 < 50MB 单文件，全读 + 切尾足够。
    单文件 50MB / 平均每行 200B ≈ 26 万行；scan 5000 行覆盖最近活动绰绰有余。
    """
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    try:
        size = path.stat().st_size
    except OSError:
        return []

    # 大于 5MB 才走反向分块；小文件全读最省心
    if size <= 5 * 1024 * 1024:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fp:
                lines = fp.readlines()
        except OSError:
            return []
        tail = lines[-max_lines_to_scan:]
    else:
        # 反向读末尾 ~1MB
        try:
            with path.open("rb") as fp:
                fp.seek(-min(1024 * 1024, size), 2)
                blob = fp.read()
        except OSError:
            return []
        text = blob.decode("utf-8", errors="replace")
        lines_raw = text.splitlines()
        # 第一行可能被截断了，丢
        tail = lines_raw[1:][-max_lines_to_scan:]

    for ln in tail:
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                events.append(obj)
        except (ValueError, TypeError):
            continue
    return events


def _filter(
    events: List[Dict[str, Any]],
    since_ms: Optional[float],
    event_type: Optional[str],
    level: Optional[str],
    grep_pat: Optional[re.Pattern],
    status_min: Optional[int],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ev in events:
        ts = ev.get("server_ts") or ev.get("ts")
        if since_ms is not None and isinstance(ts, (int, float)) and ts < since_ms:
            continue
        if event_type and ev.get("type") != event_type:
            continue
        if level:
            evlvl = (ev.get("level") or "").upper()
            if evlvl != level.upper():
                continue
        if status_min is not None:
            st = ev.get("status")
            if not isinstance(st, int) or st < status_min:
                continue
        if grep_pat is not None:
            blob_parts = []
            for k in ("msg", "stack", "url", "name", "source"):
                v = ev.get(k)
                if isinstance(v, str):
                    blob_parts.append(v)
            blob = "\n".join(blob_parts)
            if not grep_pat.search(blob):
                continue
        out.append(ev)
    return out


def _format_event_for_output(ev: Dict[str, Any]) -> Dict[str, Any]:
    """精简单条事件输出——保留关键字段。"""
    keep = {}
    for k in (
        "type", "level", "ts", "server_ts", "msg", "stack", "url",
        "method", "status", "duration_ms", "source", "lineno", "colno",
        "name", "page", "_truncated",
    ):
        if k in ev:
            keep[k] = ev[k]
    return keep


async def read_frontend_events_tool(
    since_min: int = _DEFAULT_SINCE_MIN,
    event_type: Optional[str] = None,
    level: Optional[str] = None,
    grep: Optional[str] = None,
    status_min: Optional[int] = None,
    max_events: int = _DEFAULT_MAX,
    **_ctx: Any,
) -> str:
    """读前端遥测事件（最近 N 分钟，按 type/level/grep 过滤）。

    Args:
        since_min: 只看最近 N 分钟的事件（默认 30；max 1440 = 24h）
        event_type: console / network / error / unhandledrejection（None=全部）
        level: ERROR / WARN / INFO（大小写不敏感；None=全部）
        grep: 正则（搜 msg/stack/url/name/source 拼接体）
        status_min: 网络事件最小 status（如 400 看 4xx+5xx）
        max_events: 返回上限（默认 100，max 500）
    """
    # 参数 sanitize（在检查文件存在前做，让无效参数立即返 error 而非空 events）
    try:
        since_min = int(since_min) if since_min is not None else _DEFAULT_SINCE_MIN
    except (TypeError, ValueError):
        since_min = _DEFAULT_SINCE_MIN
    since_min = max(1, min(since_min, 1440))

    try:
        max_events = int(max_events) if max_events is not None else _DEFAULT_MAX
    except (TypeError, ValueError):
        max_events = _DEFAULT_MAX
    max_events = max(1, min(max_events, _MAX_RETURNED_EVENTS))

    if event_type is not None:
        event_type = str(event_type).strip().lower()
        if event_type not in {"console", "network", "error", "unhandledrejection", "perf", "custom"}:
            return json.dumps({
                "error": f"unknown event_type {event_type!r}",
                "hint": "supported: console / network / error / unhandledrejection",
            }, ensure_ascii=False)

    norm_level: Optional[str] = None
    if level is not None:
        up = str(level).strip().upper()
        if up == "WARNING":
            up = "WARN"
        if up not in {"ERROR", "WARN", "INFO", "DEBUG"}:
            return json.dumps({
                "error": f"unknown level {level!r}",
                "hint": "supported: ERROR / WARN / INFO / DEBUG",
            }, ensure_ascii=False)
        norm_level = up

    grep_pat: Optional[re.Pattern] = None
    if grep:
        try:
            grep_pat = re.compile(grep)
        except re.error as exc:
            return json.dumps({"error": f"invalid regex {grep!r}: {exc}"}, ensure_ascii=False)

    if status_min is not None:
        try:
            status_min = int(status_min)
        except (TypeError, ValueError):
            status_min = None

    # 参数都过了再看文件存不存在
    path = _resolve_path()
    if not path.exists():
        return json.dumps({
            "events": [],
            "matched": 0,
            "scanned": 0,
            "path": str(path),
            "hint": (
                "frontend_events.jsonl 还没生成——前端可能没启 telemetry 或还没产生任何事件。"
                "确认前端已启动 + main.tsx 的 initDevTelemetry() 已 boot。"
            ),
        }, ensure_ascii=False)

    since_ms = (time.time() - since_min * 60) * 1000

    events = _parse_events_tail(path)
    matched = _filter(events, since_ms, event_type, norm_level, grep_pat, status_min)

    truncated = False
    if len(matched) > max_events:
        matched = matched[-max_events:]
        truncated = True

    payload: Dict[str, Any] = {
        "path": str(path),
        "since_min": since_min,
        "filters": {
            "event_type": event_type,
            "level": norm_level,
            "grep": grep,
            "status_min": status_min,
        },
        "scanned": len(events),
        "matched": len(matched),
        "truncated": truncated,
        "events": [_format_event_for_output(ev) for ev in matched],
    }
    out = json.dumps(payload, ensure_ascii=False)
    # 输出字节硬截
    if len(out.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        # 二分丢事件
        lo, hi = 0, len(matched)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            payload["events"] = [_format_event_for_output(ev) for ev in matched[-mid:]]
            payload["matched"] = mid
            payload["truncated"] = True
            cand = json.dumps(payload, ensure_ascii=False)
            if len(cand.encode("utf-8")) <= _MAX_OUTPUT_BYTES:
                lo = mid
                out = cand
            else:
                hi = mid - 1
    return out


# ============================================================================
# handler
# ============================================================================

_CONTEXT_KEYS = {"session_id", "llm_client", "trace_id", "agent_id"}


def _drop(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k not in _CONTEXT_KEYS}


async def _handle_read_frontend_events(**kwargs: Any) -> str:
    args = _drop(kwargs)
    return await read_frontend_events_tool(
        since_min=args.get("since_min", _DEFAULT_SINCE_MIN),
        event_type=args.get("event_type"),
        level=args.get("level"),
        grep=args.get("grep"),
        status_min=args.get("status_min"),
        max_events=args.get("max_events", _DEFAULT_MAX),
    )


__all__ = ["read_frontend_events_tool", "_handle_read_frontend_events"]
