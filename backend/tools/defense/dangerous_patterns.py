"""向后兼容 shim — 内部走 v2 `_internal/dangerous_patterns`。

D-4 之后此文件不再维护规则；新增模式去
`backend/packages/harness/liuruyan/guardrails/_internal/dangerous_patterns.py`。

公开符号 `DANGEROUS_PATTERNS` / `detect_dangerous_command` 保持不变，
任何已经 `from backend.tools.defense.dangerous_patterns import ...` 的代码
（包括 v1 Guardian.intercept）零改动继续工作。

允许方向（boundary CI 红线）：backend.* → harness.* 是允许的；
harness.* → backend.* 才禁止。
"""

from __future__ import annotations

from backend.packages.harness.liuruyan.guardrails._internal.dangerous_patterns import (  # noqa: E501
    DANGEROUS_PATTERNS,
    detect_dangerous_command,
)

__all__ = ["DANGEROUS_PATTERNS", "detect_dangerous_command"]
