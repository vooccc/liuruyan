"""
向后兼容层 - ToolResultBudget

保持原有导入路径可用：
from backend.tool_result_budget import ToolResultBudget
"""

from backend.tools.defense.budget import ToolResultBudget

__all__ = ["ToolResultBudget"]
