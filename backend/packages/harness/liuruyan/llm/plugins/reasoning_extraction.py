"""v2 · H · Slice 5 · Reasoning 标签抽取 plugin.

OpenAI 兼容路径上跑的本地/第三方推理模型（gpt-oss、Gemini OpenAI compat、
Qwen 老路径、seed-oss 等），不发标准 `delta.reasoning_content`，而是在
text 流里嵌 `<think>...</think>` 类标签。本 plugin 用状态机抽这些标签。

支持的标签按 model 路由：
- `gpt-oss-*`：`<reasoning>` / `</reasoning>`
- `gemini-*` (OpenAI compat)：`<thought>` / `</thought>`
- `seed-oss-*`：`<seed:think>` / `</seed:think>`
- 默认（qwen / r1 distilled / 多数本地推理模型）：`<think>` / `</think>`

设计参考: Cherry Studio `reasoningExtractionPlugin.ts`
- 状态机：扫 ContentDelta 流，识别 open_tag → close_tag 之间的内容转
  ReasoningDelta；标签外原样透传 ContentDelta
- 跨 chunk 边界：buffer 累积，partial tag 留下次续

⚠️ 仅在 registry 显式启用时装载（按 model id 路由）。默认 OpenAI 路径
不启用——避免误把正文里的 `<think>` 字面当 reasoning（如代码示例）。
"""

from __future__ import annotations

from typing import AsyncIterator

from ..context import ParseContext
from ..events import ContentDelta, ParsedEvent, ReasoningDelta
from ..protocols import StreamPlugin


def make_reasoning_extraction_plugin(
    *,
    open_tag: str = "<think>",
    close_tag: str = "</think>",
    name: str = "reasoning_extraction",
) -> StreamPlugin:
    """工厂：创建一个绑定标签对的 reasoning extraction plugin.

    Args:
        open_tag: 推理段开始标签
        close_tag: 推理段结束标签
        name: plugin 名（trace / 日志用）
    """

    async def plugin(
        events: AsyncIterator[ParsedEvent],
        ctx: ParseContext,
    ) -> AsyncIterator[ParsedEvent]:
        # 状态机
        in_reasoning = False
        buffer = ""

        async for ev in events:
            if not isinstance(ev, ContentDelta):
                # 非 ContentDelta：先 flush buffer 再透传
                for produced in _flush(buffer, in_reasoning):
                    yield produced
                buffer = ""
                yield ev
                continue

            buffer += ev.text
            while buffer:
                if not in_reasoning:
                    # 找 open_tag
                    idx = buffer.find(open_tag)
                    if idx == -1:
                        # 没找到 open_tag：保留可能 partial tag 的尾部，前面 yield
                        partial_keep = _partial_prefix_len(buffer, open_tag)
                        safe = buffer[:-partial_keep] if partial_keep > 0 else buffer
                        if safe:
                            yield ContentDelta(text=safe)
                        buffer = buffer[-partial_keep:] if partial_keep > 0 else ""
                        break
                    # 找到了：[..., idx) 是正文，从 idx + len(open_tag) 进入 reasoning
                    if idx > 0:
                        yield ContentDelta(text=buffer[:idx])
                    buffer = buffer[idx + len(open_tag):]
                    in_reasoning = True
                else:
                    # 在 reasoning 段：找 close_tag
                    idx = buffer.find(close_tag)
                    if idx == -1:
                        # 没找到 close_tag：保留 partial tag 尾部，前面 yield ReasoningDelta
                        partial_keep = _partial_prefix_len(buffer, close_tag)
                        safe = buffer[:-partial_keep] if partial_keep > 0 else buffer
                        if safe:
                            yield ReasoningDelta(text=safe)
                        buffer = buffer[-partial_keep:] if partial_keep > 0 else ""
                        break
                    # 找到了：[..., idx) 是 reasoning 内容，从 idx + len(close_tag) 退出
                    if idx > 0:
                        yield ReasoningDelta(text=buffer[:idx])
                    buffer = buffer[idx + len(close_tag):]
                    in_reasoning = False

        # 流末（async for 退出）：flush buffer 残余
        for produced in _flush(buffer, in_reasoning):
            yield produced

    plugin.name = name  # type: ignore[attr-defined]
    return plugin


def _flush(buffer: str, in_reasoning: bool) -> list[ParsedEvent]:
    """流末把 buffer 残余 yield 出来。

    残余分两种情况：
    - 流末仍在 reasoning 段（close_tag 未匹配到）→ 残余作 ReasoningDelta
    - 流末在正文段 → 残余作 ContentDelta
    """
    if not buffer:
        return []
    if in_reasoning:
        return [ReasoningDelta(text=buffer)]
    return [ContentDelta(text=buffer)]


def _partial_prefix_len(buffer: str, tag: str) -> int:
    """检测 buffer 末尾是否是 tag 的 partial prefix。

    例：buffer 末尾 "<thi"，tag="<think>" → 返回 4（保留这 4 字符等下次续）。
    没有 partial 时返回 0。
    """
    max_check = min(len(buffer), len(tag) - 1)
    for n in range(max_check, 0, -1):
        if tag.startswith(buffer[-n:]):
            return n
    return 0


__all__ = ["make_reasoning_extraction_plugin"]
