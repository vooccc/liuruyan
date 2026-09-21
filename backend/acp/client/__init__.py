"""柳如烟 ACP client：方向 B —— 在柳如烟里召唤外部 ACP agent（如 Claude Code）。

设计文档：@_docs/acp_integration_design.md §6

Phase 5 MVP 范围：
- 暴露单工具 `delegate_acp_agent(agent, prompt, cwd?)` 给 LLM
- 支持 hardcoded agent: `claude-code` (npx @zed-industries/claude-code-acp)
- 每次 spawn 新子进程（无 connection 复用，Phase 5.5 加）
- 反向能力：read_text_file / write_text_file 直接走本地 IO；terminal 拒绝
- request_permission 自动 allow（用户已经主动召唤了 → 默认信任，Phase 5.5 加批准 UI）

Phase 5.5（不在 MVP）：
- connection 复用（同 (agent, cwd) 复用进程）
- 多模态 / image block 透传
- 反向 terminal/* 接 command_manager
- 在柳如烟前端展示外部 agent 流式输出（卡片化）
"""

__all__ = []
