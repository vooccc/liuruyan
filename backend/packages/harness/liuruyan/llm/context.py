"""v2 · H · ParseContext - 单次 stream 解析的上下文

参考: _docs/architecture_v2/01_master_plan.md § D7

设计原则：
1. ParseContext 是 frozen dataclass — parser 不允许改 ctx，跟
   middlewares.RunContext 同模式。
2. 显式传参，**不**走 ContextVar — parser 是无副作用的纯协议适配层，
   ContextVar 体系归 backend 主循环管。
3. plugins 是字段而非全局注册表 — 同一进程内不同 session/agent 可以
   按需用不同的 plugin 链（registry.get_parser 内部按 provider/model/base_url
   选 plugin 集合后传入）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .protocols import StreamPlugin


@dataclass(frozen=True, slots=True)
class ParseContext:
    """parser 跨 chunk 共享的不可变上下文。

    parser 内部累积状态（content / reasoning / tool_calls）放在
    parse_stream 函数的局部变量里，**不**塞进 ctx。

    Fields:
        provider: "openai" | "anthropic" | "openai_compat"
        model: 模型 ID（如 "claude-opus-4-7" / "deepseek-chat" / "astron-code-latest"）
        base_url: vendor endpoint，用于按域名分发讯飞/OpenRouter 等
        plugins: 按声明顺序应用的 IR → IR 插件链；空 tuple 表示不叠加
        metadata: 调用方私有数据（trace_id 等），plugin 可读不可改
    """

    provider: str
    model: str
    base_url: str | None = None
    plugins: tuple["StreamPlugin", ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = ["ParseContext"]
