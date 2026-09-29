"""
工具防御系统 - 统一入口

集成：
1. Guardian - 权限守卫（角色权限控制）
2. ToolResultBudget - 结果预算管理（防止 token 溢出）
3. JSON 防御 - 4 层 JSON 修复和验证（未来集成）
"""

from .guardian import Guardian, get_guardian, Action, AgentCapability, AccessRule
from .budget import ToolResultBudget

__all__ = [
    "Guardian",
    "get_guardian",
    "Action",
    "AgentCapability",
    "AccessRule",
    "ToolResultBudget",
]
