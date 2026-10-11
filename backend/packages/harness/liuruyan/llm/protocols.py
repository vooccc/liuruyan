"""v2 · H · StreamParser / StreamPlugin Protocol 类型契约

参考: docs/architecture_v2/01_master_plan.md § D7

为什么 Protocol 而非 ABC：
- D7 决策原话："LLMAdapter 走 Protocol，每个 provider 一个文件"
- Parser 实现是 async generator 函数（不是类），Protocol 是结构性子类型，
  函数也能满足
- ABC 强制继承会让"加 provider = 写一个文件"这条路变重

为什么不 runtime_checkable：
- 我们在 registry 里按 provider 字符串硬路由，不需要 isinstance 分派
- runtime_checkable 会绕开 hash 检查变慢
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, Any

from .context import ParseContext
from .events import ParsedEvent


class StreamParser(Protocol):
    """vendor SDK 流式响应 → ParsedEvent 流的解析器协议。

    一个 parser 实现 = 一个 provider 的协议适配器。

    实现形式自由：
    - async generator function: `async def parse_xxx(sdk_stream, ctx): ... yield ...`
    - 类的 `__call__` async generator method
    - 任何 callable 接受 `(sdk_stream, ctx)` 返 `AsyncIterator[ParsedEvent]`

    实现纪律：
    1. 不读 ContextVar / 全局状态
    2. 不调外部副作用（HTTP / 文件 / DB）
    3. 单 chunk 解析失败 → yield ParseError 事件 + 继续
       流级别异常（SDK 抛）→ 不捕获穿透抛出
    4. yield Usage 事件最多一次（流末）
    5. yield RoundEnd 事件恰一次（流末，最后）
    """

    def __call__(
        self,
        sdk_stream: AsyncIterator[Any],
        ctx: ParseContext,
    ) -> AsyncIterator[ParsedEvent]: ...


class StreamPlugin(Protocol):
    """IR → IR 插件协议（Slice 3+ 启用）。

    一个 plugin = 一个协议怪癖修复（如 tool_call 累积、文本泄漏检测、
    reasoning 标签抽取）。

    实现纪律（同 StreamParser）：
    1. 输入输出都是 ParsedEvent 流——不直接接触 vendor SDK chunk
    2. 无副作用、无 ContextVar
    3. 顺序约束：插件按 ParseContext.plugins tuple 声明顺序应用，
       靠前的插件在最外层包装（先看到事件、最后产事件）
       —— 对应标准洋葱模型中间件（Middleware reverse wrap）的语义

    `name` 是诊断字段：日志 / trace / 错误归因用。
    """

    name: str

    def __call__(
        self,
        events: AsyncIterator[ParsedEvent],
        ctx: ParseContext,
    ) -> AsyncIterator[ParsedEvent]: ...


__all__ = ["StreamParser", "StreamPlugin"]
