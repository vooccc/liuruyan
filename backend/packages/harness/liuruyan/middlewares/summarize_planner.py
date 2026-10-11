"""v2 · C5-b/c · summarize_planner — 摘要保留策略 + apply 算法 + Summarizer 协议

设计：docs/architecture_v2/03_middlewares.md § 09 SummarizationGateMiddleware

模块分三块：
- ``plan_summarization``（C5-b-1）：纯算法决定哪些保留 / 哪些压缩
- ``apply_summary``（C5-c-1）：纯算法按 plan + summary 文本重写 messages
- ``Summarizer`` 协议（C5-c-1）：异步 ``async (chunk_text) -> str``；
  backend 实现真 LLM 调用（cheaper 模型）；mw 09 通过 DI 拿到

不调 LLM 在本模块——纯算法可独立单测。真 LLM 调用由 backend.channels 等
backend 层（依赖注入）。

保留策略（按 03_middlewares.md § 09）：
1. **system messages**：永远保留（角色 prompt / 注入的 USER+MEMORY 等）
2. **最近 N 轮 user/assistant 对**：N 默认 5
3. **最近 K 个工具调用块**：K 默认 3；一个"工具调用块" = 一条
   ``assistant{tool_calls}`` + 之后所有对应 ``tool`` messages
4. **其余**：压缩

apply_summary 输出新 messages：keep_indices 顺序保留；compress_indices 整块
合并 → 单条 ``role=system`` + ``<previous_conversation_summary>``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

log = logging.getLogger(__name__)


# 摘要 system message 的 tag——03_middlewares.md § 09 要求 "tags=['middleware:summarize']"
# 我们用 inline tag 形式（Anthropic 不支持 message-level tags，用文本嵌入）
_SUMMARY_TAG_OPEN = "<previous_conversation_summary>"
_SUMMARY_TAG_CLOSE = "</previous_conversation_summary>"


_DEFAULT_RECENT_USER_ASSISTANT = 5
_DEFAULT_RECENT_TOOL_BLOCKS = 3


@dataclass(slots=True)
class SummarizationPlan:
    """摘要计划——指导 mw 09 哪些 messages 保留 / 压缩。

    ``keep_indices`` + ``compress_indices`` 互斥；并集 = range(len(messages))。
    """

    keep_indices: list[int]                  # 保留的 messages 索引（升序）
    compress_indices: list[int]              # 待压缩的 messages 索引（升序）
    reasoning: str = ""                      # 人类可读策略描述
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def kept_count(self) -> int:
        return len(self.keep_indices)

    @property
    def compress_count(self) -> int:
        return len(self.compress_indices)


def plan_summarization(
    messages: list[dict[str, Any]],
    *,
    recent_user_assistant: int = _DEFAULT_RECENT_USER_ASSISTANT,
    recent_tool_blocks: int = _DEFAULT_RECENT_TOOL_BLOCKS,
) -> SummarizationPlan:
    """决定 messages 保留 / 压缩计划。

    参数：
    - messages: OpenAI std messages（含 system / user / assistant / tool）
    - recent_user_assistant: 末尾保留 N 轮 user/assistant 对
    - recent_tool_blocks: 末尾保留 K 个 tool_calls 块

    返回 SummarizationPlan。

    算法：
    1. 找所有 system 索引 → 全保留
    2. 倒序扫，识别 tool_calls 块（assistant{tool_calls} + 之后 tool 消息）
       和 user/assistant 对话对
    3. 收集最近 N 轮对话 + 最近 K 个 tool 块的所有 indices
    4. 1+3 并集 = keep；其余 = compress
    """
    n = len(messages)
    if n == 0:
        return SummarizationPlan(
            keep_indices=[], compress_indices=[],
            reasoning="empty messages; nothing to plan",
        )

    keep: set[int] = set()

    # 1. system 永远保留
    system_indices = [i for i, m in enumerate(messages) if _role(m) == "system"]
    keep.update(system_indices)

    # 2. 识别"工具调用块"：assistant{tool_calls} + 紧随的 tool 消息
    # 倒序扫；每遇到 assistant{tool_calls}，往后吃所有连续 tool；构成一个块
    tool_blocks: list[list[int]] = []  # 每个块是 [indices]，按时间序（早→晚）
    i = 0
    while i < n:
        m = messages[i]
        if _role(m) == "assistant" and _has_tool_calls(m):
            block = [i]
            j = i + 1
            while j < n and _role(messages[j]) == "tool":
                block.append(j)
                j += 1
            tool_blocks.append(block)
            i = j
        else:
            i += 1

    # 3. 识别 user/assistant 对话对：连续的 (user, assistant) 不含 tool_calls
    # 简化策略：列出所有非 tool / 非 system 的 user 和 assistant（不含 tool_calls
    # 的 assistant），按时间序。每"轮" = 1 user + 后续紧邻 1 个 plain assistant。
    # 实际更宽容：用户 N 轮 = 末尾 N 个 plain user/assistant message。
    plain_dialog_indices: list[int] = []
    for i, m in enumerate(messages):
        role = _role(m)
        if role == "system" or role == "tool":
            continue
        if role == "assistant" and _has_tool_calls(m):
            continue  # tool_calls 的 assistant 已在 tool 块里
        if role in ("user", "assistant"):
            plain_dialog_indices.append(i)

    # 4. 保留最近 N 轮——简化为最近 N*2 条（user + assistant 各算一条）
    # 注意：N=0 时 recent_dialog_count=0；arr[-0:] == arr[0:] 是全部，不是 []，
    # 所以要单独 short-circuit 0 case
    recent_dialog_count = recent_user_assistant * 2
    if recent_dialog_count > 0:
        if len(plain_dialog_indices) <= recent_dialog_count:
            keep.update(plain_dialog_indices)
        else:
            keep.update(plain_dialog_indices[-recent_dialog_count:])

    # 5. 保留最近 K 个 tool_blocks（同上 N=0 短路）
    if recent_tool_blocks > 0:
        if len(tool_blocks) <= recent_tool_blocks:
            for block in tool_blocks:
                keep.update(block)
        else:
            for block in tool_blocks[-recent_tool_blocks:]:
                keep.update(block)

    keep_sorted = sorted(keep)
    compress_sorted = sorted(set(range(n)) - keep)

    reasoning = (
        f"plan: keep {len(keep_sorted)}/{n} messages "
        f"(system={len(system_indices)}, "
        f"recent_dialog≤{recent_dialog_count}, "
        f"recent_tool_blocks≤{recent_tool_blocks} "
        f"of {len(tool_blocks)} total)"
    )

    return SummarizationPlan(
        keep_indices=keep_sorted,
        compress_indices=compress_sorted,
        reasoning=reasoning,
        metadata={
            "total_messages": n,
            "system_count": len(system_indices),
            "tool_block_count": len(tool_blocks),
            "dialog_count": len(plain_dialog_indices),
            "recent_user_assistant_limit": recent_user_assistant,
            "recent_tool_blocks_limit": recent_tool_blocks,
        },
    )


# ── helpers ────────────────────────────────────────────────────────────


def _role(m: Any) -> str:
    if not isinstance(m, dict):
        return ""
    return str(m.get("role") or "")


def _has_tool_calls(m: Any) -> bool:
    if not isinstance(m, dict):
        return False
    tc = m.get("tool_calls")
    return isinstance(tc, list) and len(tc) > 0


# ── apply: 按 plan + summary 重写 messages ──────────────────────────────


def apply_summary(
    messages: list[dict[str, Any]],
    plan: SummarizationPlan,
    summary_text: str,
) -> list[dict[str, Any]]:
    """按 plan 重写 messages：保留 keep_indices；compress 区域合并成单条
    system message 含 ``<previous_conversation_summary>{summary}</...>``。

    输出顺序：保留**原始 messages 顺序**；compress 段落（连续 compress_indices）
    替换成一条 system message。多个不连续的 compress 段落各产生一条 summary
    system message（极少见，实际 plan 通常 compress 末尾 N 轮以外的连续区间）。

    边界条件：
    - ``plan.compress_indices == []`` → messages 直接返回 deep copy（不动）
    - ``summary_text`` 为空 → compress 区域被 drop（仍保 keep；适用于 summarizer
      失败 fallback——不注入空 summary 减少噪音）

    保 messages 配对契约：plan_summarization 已经保证 ``assistant{tool_calls}``
    跟对应 ``tool`` messages 整块同进同出（要么全 keep 要么全 compress）。本
    函数不再校验，但要相信 plan 输出的不变量。
    """
    if not plan.compress_indices:
        # 全保留 → 直接返回原 messages（浅 copy 避免别名）
        return list(messages)

    n = len(messages)
    keep_set = set(plan.keep_indices)
    compress_set = set(plan.compress_indices)

    # 检测连续 compress 段落（同段落合并成一条 summary system）
    out: list[dict[str, Any]] = []
    in_compress_run = False
    summary_text_clean = (summary_text or "").strip()

    for i in range(n):
        if i in keep_set:
            out.append(messages[i])
            in_compress_run = False
            continue
        if i in compress_set:
            if not in_compress_run:
                # 第一次进入压缩段 → 在此位置插一条 summary system message
                if summary_text_clean:
                    out.append({
                        "role": "system",
                        "content": _wrap_summary(summary_text_clean),
                    })
                in_compress_run = True
            # 后续 compress 索引不重复发——已合并到上面那条
            continue
        # 既不 keep 也不 compress（不应出现，plan 不变量保证）
        log.warning(
            "apply_summary: index %d in neither keep nor compress; passing through",
            i,
        )
        out.append(messages[i])
        in_compress_run = False

    return out


def _wrap_summary(summary_text: str) -> str:
    """把 summary 文本包成带 tag 的 system message content。"""
    return f"{_SUMMARY_TAG_OPEN}\n{summary_text}\n{_SUMMARY_TAG_CLOSE}"


# ── Summarizer 协议（DI 接 backend LLM 调用）────────────────────────────


@runtime_checkable
class Summarizer(Protocol):
    """跑摘要的异步可调用对象。

    由 backend 层（backend/channels/ 或类似）实现：包装 v1 LLMClient.chat 调
    cheaper 模型生成摘要。harness mw 09 通过 DI 拿到 instance，绝不直接 import
    backend.llm_client（boundary 红线）。

    签名：``async (text_to_summarize: str) -> str``
    - 输入：拼好的待压缩消息文本（apply_summary 之前 mw 09 自己拼）
    - 输出：摘要 string；失败抛异常或返空字符串（mw 09 决策 fallback）
    """

    async def __call__(self, text_to_summarize: str) -> str: ...


# 函数签名替代（FactoryFunc 风格；运行时 isinstance(check_callable, Summarizer)
# 不一定 True 因为 Protocol + __call__ 在类外可能不算）
SummarizerFunc = Callable[[str], Awaitable[str]]


__all__ = [
    "SummarizationPlan",
    "Summarizer",
    "SummarizerFunc",
    "apply_summary",
    "plan_summarization",
]
