"""liuruyan.agents — 主 agent 编排层。

LeadAgent: v2 chain shim — 在 v1 主聊天路径里包一层 MiddlewareRunner，
跑 chain 的 before_agent / before_model / after_model 三个钩子（一次）。
最小 shim：4 个 mw 真生效，4 个降级 metric-only，等工具循环上移再补全。

详见 docs/architecture_v2/03_middlewares.md。
"""

from .lead_agent import LeadAgent

__all__ = ["LeadAgent"]

