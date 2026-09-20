"""
柳如烟智能体后端
"""
# v2 harness 包路径注入（零配置启动；不强制 editable install）
# 详见 _docs/architecture_v2/04_phase_a.md A1
import sys as _sys
from pathlib import Path as _Path
_HARNESS_PATH = _Path(__file__).parent / "packages" / "harness"
if _HARNESS_PATH.exists() and str(_HARNESS_PATH) not in _sys.path:
    _sys.path.insert(0, str(_HARNESS_PATH))
del _sys, _Path, _HARNESS_PATH

from .config import ConfigLoader, get_config_loader

__all__ = ["ConfigLoader", "get_config_loader"]
