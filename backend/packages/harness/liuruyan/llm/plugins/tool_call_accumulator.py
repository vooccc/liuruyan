"""v2 · H · Slice 3 · ToolCall 累积器.

按 **toolCallId** 累积 ToolCallDelta 流，流末（RoundEnd 之前）一次性 emit
N 个 ToolCallComplete 事件（每个 toolCall 一个）。

修的潜在 bug（memory: gpt-5.4 工具循环 messages 配对修复）：
- 原 llm_client._chat_one_round 按 `tc.index` 累积 → 同名 tool_call / 跨 chunk
  index 不稳定时累积出错
- Cherry/正确做法：按 `toolCallId` 索引（vendor SDK 给的稳定 id）

行为契约：
- **吞掉** ToolCallDelta 事件（不透传）
- 累积所有 ToolCallDelta，按 toolCallId 索引
- 如 ToolCallDelta 没 id 但有 index → 用上次同 index 见过的 toolCallId 回查
- 如完全没法定位 → 用 `__index_<n>` 作 key 兜底（不应该发生但防御）
- RoundEnd 之前 emit 所有累积的 ToolCallComplete（按 id 排序保证稳定）
- 其他事件（content_delta / reasoning_delta / final_assistant_content / ...）
  原样透传

实例隔离：accumulator 是 plugin 函数的局部变量——每次新调用新 accumulator。
**不**用 class-level static Map（Cherry 那是技术债）。

⚠️ 仅装载在 OpenAI / OpenAI 兼容路径（registry.py 配置）。Anthropic 路径用
final_message 作权威源，不需要本 plugin。
"""

from __future__ import annotations

from typing import AsyncIterator

from ..context import ParseContext
from ..events import (
    ParsedEvent,
    RoundEnd,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStarted,
)


async def tool_call_accumulator(
    events: AsyncIterator[ParsedEvent],
    ctx: ParseContext,
) -> AsyncIterator[ParsedEvent]:
    """按 toolCallId 累积 ToolCallDelta，流末 emit ToolCallComplete."""
    # toolCallId -> {"id", "name", "arguments"}
    accumulator: dict[str, dict[str, str]] = {}
    # vendor index -> toolCallId（input_json_delta 没带 id 时回查用）
    index_to_id: dict[int, str] = {}
    # P1 体感优化：name 一就位立即 emit ToolCallStarted，让前端先渲染 calling 卡。
    # set 记已 emit 过的 tc_id 防重；ToolCallComplete 仍在流末发完整版。
    started_emitted: set[str] = set()

    async for ev in events:
        if isinstance(ev, ToolCallDelta):
            # 定位 toolCallId（三层 fallback）：
            # 1) ev.id 直接给的真 id
            # 2) ev.index 之前见过的 toolCallId（input_json_delta 没带 id 时）
            # 3) `__index_<n>` 兜底（完全没 id，不该发生但防御）
            if ev.id:
                # 真 id 来了：如果该 index 之前用了 fallback key，rekey 升级
                old_key = index_to_id.get(ev.index)
                if old_key and old_key != ev.id and old_key in accumulator:
                    accumulator[ev.id] = accumulator.pop(old_key)
                    accumulator[ev.id]["id"] = ev.id
                tc_id = ev.id
            elif ev.index in index_to_id:
                tc_id = index_to_id[ev.index]
            else:
                tc_id = f"__index_{ev.index}"
            index_to_id[ev.index] = tc_id

            if tc_id not in accumulator:
                accumulator[tc_id] = {"id": tc_id, "name": "", "arguments": ""}
            entry = accumulator[tc_id]
            if ev.name:
                entry["name"] += ev.name
            if ev.arguments:
                entry["arguments"] += ev.arguments
            # P1 体感优化：name 一就位（即使首个 chunk 只是 "sea"）立即透传一个
            # ToolCallStarted——前端 by id 配对、收到 Complete 时覆盖 name 即可。
            # 防重：每个 tc_id 最多 emit 一次 started。
            if entry["name"] and tc_id not in started_emitted:
                started_emitted.add(tc_id)
                yield ToolCallStarted(id=entry["id"], name=entry["name"])
            # 吞掉 ToolCallDelta，不透传
            continue

        if isinstance(ev, RoundEnd):
            # 流末：先 emit 所有累积体，再 emit RoundEnd
            for entry in sorted(accumulator.values(), key=lambda e: e["id"]):
                yield ToolCallComplete(
                    id=entry["id"],
                    name=entry["name"],
                    arguments_raw=entry["arguments"],
                )
            accumulator.clear()
            index_to_id.clear()
            started_emitted.clear()
            yield ev
            continue

        # 其他事件原样透传
        yield ev


# 标记 name 字段（StreamPlugin Protocol 要求）
tool_call_accumulator.name = "tool_call_accumulator"  # type: ignore[attr-defined]


__all__ = ["tool_call_accumulator"]
