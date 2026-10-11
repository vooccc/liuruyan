"""v2 · D-1 · GuardrailProvider 协议 + 数据契约。

设计原则（docs/architecture_v2/01_master_plan.md § D6 + 03_middlewares.md § 11）：

1. **Protocol 而非继承基类**——duck typing，易测试，不绑死继承树。
2. **同步 check**——provider 内部都是纯计算（regex / 字符串匹配），mw 11
   `before_tool` 是 async 但内部直接同步调；这样 v1 Guardian (D-4 shim) 也能复用。
3. **frozen request**——provider 不能改 req；并发安全。
4. **None 返回值**——provider 不关心这次调用时返 None；mw 11 链式跳到下一个。

decision 字符串（"ALLOW"/"DENY"/"CONFIRM"）和 v1 `backend.tools.defense.guardian.Action.value.upper()`
一一对应，D-4 shim 切换无翻译成本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

Decision = Literal["ALLOW", "DENY", "CONFIRM"]


@dataclass(frozen=True, slots=True)
class GuardrailRequest:
    """Provider check 入参快照（不可变）。

    `paths` 是调用层从 `arguments` 提取的路径字符串元组（绝对/相对都可能，provider
    内部按需 normalize）。`safe_root` / `project_root` 在 mw 11 实例化链时传入；
    provider 自己不读 env / 配置文件——保持纯函数。
    """

    agent_id: str
    tool_name: str
    arguments: dict[str, Any]
    operation: Literal["read", "write", "execute"]
    paths: tuple[str, ...] = ()
    project_root: str | None = None
    safe_root: str | None = None


@dataclass(frozen=True, slots=True)
class GuardrailVerdict:
    """Provider 决定。

    - `decision`: "ALLOW" / "DENY" / "CONFIRM"（与 v1 Action 字符串值大写对齐）
    - `reason`: 给前端 / audit log 看的人类可读原因
    - `provider`: 哪条 provider 命中（自动填，等于 provider.name）
    """

    decision: Decision
    reason: str = ""
    provider: str = ""


@runtime_checkable
class GuardrailProvider(Protocol):
    """同步 Protocol。

    任何类有 `name: str` 类属性 + `check(req) -> Verdict | None` 方法即满足。

    返回值约定：
    - `None`        — 本 provider 不关心这次调用
    - `Verdict(ALLOW)` — 显式放行（一般 provider 不返这个，让链继续走下去）
    - `Verdict(DENY)`  — 拦截（mw 11 把工具结果替换成 error）
    - `Verdict(CONFIRM)` — M2 实时弹窗用，M1 阶段当 DENY 处理
    """

    name: str

    def check(self, req: GuardrailRequest) -> GuardrailVerdict | None:
        ...


__all__ = [
    "Decision",
    "GuardrailProvider",
    "GuardrailRequest",
    "GuardrailVerdict",
]
