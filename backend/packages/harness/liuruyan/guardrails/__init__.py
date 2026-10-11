"""liuruyan.guardrails — Phase D · GuardrailProvider 协议 + 4 个 builtin provider。

公开导出：
- 协议：GuardrailProvider / GuardrailRequest / GuardrailVerdict / Decision
- 4 个 builtin：RedZoneProvider / SensitiveContentProvider / SafeRootProvider /
  DangerousCommandProvider

mw 11 GuardrailChainMiddleware 在 D-3 用这些组建链。
v1 Guardian shim 在 D-4 反向 import 这些 provider 替换内部实现。

详见 docs/architecture_v2/03_middlewares.md § 11
   + docs/architecture_v2/01_master_plan.md § Phase D。
"""

from .builtin import (
    DangerousCommandProvider,
    RedZoneProvider,
    SafeRootProvider,
    SensitiveContentProvider,
)
from .protocol import (
    Decision,
    GuardrailProvider,
    GuardrailRequest,
    GuardrailVerdict,
)

__all__ = [
    "DangerousCommandProvider",
    "Decision",
    "GuardrailProvider",
    "GuardrailRequest",
    "GuardrailVerdict",
    "RedZoneProvider",
    "SafeRootProvider",
    "SensitiveContentProvider",
]
