"""4 个 builtin GuardrailProvider。

按风险等级倒序在 mw 11 链中调用：
1. RedZoneProvider           红区硬保护（自进化"内核层"，永远 DENY）
2. SensitiveContentProvider  敏感目录前缀（凭证 / 系统配置）
3. SafeRootProvider          break-glass 写范围锁
4. DangerousCommandProvider  shell 危险命令 regex

每个 provider 可独立开关——UI 上由用户控制（D-4/D-5 落地）。
"""

from .dangerous_command import DangerousCommandProvider
from .red_zone import RedZoneProvider
from .safe_root import SafeRootProvider
from .sensitive_content import SensitiveContentProvider

__all__ = [
    "DangerousCommandProvider",
    "RedZoneProvider",
    "SafeRootProvider",
    "SensitiveContentProvider",
]
