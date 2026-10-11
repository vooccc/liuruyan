"""Gateway 跨协议互转子模块（M2-B）。

红线：
- 允许 import `..packages.harness.*`（harness 是 LLM 工程层，被设计为可复用）
- 不 import `backend.*`（除了同包 `..providers.schema` `..utils.api_host`，与 adapter.py 一致）
- 不修改 harness 内任何文件；只调用 harness 暴露的 public converter
- 翻译失败时降级 + 打 `[GATEWAY-TRANSLATE-WARN]` 日志，绝不抛进 client 链路

设计文档：`docs/GATEWAY.md`
"""
from .params import (
    openai_request_to_anthropic,
    anthropic_request_to_openai,
)
from .response_nonstream import (
    anthropic_response_to_openai,
    openai_response_to_anthropic,
)

__all__ = [
    "openai_request_to_anthropic",
    "anthropic_request_to_openai",
    "anthropic_response_to_openai",
    "openai_response_to_anthropic",
]
