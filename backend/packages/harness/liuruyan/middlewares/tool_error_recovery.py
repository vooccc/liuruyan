"""v2 · B-4 · ToolErrorRecoveryMiddleware

工具内部抛异常时把 is_error=True 的 ToolResult.content 消毒：
- 剪掉 Python/JS stacktrace（防 prompt injection / 内部细节漏给 LLM）
- 保留首段错误描述 + 最后一行错误类型（信息量足够 LLM 决策）
- 加 [tool=<name> error] 前缀让 LLM 看到是哪个工具失败
- log 全栈 + trace_id（运维能查）

参考：docs/architecture_v2/03_middlewares.md § 13

钩子：after_tool

实现侧：直接 in-place 修改 `result.content`（ToolResult 是 mutable dataclass）。
返回 None 表示 state 不改；result 修改即为副作用。

异常隔离：内部 try/except；任何异常 swallow + log。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import AgentMiddleware, RunContext, ThreadState, ToolCall, ToolResult

log = logging.getLogger(__name__)


class ToolErrorRecoveryMiddleware(AgentMiddleware):
    name = "tool_error_recovery"

    # stacktrace 关键 marker
    _STACKTRACE_MARKERS = (
        "Traceback (most recent call last)",
        "Traceback (most recent",
    )
    # JS-like / generic stack frame 起始
    _FRAME_PREFIXES = ("File ", "  File ", "at ", "    at ")

    async def after_tool(
        self,
        state: ThreadState,
        tool_call: ToolCall,
        result: ToolResult,
        ctx: RunContext,
    ) -> dict[str, Any] | None:
        try:
            if not result.is_error:
                return None

            raw = result.content if isinstance(result.content, str) else str(result.content)
            sanitized = self._sanitize(raw)
            prefixed = f"[tool={tool_call.name} error] {sanitized}".rstrip()

            # log 完整原文 + trace_id（运维查询用）
            log.warning(
                "ToolErrorRecovery: tool=%s trace=%s run=%s "
                "original_len=%d sanitized_len=%d",
                tool_call.name,
                ctx.trace_id,
                ctx.run_id,
                len(raw),
                len(prefixed),
            )

            result.content = prefixed
            result.metadata["error_recovered"] = True
            result.metadata["original_length"] = len(raw)
            return None
        except Exception:
            log.exception(
                "ToolErrorRecoveryMiddleware swallowed exception (run=%s)",
                ctx.run_id,
            )
            return None

    # ── sanitize ─────────────────────────────────────────────────────────

    def _sanitize(self, content: str) -> str:
        """剪掉 stacktrace，保留首段描述 + 最后一行错误类型。"""
        if not self._has_stacktrace(content):
            return content.strip()

        lines = content.splitlines()

        # 头部：到第一个 stacktrace 起始为止
        head: list[str] = []
        for line in lines:
            stripped = line.strip()
            if any(stripped.startswith(m) for m in self._STACKTRACE_MARKERS):
                break
            if any(line.startswith(p) for p in self._FRAME_PREFIXES):
                break
            head.append(line)

        # 尾部：最后一条非空、非 frame、非 Traceback 起始的行
        tail = ""
        for line in reversed(lines):
            s = line.strip()
            if not s:
                continue
            if any(s.startswith(m) for m in self._STACKTRACE_MARKERS):
                continue
            if any(line.startswith(p) for p in self._FRAME_PREFIXES):
                continue
            tail = s
            break

        head_text = "\n".join(head).strip()
        if tail and tail != head_text:
            return (head_text + "\n" + tail).strip() if head_text else tail
        return head_text or tail

    def _has_stacktrace(self, content: str) -> bool:
        if any(m in content for m in self._STACKTRACE_MARKERS):
            return True
        # JS / 通用 frame 形态（行首多空格 + "at functionName"）
        for line in content.splitlines():
            if line.startswith("  File ") or line.lstrip().startswith("at "):
                # 至少两条 frame 才认定是 stacktrace（单行 "at " 可能是普通文本）
                pass
        # 至少出现 2 条 frame 关键字
        frame_count = sum(
            1
            for line in content.splitlines()
            if line.startswith("  File ") or line.lstrip().startswith("at ")
        )
        return frame_count >= 2


__all__ = ["ToolErrorRecoveryMiddleware"]
