"""v2 · H · P2 · OutgoingPlugin 框架

CoreRequest → CoreRequest 改写插件层。在 ApiClient 调 SDK 之前依次跑，
每个 plugin 接收并返回 CoreRequest（可能改写后的副本）。

设计目的：把散落在 backend client 里的 outgoing 改写逻辑（cache_control /
thinking_kwargs / qwen /think 软开关 / Gemini thought_signature / OVMS
no_think 等）抽成插件链，挂在 ApiClient `_build_sdk_params` 之前。

跟 StreamPlugin（IR → IR）的对偶：
- StreamPlugin 改 vendor SDK 响应（incoming）
- OutgoingPlugin 改 CoreRequest（outgoing，发出去之前）

参考：
- `docs/architecture_spec.md` § 四 P2
- `docs/research_notes.md`
- `references/provider-models`

模块 import 时自动调 `register_builtin_plugins()` 把 P2-B/D 落地的 plugin
全部注册到默认 builder。生产路径任何 `from liuruyan.llm import ...` 都会
触发本模块 import 走完一次。测试路径用 `reset_default_outgoing_plugin_builder()`
+ 手动 `register_builtin_plugins()` 做隔离。
"""

from __future__ import annotations

from .anthropic_cache import AnthropicCachePlugin
from .anthropic_thinking import AnthropicThinkingPlugin
from .anthropic_tools_cache import AnthropicToolsCachePlugin
from .builder import (
    OutgoingPluginBuilder,
    get_default_outgoing_plugin_builder,
    reset_default_outgoing_plugin_builder,
)
from .ovms_no_think import OvmsNoThinkPlugin
from .protocol import OutgoingPlugin
from .qwen_thinking import QwenThinkingPlugin
from .skip_gemini_thought_signature import SkipGeminiThoughtSignaturePlugin


def register_builtin_plugins(builder: OutgoingPluginBuilder | None = None) -> None:
    """把所有 builtin plugin 按 predicate 注册到指定 builder（默认到进程单例）。

    幂等性警告：同一 builder 重复调用会重复注册导致 plugin chain 含多个相同
    实例。生产路径模块 import 时自动调一次；测试 reset 后需要重新调。

    已启用注册：
    1. AnthropicCachePlugin（provider=='anthropic'，改 req.system / extras["system"]）
    2. AnthropicThinkingPlugin（provider=='anthropic'，改 extras["thinking"] /
       extras["temperature"]）
    3. QwenThinkingPlugin（model 含 "qwen3-" 或 "qwen-3-"，按 extras["qwen_thinking_mode"]
       opt-in 追 /think 或 /no_think）
    4. SkipGeminiThoughtSignaturePlugin（provider=='gemini' 或 base_url 含
       generativelanguage.googleapis.com，剥 thought_signature 字段）
    5. OvmsNoThinkPlugin（provider=='ovms' 或 base_url 含 'ovms'，仅工具调用
       场景追 /no_think 自动关 thinking）

    Anthropic 链顺序：cache 先 thinking 后（两者作用维度无重叠，先后等价；
    便于阅读）。Qwen / Gemini / OVMS 是 OpenAI 兼容路径独有，跟 Anthropic
    不冲突。
    """
    if builder is None:
        builder = get_default_outgoing_plugin_builder()

    # ── Anthropic-only plugins（按 provider id 严格匹配）────────────────
    # 注册顺序 = 执行顺序：先 CachePlugin 设好 extras["system"]，
    # ToolsCachePlugin 才能据此判断"system 缓存已激活"再打 tools cache。
    builder.register(
        predicate=lambda p, m, b: p == "anthropic",
        plugin=AnthropicCachePlugin(),
    )
    builder.register(
        predicate=lambda p, m, b: p == "anthropic",
        plugin=AnthropicToolsCachePlugin(),
    )
    builder.register(
        predicate=lambda p, m, b: p == "anthropic",
        plugin=AnthropicThinkingPlugin(),
    )

    # ── OpenAI 兼容路径 model-specific plugins ────────────────────────
    # Qwen3 系列：百炼 / SiliconFlow / OpenRouter aggregator 等任何 OpenAI
    # 兼容协议下跑 qwen3 模型都生效（按 model id 路由，不限 provider）
    builder.register(
        predicate=lambda p, m, b: bool(m) and (
            m.lower().startswith("qwen3-") or m.lower().startswith("qwen-3-")
        ),
        plugin=QwenThinkingPlugin(),
    )

    # Gemini OpenAI 兼容路径：剥 tool_call args 中的 thought_signature 字段
    # 路由：provider id 'gemini' / 'google' 别名 / base_url 含 google
    # generativelanguage 域名（用户自定义 endpoint 也覆盖）
    builder.register(
        predicate=lambda p, m, b: p == "gemini" or (
            b is not None and "generativelanguage.googleapis.com" in b
        ),
        plugin=SkipGeminiThoughtSignaturePlugin(),
    )

    # OVMS 自托管 OpenAI 兼容端点：MCP 工具调用前关 thinking
    # 路由：保守 predicate—— provider id 含 'ovms' 或 base_url 含 'ovms'
    # （OVMS 是用户自部署，没有标准 base_url，用关键字探测；不命中就不跑，
    # 0 影响其他用户）
    builder.register(
        predicate=lambda p, m, b: (p and "ovms" in p.lower()) or (
            b is not None and "ovms" in b.lower()
        ),
        plugin=OvmsNoThinkPlugin(),
    )


# 模块 import 时自动注册（生产路径）
register_builtin_plugins()


__all__ = [
    "OutgoingPlugin",
    "OutgoingPluginBuilder",
    "AnthropicCachePlugin",
    "AnthropicToolsCachePlugin",
    "AnthropicThinkingPlugin",
    "QwenThinkingPlugin",
    "SkipGeminiThoughtSignaturePlugin",
    "OvmsNoThinkPlugin",
    "register_builtin_plugins",
    "get_default_outgoing_plugin_builder",
    "reset_default_outgoing_plugin_builder",
]
