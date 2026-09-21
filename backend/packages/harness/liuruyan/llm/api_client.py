"""v2 · H · P1-A · ApiClient Protocol + CoreRequest / RequestContext

P1-A：ApiClient 协议层定义（纯加法，不动现有代码）。

参考：
- _docs/handover_p1_p2_apiclient_plan.md § 三 P1-A
- _docs/architecture_v2/01_master_plan.md § D7（LLMAdapter Protocol per provider）
- _research/cherry-studio-main/src/renderer/src/aiCore/clients/（设计参考）

设计意图：
ApiClient 是 provider-agnostic 的 LLM client 协议。每个 provider 一个实现：
- AnthropicApiClient (P1-B)
- OpenAICompatibleApiClient (P1-C)
- GeminiApiClient / BedrockApiClient / Codex... (未来)

生命周期：
1. caller 给 CoreRequest（vendor-agnostic, OpenAI std messages 格式）
2. ApiClient.send_stream(req, ctx) 返 AsyncIterator[ParsedEvent]
3. 内部：CoreRequest → SDK params → SDK stream call → vendor chunk →
   parser → ParsedEvent 流

实现纪律（与 StreamParser 同源）：
1. send_stream 是 async generator（流末必产 RoundEnd 事件）
2. 不读 ContextVar / 全局状态（一切走 ctx 参数）
3. 错误归一化为 LLMError（具体 enum 在 backend 侧；harness 保持纯净）
4. 单文件单 provider，放 liuruyan/llm/clients/<provider>.py（P1-B/C 启用）

⚠️ harness 永不 import backend.* —— 本层仅定义协议 + 实现，
   实算账 / dispatch_batch / chain_hook 等留 backend client。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol

from .events import ParsedEvent


@dataclass(frozen=True)
class CoreRequest:
    """Vendor-agnostic LLM 请求。

    ApiClient 实现把 messages 翻译成 vendor SDK params。

    Fields:
        messages: OpenAI std 格式 list[dict]。Anthropic / Gemini 的转换
            发生在 ApiClient 内部，caller 不感知。
        system: 提取后的 system prompt 串。caller 应已从 messages 里抽出来
            （Anthropic API 要求 system 单独传；OpenAI 也支持顶层 system）。
        tools: OpenAI std tool 定义。Anthropic 的格式转换在 ApiClient 内部做。
        model: 完整 model id（如 "claude-opus-4-7" / "gpt-4o" /
            "deepseek-chat"）。
        stream: 是否流式（默认 True；P1 阶段 ApiClient 仅实现 stream 模式，
            sync 等 P2+）。
        max_tokens: max output tokens。默认 8192。
        extras: provider-specific 字段（如 thinking_budget / cache_control
            opt-in 等）。**不进核心契约**——具体字段由各 ApiClient 实现自行
            约定 + 文档化。

    frozen=True 防止 caller 改 attribute；`extras` / `messages` / `tools`
    内容可变是 Python frozen dataclass 限制（不深复制），caller 自律。
    """

    messages: List[Dict[str, Any]]
    system: Optional[str]
    tools: Optional[List[Dict[str, Any]]]
    model: str
    stream: bool = True
    max_tokens: int = 8192
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestContext:
    """Cross-cutting 上下文（用于日志 / trace / 取消信号）。

    跟 CoreRequest 拆开是因为：CoreRequest 是请求本身，可序列化、可缓存；
    RequestContext 是请求**关于谁**，不可序列化（cancel_event 是 asyncio
    对象）。

    Fields:
        agent_id: 当前 agent（如 "agent_1776142164_4029"）。
        session_id: 会话 id（Session.id）。
        trace_id: 追踪 id，跨 SSE / mw chain 传递。
        cancel_event: asyncio.Event 协作式取消信号。ApiClient 实现应轮询
            此 event，set 时尽快终止 stream。类型保持 Any 不引入 asyncio
            类型依赖（harness 边界纪律）。
    """

    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    trace_id: Optional[str] = None
    cancel_event: Optional[Any] = None


class ApiClient(Protocol):
    """Provider-agnostic LLM client 协议。

    实现者：
    - AnthropicApiClient (P1-B)
    - OpenAICompatibleApiClient (P1-C)
    - GeminiApiClient / BedrockApiClient / Codex... (未来)

    每个实现 = 一个 vendor 协议适配器。

    实现形式可以是类（方法 send_stream）或函数 closure，只要满足 Protocol
    结构性子类型。

    实现纪律：
    1. send_stream 是 async generator，流末必产 RoundEnd 事件
    2. 不读 ContextVar / 全局状态（一切走 ctx 参数）
    3. 错误归一化为 LLMError（具体 enum 在 backend 侧；harness 保持纯净）
    4. 单文件单 provider，放 liuruyan/llm/clients/<provider>.py

    `name` 是诊断字段，与 StreamPlugin.name 用法一致（日志 / trace / 错误归因）。

    为什么不 runtime_checkable：
    - 调用方按 provider 字符串硬路由（registry.get_client）
    - runtime_checkable 会让 isinstance 走 hash 检查变慢
    - 跟 StreamParser / StreamPlugin 风格一致
    """

    name: str

    def send_stream(
        self,
        req: CoreRequest,
        ctx: RequestContext,
    ) -> AsyncIterator[ParsedEvent]: ...


__all__ = ["ApiClient", "CoreRequest", "RequestContext"]
