"""v2 · H · OpenAI / OpenAI-compat 协议 stream parser.

迁移自 `backend/llm_client.py:_chat_one_round` chunk loop（行为 1:1 等价）。

参考:
- `docs/architecture_v2/01_master_plan.md` § D7（LLMAdapter Protocol）
- `docs/research_notes.md`（切割面分析）

设计：
- 接收"已经是 async iterable 的 SDK 流"——idle timeout 由 caller 包
  （llm_client._stream_chunks 仍负责）。Parser 只做协议翻译。
- 单 chunk 解析失败 → yield ParseError 继续；流级异常穿透抛出。
- 不读 ContextVar、不调外部副作用——纯协议适配层。
- yield 顺序保证：reasoning_delta / content_delta / tool_call_delta 按帧
  到达顺序产出；usage 在 round_end 之前一次；round_end 最后一次。

⚠️ Slice 1 阶段：tool_call 增量按 vendor 原 index 透传。
   Slice 3 加 tool_call_accumulator plugin 后才改为按 toolCallId 累积。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from ..context import ParseContext
from ..events import (
    ContentDelta,
    ParseError,
    ParsedEvent,
    ReasoningDelta,
    RoundEnd,
    ToolCallDelta,
    Usage,
)
from ..plugins import apply_plugins


async def _raw_openai_events(
    sdk_stream: AsyncIterator[Any],
    ctx: ParseContext,
) -> AsyncIterator[ParsedEvent]:
    """vendor SDK chunk → ParsedEvent 的核心翻译（无 plugin 处理）。

    Slice 1 行为完全保留——应用 plugin 链由外层 `parse_openai_stream` 完成。
    """
    final_usage: Any = None
    finish_reason: str | None = None

    async for chunk in sdk_stream:
        try:
            # 末 chunk：仅 usage、choices 空
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                final_usage = chunk_usage

            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            choice = choices[0]
            if choice is None:
                continue

            # finish_reason: 通常仅最后一帧带真值，每帧覆盖即可
            cur_finish = getattr(choice, "finish_reason", None)
            if cur_finish is not None:
                finish_reason = cur_finish

            delta = getattr(choice, "delta", None)
            if delta is None:
                continue

            # 1. reasoning（GLM/DeepSeek 的 delta.reasoning_content）
            #    跟 content 不互斥——同帧可能都有
            reasoning_text = getattr(delta, "reasoning_content", None)
            if reasoning_text:
                yield ReasoningDelta(text=reasoning_text)

            # 2. content vs tool_calls：原代码 line 1003 `continue` 实现的互斥
            content = getattr(delta, "content", None)
            if content:
                yield ContentDelta(text=content)
                continue

            tool_calls = getattr(delta, "tool_calls", None)
            if tool_calls:
                for tc in tool_calls:
                    fn = getattr(tc, "function", None)
                    yield ToolCallDelta(
                        index=getattr(tc, "index", 0),
                        id=getattr(tc, "id", None),
                        name=getattr(fn, "name", None) if fn is not None else None,
                        arguments=getattr(fn, "arguments", None) if fn is not None else None,
                    )
        except Exception as exc:
            # 单 chunk 解析失败 → 转 ParseError 事件继续
            # （流级异常如 SDK 抛 / 网络断会绕过 try 直接穿透）
            yield ParseError(
                message=f"chunk parse failed: {type(exc).__name__}: {exc}",
                detail={"provider": ctx.provider, "model": ctx.model},
            )

    # 流结束：先 usage（如有）再 round_end
    if final_usage is not None:
        yield Usage(provider=ctx.provider, payload=final_usage)
    yield RoundEnd(finish_reason=finish_reason)


async def parse_openai_stream(
    sdk_stream: AsyncIterator[Any],
    ctx: ParseContext,
) -> AsyncIterator[ParsedEvent]:
    """OpenAI ChatCompletion 流式响应 → ParsedEvent 流（含 plugin 链）。

    Slice 3+：在 raw events 之上叠加 ctx.plugins。空 plugins tuple 时透传。
    """
    raw = _raw_openai_events(sdk_stream, ctx)
    async for ev in apply_plugins(raw, ctx.plugins, ctx):
        yield ev
