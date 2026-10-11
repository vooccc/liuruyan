"""v2 · H · Slice 3 · Plugin 链组合器.

设计：**plugins 顺序 = 数据流顺序**（直观语义，跟 stream transformStream
pipeline 自然匹配）。

```
plugins = [A, B, C]        # 顺序 = pipeline 顺序
events = raw_events
for plugin in plugins:     # A → B → C
    events = plugin(events, ctx)  # A(raw) → B(A(raw)) → C(B(A(raw)))
```

数据流向：raw → A → B → C → consumer
- A 最先看到 raw events
- C 最后处理（紧邻 consumer）

这是**流处理 pipeline 的直观语义**，采用标准数据流转换模型。

具体应用举例（Slice 4 之后）：
plugins = [text_leaked_tool_call, tool_call_accumulator]
- text_leaked 先看 raw ContentDelta，识别伪装文本，转 ToolCallDelta
- accumulator 后看 ToolCallDelta（包括上游转过来的），按 toolCallId 累积
  成 ToolCallComplete

实例隔离：每次 apply_plugins 调用产生独立的累积器（plugin 内部的局部变量），
跨调用不共享——Python 默认就是 per-call 隔离。**不要**用 class-level
mutable state。
"""

from __future__ import annotations

from typing import AsyncIterator

from ..context import ParseContext
from ..events import ParsedEvent
from ..protocols import StreamPlugin


async def apply_plugins(
    events: AsyncIterator[ParsedEvent],
    plugins: tuple[StreamPlugin, ...],
    ctx: ParseContext,
) -> AsyncIterator[ParsedEvent]:
    """按声明顺序应用 plugin 链（顺序 = 数据流顺序）。

    plugins 为空时透传 events 不变。
    """
    if not plugins:
        async for ev in events:
            yield ev
        return

    pipeline = events
    for plugin in plugins:
        pipeline = plugin(pipeline, ctx)

    async for ev in pipeline:
        yield ev


__all__ = ["apply_plugins"]
