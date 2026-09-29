"""v2 · D-5 · VirtualPathSubstitutionMiddleware — 工具 args 虚拟路径透明翻译

钩子：``before_tool``

职责：每次工具调用前，把 arguments 里的 ``/mnt/user-data/workspace`` 等虚拟
路径替换成物理路径（``{base}/users/{uid}/agents/{aid}/threads/{tid}/workspace``
等），让真工具（read_file / bash / write_file 等）拿到物理路径直接干活。

参考：_docs/architecture_v2/06_security_and_guardrails.md § 五（虚拟路径）+
``sandbox/virtual_path.py``

## 链上位置

**必须在 mw 11 GuardrailChain 之前**——mw 11 的 SafeRootProvider 检测路径
是否在 SAFE_ROOT 子树内，必须先翻译成物理路径再校验，否则 ``/mnt/*`` 永远
不在物理 SAFE_ROOT 里 → 误 DENY 整个工具调用。

```
chain 顺序：
  ...
  mw 02 ThreadData                    # 准备 thread_data 路径上下文
  mw N  VirtualPathSubstitution ←  本切片新加（在 11 之前）
  mw 11 GuardrailChain (SafeRoot)     # 现在能看到物理路径，正确校验
  mw 12 SandboxAudit
  mw 13 ToolErrorRecovery
  ...
```

## 行为契约

1. **args 不含 /mnt/* → 不动**：返 None（runner 跳过）
2. **args 含 /mnt/* → 返新 ToolCall**：runner 替换 tool_call，继续往下传给
   后续 mw（含 mw 11 SafeRoot 校验物理路径）
3. **ctx 缺 user_id/agent_id/session_id → 优雅降级**：用 ContextVar 默认值
   兜底（``from_context`` 内部已处理）；session_id 缺时只能翻译 agent-level
   挂载点（/mnt/agent-data / /mnt/skills）
4. **异常 swallow + log**：路径翻译失败不阻断工具调用——返 None 让原 args
   继续走（v1 行为）

## 跟 v1 dispatch_batch 的接力

v1 ``dispatch_batch`` line 319-323 已经支持 ``replaced_args`` 协议——
``LeadAgent._build_v1_pre_hook`` 检测 ``effective_tc.arguments != v2_tc.arguments``
时返 ``{"replaced_args": ...}``，dispatch_batch 用新 args 调真 execute。
本 mw 改 ToolCall.arguments → runner 经 LeadAgent v1 hook → dispatch_batch
拿到 replaced_args → 真工具 execute(物理路径)。

异常隔离：内部 try/except；任何异常 swallow + log。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import AgentMiddleware, RunContext, ThreadState, ToolCall

log = logging.getLogger(__name__)


class VirtualPathSubstitutionMiddleware(AgentMiddleware):
    name = "virtual_path_substitution"

    async def before_tool(
        self,
        state: ThreadState,
        tool_call: ToolCall,
        ctx: RunContext,
    ) -> ToolCall | dict[str, Any] | None:
        try:
            args = tool_call.arguments
            if not isinstance(args, dict) or not args:
                return None

            # 快速预检：args 里有任何字符串以 /mnt/ 开头才进入翻译
            # （避免每次都构造 vpc + 跑全量 replace；read_file 等工具调用频次高）
            if not _args_contain_virtual_path(args):
                return None

            # lazy import 避免模块加载期跑 sandbox 子模块
            from ..sandbox.virtual_path import (
                from_context,
                replace_virtual_paths_in_args,
            )

            vpc = from_context(
                user_id=ctx.user_id,
                agent_id=ctx.agent_id,
                session_id=ctx.session_id,
            )
            new_args = replace_virtual_paths_in_args(args, vpc)

            if new_args == args:
                return None  # 没真发生替换（可能 path 字段不在 path_params 里）

            log.debug(
                "VirtualPathSubstitution: tool=%s args replaced "
                "(user=%s agent=%s session=%s)",
                tool_call.name,
                ctx.user_id, ctx.agent_id, ctx.session_id,
            )

            # 返新 ToolCall——runner 替换 current 继续传给后续 mw
            return ToolCall(
                id=tool_call.id,
                name=tool_call.name,
                arguments=new_args,
            )
        except Exception:
            log.exception(
                "VirtualPathSubstitutionMiddleware swallowed exception "
                "(tool=%s run=%s)",
                tool_call.name, ctx.run_id,
            )
            return None


def _args_contain_virtual_path(args: dict[str, Any]) -> bool:
    """快速预检：args 里有任何字符串值以 /mnt/ 开头。

    递归看 dict / list；保守匹配 ``/mnt/`` 前缀（``replace_virtual_paths_in_args``
    内部会精确匹配 5 个挂载点，预检宽松不影响正确性，只影响性能：预检命中
    时进入精确翻译；不命中直接 return None 跳过。
    """
    return _value_contains_virtual_path(args)


def _value_contains_virtual_path(v: Any) -> bool:
    if isinstance(v, str):
        return "/mnt/" in v
    if isinstance(v, dict):
        return any(_value_contains_virtual_path(x) for x in v.values())
    if isinstance(v, list):
        return any(_value_contains_virtual_path(x) for x in v)
    return False


__all__ = ["VirtualPathSubstitutionMiddleware"]
