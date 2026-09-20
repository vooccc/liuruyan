"""path 段对齐匹配工具。

跟 v1 `backend/tools/defense/guardian.py` 内 `_normalize_path` /
`_segment_endswith` / `_segment_contains` 行为一致——D-4 shim 切换时
v1 不再维护自己的副本，全部走这边。

为什么"段对齐"：路径匹配必须对齐目录边界。`_segment_endswith` 让
`/etc/sudoers.d/myrule` 不会撞红区清单里的 `/etc/sudoers`；`myguardian.py`
不会撞 `guardian.py`。
"""

from __future__ import annotations

import fnmatch
import os


def normalize_path(path: str) -> str:
    """规范化 + 软 realpath；两步都失败时退回 normpath。"""
    try:
        return os.path.normpath(os.path.realpath(path))
    except Exception:
        return os.path.normpath(path)


def segment_endswith(haystack: str, needle: str) -> bool:
    """段对齐 endswith：haystack 以 needle 结尾，且 needle 前一字符是 / 或 string start。"""
    if not haystack.endswith(needle):
        return False
    head_idx = len(haystack) - len(needle)
    return head_idx == 0 or haystack[head_idx - 1] == "/"


def segment_contains(haystack: str, needle: str) -> bool:
    """段对齐 contains：needle 出现在 haystack 中，且后面紧跟 / 或 string end。"""
    idx = haystack.find(needle)
    while idx != -1:
        end = idx + len(needle)
        if end == len(haystack) or haystack[end] == "/":
            return True
        idx = haystack.find(needle, idx + 1)
    return False


def to_unix(path: str) -> str:
    """\\ 全部换 /。便于跨平台对齐匹配。"""
    return path.replace("\\", "/")


def matches_backend_path(normalized: str) -> bool:
    """命中 **/backend/** 算后端目录写。fnmatch 比 substring 更稳。

    用 fnmatch 匹配 `*backend*` 后再用绝对的 sep 测试，避免 `homebackend` 误判。
    """
    if not fnmatch.fnmatch(normalized, "*backend*"):
        return False
    sep_b = os.sep + "backend"
    return sep_b + os.sep in normalized or normalized.endswith(sep_b)


__all__ = [
    "matches_backend_path",
    "normalize_path",
    "segment_contains",
    "segment_endswith",
    "to_unix",
]
