"""
极致性能的子代理系统

核心特性：
- 并行启动，零等待
- 流式响应，实时进度
- 智能缓存，避免重复计算
- 轻量启动，< 100ms
"""

from .engine import SubAgentEngine, SubAgentStatus, SubAgentResult
from .templates import SUBAGENT_TEMPLATES

__all__ = [
    "SubAgentEngine",
    "SubAgentStatus",
    "SubAgentResult",
    "SUBAGENT_TEMPLATES",
]
