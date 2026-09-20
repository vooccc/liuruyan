"""ACP 子系统暴露给柳如烟 LLM 的工具。

Phase 5：`delegate_acp_agent` —— 召唤外部 ACP agent 干活。
"""

from .delegate_acp_agent import register as register_delegate_acp_agent_tool

__all__ = ["register_delegate_acp_agent_tool"]
