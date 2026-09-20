"""v2 · D-3 · SandboxAuditMiddleware (mw 12)

before_tool 钩子在 write / execute 类工具执行前落 audit 日志。

落盘格式：`{base}/audit/{YYYY-MM-DD}/{op_id}.json`

```json
{
  "op_id": "...",
  "ts": 1722800000.0,
  "user_id": "u1",
  "agent_id": "liuruyan",
  "session_id": "...",
  "trace_id": "...",
  "tool_name": "write_file",
  "operation": "write",
  "paths": ["..."],
  "args_summary": "..."  ← truncate 前 256 字符，避免 audit 文件膨胀
}
```

不做：
- **备份恢复**——v1 backup.py 已有逻辑；D-4 shim 时让 v1 Guardian 继续负责
  写前备份；mw 12 不重复实现，避免双备份
- **CONFIRM 弹窗**——M2 上线时再加（跟 cron / 用户偏好结合）
- **after_tool 失败回滚**——同上，留给 v1 backup

异常完全 swallow——audit 失败不能影响主对话。
"""

from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path
from typing import Any, Iterable

from ..persistence.atomic import atomic_write_json_async
from ..runtime.context import (
    get_effective_agent_id,
    get_effective_user_id,
)
from ..sandbox.virtual_path import get_default_base
from .base import AgentMiddleware, RunContext, ThreadState, ToolCall

log = logging.getLogger(__name__)


# 默认要 audit 的工具集（写 + 执行 + 删除）。
DEFAULT_AUDIT_WRITE_TOOLS: frozenset[str] = frozenset(
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
DEFAULT_AUDIT_EXECUTE_TOOLS: frozenset[str] = frozenset(
    {"execute_command", "execute_powershell"}
)
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

# args 序列化时的最大字符数——防 audit 文件膨胀（agent 一次写 1MB 内容也能追上）。
MAX_ARGS_SUMMARY_CHARS: int = 256


class SandboxAuditMiddleware(AgentMiddleware):
    """落 audit log；不阻塞主流程；异常完全 swallow。"""

    name = "sandbox_audit"

    def __init__(
        self,
        *,
        base: Path | None = None,
        write_tools: Iterable[str] = DEFAULT_AUDIT_WRITE_TOOLS,
        execute_tools: Iterable[str] = DEFAULT_AUDIT_EXECUTE_TOOLS,
        path_params: Iterable[str] = DEFAULT_PATH_PARAMS,
    ) -> None:
        # base lazy 求值——构造时 env 可能还没设
        self._base_override = base
        self._write_tools = frozenset(write_tools)
        self._execute_tools = frozenset(execute_tools)
        self._path_params = frozenset(path_params)

    async def before_tool(
        self,
        state: ThreadState,
        tool_call: ToolCall,
        ctx: RunContext,
    ) -> dict[str, Any] | None:
        try:
            if not self._is_audit_target(tool_call.name):
                return None
            record = self._build_record(tool_call, ctx)
            audit_path = self._audit_path(record["op_id"], record["ts"])
            await atomic_write_json_async(audit_path, record)
        except Exception:
            log.exception(
                "sandbox_audit before_tool failed tool=%s trace=%s",
                tool_call.name,
                ctx.trace_id,
            )
        return None

    # ── 内部 ───────────────────────────────────────────────────────────

    def _is_audit_target(self, tool_name: str) -> bool:
        return tool_name in self._write_tools or tool_name in self._execute_tools

    def _build_record(self, tool_call: ToolCall, ctx: RunContext) -> dict[str, Any]:
        args = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        ts = time.time()
        op_id = f"{int(ts * 1000)}.{secrets.token_hex(4)}"
        operation = self._infer_operation(tool_call.name)
        paths = self._extract_paths(args)
        return {
            "op_id": op_id,
            "ts": ts,
            "user_id": ctx.user_id or get_effective_user_id(),
            "agent_id": ctx.agent_id or get_effective_agent_id(),
            "session_id": ctx.session_id,
            "trace_id": ctx.trace_id,
            "tool_name": tool_call.name,
            "tool_call_id": tool_call.id,
            "operation": operation,
            "paths": list(paths),
            "args_summary": self._summarize_args(args),
        }

    def _audit_path(self, op_id: str, ts: float) -> Path:
        date = time.strftime("%Y-%m-%d", time.gmtime(ts))
        base = self._base_override or get_default_base()
        return base / "audit" / date / f"{op_id}.json"

    def _infer_operation(self, tool_name: str) -> str:
        if tool_name in self._write_tools:
            return "write"
        if tool_name in self._execute_tools:
            return "execute"
        return "read"

    def _extract_paths(self, args: dict[str, Any]) -> tuple[str, ...]:
        out: list[str] = []
        for k in self._path_params:
            v = args.get(k)
            if isinstance(v, str) and v:
                out.append(v)
        return tuple(out)

    @staticmethod
    def _summarize_args(args: dict[str, Any]) -> str:
        try:
            text = repr(args)
        except Exception:
            return "<unrepresentable>"
        if len(text) > MAX_ARGS_SUMMARY_CHARS:
            return text[: MAX_ARGS_SUMMARY_CHARS] + "...<truncated>"
        return text


__all__ = [
    "DEFAULT_AUDIT_EXECUTE_TOOLS",
    "DEFAULT_AUDIT_WRITE_TOOLS",
    "DEFAULT_PATH_PARAMS",
    "MAX_ARGS_SUMMARY_CHARS",
    "SandboxAuditMiddleware",
]
