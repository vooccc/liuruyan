"""DangerousCommandProvider — shell 危险命令 regex 拦截。

仅对 `execute_command` / `execute_powershell` 等 shell 类工具生效。命中
DANGEROUS_PATTERNS 任一即 DENY。

不能完全防什么（同 v1 dangerous_patterns docstring）：
- 角色用 shell 写到 backend/* 的文件 → SAFE_ROOT 兜底
- 编码混淆（base64 解码后执行）→ 任何静态 regex 都防不住

跟 v1 `Guardian.intercept` 中危险命令检测分支行为一致。
"""

from __future__ import annotations

from typing import Iterable

from .._internal.dangerous_patterns import detect_dangerous_command
from ..protocol import GuardrailRequest, GuardrailVerdict


# 默认拦截的工具白名单——shell 类。其它工具不查 command 字段。
DEFAULT_SHELL_TOOLS: tuple[str, ...] = (
    "execute_command",
    "execute_powershell",
)


class DangerousCommandProvider:
    """检测 shell 工具的 command 参数是否命中危险模式。"""

    name: str = "dangerous_command"

    def __init__(self, *, shell_tools: Iterable[str] = DEFAULT_SHELL_TOOLS) -> None:
        self._shell_tools: frozenset[str] = frozenset(shell_tools)

    def check(self, req: GuardrailRequest) -> GuardrailVerdict | None:
        if req.tool_name not in self._shell_tools:
            return None
        cmd = req.arguments.get("command")
        if not isinstance(cmd, str) or not cmd:
            return None
        match = detect_dangerous_command(cmd)
        if match:
            return GuardrailVerdict(
                decision="DENY",
                reason=f"危险命令拦截：{match}",
                provider=self.name,
            )
        return None


__all__ = ["DangerousCommandProvider", "DEFAULT_SHELL_TOOLS"]
