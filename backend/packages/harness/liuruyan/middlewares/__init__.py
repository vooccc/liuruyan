"""liuruyan.middlewares — Phase B 中间件链。

公开导出基类 + 数据契约；具体 middleware 按 03_middlewares.md § 二顺序在
Phase B 子刀里逐个加。

已落地：
- B-1a:  基类 + 数据契约
- B-1b:  LoopDetectionMiddleware（08）
- B-2:   DanglingToolCallMiddleware（07）
- B-3:   SubagentLimitMiddleware（16）
- B-4:   ToolErrorRecoveryMiddleware（13）
- B-5:   ClarificationMiddleware（17，链中必须最后）
- B-6:   ContextSetupMiddleware（01，链中必须最先）
- B-7:   UploadsInjectMiddleware（05）
- B-8:   SkillsInjectMiddleware（04）
- B-9:   MiddlewareRunner（链调度器）
- B-10:  ThreadDataMiddleware（02，闭环 Phase D 虚拟路径）
- B-11:  TitleGenMiddleware（14，emit title_gen_requested 让 backend 异步生成标题）
- B-12:  UserMemoryInjectMiddleware（03，C3-a 接管记忆 system message 注入；v1 砍同步）
- B-13:  MemoryQueueMiddleware（15，C3-b metric-only emit memory_refine_requested；v1 主路径仍真触发）
- B-14:  LLMErrorHandlingMiddleware（06，C4-a metric-only emit llm_error_observed；v1 异常归一化仍真触发）
- B-15:  SummarizationGateMiddleware（09，C5-a metric-only emit summarize_threshold_reached；不真压缩 messages）
- B-16:  TokenUsageMiddleware（10，chain 镜像 emit token_usage_recorded；v1 仍真算账，chain 加 trace 维度可观测性）
- D-5:   VirtualPathSubstitutionMiddleware（before_tool 翻译 /mnt/* → 物理路径；必须在 mw 11 GuardrailChain 之前）
"""

from .base import (
    AgentMiddleware,
    RunContext,
    ThreadState,
    ToolCall,
    ToolResult,
)
from .clarification import ClarificationMiddleware
from .context_setup import ContextSetupMiddleware
from .dangling_tool_call import DanglingToolCallMiddleware
from .guardrail_chain import GuardrailChainMiddleware
from .llm_error_handling import LLMErrorHandlingMiddleware
from .loop_detection import LoopDetectionMiddleware
from .memory_queue import MemoryQueueMiddleware
from .runner import MiddlewareRunner
from .sandbox_audit import SandboxAuditMiddleware
from .skills_inject import SkillsInjectMiddleware
from .subagent_limit import SubagentLimitMiddleware
from .summarization_gate import SummarizationGateMiddleware
from .thread_data import ThreadDataMiddleware
from .title_gen import TitleGenMiddleware
from .token_usage import TokenUsageMiddleware
from .tool_error_recovery import ToolErrorRecoveryMiddleware
from .uploads_inject import UploadsInjectMiddleware
from .user_memory_inject import UserMemoryInjectMiddleware
from .virtual_path_substitution import VirtualPathSubstitutionMiddleware

__all__ = [
    "AgentMiddleware",
    "ClarificationMiddleware",
    "ContextSetupMiddleware",
    "DanglingToolCallMiddleware",
    "GuardrailChainMiddleware",
    "LLMErrorHandlingMiddleware",
    "LoopDetectionMiddleware",
    "MemoryQueueMiddleware",
    "MiddlewareRunner",
    "RunContext",
    "SandboxAuditMiddleware",
    "SkillsInjectMiddleware",
    "SubagentLimitMiddleware",
    "SummarizationGateMiddleware",
    "ThreadDataMiddleware",
    "ThreadState",
    "TitleGenMiddleware",
    "TokenUsageMiddleware",
    "ToolCall",
    "ToolErrorRecoveryMiddleware",
    "ToolResult",
    "UploadsInjectMiddleware",
    "UserMemoryInjectMiddleware",
    "VirtualPathSubstitutionMiddleware",
]
