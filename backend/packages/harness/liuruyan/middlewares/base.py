"""v2 · B · Middleware 协议 + 数据契约

每个中间件实现 5 个钩子里需要的部分（不需要的不实现，base 给 no-op 默认）。

设计原则（_docs/architecture_v2/03_middlewares.md § 一）：

1. 异常**绝不向上抛** — 子类钩子内部必须 try/except + log；
   base 不强制套 try/except，是因为某些 middleware（ContextSetup）
   反而需要异常向上抛 abort run（明确例外）。
2. 返回值语义：
   - `dict` → merge 到 state
   - `None` → 不改 state
   - `before_tool` 还可以返回 `ToolCall` 替换原调用
3. 五个钩子**串行**执行（按 build_middleware_chain 注册顺序）。
4. 单次执行 < 200ms（除非 SummarizationGate 等明确 long-running）。

数据契约（state / ctx / tool_call / tool_result）保持最小化：
- `ThreadState` 就是 `dict[str, Any]`，因为不同 phase 会往里加字段；
  常见 key 见 docstring。
- `RunContext` 是 frozen dataclass，五元组 + 业务两元组（跟 ContextVar 同集合）。
- `ToolCall` / `ToolResult` 是中性 dataclass，不绑定具体 LLM 协议
  （消费 v1 OpenAI std messages 时由调用层做 normalize）。
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import Any, ClassVar

# ── 数据契约 ────────────────────────────────────────────────────────────


# `ThreadState` 是 dict[str, Any]，常见 key：
#   messages:    list[dict]   # OpenAI std messages（柳如烟 v1 已对齐）
#   thread_data: dict         # A02 ThreadDataMiddleware 写：workspace_path / uploads_path / ...
#   token_usage: dict         # A10 TokenUsageMiddleware 累加
#   uploaded_files: set[str]  # A05 UploadsInjectMiddleware 跟踪
#   subagent_count: int       # A16 SubagentLimitMiddleware
#
# 用 dict 不强类型，是为了让中间件可以增量加字段；调度层只读自己关心的 key。
ThreadState = dict[str, Any]


@dataclass(frozen=True, slots=True)
class RunContext:
    """单次 run 的上下文五元组 + 业务两元组（跟 runtime.context ContextVar 同集合）。

    Middleware 通过 ctx 参数拿到当前 run 的身份，避免在 hook 里再 import
    `get_effective_user_id()` 等（虽然两条路径都该返回同一个值）。

    frozen=True：middleware 不允许改 ctx；要改 user_id/agent_id 走 with_context。
    """

    user_id: str
    agent_id: str
    session_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    channel: str | None = None
    request_id: str | None = None


@dataclass(slots=True)
class ToolCall:
    """中性 ToolCall — 不绑定 OpenAI / Anthropic 具体格式。

    LLM 协议 → ToolCall 的 normalize 由调用层（agent 主循环）做。
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolResult:
    """工具执行结果。"""

    tool_call_id: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Middleware ABC ────────────────────────────────────────────────────


class AgentMiddleware(ABC):
    """中间件基类。子类实现需要的钩子；不实现的走默认 no-op。

    `name` 类属性必须设置（用于 trace / 日志 / 配置开关）。
    """

    name: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # 抽象基类自身允许 name="";具体子类必须显式设置。
        # `__abstractmethods__` 在某些导入路径下尚未 attach，用 getattr 兜底。
        abstract = getattr(cls, "__abstractmethods__", frozenset())
        if not abstract and not cls.name:
            raise TypeError(
                f"{cls.__name__} must set class-level `name` attribute"
            )

    async def before_agent(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        """主循环开始前。返回 dict 会 merge 到 state，None 不改。"""
        return None

    async def before_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        """每次调 LLM 前。常用于注入 prompt / 修改 messages。"""
        return None

    async def after_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        """LLM 返回后、工具调用前。"""
        return None

    async def before_tool(
        self,
        state: ThreadState,
        tool_call: ToolCall,
        ctx: RunContext,
    ) -> ToolCall | dict[str, Any] | None:
        """单个工具调用前。

        - 返回新 `ToolCall` → 替换原调用（如改参数）
        - 返回 `dict` 含 `short_circuit` key → 短路（如 guardrail deny）
        - 返回 `None` → 不改
        """
        return None

    async def after_tool(
        self,
        state: ThreadState,
        tool_call: ToolCall,
        result: ToolResult,
        ctx: RunContext,
    ) -> dict[str, Any] | None:
        """单个工具结果后。"""
        return None


__all__ = [
    "AgentMiddleware",
    "RunContext",
    "ThreadState",
    "ToolCall",
    "ToolResult",
]
