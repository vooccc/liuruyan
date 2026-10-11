"""柳如烟 LLM Gateway 子系统（对外 LLM 网关，可插拔可砍除）。

外部客户端（Cursor / Claude Code / Codex / 任意标准 OpenAI 兼容客户端）
通过 `http://<host>:<port>/gateway/v1/*` + 一把 token，调用柳如烟里所有 provider。

设计规范：`docs/GATEWAY.md`
网关安全隔离规范：网关端口隔离与安全沙箱规范

⚠️ 本子系统**严禁** import：
    backend.llm_client / backend.anthropic_client / backend.agent /
    backend.session / backend.transcript / backend.subagent / backend.worker /
    backend.computer_use / backend.mcp / backend.acp / backend.channels /
    backend.cron_scheduler / backend.social_* / backend.memory_*

只允许 import：
    backend.paths / backend.providers.store / backend.providers.schema /
    backend.utils.api_host / backend.config
"""
from __future__ import annotations

from .routes import router

__all__ = ["router"]
