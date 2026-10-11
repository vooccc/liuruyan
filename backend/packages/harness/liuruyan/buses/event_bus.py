"""v2 · C-1 · EventBus — Agent → 多消费者 fan-out

设计：docs/architecture_v2/02_runtime_core.md § 四

核心不变量：
1. 一个慢消费者**不能**拖累其它消费者（前端慢绝不能拖死 IM / TraceJournal）
   → 用 `asyncio.wait_for(cb(event), timeout=slow_ms)`，超时 fire-and-forget
2. 异常**不能**向上抛 → catch 全部 + log
3. 事件**不保证顺序**到达不同消费者；同一消费者内顺序由 publish 调用顺序决定

订阅 schema：
    bus.subscribe("llm_chunk", cb)            # 订阅单类型
    bus.subscribe(None, cb)                   # wildcard 订阅所有
    bus.subscribe("tool_start", cb, slow_threshold_ms=500)  # 慢消费者放宽
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

from .event_schema import AgentEvent

log = logging.getLogger(__name__)

EventCallback = Callable[[AgentEvent], Awaitable[Any]]


class EventBus:
    """fan-out 总线。订阅者可按 type 或 wildcard。"""

    DEFAULT_SLOW_MS = 100.0

    def __init__(self) -> None:
        # type → list[(callback, slow_threshold_ms)]
        self._subs: dict[str, list[tuple[EventCallback, float]]] = defaultdict(list)
        self._wildcard: list[tuple[EventCallback, float]] = []
        self._closed = False

    # ── 订阅 ──────────────────────────────────────────────────────────

    def subscribe(
        self,
        event_type: str | None,
        cb: EventCallback,
        *,
        slow_threshold_ms: float = DEFAULT_SLOW_MS,
    ) -> Callable[[], None]:
        """订阅单类型或 wildcard（None）。返回 unsubscribe 函数。"""
        entry = (cb, float(slow_threshold_ms))
        if event_type is None:
            self._wildcard.append(entry)

            def _un() -> None:
                try:
                    self._wildcard.remove(entry)
                except ValueError:
                    pass

            return _un

        self._subs[event_type].append(entry)

        def _un_typed() -> None:
            try:
                self._subs[event_type].remove(entry)
            except ValueError:
                pass

        return _un_typed

    # ── 发布 ──────────────────────────────────────────────────────────

    async def publish(self, event: AgentEvent) -> None:
        """fan-out。慢消费者超时 fire-and-forget，不阻塞其它订阅者。"""
        if self._closed:
            log.warning("EventBus.publish on closed bus, event=%s", event.type)
            return

        targets = list(self._subs.get(event.type, [])) + list(self._wildcard)
        for cb, slow_ms in targets:
            try:
                await asyncio.wait_for(cb(event), timeout=slow_ms / 1000)
            except asyncio.TimeoutError:
                log.warning(
                    "EventBus slow consumer dropped event=%s cb=%s threshold=%.0fms",
                    event.type,
                    getattr(cb, "__name__", repr(cb)),
                    slow_ms,
                )
            except Exception:
                log.exception(
                    "EventBus subscriber failed event=%s cb=%s",
                    event.type,
                    getattr(cb, "__name__", repr(cb)),
                )

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def flush(self, timeout: float = 5.0) -> None:
        """优雅关闭前调：等所有 in-flight publish 处理完。

        当前实现是同步 publish + 单 task 顺序，没有积压；保留接口给未来
        异步队列实现使用。
        """
        # placeholder: 当前 publish 是 async 同步 fan-out，不需要 drain
        return

    def close(self) -> None:
        """禁止后续 publish。"""
        self._closed = True


__all__ = ["EventBus", "EventCallback"]
