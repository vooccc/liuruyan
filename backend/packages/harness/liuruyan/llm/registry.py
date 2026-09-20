"""v2 · H · Stream parser registry — 按 provider/model/base_url 路由

设计：
- 函数式 dispatch（不是注册表类）—— 跟 `backend/llm_adapters/provider.py` 同风格
- 返一个 callable 满足 StreamParser Protocol（async generator function）
- 后续 Slice 3+ 在此根据 base_url / model 决定附带哪些 plugin

⚠️ Slice 1 阶段：仅 OpenAI parser 落地，无 plugin 链。
"""

from __future__ import annotations

from typing import Callable

from .context import ParseContext
from .parsers import parse_anthropic_stream, parse_openai_stream
from .plugins import (
    make_reasoning_extraction_plugin,
    make_text_leaked_tool_call_plugin,
    openrouter_redacted,
    tool_call_accumulator,
)
from .plugins.grammars import PythonCallGrammar
from .protocols import StreamParser, StreamPlugin


def _select_reasoning_tag_pair(model: str) -> tuple[str, str] | None:
    """按 model id 选 reasoning tag pair。

    保守策略：仅对已知协议路径启用——避免误抽正文里的 `<think>` 字面
    （如代码示例里的 HTML/XML）。

    返回 (open_tag, close_tag)；不启用时返 None。
    """
    m = (model or "").lower()
    if "gpt-oss" in m:
        return ("<reasoning>", "</reasoning>")
    if "gemini" in m:
        # OpenAI compat 路径下 Gemini 用 <thought>；ai-sdk 原生路径已有 reasoning-delta
        return ("<thought>", "</thought>")
    if "seed-oss" in m:
        return ("<seed:think>", "</seed:think>")
    # GLM-R1 distilled / Qwen 老路径等本地推理模型常用 <think>
    # 仅当 model id 含 "r1" / "thinking" / "qwen3" 等已知模式才启用
    if "r1" in m or "thinking" in m or "qwen3" in m or "qwq" in m:
        return ("<think>", "</think>")
    return None


def _select_plugins(
    provider: str,
    model: str,
    base_url: str | None,
    get_known_tool_names: Callable[[], set[str]] | None,
) -> tuple[StreamPlugin, ...]:
    """按 provider/model/base_url 选装 plugin 链。

    顺序 = 数据流顺序（先注册的先看到 raw events）。详见
    plugins/base.py:apply_plugins。

    Slice 4 顺序：text_leaked_tool_call → tool_call_accumulator
    - text_leaked 先把 ContentDelta 里伪装的 tool_call 转 ToolCallDelta
    - accumulator 后看 ToolCallDelta（包括上游转过来的），按 toolCallId 累积
      成 ToolCallComplete
    """
    plugins: list[StreamPlugin] = []

    if provider in ("openai", "openai_compat"):
        # Slice 5：reasoning 标签状态机——按 model id 路由 tag pair
        reasoning_pair = _select_reasoning_tag_pair(model)
        if reasoning_pair is not None:
            open_tag, close_tag = reasoning_pair
            plugins.append(
                make_reasoning_extraction_plugin(
                    open_tag=open_tag,
                    close_tag=close_tag,
                    name=f"reasoning_extraction_{open_tag.strip('<>/')}",
                )
            )

        # Slice 6：OpenRouter 路径删 reasoning 里的 [REDACTED] 字面量
        if base_url and "openrouter.ai" in base_url:
            plugins.append(openrouter_redacted)

        # Slice 4：讯飞 OpenAI 兼容路径（base_url 含 xf-yun.com）启用 Python-call
        # grammar 救文本泄漏 tool_call（讯飞 GLM5 案例）
        if base_url and "xf-yun.com" in base_url and get_known_tool_names is not None:
            plugins.append(
                make_text_leaked_tool_call_plugin(
                    grammar_factory=lambda: PythonCallGrammar(get_known_tool_names),
                    name="text_leaked_python_call",
                )
            )
        # Slice 4.5 可在此加 DSML grammar（按 model=deepseek-*）

        # 所有 OpenAI 兼容路径默认按 toolCallId 累积（Slice 3）
        plugins.append(tool_call_accumulator)

    # Anthropic 路径：默认空（final_message 是权威源）；Slice 5+ 才考虑加

    return tuple(plugins)


def get_parser(
    provider: str,
    model: str,
    base_url: str | None = None,
    *,
    get_known_tool_names: Callable[[], set[str]] | None = None,
) -> tuple[StreamParser, ParseContext]:
    """按 provider 路由到对应 parser，并构造 ParseContext。

    支持的 provider：
    - "openai" / "openai_compat"（含讯飞 OpenAI 兼容 / GLM / Kimi / DeepSeek 等）
    - "anthropic"（claude-* 系列）

    Args:
        provider: vendor 标识
        model: 模型 ID（用于 plugin 路由）
        base_url: vendor endpoint（用于按域名路由如讯飞）
        get_known_tool_names: callable 返回当前已注册的工具名集合，给
            text_leaked_tool_call grammar 做白名单过滤用。**必须由 caller
            注入** —— harness 永不直接 import backend.tool_registry。

    Returns:
        (parser, ctx) — caller 调 `async for ev in parser(sdk_stream, ctx, ...): ...`
        ctx.plugins 已由 `_select_plugins` 装配；parser 内部自动应用。
    """
    plugins = _select_plugins(provider, model, base_url, get_known_tool_names)

    if provider in ("openai", "openai_compat"):
        ctx = ParseContext(
            provider=provider,
            model=model,
            base_url=base_url,
            plugins=plugins,
        )
        return parse_openai_stream, ctx

    if provider == "anthropic":
        ctx = ParseContext(
            provider=provider,
            model=model,
            base_url=base_url,
            plugins=plugins,
        )
        return parse_anthropic_stream, ctx

    raise ValueError(
        f"no stream parser registered for provider={provider!r} "
        f"(model={model!r}); supported: openai / openai_compat / anthropic"
    )


__all__ = ["get_parser"]
