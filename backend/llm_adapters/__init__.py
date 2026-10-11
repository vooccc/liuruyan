"""LLM provider adapters: 把 v3 写盘格式（Anthropic content blocks）归一化到 OpenAI
ChatCompletion 标准消息序列，然后按目标 provider 转换成对应 API 格式。

设计参考：`references/llm-adapters`。

阶段进度（见 `docs/architecture_spec.md`）：
- A：history_entry_to_openai_std（本模块）—— v3 → OpenAI std 归一化 ✓
- B/C：build_messages 接入 adapter ✓
- D：写盘统一 v3 + build_messages_v2 改名回 build_messages ✓
- E (2026-05-20)：盘上 v1/v2 残留扫描 0 条后，清掉兼容分支 + 函数文件改名 ✓
"""

from .history_to_openai_std import history_entry_to_openai_std
from .anthropic_native import to_anthropic_messages, convert_tools_to_anthropic
from .openai_compat import to_openai_messages
from .provider import infer_provider_kind, clear_provider_cache, ProviderKind

__all__ = [
    "history_entry_to_openai_std",
    "to_anthropic_messages",
    "convert_tools_to_anthropic",
    "to_openai_messages",
    "infer_provider_kind",
    "clear_provider_cache",
    "ProviderKind",
]
