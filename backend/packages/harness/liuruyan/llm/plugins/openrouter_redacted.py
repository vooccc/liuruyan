"""v2 · H · Slice 6 · OpenRouter `[REDACTED]` 字面量过滤 plugin.

OpenRouter 在 reasoning 文本里偶尔塞 `[REDACTED]` 字面量（隐私保护）——
这些字面量直接展示给用户既无意义又破坏阅读体验。本 plugin 单字符串
replace 删掉。

参考: Cherry Studio `openrouterReasoningPlugin.ts`

设计：
- 仅作用于 ReasoningDelta（不动 ContentDelta —— 正文里 `[REDACTED]` 可能
  是真实内容如代码示例）
- 简单字符串 replace，不做状态机
- 跨 chunk 边界：buffer 末尾如果是 `[REDACT` 这种 partial 留下次续

⚠️ 仅在 base_url 含 openrouter.ai 时由 registry 装载。其他路径不启用。
"""

from __future__ import annotations

from typing import AsyncIterator

from ..context import ParseContext
from ..events import ParsedEvent, ReasoningDelta
from ..protocols import StreamPlugin


_REDACTED = "[REDACTED]"
_PARTIAL_PREFIX_KEEP = len(_REDACTED) - 1  # 最长可能 partial 长度


def _partial_redacted_suffix_len(text: str) -> int:
    """检测 text 末尾是否是 `[REDACTED]` 的 partial prefix。"""
    max_check = min(len(text), _PARTIAL_PREFIX_KEEP)
    for n in range(max_check, 0, -1):
        if _REDACTED.startswith(text[-n:]):
            return n
    return 0


async def openrouter_redacted(
    events: AsyncIterator[ParsedEvent],
    ctx: ParseContext,
) -> AsyncIterator[ParsedEvent]:
    """OpenRouter [REDACTED] 字面量删除 plugin."""
    buffer = ""

    async for ev in events:
        if not isinstance(ev, ReasoningDelta):
            # 流末/非 ReasoningDelta：先 flush buffer
            if buffer:
                cleaned = buffer.replace(_REDACTED, "")
                if cleaned:
                    yield ReasoningDelta(text=cleaned)
                buffer = ""
            yield ev
            continue

        buffer += ev.text
        # 删已闭合的 [REDACTED]
        if _REDACTED in buffer:
            buffer = buffer.replace(_REDACTED, "")
        # 末尾保留 partial 等下次续
        partial_keep = _partial_redacted_suffix_len(buffer)
        if partial_keep > 0:
            safe = buffer[:-partial_keep]
            if safe:
                yield ReasoningDelta(text=safe)
            buffer = buffer[-partial_keep:]
        else:
            if buffer:
                yield ReasoningDelta(text=buffer)
            buffer = ""

    # 流末（无 RoundEnd 等触发）：flush 残余（可能是真 partial 不在 [REDACTED]，原样发）
    if buffer:
        yield ReasoningDelta(text=buffer)


openrouter_redacted.name = "openrouter_redacted"  # type: ignore[attr-defined]


__all__ = ["openrouter_redacted"]
