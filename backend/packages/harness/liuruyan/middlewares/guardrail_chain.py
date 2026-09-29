"""v2 · D-3 · GuardrailChainMiddleware (mw 11)

before_tool 钩子调 GuardrailProvider 链做 allow/deny。

链顺序按风险等级倒序（_docs/architecture_v2/03_middlewares.md § 11）：

  1. RedZoneProvider           红区硬保护（自进化"内核层"）
  2. SafeRootProvider          break-glass 写范围锁
  3. DangerousCommandProvider  shell 危险命令 regex
  4. SensitiveContentProvider  敏感目录前缀

任一 provider 返 DENY/CONFIRM → 立即短路（不再调用后续 provider，也不
执行工具）。短路通过 returning `{"short_circuit": ToolResult(error=...)}`
完成，runner 内部检测 short_circuit 把它当工具结果。

每个 provider 可独立开关——构造时传 `enabled` map（默认全 True）。

⚠️ chain shim 当前**未接 before_tool**（工具循环还没上移到 chain），
因此 mw 11 实际跑测试 + 静态构造但运行时不被调用；D-4 v1 Guardian shim
内部反向调用本 mw（或同款 provider 链）替换原 Guardian.intercept。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Mapping

from ..guardrails import (
    DangerousCommandProvider,
    GuardrailProvider,
    GuardrailRequest,
    GuardrailVerdict,
    RedZoneProvider,
    SafeRootProvider,
    SensitiveContentProvider,
)
from .base import AgentMiddleware, RunContext, ThreadState, ToolCall, ToolResult

log = logging.getLogger(__name__)


# 跟 v1 Guardian 对齐的工具分类
DEFAULT_PATH_PARAMS: frozenset[str] = frozenset(
    {
        "file_path",
        "path",
        "directory",
        "source",
        "destination",
        "notebook_path",
    }
)
DEFAULT_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "delete",
        "copy",
        "move",
        "create_directory",
        "edit",
        "multi_edit",
        "notebook_edit",
    }
)
DEFAULT_EXECUTE_TOOLS: frozenset[str] = frozenset(
    {"execute_command", "execute_powershell"}
)


def _default_providers() -> tuple[GuardrailProvider, ...]:
    return (
        RedZoneProvider(),
        SafeRootProvider(),
        DangerousCommandProvider(),
        SensitiveContentProvider(),
    )


class GuardrailChainMiddleware(AgentMiddleware):
    """串行调用 Provider 链，第一个非 None verdict 命中即生效。"""

    name = "guardrail_chain"

    def __init__(
        self,
        *,
        providers: Iterable[GuardrailProvider] | None = None,
        enabled: Mapping[str, bool] | None = None,
        path_params: Iterable[str] = DEFAULT_PATH_PARAMS,
        write_tools: Iterable[str] = DEFAULT_WRITE_TOOLS,
        execute_tools: Iterable[str] = DEFAULT_EXECUTE_TOOLS,
        safe_root: str | None = None,
        project_root: str | None = None,
    ) -> None:
        chain = tuple(providers) if providers is not None else _default_providers()
        self._providers: tuple[GuardrailProvider, ...] = chain
        self._enabled: dict[str, bool] = {p.name: True for p in chain}
        if enabled:
            self._enabled.update(enabled)
        self._path_params = frozenset(path_params)
        self._write_tools = frozenset(write_tools)
        self._execute_tools = frozenset(execute_tools)
        self._safe_root = safe_root or None
        self._project_root = project_root or None

    @property
    def providers(self) -> tuple[GuardrailProvider, ...]:
        return self._providers

    def is_enabled(self, name: str) -> bool:
        return self._enabled.get(name, True)

    async def before_tool(
        self,
        state: ThreadState,
        tool_call: ToolCall,
        ctx: RunContext,
    ) -> dict[str, Any] | None:
        try:
            verdict = self._evaluate(tool_call, ctx)
        except Exception:
            log.exception("guardrail_chain.before_tool unexpected exception")
            return None

        if verdict is None or verdict.decision == "ALLOW":
            return None

        # M1 阶段 CONFIRM 当 DENY 处理（跟 v1 一致；M2 再加实时弹窗）
        result = ToolResult(
            tool_call_id=tool_call.id,
            content=self._error_payload(verdict),
            is_error=True,
            metadata={
                "guardrail_decision": verdict.decision,
                "guardrail_provider": verdict.provider,
                "guardrail_reason": verdict.reason,
            },
        )
        log.info(
            "guardrail_chain DENY tool=%s provider=%s reason=%s trace=%s",
            tool_call.name,
            verdict.provider,
            verdict.reason,
            ctx.trace_id,
        )
        return {"short_circuit": result}

    # ── 内部 ────────────────────────────────────────────────────────────

    def _evaluate(
        self,
        tool_call: ToolCall,
        ctx: RunContext,
    ) -> GuardrailVerdict | None:
        """串行跑 provider 链；第一个非 None 即返。

        provider 自身异常 → swallow + log，不影响后续 provider。
        """
        req = self._build_request(tool_call, ctx)
        for p in self._providers:
            if not self.is_enabled(p.name):
                continue
            try:
                verdict = p.check(req)
            except Exception:
                log.exception("guardrail_chain provider=%s.check raised", p.name)
                continue
            if verdict is None:
                continue
            if verdict.decision == "ALLOW":
                # 显式 ALLOW 不短路；让链继续走下去
                continue
            return verdict
        return None

    def _build_request(
        self,
        tool_call: ToolCall,
        ctx: RunContext,
    ) -> GuardrailRequest:
        args = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        operation = self._infer_operation(tool_call.name)
        paths = self._extract_paths(args)
        return GuardrailRequest(
            agent_id=ctx.agent_id,
            tool_name=tool_call.name,
            arguments=args,
            operation=operation,
            paths=paths,
            project_root=self._project_root,
            safe_root=self._safe_root,
        )

    def _infer_operation(self, tool_name: str):
        if tool_name in self._write_tools:
            return "write"
        if tool_name in self._execute_tools:
            return "execute"
        return "read"

    def _extract_paths(self, args: dict[str, Any]) -> tuple[str, ...]:
        paths: list[str] = []
        for k in self._path_params:
            v = args.get(k)
            if isinstance(v, str) and v:
                paths.append(v)
        return tuple(paths)

    @staticmethod
    def _error_payload(verdict: GuardrailVerdict) -> str:
        """tool result content — JSON 让 LLM 看得懂。"""
        return json.dumps(
            {
                "error": "guardrail_denied",
                "decision": verdict.decision,
                "provider": verdict.provider,
                "reason": verdict.reason,
            },
            ensure_ascii=False,
        )


__all__ = [
    "DEFAULT_EXECUTE_TOOLS",
    "DEFAULT_PATH_PARAMS",
    "DEFAULT_WRITE_TOOLS",
    "GuardrailChainMiddleware",
]
