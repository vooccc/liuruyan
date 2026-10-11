"""v2 · C-2 · TraceJournal — 链路落盘

设计：docs/architecture_v2/02_runtime_core.md § 五

订阅 EventBus wildcard，每个 AgentEvent append 到 per-trace JSONL：
    {base_dir}/traces/{YYYY-MM-DD}/{top_trace_id}.jsonl

trace_id 嵌套式，分隔符 `:`：
    abc12345                       ← 顶层
    abc12345:sub:1                 ← 第一个 subagent
    abc12345:sub:1:tool:read       ← subagent 内的工具调用

落盘按"顶层 trace"归一文件——所有 child trace 都写到顶层那份 jsonl。

并发写：用 atomic_append_jsonl_async（POSIX 单 write < PIPE_BUF 原子；
Windows 走 portalocker / fallback）。

留存策略 30 天 + > 1MB gz 不在本切片实现，等 cron 接入后做（cron_create
配 daily 清理 job）。
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from ..buses import AgentEvent, EventBus
from ..persistence.atomic import atomic_append_jsonl_async

log = logging.getLogger(__name__)


# ── trace_id 生成 ──────────────────────────────────────────────────────


def new_trace_id() -> str:
    """顶层 trace_id：8 字符 base62（url-safe）。"""
    # token_urlsafe(6) 出 8 字符；不过偶尔含 `-` / `_`，为了文件路径稳定取前 8
    return secrets.token_urlsafe(6)[:8]


def child_trace_id(parent: str, kind: str, n: int | str) -> str:
    """嵌套 trace_id。

    例：child_trace_id("abc12345", "sub", 1) → "abc12345:sub:1"
    """
    return f"{parent}:{kind}:{n}"


def top_trace_id(trace_id: str) -> str:
    """从嵌套 trace 拿顶层（取第一个 `:` 之前的部分）。"""
    return trace_id.split(":", 1)[0]


# ── TraceJournal ──────────────────────────────────────────────────────


class TraceJournal:
    """订阅 EventBus 把所有事件 append 到顶层 trace 的 jsonl 文件。"""

    # 落盘 IO 比一般 cb 慢，给宽点 timeout 避免被 EventBus 误判慢消费者
    DEFAULT_SLOW_THRESHOLD_MS = 500.0

    def __init__(
        self,
        base_dir: Path | str,
        *,
        slow_threshold_ms: float = DEFAULT_SLOW_THRESHOLD_MS,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.slow_threshold_ms = float(slow_threshold_ms)
        self._unsubscribe: callable | None = None  # type: ignore[valid-type]
        self._writes = 0  # 调试用
        self._errors = 0

    # ── 装配 ────────────────────────────────────────────────────────────

    def attach_to(self, bus: EventBus) -> None:
        """注册到 EventBus（wildcard 订阅所有事件）。"""
        if self._unsubscribe is not None:
            log.warning("TraceJournal already attached, skipping")
            return
        self._unsubscribe = bus.subscribe(
            None,
            self._on_event,
            slow_threshold_ms=self.slow_threshold_ms,
        )

    def detach(self) -> None:
        """从 EventBus 取消订阅（关闭前调用）。"""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    # ── 内部 ─────────────────────────────────────────────────────────────

    async def _on_event(self, event: AgentEvent) -> None:
        try:
            path = self._path_for(event)
            await atomic_append_jsonl_async(path, event.to_dict())
            self._writes += 1
        except Exception:
            self._errors += 1
            log.exception(
                "TraceJournal failed to persist event=%s trace=%s",
                event.type,
                event.trace_id,
            )

    def _path_for(self, event: AgentEvent) -> Path:
        """{base}/traces/{YYYY-MM-DD}/{top_trace}.jsonl"""
        date = datetime.fromtimestamp(event.ts, tz=timezone.utc).strftime("%Y-%m-%d")
        top = top_trace_id(event.trace_id)
        return self.base_dir / "traces" / date / f"{top}.jsonl"

    # ── 调试 ────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, int]:
        return {"writes": self._writes, "errors": self._errors}


__all__ = ["TraceJournal", "new_trace_id", "child_trace_id", "top_trace_id"]
