"""
向后兼容层 - Guardian

保持原有导入路径可用：
from backend.guardian import Guardian, get_guardian
"""

from backend.tools.defense.guardian import Guardian, get_guardian, Action, AgentCapability, AccessRule

__all__ = ["Guardian", "get_guardian", "Action", "AgentCapability", "AccessRule"]
