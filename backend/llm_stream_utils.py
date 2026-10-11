"""
LLM 上游流 chunk-level idle timeout helper（对齐成熟客户端做法）。

## 为什么有这个文件

原四处 `_chat_stream` / `_chat_one_round`（anthropic_client + llm_client）的
idle timeout 写法是：

    last_heartbeat = time.time()
    async for ev in api_client.send_stream(...):
        if time.time() - last_heartbeat > timeout:
            raise LLMError(...)
        last_heartbeat = time.time()
        ...

这是个 P0 隐疾：判断在 `async for` **循环体内**，只有"下一个 ev 到达后"才能跑到
检查。如果上游 provider 静默挂死（TCP 没断、不发任何 chunk 也不发 EOF），
`async for` 永远 await 在 `__anext__()`，循环体永远进不去，timeout 等于摆设。

### 线上事故 2026-05-13

某 OpenAI 兼容反代（`custom-eeoxlk3ftc` / gpt-5.5）在 tool_result 喂回去后
返回 `pre_consume_token_quota_failed` 业务错误，但**不 close stream**：
1. `LLMClient._chat_one_round` 的 `async for ev` 永远 await
2. → `pump_agent` 永不 put `AGENT_SENTINEL` 到 merged_queue
3. → `backend/main.py::chat()` generator 的 `merged_queue.get()` 永远 hang
4. → SSE 永远不 yield `[DONE]`
5. → 前端 `chat.ts::sendMessage` 的 `reader.read()` 永远 done=false
6. → assistant message `isStreaming: true` 永不翻 false
7. → **聊天框光标一直闪，但其实会话已经断了**

## 正确写法（对齐标准流式客户端与 OpenAI SDK 抽象层）

把每次 `__anext__` 用 `asyncio.wait_for` 包起来，timeout 真正落在等待 chunk 的
那个 await 上：

    async for chunk in iter_stream_with_idle_timeout(source, idle_timeout):
        ...

参考实现：`backend/subagent/engine.py::_iter_chunks_with_idle`（subagent 路径
更复杂，要区分首包/工具间/常规三种 idle policy；本 helper 是 chat 主路径的
简化版——所有 chunk 一视同仁套 idle timeout）。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, TypeVar

from .anthropic_client import LLMError, LLMErrorCode

T = TypeVar("T")


async def iter_stream_with_idle_timeout(
    source: AsyncIterator[T],
    idle_timeout: float,
    *,
    label: str = "stream",
) -> AsyncIterator[T]:
    """
    把任意 async iterator 套 chunk-level idle timeout。

    每次取下一个 chunk 用 `asyncio.wait_for(idle_timeout)` 包住；超时则抛
    `LLMError(NETWORK_ERROR, retryable=True)`，跟原四处老写法对外契约对齐
    （`_is_retryable_llm_error` 会识别这种错误进行内层重试 / 外层 fallback）。

    Semantics：
    - `idle_timeout <= 0`：禁用 timeout，等价于直接 `async for`（debug /
      单元测试可用）
    - **首个 chunk 等待也走同样 timeout** —— 跟 subagent engine 不同：那边有
      总 timeout 单独管首包，本路径没有总 timeout 概念，把首包也纳入 idle
      = TTFB 上限，等同于"等了 idle_timeout 秒还没动静就放弃"。默认 180s
      （`OPENAI_STREAM_IDLE_TIMEOUT_MS` / `runtime_budget.json`）对所有正
      常模型 TTFB 都足够；超出说明 provider 有问题，该放弃就放弃。

    Args:
        source: 上游 async iterator（典型：`api_client.send_stream(...)`）
        idle_timeout: 秒，<=0 禁用
        label: 错误消息里带上的标识（"openai" / "anthropic" / "anthropic_tools"
            等），方便日志区分是哪条路径挂了

    Raises:
        LLMError(NETWORK_ERROR, retryable=True): chunk 间隔超 idle_timeout
        其他异常: 透传 source 抛的异常（不吞）
    """
    if idle_timeout <= 0:
        async for item in source:
            yield item
        return

    iterator = source.__aiter__()
    while True:
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            raise LLMError(
                f"Stream idle timeout: no data for {int(idle_timeout)}s ({label})",
                LLMErrorCode.NETWORK_ERROR,
                retryable=True,
            ) from exc
        yield chunk
