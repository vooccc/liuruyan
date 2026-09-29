"""v2 · H · Slice 2 · Anthropic 原生协议 stream parser.

迁移自 `backend/anthropic_client.py:_chat_one_round` 的 stream event loop
（行为 1:1 等价；不含 inner network retry —— 那是 backend client 控制流）。

参考:
- `_docs/architecture_v2/01_master_plan.md` § D7
- `_docs/research_chunk_parsing_topology_2026_05_02.md`（差异点表）

Anthropic 跟 OpenAI 三处差异：

1. **Stream 是 async context manager** — caller 负责 `async with` 进入；
   parser 接收 enter 后的 stream 对象（已 async iterable）

2. **流末 final_message** — SDK 额外调用 `await s.get_final_message()` 返回
   权威 Message 对象。Caller 通过 `final_message_getter` callable 注入；
   parser 流末调一次产 FinalAssistantContent 事件

3. **inner network retry** — 留在 backend client 外层 while True；parser
   仍是单次流的纯解析，异常穿透抛出由外层接

Stream event 类型映射：

| Anthropic event       | ParsedEvent              | 说明 |
|-----------------------|--------------------------|------|
| content_block_start (tool_use)   | ToolCallDelta(index, id, name) | 启动 tool block |
| content_block_start (thinking)   | (无)                            | 状态机标记 |
| content_block_start (text)       | (无)                            | 状态机标记 |
| content_block_delta (text_delta) | ContentDelta(text)              | 正文增量 |
| content_block_delta (thinking_delta) | ReasoningDelta(text)        | 思考增量 |
| content_block_delta (input_json_delta) | ToolCallDelta(index, arguments) | tool args 增量 |
| content_block_stop    | (无)                            | pass |
| message_delta         | (累积 finish_reason)            | stop_reason |

⚠️ ToolCallDelta.index 是 Anthropic content block 的位置（不是 OpenAI tool_calls
数组的 index）—— 跨 vendor 概念差异；消费者按 vendor 自处理。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable

from ..context import ParseContext
from ..events import (
    ContentDelta,
    FinalAssistantContent,
    ParseError,
    ParsedEvent,
    ReasoningDelta,
    RoundEnd,
    ToolCallDelta,
    Usage,
)
from ..plugins import apply_plugins


async def _raw_anthropic_events(
    sdk_stream: AsyncIterator[Any],
    ctx: ParseContext,
    final_message_getter: Callable[[], Awaitable[Any]] | None,
) -> AsyncIterator[ParsedEvent]:
    """Anthropic Messages 流式响应 → ParsedEvent 流。

    Args:
        sdk_stream: 已经 `async with` 进入后的 stream 对象（async iterable
            of `RawMessageStreamEvent`）
        ctx: ParseContext（provider="anthropic"）
        final_message_getter: 可选 callable，返回 awaitable
            `final_message`。流末调一次。Caller 通常传
            `lambda: s.get_final_message()`。

    Yields:
        ParsedEvent 流，顺序：
        - 流中：ContentDelta / ReasoningDelta / ToolCallDelta（按 stream events 翻译）
        - 流末（如 getter 提供）：FinalAssistantContent → Usage → RoundEnd
        - 流末（如 getter 不提供）：仅 RoundEnd
    """
    finish_reason: str | None = None

    async for event in sdk_stream:
        try:
            ev_type = getattr(event, "type", None)

            if ev_type == "content_block_start":
                cb = getattr(event, "content_block", None)
                if cb is None:
                    continue
                cb_type = getattr(cb, "type", None)
                if cb_type == "tool_use":
                    yield ToolCallDelta(
                        index=getattr(event, "index", 0),
                        id=getattr(cb, "id", None),
                        name=getattr(cb, "name", None),
                        # arguments 等 input_json_delta 来填
                    )
                # thinking / text block_start 仅状态标记，不产事件

            elif ev_type == "content_block_delta":
                delta = getattr(event, "delta", None)
                if delta is None:
                    continue
                delta_type = getattr(delta, "type", None)
                if delta_type == "text_delta":
                    yield ContentDelta(text=getattr(delta, "text", "") or "")
                elif delta_type == "thinking_delta":
                    yield ReasoningDelta(text=getattr(delta, "thinking", "") or "")
                elif delta_type == "input_json_delta":
                    yield ToolCallDelta(
                        index=getattr(event, "index", 0),
                        arguments=getattr(delta, "partial_json", "") or "",
                    )

            elif ev_type == "content_block_stop":
                # block 结束标记——parser 不产事件，由消费层按 index 决定何时收尾
                pass

            elif ev_type == "message_delta":
                delta = getattr(event, "delta", None)
                if delta is not None and hasattr(delta, "stop_reason"):
                    sr = getattr(delta, "stop_reason", None)
                    if sr is not None:
                        finish_reason = sr

            # 其他 event types（如 message_start / message_stop）不需要翻译
        except Exception as exc:
            yield ParseError(
                message=f"event parse failed: {type(exc).__name__}: {exc}",
                detail={"provider": ctx.provider, "model": ctx.model},
            )

    # 流末：拿 final_message（含权威 content blocks + usage）
    final_message = None
    if final_message_getter is not None:
        try:
            final_message = await final_message_getter()
        except Exception as exc:
            yield ParseError(
                message=f"final_message fetch failed: {type(exc).__name__}: {exc}",
                detail={"provider": ctx.provider, "model": ctx.model},
            )

    if final_message is not None:
        yield FinalAssistantContent(final_message=final_message)
        usage_obj = getattr(final_message, "usage", None)
        if usage_obj is not None:
            yield Usage(provider=ctx.provider, payload=usage_obj)
        # finish_reason fallback：流中没拿到 → 用 final_message.stop_reason
        if finish_reason is None:
            finish_reason = getattr(final_message, "stop_reason", None)

    yield RoundEnd(finish_reason=finish_reason)


async def parse_anthropic_stream(
    sdk_stream: AsyncIterator[Any],
    ctx: ParseContext,
    *,
    final_message_getter: Callable[[], Awaitable[Any]] | None = None,
) -> AsyncIterator[ParsedEvent]:
    """Anthropic Messages 流式响应 → ParsedEvent 流（含 plugin 链）。

    Slice 3+：在 raw events 之上叠加 ctx.plugins。Anthropic 默认空（用
    final_message 作权威源），但如未来按 model id 启用 reasoning_extraction
    等 plugin 也走这条路径。
    """
    raw = _raw_anthropic_events(sdk_stream, ctx, final_message_getter)
    async for ev in apply_plugins(raw, ctx.plugins, ctx):
        yield ev
