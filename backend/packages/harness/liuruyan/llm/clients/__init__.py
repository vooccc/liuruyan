"""v2 · H · ApiClient 实现层（per provider 一个文件）。

P1-A 定义了 ApiClient Protocol（`liuruyan.llm.api_client`）。
本子包是各 provider 的实现：

- `anthropic_message_converter.py`: OpenAI std messages → Anthropic content blocks
  （搬自 backend/llm_adapters/anthropic_native.py，原文件改为 thin shim）
- `anthropic.py` (P1-B-2): AnthropicApiClient 实现
- `openai_compat.py` (P1-C): OpenAICompatibleApiClient 实现（未启用）
- `gemini.py` / `bedrock.py` / ...: 未来扩展

实现纪律详见 `liuruyan.llm.api_client.ApiClient` docstring。
"""
from __future__ import annotations
