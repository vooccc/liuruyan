"""
工具系统统一入口。注册表实现见 backend.tools.builtin.clean_registry。

使用：
    from backend.tools.builtin.clean_registry import registry

    tools = registry.get_tools()
    result = await registry.execute_tool("web_search", {"query": "AI 投资新闻"})
"""

# 导入清理后的工具注册表
from .builtin.clean_registry import registry

def get_tool_registry():
    """获取工具注册表单例"""
    return registry

__all__ = [
    "get_tool_registry",
    "registry",
]
