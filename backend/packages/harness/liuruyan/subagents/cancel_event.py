"""v2 · F-0a · CancelEvent — 协作式取消信号

设计：_docs/architecture_v2/01_master_plan.md Phase F + 02_runtime_core.md § 二

协作式（cooperative）取消：被取消的任务**自己负责检查**取消信号 + 体面退出。
跟"硬取消"（task.cancel）的区别：
- task.cancel：通过 ``asyncio.CancelledError`` 异步抛——可能在任意 await 点
  打断；中间态（写一半的 jsonl / 半完成的 tool dispatch）容易留下残骸
- CancelEvent：被取消任务在固定的检查点（``raise_if_cancelled()`` / ``wait()``
  返回时）才感知；可以在抛出前完成 cleanup（关流、落 partial result、
  释放 lock）

适合长跑任务：subagent / worker / 流式 LLM 调用。

API：

```python
event = CancelEvent(trace_id="parent-xyz")

# 任务内部周期检查
async def long_running_task(event: CancelEvent):
    for i in range(1000):
        event.raise_if_cancelled()  # 取消时抛 CancelEventCancelled
        ...
        await asyncio.sleep(1)

# 外部取消
event.cancel(reason="user pressed stop")

# 等待取消触发（用于 select-style 多路）
async def watcher():
    await event.wait()
    print(f"cancelled by: {event.reason}")

# F-3 helper：竞速 await（subagent 阻塞 spawn 用）
result = await wait_with_cancel(some_coro, event)
# 正常完成 → 返 some_coro 结果
# event.cancel() 先触发 → 抛 CancelEventCancelled，some_coro task.cancel
```

派生子事件：

```python
parent = CancelEvent(trace_id="parent")
child = parent.child(trace_id="child")  # 父取消 → 子也取消（自动级联）

parent.cancel(reason="user stop")
assert child.is_cancelled()
assert child.reason.startswith("[parent]")  # 标记级联来源
```
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Optional, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class CancelEventCancelled(Exception):
    """``CancelEvent.raise_if_cancelled()`` 在事件已 set 时抛出。"""

    def __init__(self, reason: str = "", trace_id: str = "") -> None:
        super().__init__(reason or "cancel event triggered")
        self.reason = reason
        self.trace_id = trace_id


class CancelEvent:
    """协作式取消信号；包 ``asyncio.Event`` + 携带 reason/trace_id。

    线程安全：``cancel()`` 可从任意线程调（asyncio.Event 自身的限制是只能在
    创建它的 event loop 上 ``set()``——这里我们不用 loop-bound API，仅 Event
    内部 wait/is_set，所以 cancel 跨线程调安全）。
    """

    __slots__ = ("_event", "_reason", "_trace_id", "_children")

    def __init__(self, *, trace_id: str = "") -> None:
        self._event = asyncio.Event()
        self._reason: str = ""
        self._trace_id: str = trace_id
        # 弱引用比较繁琐，直接存——子事件 release 后由 parent 的 cancel 不会
        # 拿到陈旧引用因为 cancel 是 idempotent
        self._children: list[CancelEvent] = []

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def trace_id(self) -> str:
        return self._trace_id

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "") -> None:
        """触发取消；idempotent（多次调只记录第一次 reason）。"""
        if self._event.is_set():
            return
        self._reason = reason or "cancelled"
        self._event.set()
        # 级联取消所有子事件
        for child in self._children:
            try:
                child.cancel(reason=f"[parent:{self._trace_id or '?'}] {self._reason}")
            except Exception:
                log.exception(
                    "[CancelEvent] cascade cancel failed parent=%s child=%s",
                    self._trace_id, child._trace_id,
                )

    def raise_if_cancelled(self) -> None:
        """检查点：如果 cancelled 抛 CancelEventCancelled。

        在长跑循环里周期调用——典型用法：每轮迭代开头调一次。
        """
        if self._event.is_set():
            raise CancelEventCancelled(
                reason=self._reason,
                trace_id=self._trace_id,
            )

    async def wait(self) -> None:
        """阻塞等取消触发。可跟其它 awaitable 用 ``asyncio.wait`` 多路等。"""
        await self._event.wait()

    def child(self, *, trace_id: str = "") -> "CancelEvent":
        """派生子 CancelEvent；父取消 → 子自动取消。

        子取消不影响父——单向级联（subagent 完成不该影响 parent）。
        """
        c = CancelEvent(trace_id=trace_id)
        self._children.append(c)
        # 如果 parent 已经 cancelled，立刻级联给 child
        if self._event.is_set():
            c.cancel(reason=f"[parent:{self._trace_id or '?'}] {self._reason}")
        return c


async def wait_with_cancel(
    awaitable: Awaitable[T],
    cancel_event: Optional[CancelEvent],
) -> T:
    """F-3 helper：竞速 await——任 awaitable 完成 *或* cancel_event 触发先到。

    用法（subagent / 阻塞 spawn）：

        result = await wait_with_cancel(engine.spawn_parallel(...), cancel_event)

    行为：

    - ``cancel_event`` 为 None → 直接 ``await awaitable``（行为完全等价）
    - ``awaitable`` 先完成 → 返其结果；watcher 协程被取消清理
    - ``cancel_event.cancel()`` 先触发 → ``awaitable`` 对应 task 被
      ``task.cancel()``，抛 ``CancelEventCancelled``（reason / trace_id 来自
      cancel_event）让上游决定怎么响应

    设计：
    - awaitable 已经是 task → 跟其它路径一致用 wrap_future / shield
    - 否则 ``ensure_future`` 包成 task 后竞速；cleanup 路径无论哪边赢都尝试
      cancel 另一个等待者，避免 leak（可能导致 "Task was destroyed but
      pending" warning）

    注意：``awaitable`` 内部如果不响应 ``CancelledError``（比如调了
    ``shield``），cancel 触发后实际仍会跑到完成——这是 cooperative cancel
    的固有限制。本 helper 只保证 await 控制流立即返回 + 抛
    ``CancelEventCancelled``，不保证底层任务真停。
    """
    if cancel_event is None:
        return await awaitable  # type: ignore[return-value]

    main_task = asyncio.ensure_future(awaitable)
    waiter = asyncio.ensure_future(cancel_event.wait())
    try:
        done, pending = await asyncio.wait(
            {main_task, waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if main_task in done:
            # awaitable 完成：清理 waiter
            if not waiter.done():
                waiter.cancel()
            return main_task.result()
        # cancel_event 先触发：取消 main_task
        main_task.cancel()
        try:
            await main_task
        except (asyncio.CancelledError, BaseException):
            # 内部任何异常 swallow——已经是取消语义
            pass
        raise CancelEventCancelled(
            reason=cancel_event.reason or "cancelled",
            trace_id=cancel_event.trace_id,
        )
    finally:
        # 双保险：确保两个 task 都不会泄漏
        for t in (main_task, waiter):
            if not t.done():
                t.cancel()


__all__ = [
    "CancelEvent",
    "CancelEventCancelled",
    "wait_with_cancel",
]
