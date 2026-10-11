"""SafeRootProvider — break-glass 写范围锁。

env `LIURUYAN_WRITE_SAFE_ROOT` 配置后：所有 write 操作必须落在该根之下，
否则 DENY。env 未配 → provider 不关心（None）。

跟 v1 `Guardian._is_under_safe_root` 行为一致。
SAFE_ROOT 在构造时由调用层（mw 11 / v1 shim）传入；provider 自己不读 env。
"""

from __future__ import annotations

from .._internal.path_utils import normalize_path, to_unix
from ..protocol import GuardrailRequest, GuardrailVerdict


class SafeRootProvider:
    """SAFE_ROOT 范围锁。仅拦截 write，且 req.safe_root 非空时生效。"""

    name: str = "safe_root"

    def check(self, req: GuardrailRequest) -> GuardrailVerdict | None:
        if req.operation != "write":
            return None
        safe_root = req.safe_root
        if not safe_root:
            return None
        if not req.paths:
            return None
        # safe_root 自己也 normalize — 否则 Windows 上 realpath 拼 cwd 后形态不对称
        safe_root_norm = normalize_path(safe_root)
        for raw in req.paths:
            normalized = normalize_path(raw)
            if not self._is_under(normalized, safe_root_norm):
                return GuardrailVerdict(
                    decision="DENY",
                    reason=f"超出 SAFE_ROOT 范围：所有写操作必须在 {safe_root} 下",
                    provider=self.name,
                )
        return None

    @staticmethod
    def _is_under(normalized: str, safe_root: str) -> bool:
        norm_unix = to_unix(normalized)
        root_unix = to_unix(safe_root).rstrip("/") + "/"
        return norm_unix == root_unix.rstrip("/") or norm_unix.startswith(root_unix)


__all__ = ["SafeRootProvider"]
