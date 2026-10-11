"""v2 · H · Slice 4 · 文本泄漏 tool_call plugin.

vendor 把 native tool_call 当正文文本吐出来时（讯飞 GLM5 等 OpenAI 兼容
路径常见），plugin 扫 ContentDelta 流识别 tool_call 模式 → 转 ToolCallDelta
→ 让下游 tool_call_accumulator 接力累积。

设计：
- 工厂模式：`make_text_leaked_tool_call_plugin(grammar)` 返回绑定 grammar 的
  StreamPlugin。grammar 是 stateful 状态机对象，per-stream 实例（不共享）
- ContentDelta 喂给 grammar；grammar 产 ToolCallDelta + 余下 ContentDelta
- 非 ContentDelta 事件原样透传（先 flush 当前 buffer 防止跨事件错位）
- 流末（RoundEnd 之前）调 grammar.flush()

参考: docs/research_notes.md § 五（讯飞）
"""

from __future__ import annotations

from typing import AsyncIterator

from ..context import ParseContext
from ..events import ContentDelta, ParsedEvent, RoundEnd
from ..protocols import StreamPlugin


def make_text_leaked_tool_call_plugin(
    grammar_factory,
    *,
    name: str = "text_leaked_tool_call",
) -> StreamPlugin:
    """工厂：创建一个绑定 grammar 的 plugin.

    Args:
        grammar_factory: callable that returns a fresh grammar instance per call.
            每个 stream 用独立 grammar 实例（避免跨 stream buffer 污染）。
        name: plugin 名（trace / 日志用）

    Returns:
        StreamPlugin async generator function（满足 Protocol）。
    """

    async def plugin(
        events: AsyncIterator[ParsedEvent],
        ctx: ParseContext,
    ) -> AsyncIterator[ParsedEvent]:
        grammar = grammar_factory()

        async for ev in events:
            if isinstance(ev, ContentDelta):
                for produced_ev in grammar.feed(ev.text):
                    yield produced_ev
            else:
                # 任何非 ContentDelta（含 RoundEnd / Usage / ToolCallDelta / 等）
                # 都先 flush grammar buffer 防文本错位，再透传 ev
                for produced_ev in grammar.flush():
                    yield produced_ev
                yield ev

    plugin.name = name  # type: ignore[attr-defined]
    return plugin


__all__ = ["make_text_leaked_tool_call_plugin"]
