"""v2 · H · StreamParser IR (Internal Representation) 事件类型

参考: docs/architecture_v2/01_master_plan.md § D7（LLMAdapter Protocol）

设计原则：
1. ParsedEvent 是 vendor-agnostic 的内部表示——OpenAI / Anthropic / 任何
   未来 provider 都把 raw chunk 翻译成这套事件，下游消费者只看 IR。
2. 事件粒度对齐 OpenAI 流式协议（delta），Anthropic 路径在 parser 内部
   做 event-type → ParsedEvent 的翻译。
3. dataclass(frozen, slots) — 不可变，跨 await 安全。
4. 为 Slice 3 plugin 框架预留：plugin 在 ParsedEvent 流上做 IR → IR 转换，
   永不直接接触 vendor SDK chunk。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

ParsedEventType = Literal[
    "content_delta",            # 正文增量
    "reasoning_delta",          # 思考增量（含 GLM reasoning_content / Anthropic thinking_delta）
    "tool_call_delta",          # tool_call 增量（id / index / name / arguments 任意子集，跨 chunk 累积由消费者负责）
    "tool_call_complete",       # tool_call 累积完成（由 tool_call_accumulator plugin 产；按 toolCallId 索引）
    "final_assistant_content",  # Anthropic 特有：流末权威 content blocks（final_message）
    "usage",                    # token usage 终值（流末一次）
    "round_end",                # 流结束 + finish_reason
    "error",                    # 解析期非穿透错误（让消费者决定是否重试）
]


@dataclass(frozen=True, slots=True)
class ContentDelta:
    """正文增量。多个 ContentDelta 拼接构成最终 assistant content。"""

    type: Literal["content_delta"] = "content_delta"
    text: str = ""


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    """思考/推理增量。

    覆盖：
    - OpenAI compat 路径的 `delta.reasoning_content`（GLM/DeepSeek 等）
    - Anthropic 路径的 `thinking_delta` 块
    - 后续 Slice 5 reasoning_extraction plugin 从 text 中抽出的标签内容
    """

    type: Literal["reasoning_delta"] = "reasoning_delta"
    text: str = ""


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """tool_call 增量。

    跨 chunk 累积语义对齐 OpenAI SDK：
    - `index` 来自 vendor SDK，是同一轮内 tool_call 在数组中的位置
    - `id` 通常只在第一个 chunk 出现（id 字符串）
    - `name` 通常只在前几个 chunk 累积（function name 字符串片段）
    - `arguments` 跨多 chunk 拼接（function arguments JSON 字符串片段）

    ⚠️ Slice 1 阶段消费者（llm_client）按 index 累积，跟现状一致。
    ⚠️ Slice 3 引入 tool_call_accumulator plugin 后改为按 toolCallId 累积，
       届时 plugin 会输出 tool_call_complete 事件代替本类的逐 delta 形态。
    """

    type: Literal["tool_call_delta"] = "tool_call_delta"
    index: int = 0
    id: str | None = None
    name: str | None = None
    arguments: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """tool_call name 首次就位事件（P1 体感优化，2026-05-16）。

    设计目的：vendor SDK 流式 tool_call 时，name 通常先于 arguments 到达。
    本事件让消费层在 name 一就位（args 还在流）时就能向前端 emit
    `tool_call_started`，让 UI 立即渲染 calling 卡，争取 1-3 秒"边想边出"的
    填充感——而不是等 ToolCallComplete 流末才一次性出现 N 张卡。

    后续 ToolCallComplete 仍在流末发出（带完整 arguments），消费层按
    `id` 配对、覆盖更新前端 segment 即可。
    """

    type: Literal["tool_call_started"] = "tool_call_started"
    id: str = ""                           # toolCallId
    name: str = ""                         # function name（已就位）


@dataclass(frozen=True, slots=True)
class ToolCallComplete:
    """tool_call 累积完成事件——由 `tool_call_accumulator` plugin 在流末前产出。

    设计目的：vendor SDK 的 tool_call 增量协议（OpenAI delta.tool_calls /
    Anthropic input_json_delta）跨 chunk 累积时，**按 toolCallId 索引**比按
    array index 索引更可靠（同名 tool_call 时 array index 可能不稳定）。

    本事件取代 ToolCallDelta 流的逐增量消费——消费层看到本事件时，可以认为
    一个完整的 tool_call 已经组装好（id/name/arguments 全部就位）。

    `arguments_raw` 是累积后的 JSON 字符串（**未** parse），消费层负责调
    `_normalize_tool_arguments` 等做 JSON 解析（保持原 _chat_one_round 行为）。

    ⚠️ 注意：本事件仅在装载 `tool_call_accumulator` plugin 的 ParseContext 下
    才会产出。Anthropic 路径默认**不**装载（用 final_message 作权威源）。
    """

    type: Literal["tool_call_complete"] = "tool_call_complete"
    id: str = ""                           # toolCallId（vendor SDK 给的稳定 id）
    name: str = ""                         # function name（累积完整后）
    arguments_raw: str = ""                # 累积的 JSON 字符串（未 parse）
    thought_signature: Optional[str] = None


@dataclass(frozen=True, slots=True)
class FinalAssistantContent:
    """Anthropic 特有：流末 SDK 提供的权威 content blocks。

    Anthropic 的 `client.messages.stream` 跑完后通过 `await s.get_final_message()`
    返回完整 `Message` 对象，含：
    - 权威 content blocks（text / thinking / tool_use；后者带已 parsed input dict）
    - 权威 stop_reason
    - 权威 usage

    OpenAI 协议没有等价物（usage 在末 chunk、tool_call 累积靠 delta），
    所以本事件**仅 Anthropic parser 产**。

    消费层用法：
    - 优先从 final_message.content 里的 tool_use blocks 拿到 parsed args
      （比 partial_json 累积可靠）
    - assistant_msg 组装时复用 content blocks（含 thinking blocks 完整原文，
      extended thinking 协议要求）
    """

    type: Literal["final_assistant_content"] = "final_assistant_content"
    final_message: Any = None       # vendor SDK final_message 对象（Anthropic Message）


@dataclass(frozen=True, slots=True)
class Usage:
    """流末 token usage。

    Vendor-specific payload 直接透传——避免 IR 层耦合 token 计算口径。
    消费者（llm_client）的 _normalize_*_usage / _record_*_usage 仍走自己的逻辑。

    `provider` 区分翻译来源；payload 是 vendor SDK 原始 usage 对象的引用
    （ChatCompletionChunk.usage / final_message.usage），消费者负责按
    provider 调对应的 normalize 函数。
    """

    type: Literal["usage"] = "usage"
    provider: str = ""              # "openai" | "anthropic" | ...
    payload: Any = None             # vendor SDK 原 usage 对象


@dataclass(frozen=True, slots=True)
class RoundEnd:
    """单轮流式结束。

    `finish_reason` 用 vendor 原值（OpenAI: "stop"/"tool_calls"/"length"...，
    Anthropic: "end_turn"/"tool_use"/"max_tokens"...）。消费者按需 normalize。
    """

    type: Literal["round_end"] = "round_end"
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ParseError:
    """解析期非穿透错误（消费者决定是否重试）。

    单 chunk 解析异常 → ParseError 事件 + 继续流；
    流级别异常（连接断 / SDK 抛 ）→ 直接穿透抛出，不走本事件。
    """

    type: Literal["error"] = "error"
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


# 联合类型——给静态检查 + isinstance 分派用
ParsedEvent = Union[
    ContentDelta,
    ReasoningDelta,
    ToolCallDelta,
    ToolCallStarted,
    ToolCallComplete,
    FinalAssistantContent,
    Usage,
    RoundEnd,
    ParseError,
]


__all__ = [
    "ParsedEventType",
    "ContentDelta",
    "ReasoningDelta",
    "ToolCallDelta",
    "ToolCallStarted",
    "ToolCallComplete",
    "FinalAssistantContent",
    "Usage",
    "RoundEnd",
    "ParseError",
    "ParsedEvent",
]
