"""v2 · B-14 / C4-b · LLMErrorHandlingMiddleware (mw 06)

钩子：``after_model``

职责：每次 v1 LLM 调用抛出可归一化错误后，通过 ``state["last_llm_error"]``
看到错误信息，emit ``llm_error_observed`` 事件 + 按错误 kind 决策是否
设 ``state["should_end"]`` 让 chain 终止本次工具循环。

参考：docs/architecture_v2/03_middlewares.md § 06

## 阶段

- C4-a (落地)：metric-only emit + 不动 messages
- C4-c (落地)：v1 异常路径调 chain_hook 传 llm_error → state["last_llm_error"]
- **C4-b (本切片)**：按错误 kind 决策 should_end；env LIURUYAN_LLM_ERROR_ACTIVATE=1
  时真接管；OFF 时仍只 emit

跟 mw 09 真接管对照：
- mw 09 真接管 = 调 summarizer LLM + 替换 messages（state["messages"]）
- mw 06 真接管 = 不调 LLM；按 kind 决策让 chain 终止（state["should_end"]）
- 都是"半激活 → 真接管"演进路径

## 决策表

按 ``_classify_llm_exception`` 归一化的 kind：

| kind | retryable? | C4-b 决策 |
|---|---|---|
| ``rate_limit`` | yes | pass-through（v1 retry wrapper 自己重试） |
| ``upstream_busy`` | yes | pass-through |
| ``network_error`` | yes | pass-through |
| ``overloaded`` | yes | pass-through |
| ``one_api_error`` | yes | pass-through |
| ``engine_internal_error`` | yes | pass-through |
| ``auth_error`` | no | should_end（用户感知后修配置再试） |
| ``context_too_long`` | no | should_end（mw 09 摘要应该提前压住，到这里说明超出） |
| ``content_moderation`` | no | should_end（继续也只会继续被审核拒） |
| 其他 unknown | ? | pass-through（保守） |

## flag 控制

- ``LIURUYAN_LLM_ERROR_ACTIVATE=1`` → 真接管（按决策表 set should_end）
- 默认 OFF → 仅 emit（C4-a 行为）

跟 mw 09 ``LIURUYAN_SUMMARIZE_ACTIVATE`` 同模式：保守 default，让用户先观察
trace journal 验证决策合理后再 enable。

## 事件 schema（payload）

```
{
    "kind": str,
    "friendly": str,           # 截断到 500 字符
    "err_type": str,
    "err_str": str,            # 截断到 500 字符
    "activated": bool,         # 是否真接管（flag ON）
    "decision": str,           # "should_end" / "pass_through" / "metric_only"
    "metric_only": bool,       # backward-compat：activated 反向；C4-a 兼容
}
```

异常隔离：内部 try/except；任何异常 swallow + log。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..buses import AgentEvent, get_event_bus
from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


_FRIENDLY_MAX = 500
_ERR_STR_MAX = 500
_ACTIVATE_FLAG_ENV = "LIURUYAN_LLM_ERROR_ACTIVATE"


# 按 kind 决策"是否应该 should_end"
# True  = should_end（non-retryable，继续也无意义）
# False = pass-through（retryable / unknown，让 v1 自己处理）
_KIND_SHOULD_END: dict[str, bool] = {
    # retryable
    "rate_limit": False,
    "upstream_busy": False,
    "network_error": False,
    "overloaded": False,
    "one_api_error": False,
    "engine_internal_error": False,
    # non-retryable
    "auth_error": True,
    "context_too_long": True,
    "content_moderation": True,
}


def _is_activate_enabled() -> bool:
    raw = (os.getenv(_ACTIVATE_FLAG_ENV) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _decide(kind: str) -> bool:
    """按错误 kind 决策是否 should_end。未知 kind 默认 pass-through。"""
    return _KIND_SHOULD_END.get(kind, False)


class LLMErrorHandlingMiddleware(AgentMiddleware):
    name = "llm_error_handling"

    async def after_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            err = state.get("last_llm_error")
            if not err or not isinstance(err, dict):
                return None

            bus = get_event_bus()
            if bus is None:
                log.debug(
                    "LLMErrorHandlingMiddleware: event_bus not configured, skip emit "
                    "(run=%s session=%s)",
                    ctx.run_id, ctx.session_id,
                )
                return None

            kind = str(err.get("kind") or "")
            activated = _is_activate_enabled()

            # C4-b：决策 should_end / pass_through
            should_end = False
            if activated:
                should_end = _decide(kind)
                decision = "should_end" if should_end else "pass_through"
            else:
                decision = "metric_only"

            event = AgentEvent(
                type="llm_error_observed",
                trace_id=ctx.trace_id or "",
                session_id=ctx.session_id or "",
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
                payload={
                    "kind": kind,
                    "friendly": _trunc(str(err.get("friendly") or ""), _FRIENDLY_MAX),
                    "err_type": str(err.get("err_type") or ""),
                    "err_str": _trunc(str(err.get("err_str") or ""), _ERR_STR_MAX),
                    "activated": activated,
                    "decision": decision,
                    "metric_only": not activated,  # backward-compat
                },
            )
            await bus.publish(event)
            log.info(
                "LLMErrorHandling: session=%s agent=%s kind=%s decision=%s trace=%s",
                ctx.session_id, ctx.agent_id, kind, decision, ctx.trace_id,
            )

            # 清掉 state["last_llm_error"]（多轮对话每次错误各 emit 一次）
            ret: dict[str, Any] = {"last_llm_error": None}
            if should_end:
                ret["should_end"] = True
            return ret
        except Exception:
            log.exception(
                "LLMErrorHandlingMiddleware swallowed exception (run=%s)",
                ctx.run_id,
            )
            return None


def _trunc(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "..."


__all__ = ["LLMErrorHandlingMiddleware"]
