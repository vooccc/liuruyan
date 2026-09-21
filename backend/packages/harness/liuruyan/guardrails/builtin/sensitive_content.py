"""SensitiveContentProvider — 敏感目录前缀硬保护。

写操作进入 ~/.ssh / ~/.aws / /etc/sudoers.d / agents/templates 等敏感前缀
即 DENY。前缀匹配是 startswith（比红区的 endswith / contains 严格）。

~ 展开在构造时一次完成（os.path.expanduser）；展开前后两种形态都加进
集合，覆盖 normalized path 已展开和未展开两种来源。

跟 v1 `Guardian._matches_sensitive_prefix` 行为一致。
"""

from __future__ import annotations

import os
from typing import Iterable

from .._internal.path_utils import normalize_path, to_unix
from .._internal.redzone_data import SENSITIVE_PREFIXES_STATIC
from ..protocol import GuardrailRequest, GuardrailVerdict


class SensitiveContentProvider:
    """敏感目录前缀匹配。仅拦截 write。"""

    name: str = "sensitive_content"

    def __init__(self, *, extra_prefixes: Iterable[str] = ()) -> None:
        prefixes: set[str] = set()
        for raw in (*SENSITIVE_PREFIXES_STATIC, *extra_prefixes):
            if not raw:
                continue
            try:
                expanded = os.path.expanduser(raw)
            except Exception:
                expanded = raw
            prefixes.add(expanded)
            if expanded != raw:
                prefixes.add(raw)
        self._prefixes: frozenset[str] = frozenset(prefixes)

    def check(self, req: GuardrailRequest) -> GuardrailVerdict | None:
        if req.operation != "write":
            return None
        if not req.paths:
            return None
        for raw in req.paths:
            normalized = normalize_path(raw)
            hit = self._matches(normalized, raw)
            if hit:
                return GuardrailVerdict(
                    decision="DENY",
                    reason=f"敏感目录硬保护：禁止写入 {hit}（凭证/系统配置）",
                    provider=self.name,
                )
        return None

    def _matches(self, normalized: str, original: str) -> str | None:
        norm_unix = to_unix(normalized)
        orig_unix = to_unix(original)
        for prefix in self._prefixes:
            if not prefix:
                continue
            pref_unix = to_unix(prefix)
            if not pref_unix.endswith("/"):
                pref_unix = pref_unix + "/"
            # startswith：路径以前缀开头（绝对路径）
            if norm_unix.startswith(pref_unix) or orig_unix.startswith(pref_unix):
                return prefix
            # 兜底：路径中段出现 /<prefix>/（normalized 已展开 ~ 的情况）
            sentinel = "/" + pref_unix
            if sentinel in norm_unix or sentinel in orig_unix:
                return prefix
        return None


__all__ = ["SensitiveContentProvider"]
