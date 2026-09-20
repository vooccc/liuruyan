"""RedZoneProvider — 自进化"内核层"硬保护。

写操作命中下列任一即 DENY，无视任何 override：
- RED_ZONE_STATIC：Guardian / paths / 红区清单文件本身（v1 + v2）
- SENSITIVE_FILES_STATIC：凭证 / shell rc / 系统配置文件名（endswith）
- extra_paths：构造时附加（v1 Guardian.reload_config 会传 guardian_config.json
  和 models.secrets.json 的运行时绝对路径，因为这些路径靠 paths.* 算）

只关心 `operation == "write"`；read 不拦——红区文件只读是允许的。
"""

from __future__ import annotations

from typing import Iterable

from .._internal.path_utils import (
    normalize_path,
    segment_contains,
    segment_endswith,
    to_unix,
)
from .._internal.redzone_data import RED_ZONE_STATIC, SENSITIVE_FILES_STATIC
from ..protocol import GuardrailRequest, GuardrailVerdict


class RedZoneProvider:
    """红区匹配——段对齐 endswith / contains。"""

    name: str = "red_zone"

    def __init__(self, *, extra_paths: Iterable[str] = ()) -> None:
        red: set[str] = set(RED_ZONE_STATIC)
        red.update(SENSITIVE_FILES_STATIC)
        red.update(p for p in extra_paths if p)
        self._red_zone_paths: frozenset[str] = frozenset(red)

    def check(self, req: GuardrailRequest) -> GuardrailVerdict | None:
        if req.operation != "write":
            return None
        if not req.paths:
            return None
        for raw in req.paths:
            normalized = normalize_path(raw)
            if self._matches(normalized, raw):
                return GuardrailVerdict(
                    decision="DENY",
                    reason="Guardian 红区硬保护：禁止修改自身代码/配置（无视任何 override）",
                    provider=self.name,
                )
        return None

    def _matches(self, normalized: str, original: str) -> bool:
        norm_unix = to_unix(normalized)
        orig_unix = to_unix(original)
        for red in self._red_zone_paths:
            if not red:
                continue
            red_unix = to_unix(red)
            if segment_endswith(norm_unix, red_unix) or segment_endswith(
                orig_unix, red_unix
            ):
                return True
            sentinel = "/" + red_unix.lstrip("/")
            if segment_contains(norm_unix, sentinel) or segment_contains(
                orig_unix, sentinel
            ):
                return True
        return False


__all__ = ["RedZoneProvider"]
