"""MCP server 内置目录（spotlight 数据源）。"""

from .builtin_catalog import BUILTIN_CATALOG, get_catalog_entry  # noqa: F401

__all__ = ["BUILTIN_CATALOG", "get_catalog_entry"]
