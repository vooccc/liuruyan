"""v2 · H · LLM stream parser layer.

参考:
- `docs/architecture_v2/01_master_plan.md` § D7（LLMAdapter Protocol per provider）
- `docs/architecture_v2/02_runtime_core.md` § 二.2（harness/llm/ 占位）
- `docs/research_notes.md`（Cherry plugin 翻译地图）
- `docs/research_notes.md`（切割面分析）

三层 IR 模型（详见 plan 文件）：
- Layer 1 (parser): vendor SDK chunk → ParsedEvent
- Layer 2 (plugin): IR → IR 转换（Slice 3+ 启用）
- Layer 3 (consumer): backend `llm_client._chat_one_round` 翻译回老 yield 事件

公开符号：
- `get_parser(provider, model, base_url)` — 唯一入口
- `ParseContext`, `StreamParser`, `StreamPlugin` — 协议契约
- `ParsedEvent` 及其子类 — IR 事件

⚠️ harness 永不 import backend.* / app.* —— 本层仅定义协议 + 实现，
   实算账 / dispatch_batch / chain_hook 等留 backend client。
"""

from __future__ import annotations

from .api_client import ApiClient, CoreRequest, RequestContext
from .context import ParseContext
from .outgoing_plugins import (
    AnthropicCachePlugin,
    AnthropicThinkingPlugin,
    OutgoingPlugin,
    OutgoingPluginBuilder,
    OvmsNoThinkPlugin,
    QwenThinkingPlugin,
    SkipGeminiThoughtSignaturePlugin,
    register_builtin_plugins,
)
from .outgoing_plugins.builder import (
    get_default_outgoing_plugin_builder,
    reset_default_outgoing_plugin_builder,
)
from .provider_def import ProviderDef
from .provider_registry import (
    ProviderNotFoundError,
    ProviderRegistry,
    get_default_registry,
    reset_default_registry,
)
from .events import (
    ContentDelta,
    FinalAssistantContent,
    ParseError,
    ParsedEvent,
    ParsedEventType,
    ReasoningDelta,
    RoundEnd,
    ToolCallComplete,
    ToolCallDelta,
    Usage,
)
from .protocols import StreamParser, StreamPlugin
from .registry import get_parser

__all__ = [
    # registry
    "get_parser",
    # context
    "ParseContext",
    # protocols
    "StreamParser",
    "StreamPlugin",
    # api client (P1-A)
    "ApiClient",
    "CoreRequest",
    "RequestContext",
    # provider registry (P1-D-1)
    "ProviderDef",
    "ProviderRegistry",
    "ProviderNotFoundError",
    "get_default_registry",
    "reset_default_registry",
    # outgoing plugins (P2-A/B/D)
    "OutgoingPlugin",
    "OutgoingPluginBuilder",
    "AnthropicCachePlugin",
    "AnthropicThinkingPlugin",
    "QwenThinkingPlugin",
    "SkipGeminiThoughtSignaturePlugin",
    "OvmsNoThinkPlugin",
    "register_builtin_plugins",
    "get_default_outgoing_plugin_builder",
    "reset_default_outgoing_plugin_builder",
    # events
    "ParsedEvent",
    "ParsedEventType",
    "ContentDelta",
    "ReasoningDelta",
    "ToolCallDelta",
    "ToolCallComplete",
    "FinalAssistantContent",
    "Usage",
    "RoundEnd",
    "ParseError",
]
