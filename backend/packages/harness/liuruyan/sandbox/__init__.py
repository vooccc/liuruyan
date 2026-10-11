"""liuruyan.sandbox — Phase D · 虚拟路径 + 沙箱。

公开导出：virtual_path 模块的核心 API（from_virtual / to_virtual /
replace_virtual_paths_in_args / replace_virtual_paths_in_command）。

详见 docs/architecture_v2/02_runtime_core.md § 六。
"""

from .virtual_path import (
    DEFAULT_COMMAND_PARAMS,
    DEFAULT_DATA_DIR_ENV,
    DEFAULT_PATH_PARAMS,
    VirtualPathContext,
    VirtualPathError,
    from_context,
    from_virtual,
    get_default_base,
    replace_virtual_paths_in_args,
    replace_virtual_paths_in_command,
    to_virtual,
)

__all__ = [
    "DEFAULT_COMMAND_PARAMS",
    "DEFAULT_DATA_DIR_ENV",
    "DEFAULT_PATH_PARAMS",
    "VirtualPathContext",
    "VirtualPathError",
    "from_context",
    "from_virtual",
    "get_default_base",
    "replace_virtual_paths_in_args",
    "replace_virtual_paths_in_command",
    "to_virtual",
]
