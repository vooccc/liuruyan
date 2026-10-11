#!/usr/bin/env python3
"""
清理后的工具注册表 - 不依赖 Hermes 系统

直接注册纯净的工具函数，使用标准的 Anthropic tool schema

包含的工具：
1. read_file - 读取文件内容
2. write_file - 写入文件内容
3. patch - 应用补丁修改文件
4. grep - 搜索文件内容（命名对齐 Claude Code/Cascade；老名 search_files 已于 2026-05-20 废弃）
5. web_search - 网络搜索
6. web_extract - 网页内容提取
"""

import logging
import asyncio
import json
import inspect
import os
import re
import time
import uuid
from pathlib import Path
from typing import Dict, List, Any, Callable, Optional, Iterable, Tuple

# ⚠️ 必须在读 _FEATURE_* env 之前 load_dotenv()——本模块顶部的
# _FEATURE_CRON_TOOL flag 是 module-load 时立刻求值，没读到就 False，
# 直接吞掉 cron_* x4 + sleep x1 的注册。clean_registry 可被独立 import
# （不经 llm_client），必须自己 load_dotenv 一次（dotenv 内部去重，多次调用安全）。
# 历史 bug：2026-05-01 实测柳如烟数工具时全部 condition_register 工具消失。
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

logger = logging.getLogger(__name__)


# 参数校验/coerce 层（InputValidationError + 别名归一化 + 类型 coerce）已抽到
# backend/tools/builtin/registry_validation.py；此处 re-import 保持公共 API。
from .registry_validation import (
    InputValidationError,
    _BOOL_TRUE,
    _BOOL_FALSE,
    _CONTEXT_INJECT_KEYS,
    _PATH_ALIAS_MAP,
    _PATHS_PLURAL_ALIAS_MAP,
    _NOTEBOOK_PATH_ALIAS_MAP,
    _FSPATH_ALIAS_MAP,
    _PATTERN_ALIAS_MAP,
    _normalize_aliases_and_validate,
    _coerce_one,
    _coerce_arguments_by_schema,
)
# 注册表引擎（CleanToolRegistry + 并发常量 + 结果预算）已抽到
# backend/tools/builtin/registry_core.py；此处 re-import 保持公共 API。
from .registry_core import (
    CONCURRENCY_READ,
    CONCURRENCY_WRITE,
    CONCURRENCY_EXCLUSIVE,
    _DEFAULT_CONCURRENCY_CLASSES,
    CleanToolRegistry,
    _BUDGET,
    _BUDGET_EXEMPT_TOOLS,
    _GATED_WRITE_TOOLS,
    _check_read_before_edit_gate,
    _update_read_cache_after_success,
    format_tool_result_for_display,
)
# 全局注册表实例
registry = CleanToolRegistry()


# 文件/项目记忆/技能包类工具注册 → backend/tools/builtin/registrations/files.py
from .registrations import files as _files_registrations
_files_registrations.register(registry)

# 网络/检索/观测类工具注册 → backend/tools/builtin/registrations/web.py
from .registrations import web as _web_registrations
_web_registrations.register(registry)
# ============================================================================
# 注册终端工具
# ============================================================================

from .terminal_tool import TerminalTool
from .command_manager import get_command_manager

_terminal_tool = TerminalTool()

# PowerShellTool 在 Windows 上才有实义；Linux/macOS 上 _powershell_tool=None，
# handler 拿到 shell="powershell" 会显式降级到 bash。
#
# 2026-05-15 决策：直接对标 Cascade，**只暴露 bash + powershell 两个 shell**。
# 不暴露 cmd——PowerShell 5.1 已能覆盖所有 Windows 系统命令（ping/dir/tracert/git/
# python ...），且 cmd `/S /C "<script>"` 单行模式 + && 子 shell 解析等老怪癖会带来
# 不必要的工程复杂度（cwd trailer 因为 cmd /C 只跑第一行根本不工作）。
# 用户真要跑老 batch，可以在 powershell 里直接 `cmd /c "echo a && echo b"` 套用。
# cmd_tool.py 文件保留作为可选 future option，但不在 registry 里 wire。
import platform as _platform
if _platform.system() == "Windows":
    from .powershell_tool import PowerShellTool
    _powershell_tool = PowerShellTool()
else:
    _powershell_tool = None

def _resolve_shell(shell: str, command: str) -> str:
    """归一 shell 参数：'auto' → 按 OS 选；显式值原样返回（不可用时降级到 bash）。

    - Windows + auto → 'powershell'（原生 Windows 路径、cmdlet、.NET）
    - 非 Windows + auto → 'bash'
    - 'powershell' 在非 Windows → 自动降级 bash
    - 'cmd' 历史选项 → 一律映射为 'powershell'（Windows）或 'bash'（其它）
      （保留兼容老 prompts，但实际不再有 cmd 独立后端）
    """
    s = (shell or "auto").strip().lower()
    if s == "auto":
        return "powershell" if _platform.system() == "Windows" else "bash"
    if s == "cmd":
        # 老 shell 选项：Windows 上重定向到 powershell，其它走 bash
        return "powershell" if _powershell_tool is not None else "bash"
    if s == "powershell" and _powershell_tool is None:
        return "bash"
    return s if s in ("bash", "powershell") else "bash"


def _resolve_tool_impl(resolved_shell: str):
    """根据归一后的 shell 名返回对应工具实例。"""
    if resolved_shell == "powershell":
        return _powershell_tool
    return _terminal_tool


def _make_sse_stream_callback(session_id: Optional[str], command_id: str,
                              command_text: str, shell: str):
    """构造 SSE 推送回调：把 stdout/stderr 增量打成 system_notice(kind=command_chunk)。

    session_id 缺失时返回 None（脚本/后台调用无害降级，不推 SSE）。
    """
    if not session_id:
        return None
    try:
        from backend.sse_manager import get_sse_manager
    except Exception:
        return None

    sse_manager = get_sse_manager()
    seq_box = {"n": 0}

    async def _emit_done(exit_code: Optional[int], success: bool) -> None:
        """命令结束后调用：推一条 stream='end' 的 sentinel chunk，前端据此自动折叠卡片。"""
        seq_box["n"] += 1
        try:
            await sse_manager.send_event(session_id, {
                "type": "system_notice",
                "kind": "command_chunk",
                "content": "",
                "payload": {
                    "command_id": command_id,
                    "command": command_text[:200],
                    "command_key": command_text,
                    "shell": shell,
                    "stream": "end",
                    "text": "",
                    "seq": seq_box["n"],
                    "done": True,
                    "exit_code": exit_code,
                    "success": bool(success),
                },
            })
        except Exception:
            pass

    async def _cb(kind: str, text: str) -> None:
        if not text:
            return
        seq_box["n"] += 1
        try:
            await sse_manager.send_event(session_id, {
                "type": "system_notice",
                "kind": "command_chunk",
                "content": "",  # 必填壳；真正数据在 payload
                "payload": {
                    "command_id": command_id,
                    "command": command_text[:200],
                    "command_key": command_text,
                    "shell": shell,
                    "stream": kind,           # 'stdout' | 'stderr'
                    "text": text,
                    "seq": seq_box["n"],
                },
            })
        except Exception:
            pass  # SSE 出错绝不阻塞命令执行

    # 把 done emitter 挂到 callback 上，调用方在 execute() 返回后调用即可
    _cb.emit_done = _emit_done  # type: ignore[attr-defined]
    return _cb


async def _handle_execute_command(command: str, workdir: str = None,
                                   timeout: int = 120,
                                   blocking: bool = True,
                                   shell: str = "auto",
                                   description: str = None,
                                   max_output_chars: int = 100_000,
                                   wait_ms_before_async: int = 0,
                                   session_id: str = None,
                                   **_unused):
    """Execute a shell command. Auto-picks shell by OS; supports cmd/bash/powershell.

    blocking=True (默认):  阻塞跑完返回完整 output（同时往当前 session 推 SSE 流式 chunk）
    blocking=False:         立刻返回 command_id JSON，模型用 command_status 拉增量

    wait_ms_before_async > 0: Cascade 同款半同步——后台启动，前 N ms 内若结束则同步返
        最终结果，否则返 command_id 让模型 poll。短命令体感等同同步，长命令不阻塞。

    shell: 'auto' (按 OS) | 'bash' | 'powershell' | 'cmd'。
        Windows auto → powershell；非 Windows auto → bash；非 Windows 上指定
        powershell/cmd 自动降级 bash。

    description: 可选的人话备注（仅写日志/SSE，不进 shell）。
    max_output_chars: 输出截断上限，默认 100K。
    session_id: 由 dispatch context 注入（不是模型传的）；用于 SSE 流式推送。
    """
    resolved = _resolve_shell(shell, command)
    tool_impl = _resolve_tool_impl(resolved)

    # session_id 优先用 dispatch context 注入的（chat 主路径，line 1799 起 llm_client
    # 调用 dispatch_batch 时 session_id=_current_session_id_var.get() 已经传进来）。
    # 退路依次：LLMClient ContextVar → harness v2 ContextVar。任何一个有就够。
    if not session_id:
        try:
            from backend.llm_client import _current_session_id_var
            session_id = _current_session_id_var.get()
        except Exception:
            session_id = None
    if not session_id:
        try:
            from backend.packages.harness.liuruyan.runtime.context import get_session_id
            session_id = get_session_id()
        except Exception:
            session_id = None

    # ── 半同步路径：wait_ms_before_async > 0 ──────────────────────────────
    # 走后台 manager 启动，前 N ms 等结束；及时结束 → 拉完整结果同步返；
    # 否则降级返 command_id 让模型 poll。
    if wait_ms_before_async and wait_ms_before_async > 0:
        try:
            spec = tool_impl.prepare_async_command(command, workdir=workdir)
        except ValueError as e:
            return f"Error: {e}"
        cid = get_command_manager().new_command_id()
        sse_cb = _make_sse_stream_callback(session_id, cid, command, resolved)
        cid = await get_command_manager().start(
            argv=spec["argv"],
            command_text=command,
            workdir=spec["cwd"],
            env=spec["env"],
            stream_callback=sse_cb,
            command_id=cid,
        )
        # 等 N ms 看是否能结束
        wait_seconds = max(0.0, float(wait_ms_before_async) / 1000.0)
        status = await get_command_manager().get_status(
            command_id=cid,
            wait_seconds=wait_seconds,
            output_chars=max_output_chars,
        )
        if status.get("status") in ("done", "killed", "error"):
            # 结束 → 同步返最终结果（向后兼容：成功返 output 字符串，失败返 Error: ...）
            if status.get("success"):
                return status.get("output", "") or ""
            err = status.get("error") or "Unknown error"
            tail = status.get("output", "")
            return f"Error: {err}\n{tail}" if tail else f"Error: {err}"
        # 没结束 → 降级 async 模式
        return json.dumps({
            "success": True,
            "command_id": cid,
            "status": "running",
            "command": command,
            "workdir": spec["cwd"],
            "shell": resolved,
            "description": description,
            "note": (
                f"Command did not finish in {wait_ms_before_async}ms; switched to "
                f"async. Use `command_status(command_id, wait_seconds=N)` to poll, "
                f"or pass kill=true to terminate."
            ),
        }, ensure_ascii=False)

    # ── 异步路径：blocking=False ─────────────────────────────────────────
    if not blocking:
        try:
            spec = tool_impl.prepare_async_command(command, workdir=workdir)
        except ValueError as e:
            return f"Error: {e}"
        cid = get_command_manager().new_command_id()
        sse_cb = _make_sse_stream_callback(session_id, cid, command, resolved)
        cid = await get_command_manager().start(
            argv=spec["argv"],
            command_text=command,
            workdir=spec["cwd"],
            env=spec["env"],
            stream_callback=sse_cb,
            command_id=cid,
        )
        return json.dumps({
            "success": True,
            "command_id": cid,
            "status": "running",
            "command": command,
            "workdir": spec["cwd"],
            "shell": resolved,
            "description": description,
            "note": (
                "Command started in background. Use `command_status(command_id, "
                "wait_seconds=N, output_chars=M)` to poll. Pass kill=true to terminate."
            ),
        }, ensure_ascii=False)

    # ── 同步阻塞路径（默认）──────────────────────────────────────────────
    # SSE 走 stream_callback：每次 stdout/stderr 增量推一条 system_notice。
    # 没 session 时 callback=None，行为退化成普通 await execute()。
    sse_cb = _make_sse_stream_callback(
        session_id, f"sync-{command[:32]}", command, resolved,
    )
    result = await tool_impl.execute(
        command=command,
        workdir=workdir,
        timeout=timeout,
        max_output_chars=max_output_chars,
        stream_callback=sse_cb,
    )

    # 推一条 stream='end' 的 sentinel chunk，前端据此把 live terminal 卡折叠成一行
    if sse_cb is not None and hasattr(sse_cb, "emit_done"):
        try:
            await sse_cb.emit_done(  # type: ignore[attr-defined]
                result.get("exit_code"),
                bool(result.get("success")),
            )
        except Exception:
            pass

    if result.get("success"):
        return result.get("output", "")
    else:
        return f"Error: {result.get('error', 'Unknown error')}"

registry.register(
    name="execute_command",
    description=(
        "Run a shell command. Picks shell by OS: Windows → powershell, others → bash. "
        "Override with `shell='bash'` / `'powershell'`. PowerShell on Windows handles all "
        "system commands (ping/dir/git/python/...) with UTF-8 output enforced. "
        "Working directory persists across calls within the session.\n\n"
        "AVOID using this for tasks with a dedicated tool — they're faster, safer, and produce better UI:\n"
        "- File content search → `grep` (NOT raw shell grep / Select-String)\n"
        "- File name / glob search → `glob_search` (NOT find / Get-ChildItem)\n"
        "- Read a file → `read_file` (NOT cat / type / Get-Content)\n"
        "- Edit a file → `edit` / `multi_edit` (NOT sed / awk / -replace)\n"
        "- Write a file → `write_file` (NOT echo > / Set-Content)\n"
        "USE this for: running scripts, package installs, git, build/test runners, OS-level ops.\n\n"
        "EXECUTION MODES:\n"
        "1. blocking=true (default): wait for completion, return full output.\n"
        "2. blocking=false: return `command_id` immediately, poll with `command_status`. "
        "Use for any command > 10s (pytest, npm install, build).\n"
        "3. wait_ms_before_async=N: half-sync — start in background, wait up to N ms; "
        "if the command finishes within the window return final output as if blocking, else "
        "return command_id for polling. Best UX for commands of unknown duration.\n\n"
        "Multiple commands: independent commands can be sent as parallel tool calls in the same response. "
        "Dependent commands → use `&&` (bash) / `;` (powershell) in one call. Don't separate with newlines."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command to execute (syntax matches the selected shell)."
            },
            "shell": {
                "type": "string",
                "enum": ["auto", "bash", "powershell"],
                "description": (
                    "Shell to use. 'auto' (default): powershell on Windows, bash elsewhere. "
                    "For old-style batch needs, run `cmd /c \"echo a && echo b\"` inside powershell."
                ),
                "default": "auto"
            },
            "workdir": {
                "type": "string",
                "description": (
                    "Working directory (optional; defaults to current persistent cwd). "
                    "Pass an absolute path; Windows drive paths like 'D:\\\\path' are fully supported "
                    "since cwd is set via subprocess cwd= (not interpolated into the script)."
                )
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120, max 600). Only applies when blocking=true.",
                "default": 120
            },
            "blocking": {
                "type": "boolean",
                "description": (
                    "If true (default), wait for completion and return full output. "
                    "If false, return a `command_id` immediately and poll with `command_status`."
                ),
                "default": True
            },
            "wait_ms_before_async": {
                "type": "integer",
                "description": (
                    "Half-sync mode (Cascade-style): start as background, wait up to N ms for "
                    "completion. Done in window → return final output (synchronous feel). "
                    "Not done → return command_id for `command_status` polling. "
                    "0 (default) disables this; use blocking= flag instead."
                ),
                "default": 0
            },
            "description": {
                "type": "string",
                "description": (
                    "Optional human-readable note about what this command does (5-10 words). "
                    "Shown in logs and UI; never enters the shell."
                )
            },
            "max_output_chars": {
                "type": "integer",
                "description": "Output truncation cap (chars). Default 100000.",
                "default": 100000
            }
        },
        "required": ["command"]
    },
    handler=_handle_execute_command,
    is_async=True,
    arg_aliases={
        "cmd": "command",
        "command_line": "command",
        "commandline": "command",
        "script": "command",
        "cwd": "workdir",
        "working_directory": "workdir",
        "directory": "workdir",
        "timeout_seconds": "timeout",
        "timeout_sec": "timeout",
        "Cwd": "workdir",
        "CommandLine": "command",
        "Blocking": "blocking",
        "WaitMsBeforeAsync": "wait_ms_before_async",
    },
)


# ----------------------------------------------------------------------------
# command_status —— 配套异步执行的状态/输出查询工具
# ----------------------------------------------------------------------------

async def _handle_command_status(command_id: str,
                                 wait_seconds: float = 0.0,
                                 output_chars: int = 4000,
                                 kill: bool = False,
                                 **_unused):
    result = await get_command_manager().get_status(
        command_id=command_id,
        wait_seconds=wait_seconds,
        output_chars=output_chars,
        kill=kill,
    )
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="command_status",
    description=(
        "Query a background command started by `execute_command(blocking=false)`. "
        "Returns current status (running/done/killed/error), exit_code (when done), "
        "elapsed time, and the latest tail of stdout/stderr.\n\n"
        "TYPICAL USAGE:\n"
        "1. Fire-and-poll: `command_status(command_id, wait_seconds=5)` — wait up to 5s "
        "for completion and grab output. Repeat until status='done'.\n"
        "2. Snapshot: `command_status(command_id)` — instant snapshot, no waiting.\n"
        "3. Kill: `command_status(command_id, kill=true)` — terminate the process and "
        "return final state.\n\n"
        "Output buffer is capped at 1MB; older bytes are dropped if exceeded "
        "(`output_truncated=true` in result). The default `output_chars=4000` returns "
        "only the tail — increase for full logs (cap remains 1MB)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command_id": {
                "type": "string",
                "description": "The command_id returned by `execute_command(blocking=false)`."
            },
            "wait_seconds": {
                "type": "number",
                "description": (
                    "Block up to N seconds waiting for completion. Returns earlier if the "
                    "command finishes. Default 0 = instant snapshot."
                ),
                "default": 0
            },
            "output_chars": {
                "type": "integer",
                "description": "Tail of stdout to return (in characters). Default 4000.",
                "default": 4000
            },
            "kill": {
                "type": "boolean",
                "description": (
                    "If true, send SIGTERM (Unix process group) / terminate (Windows) to the "
                    "command before reading status. Useful for cleanup or runaway commands."
                ),
                "default": False
            }
        },
        "required": ["command_id"]
    },
    handler=_handle_command_status,
    is_async=True
)


# Note: 旧 `execute_powershell` 已合并进 `execute_command(shell='powershell')`
# （PowerShellTool 实例化在文件顶部模块级，handler 统一 dispatch）。

# ============================================================================
# 注册 LSP 工具（Python 代码智能）
# ============================================================================

try:
    from .jedi_lsp_tool import LSPTool
    import os

    _lsp_tool = LSPTool(project_path=os.getcwd())

    async def _handle_lsp_operation(
        operation: str,
        path: str = None,
        line: int = None,
        character: int = None,
        new_name: str = None,
        query: str = None,
        root: str = None,
        language: str = None,
        **_unused,
    ):
        """Execute LSP operation.

        对外参数名是 path（跟 read_file / edit / grep 保持一致）；LSPTool 内部 API
        仍叫 file_path，只在这里转一次。
        """
        result = await _lsp_tool.execute(
            operation=operation,
            file_path=path,
            line=line,
            character=character,
            query=query,
            root=root,
            language=language,
            new_name=new_name,
        )

        if result.get("success"):
            return result.get("output", "")
        else:
            return f"Error: {result.get('error', 'Unknown error')}"

    registry.register(
        name="code_intelligence",
        description=(
            "Interact with code intelligence to navigate and analyze source semantically. "
            "Python via Jedi, TypeScript/TSX/JS/JSX via TS LanguageService. Routes by file extension.\n\n"
            "Supported operations:\n"
            "- goToDefinition: Find where a symbol is defined\n"
            "- findReferences: Find all references to a symbol\n"
            "- hover: Get type signature and docstring for a symbol\n"
            "- documentSymbol: List all symbols (classes/functions/variables) in a file\n"
            "- workspaceSymbol: Fuzzy-search symbols across the workspace\n"
            "- goToImplementation: Find concrete implementations of an interface or abstract method (TS only)\n"
            "- incomingCalls: Find functions that call the function at a position\n"
            "- outgoingCalls: Find functions called by the function at a position\n"
            "- rename: Preview semantic rename (does NOT write to disk)\n"
            "- diagnostics: Report syntax/type errors in a file (Python: syntax only; TS: syntax + semantic)\n\n"
            "Position fields (line, character) are 1-based, as shown in editors. "
            "For position-based ops, get them via workspaceSymbol or documentSymbol first.\n\n"
            "Supported file extensions: .py / .ts / .tsx / .js / .jsx / .cts / .mts / .cjs / .mjs"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "goToDefinition", "findReferences", "hover",
                        "documentSymbol", "rename",
                        "workspaceSymbol", "incomingCalls", "outgoingCalls",
                        "goToImplementation", "diagnostics",
                    ],
                    "description": "The operation to perform (see description)."
                },
                "path": {
                    "type": "string",
                    "description": "Path to the source file. Required for all ops except workspaceSymbol."
                },
                "line": {
                    "type": "integer",
                    "description": "Line number (1-based). Required for position-based ops."
                },
                "character": {
                    "type": "integer",
                    "description": "Character offset (1-based). Required for position-based ops."
                },
                "new_name": {
                    "type": "string",
                    "description": "New identifier. Required for rename."
                },
                "query": {
                    "type": "string",
                    "description": "Symbol name fragment for workspaceSymbol. Empty string lists all."
                },
                "root": {
                    "type": "string",
                    "description": "Workspace root for workspaceSymbol. Defaults to project cwd; for TS, accepts a tsconfig.json path."
                },
                "language": {
                    "type": "string",
                    "enum": ["python", "typescript"],
                    "description": "Optional language filter for workspaceSymbol."
                }
            },
            "required": ["operation"]
        },
        handler=_handle_lsp_operation,
        is_async=True,
        arg_aliases=_PATH_ALIAS_MAP,
    )

    logger.info(
        "code_intelligence registered (backends: %s)",
        ", ".join(getattr(b, "name", "?") for b in _lsp_tool._all_backends),
    )

except ImportError as e:
    logger.warning(f"LSP tool not available: {e}. Install jedi: pip install jedi")


# 任务管理类工具注册 → backend/tools/builtin/registrations/tasks.py
from .registrations import tasks as _tasks_registrations
_tasks_registrations.register(registry)

# ============================================================================
# 注册 Glob 工具
# ============================================================================

from .glob_tool import GlobTool
from .file_tools import _current_working_dir


def _make_glob_tool() -> GlobTool:
    """**每次**调 glob_* 工具都新建实例，project_root 从 ContextVar 动态读。

    2026-05-06 事故修复：旧实现是模块级单例 `_glob_tool = get_glob_tool()`，
    在 import 时锁定 `Path.cwd()`（柳如烟 backend 目录）。spawn_subagent 跨项目
    分析（带 working_directory）时，glob_search 完全忽略 ContextVar，把所有
    相对 pattern 拼到柳如烟 backend 下 → 永远查到 0 个文件 → 模型反复
    "目录混乱，让我重新探索"陷入死循环（线上 explore 子代理 21 个工具白调 case）。

    修法：和 _resolve_read_path 同款——读 working_directory ContextVar，
    没设回退 cwd。每次新建实例规避并发覆盖（ContextVar 本来就是 per-task）。
    """
    wd = _current_working_dir()
    return GlobTool(str(wd)) if wd is not None else GlobTool()

# ============================================================================
# 注册 Diagnostics 工具
# ============================================================================

from .diagnostics_tool import get_diagnostics_tool

_diagnostics_tool = get_diagnostics_tool()

# 所有 handler 都用 **_unused 兜底：
# - registry 会按签名匹配把 context (agent_id/session_id/llm_client) 塞进来，**_unused 吞掉
# - LLM 偶尔会 hallucinate 出 schema 之外的参数（比如给 glob_search 传 path），同样吞掉
# 不容忍未知参数会让一次幻觉就让整个 worker 炸掉。

async def _handle_glob_search(
    pattern: str,
    recursive: bool = True,
    include_dirs: bool = False,
    max_results: int = 1000,
    path: str = None,
    excludes=None,
    extensions=None,
    max_depth: int = None,
    file_type: str = "any",
    full_path: bool = False,
    **_unused,
):
    return _make_glob_tool().search(
        pattern=pattern,
        recursive=recursive,
        include_dirs=include_dirs,
        max_results=max_results,
        path=path,
        excludes=excludes,
        extensions=extensions,
        max_depth=max_depth,
        file_type=file_type,
        full_path=full_path,
    )

# glob_search
registry.register(
    name="glob_search",
    description=(
        "Find files or directories by name pattern. Fast, .gitignore-aware "
        "(via ripgrep when available; falls back to filesystem walk).\n\n"
        "WHEN TO USE:\n"
        "✓ Locate files by name pattern when path is unknown ('test_*.py', 'config.*')\n"
        "✓ Enumerate all files of a given type in a subtree ('**/*.tsx')\n"
        "✓ Quick directory listing — pair with `file_type='directory'`\n"
        "✗ Matches file PATHS/NAMES only, NOT file content. For function names, "
        "variable names, strings, or any code content → use `grep`.\n\n"
        "Examples:\n"
        "- pattern='**/*.py' → All Python files recursively\n"
        "- pattern='src/**/*.tsx' → TS React components only under src/\n"
        "- pattern='test_*.py', path='backend/tests' → test files in a specific tree\n"
        "- pattern='*', extensions=['ts','tsx'], excludes=['**/dist/**'] → all TS minus dist\n"
        "- pattern='*', file_type='directory', max_depth=2 → list top-2-level dirs\n\n"
        "TIPS:\n"
        "- pattern containing `/` (e.g. 'src/**/*.py') is matched against the full relative path; "
        "  patterns without `/` match the filename basename only (same as Cascade `find_by_name`).\n"
        "- Default skips .git / node_modules / __pycache__ / .venv / dist / build / .next etc.\n"
        "- Results are relative to the search root (or `path` if provided)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern (e.g., '**/*.py', 'src/**/*.tsx', 'config.*')"
            },
            "path": {
                "type": "string",
                "description": (
                    "Search root. Absolute path used as-is; relative path resolved against "
                    "project root / working_directory. Omit to search the project root."
                ),
            },
            "recursive": {
                "type": "boolean",
                "description": "Enable recursive search (default: true)",
                "default": True
            },
            "include_dirs": {
                "type": "boolean",
                "description": "Legacy: include directories in results (kept for back-compat; prefer `file_type`).",
                "default": False
            },
            "file_type": {
                "type": "string",
                "enum": ["file", "directory", "any"],
                "description": "Filter by entry kind. 'file' (default for legacy include_dirs=false), 'directory', or 'any'.",
                "default": "any",
            },
            "excludes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Glob patterns to exclude (e.g. ['**/vendor/**', '**/dist/**']).",
            },
            "extensions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Whitelist of extensions (e.g. ['ts', 'tsx'] or ['.py']). Filter applied after pattern.",
            },
            "max_depth": {
                "type": "integer",
                "minimum": 0,
                "description": "Max recursion depth relative to `path` (0 = only entries directly under path). Omit for unbounded.",
            },
            "full_path": {
                "type": "boolean",
                "description": (
                    "If true, pattern matches the full relative path (forces multi-segment matching). "
                    "If false (default), patterns without `/` match basename only."
                ),
                "default": False,
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results (default: 1000)",
                "default": 1000
            }
        },
        "required": ["pattern"]
    },
    handler=_handle_glob_search,
    is_async=True,
    arg_aliases=_PATH_ALIAS_MAP,
)

# 注册 Fast Context 语义代码检索工具
# ============================================================================

from .fast_context_tool import FastContextEngine


def _is_windows_system_dir(p: Path) -> bool:
    """判断路径是否属于 Windows 系统目录（System32 / Windows 等），避免 agent 误扫操作系统。"""
    if os.name != "nt":
        return False
    try:
        resolved = str(p.resolve()).lower().replace("/", "\\")
        sys_root = os.environ.get("SystemRoot", r"C:\Windows").lower().replace("/", "\\")
        win_dir = os.environ.get("WINDIR", r"C:\Windows").lower().replace("/", "\\")
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files").lower().replace("/", "\\")
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)").lower().replace("/", "\\")
        for prefix in (sys_root, win_dir, prog_files, prog_files_x86):
            if resolved == prefix or resolved.startswith(prefix + "\\"):
                return True
        if "\\system32" in resolved or "\\syswow64" in resolved or "\\winsxs" in resolved:
            return True
    except Exception:
        pass
    return False


def _resolve_fast_context_project_root(path: Optional[str], query: str) -> Tuple[Optional[str], Optional[str]]:
    """解析 Fast Context 的有效目标项目根目录。

    优先级：
    1. 显式指定的 path 参数（若有效且非系统目录）
    2. 从 query 文本中自动提取存在的项目路径（同 spawn_subagent）
    3. ContextVar `_current_working_dir()`
    4. `resolve_workspace_root()`
    5. `paths.legacy_project_root()`（开发模式下为当前仓库根）
    6. 兜底检查：若候选路径命中 Windows 系统目录，坚决拦截并返回错误提示。
    """
    candidate: Optional[str] = path

    # 1. 显式路径检查
    if candidate:
        p = Path(candidate).expanduser()
        if _is_windows_system_dir(p):
            return None, "指定的路径属于 Windows 系统目录（已安全拦截避免误扫描系统文件）。"
        if p.exists() and p.is_dir():
            return str(p.resolve()), None

    # 2. 从 query 中提取绝对路径兜底（如 "分析 D:\my-proj" 或 "在 D:/work/quant 搜索"）
    if not candidate and isinstance(query, str) and query:
        try:
            import re as _re
            pattern = _re.compile(
                r"(?:[A-Za-z]:[\\/][^\s`'\"<>|?*\n]+)"
                r"|(?:/(?:[^\s`'\"<>|?*\n/]+/)+[^\s`'\"<>|?*\n/]*)"
            )
            for match in pattern.finditer(query):
                raw_match = match.group(0).rstrip(".,;:)]}'\"")
                try:
                    p = Path(raw_match).expanduser()
                    for _ in range(5):
                        if p.exists() and p.is_dir() and not _is_windows_system_dir(p):
                            candidate = str(p.resolve())
                            break
                        if p.parent == p:
                            break
                        p = p.parent
                    if candidate:
                        break
                except Exception:
                    continue
        except Exception:
            pass

    if candidate:
        p = Path(candidate)
        if p.exists() and p.is_dir() and not _is_windows_system_dir(p):
            return str(p.resolve()), None

    # 3. ContextVar 工作目录
    from .file_tools import _current_working_dir
    wd = _current_working_dir()
    if wd is not None and not _is_windows_system_dir(wd):
        return str(wd.resolve()), None

    # 4. 工作区根解析 (lry_workspace)
    try:
        from backend.lry_workspace import resolve_workspace_root
        ws = resolve_workspace_root()
        if ws is not None and not _is_windows_system_dir(ws):
            return str(ws.resolve()), None
    except Exception:
        pass

    # 5. legacy_project_root（非 frozen 开发模式下即项目根）
    try:
        from backend import paths
        proj = paths.legacy_project_root()
        if proj.exists() and proj.is_dir() and not _is_windows_system_dir(proj):
            return str(proj.resolve()), None
    except Exception:
        pass

    # 6. 进程当前 cwd（仅在非系统目录时允许）
    cwd = Path.cwd()
    if not _is_windows_system_dir(cwd):
        return str(cwd.resolve()), None

    return None, (
        "未指定目标代码库路径，且当前默认工作区为 Windows 系统目录（已安全拦截避免误扫描系统文件）。"
        "请在调用 fast_context_search 时传入待搜索项目的绝对路径（参数 path='D:\\path\\to\\project'），"
        "或在 query 中包含项目绝对路径。"
    )


async def _handle_fast_context_search(
    query: str,
    path: str = None,
    max_turns: int = 3,
    model: str = None,
    **_context,
):
    """Fast Context 语义代码检索工具处理函数。"""
    session_id = _context.get("session_id")
    if not session_id:
        try:
            from backend.llm_client import _current_session_id_var
            session_id = _current_session_id_var.get()
        except Exception:
            session_id = None
    if not session_id:
        try:
            from liuruyan.runtime.context import get_session_id
            session_id = get_session_id()
        except Exception:
            session_id = None

    llm_client = _context.get("llm_client")
    if not llm_client:
        try:
            from backend.llm_client import get_llm_client
            llm_client = get_llm_client()
        except Exception:
            llm_client = None

    project_root, err_msg = _resolve_fast_context_project_root(path, query)
    if err_msg or not project_root:
        return json.dumps({
            "error": err_msg or "未能解析有效项目目录",
            "status": "error",
            "results": [],
        }, ensure_ascii=False)

    engine = FastContextEngine(project_root)
    result = await engine.search(
        query=query,
        session_id=session_id,
        llm_client=llm_client,
        max_turns=max_turns,
        search_model=model,
    )
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="fast_context_search",
    description=(
        "A search subagent (referred to as 'Fast Context') that is ideal for exploring the codebase based on a request.\n\n"
        "This tool invokes a subagent that runs parallel grep and readfile calls over multiple turns to locate relevant line ranges and files.\n\n"
        "HOW TO FORMULATE THE QUERY:\n"
        "- The search term should be a targeted natural language query based on what you are trying to accomplish, like 'Find where authentication requests are handled in the Express routes' or 'Modify the agentic rollout to use the new tokenizer and chat template' or 'Fix the bug where the user gets redirected from the /feed page'.\n"
        "- Fill out extra technical details that you as a smart model can infer in the question if necessary.\n\n"
        "BEST PRACTICES & WORKFLOW:\n"
        "- You should always use this tool to start your codebase exploration when you don't know the exact file path or symbol name.\n"
        "- Note: The files and line ranges returned by this tool may be some of the ones needed to complete the user's request, but you should be careful in evaluating the relevance of the results, since the subagent might make mistakes.\n"
        "- You should consider using classical search tools (read_file / grep) afterwards to verify results or locate the rest if necessary.\n"
        "- IMPORTANT: YOU CANNOT CALL THIS TOOL IN PARALLEL (run only one fast_context_search per turn)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Targeted natural language query describing what you are trying to find or accomplish, "
                    "including any inferred technical keywords or component names (e.g. 'Find where authentication requests are handled in Express routes')."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Search root directory (absolute path, e.g. 'D:\\workspace\\my-app'). "
                    "If the user mentioned a specific codebase or project directory, pass it here. Defaults to the current workspace root."
                ),
            },
            "max_turns": {
                "type": "integer",
                "description": "Maximum number of search iterations (default: 3, range 1-5).",
                "default": 3,
            },
            "model": {
                "type": "string",
                "description": "Optional model override for the search subagent (e.g. 'deepseek-chat', 'qwen2.5-coder').",
            },
        },
        "required": ["query"],
    },
    handler=_handle_fast_context_search,
    is_async=True,
    arg_aliases=_PATH_ALIAS_MAP,
)

# ============================================================================
# 注册 Diagnostics 工具处理函数
# ============================================================================

async def _handle_get_diagnostics(
    paths=None,
    severity: str = None,
    **_unused,
):
    """单文件 → get_diagnostics 返 {success, file_path, ..., diagnostics}；
    多文件 → check_multiple_files 返 {success, file_count, total_errors, files: [...]}。
    """
    if isinstance(paths, str):
        paths = [paths]
    if not paths or not isinstance(paths, list):
        return json.dumps({"success": False, "error": "paths is required (array of file paths)"}, ensure_ascii=False)
    if len(paths) == 1:
        return await _diagnostics_tool.get_diagnostics(paths[0], severity)
    return await _diagnostics_tool.check_multiple_files(paths, severity)


# get_diagnostics —— 合并自 get_diagnostics + check_syntax + check_multiple_files
registry.register(
    name="get_diagnostics",
    description=(
        "Get code diagnostics (syntax / type errors) for one or more files.\n\n"
        "Currently supports Python via Jedi (other languages return a clear 'unsupported' error).\n\n"
        "WHEN TO USE:\n"
        "✓ After editing Python code, verify it parses\n"
        "✓ Pre-commit sanity check on a batch of changed files\n"
        "✓ Before running, confirm no syntax errors\n\n"
        "BATCHING: pass `paths` as an array to check N files in one call. The handler runs them "
        "with shared Jedi context. Single-file? Pass a one-element array.\n\n"
        "Returns: single-file form when paths has 1 entry; {file_count, total_errors, total_warnings, files:[...]} when batched."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 20,
                "description": "File paths to check (1-20 per call)",
            },
            "severity": {
                "type": "string",
                "enum": ["error", "warning", "info", "hint"],
                "description": "Filter by severity level (optional)",
            },
        },
        "required": ["paths"],
    },
    handler=_handle_get_diagnostics,
    is_async=True,
    arg_aliases=_PATHS_PLURAL_ALIAS_MAP,
)


# ============================================================================
# 注册 Notebook 工具（Jupyter .ipynb 单元格编辑）
# 条件注册：仅当 nbformat 可用时注册；未装不影响其他工具
# ============================================================================

try:
    import nbformat as _nbformat_probe  # noqa: F401
    from .notebook_tool import notebook_edit as _handle_notebook_edit
    _NOTEBOOK_EDIT_AVAILABLE = True
except ImportError:
    _NOTEBOOK_EDIT_AVAILABLE = False
    logger.warning(
        "nbformat not installed, notebook_edit tool disabled. "
        "To enable: pip install nbformat"
    )

if _NOTEBOOK_EDIT_AVAILABLE:
    registry.register(
        name="notebook_edit",
        description=(
            "Edit a Jupyter notebook (.ipynb) cell. cell_index is 0-based.\n\n"
            "Modes:\n"
            "- replace (default): overwrite cell source at cell_index. "
            "Pass cell_type to convert between code/markdown.\n"
            "- insert: insert new cell at cell_index (cell_type REQUIRED). "
            "cell_index == total_cells appends at end.\n"
            "- delete: remove cell at cell_index (new_source / cell_type ignored).\n\n"
            "Workflow: call `read_file` first to see cell indices and current content. "
            "To create a new notebook from scratch, use `write_file` instead. "
            "To run cells, this tool can't — it edits source only.\n\n"
            "Guardian: writes to `backend/**` are blocked."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "notebook_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the .ipynb file",
                },
                "cell_index": {
                    "type": "integer",
                    "description": "Target cell position (0-based). For insert, may equal total_cells to append.",
                    "default": 0,
                },
                "new_source": {
                    "type": "string",
                    "description": "New source content for the cell (ignored for delete).",
                    "default": "",
                },
                "edit_mode": {
                    "type": "string",
                    "enum": ["replace", "insert", "delete"],
                    "description": "Edit operation mode.",
                    "default": "replace",
                },
                "cell_type": {
                    "type": "string",
                    "enum": ["code", "markdown"],
                    "description": "Cell type. REQUIRED for insert. Optional for replace (to convert type).",
                },
            },
            "required": ["notebook_path"],
        },
        handler=_handle_notebook_edit,
        is_async=True,
        arg_aliases=_NOTEBOOK_PATH_ALIAS_MAP,
    )
    logger.info("notebook_edit tool registered (nbformat available)")


# ============================================================================
# 注册 AskUserQuestion 工具（即发即返多选问答）
# ============================================================================

from .ask_user_question_tool import ask_user_question as _handle_ask_user_question

registry.register(
    name="ask_user_question",
    description=(
        "Ask the user 1-4 multiple-choice questions and wait for their selection. "
        "The user's answers are returned as THIS tool's result — you resume in the "
        "same turn with the answers in hand.\n\n"
        "After calling: stop generating immediately (no more tools, no more text). "
        "The tool result will contain the user's answers; continue from there.\n\n"
        "Use for real forks (which approach? destructive action OK? rename to what?). "
        "For plain yes/no, just ask in chat instead. Don't add an 'Other' option "
        "(UI appends a free-text fallback automatically). To recommend an option, "
        "put it first with ' (Recommended)' suffix on its label."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "description": "1-4 questions to ask the user.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Full question text, typically ending with ?",
                        },
                        "header": {
                            "type": "string",
                            "description": "Short chip tag shown in UI (≤12 chars)",
                        },
                        "multiSelect": {
                            "type": "boolean",
                            "default": False,
                            "description": "True to allow multiple answers; default single-select.",
                        },
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "1-5 words shown on the button.",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "Short sentence explaining the choice / trade-off.",
                                    },
                                    "preview": {
                                        "type": "string",
                                        "description": "Optional markdown/code snippet for side-by-side comparison.",
                                    },
                                },
                                "required": ["label", "description"],
                            },
                        },
                    },
                    "required": ["question", "header", "options"],
                },
            },
        },
        "required": ["questions"],
    },
    handler=_handle_ask_user_question,
    is_async=True,
)


# ============================================================================
# 子代理工具
# ============================================================================

async def _handle_spawn_subagent(
    agent_types: list,
    task: str,
    working_directory: str = None,
    **context
) -> dict:
    """启动子代理（工具处理函数）

    防护机制：
      1) 同轮限次：一次用户消息内最多调用 MAX_SPAWN_ATTEMPTS_PER_TURN（默认 1）次
      2) 信号量限并发：engine 内部用 semaphore 限真实并行数，防上游 LLM 限流
      3) 失败率信号：失败/限流过半时返回 success=False + should_retry=False，明确
         告知 LLM 不要重试

    Args:
        agent_types: 子代理类型列表
        task: 任务描述
        working_directory: 可选——分析的目标项目根目录（绝对路径）。设了之后
            子代理调 read_file/grep 用相对路径时会优先解到这里，避免
            被柳如烟自身后端 cwd 误命中。当 task 涉及"分析另一个项目"时**必须**给。
        **context: 上下文参数（session_id、agent_id、llm_client）
    """

    from backend.subagent.engine import (
        SubAgentEngine,
        register_spawn_attempt,
        is_rate_limit_error,
        MAX_SPAWN_ATTEMPTS_PER_TURN,
    )

    # 从上下文获取参数
    session_id = context.get('session_id')
    agent_id = context.get('agent_id')
    llm_client = context.get('llm_client')

    if not session_id or not agent_id or not llm_client:
        return {
            "success": False,
            "error": "Missing required context: session_id, agent_id, or llm_client",
            "should_retry": False,
        }

    # ── agent_types 运行时校验（schema enum 已去掉，支持动态 pack agents）──
    # 校验放这里而非 schema：pack 装/卸/reload 后立即生效，无需重新生成工具 schema。
    from backend.subagent.templates import get_subagent_templates
    available = get_subagent_templates()
    unknown = [t for t in (agent_types or []) if t not in available]
    if unknown:
        # 列出当前合法 types 帮 LLM 自我纠正
        valid_list = sorted(available.keys())
        return {
            "success": False,
            "error": (
                f"未识别的子代理类型: {unknown}。"
                f"当前可用类型: {valid_list}。"
                "注意 namespace 形式 `<pack>:<agent>`——pack 装的子代理必须带 pack 名前缀。"
            ),
            "should_retry": False,
            "hint_for_llm": "use_valid_agent_type_from_list",
        }

    # ── 第 1 层防护：同轮限次 ──
    if not register_spawn_attempt(session_id):
        return {
            "success": False,
            "error": (
                f"spawn_subagent 在本轮对话内已达到 {MAX_SPAWN_ATTEMPTS_PER_TURN} 次上限。"
                "请基于已有子代理的产出直接继续回答，不要再次调用此工具。"
            ),
            "should_retry": False,
            "hint_for_llm": "do_not_call_spawn_subagent_again_in_this_turn",
        }

    # ── working_directory 兜底提取 ──
    # 模型经常在 task 文本里塞绝对路径（"分析 d:\\foo\\bar 这个项目"）但忘了填
    # working_directory 参数。这里从 task 扫 Windows/POSIX 绝对路径，取第一个
    # **真实存在的目录**作为兜底。只在 caller 没显式传时生效，不覆盖已传值。
    if not working_directory and isinstance(task, str) and task:
        try:
            import re as _re
            from pathlib import Path as _Path
            # Windows: d:\foo\bar 或 D:/foo/bar；POSIX: /home/x/y（避开单 /）
            pattern = _re.compile(
                r"(?:[A-Za-z]:[\\/][^\s`'\"<>|?*\n]+)"
                r"|(?:/(?:[^\s`'\"<>|?*\n/]+/)+[^\s`'\"<>|?*\n/]*)"
            )
            for match in pattern.finditer(task):
                candidate = match.group(0).rstrip(".,;:)]}'\"")
                try:
                    p = _Path(candidate)
                    # 往上爬直到命中存在的目录（用户常写到子文件/不存在路径）
                    for _ in range(5):
                        if p.exists() and p.is_dir():
                            working_directory = str(p)
                            logger.info(
                                "[spawn_subagent] auto-extracted working_directory "
                                "from task: %r", working_directory
                            )
                            break
                        if p.parent == p:
                            break
                        p = p.parent
                    if working_directory:
                        break
                except Exception:
                    continue
        except Exception:
            logger.debug("[spawn_subagent] working_dir auto-extract failed", exc_info=True)

    # 创建引擎
    engine = SubAgentEngine(session_id, agent_id, llm_client)

    # F-3：从 v2 chain ContextVar 拿顶层 CancelEvent，派子 event 桥接 spawn_parallel。
    # cancel_event 触发时 wait_with_cancel 把 spawn_parallel task.cancel() + 抛
    # CancelEventCancelled，让 tool handler 返 cancel 错误结果给 LLM。
    # chain shim 未启用 → get_cancel_event 返 None → wait_with_cancel 退化成普通 await。
    child_cancel_event = None
    try:
        from liuruyan.runtime.context import get_cancel_event
        parent_cancel_event = get_cancel_event()
        if parent_cancel_event is not None:
            child_cancel_event = parent_cancel_event.child(
                trace_id=f"subagent:{session_id}"
            )
    except Exception:
        logger.exception(
            "[spawn_subagent] get_cancel_event failed; running without cancel bridge"
        )
        child_cancel_event = None

    # 并行启动子代理
    from liuruyan.subagents import CancelEventCancelled, wait_with_cancel
    try:
        results = await wait_with_cancel(
            engine.spawn_parallel(agent_types, task, working_directory=working_directory),
            child_cancel_event,
        )
    except CancelEventCancelled as exc:
        return {
            "success": False,
            "error": (
                f"subagent 运行被取消（reason={exc.reason!r} trace={exc.trace_id!r}）。"
                "后续不要重试 spawn_subagent。"
            ),
            "should_retry": False,
            "cancelled": True,
        }

    # ── 第 3 层防护：识别限流 / 失败率 ──
    total = len(results) or 1
    completed = sum(1 for r in results if r.status == "completed")
    failed = sum(1 for r in results if r.status in ["failed", "timeout"])
    rate_limited_count = sum(
        1 for r in results if r.error and is_rate_limit_error(r.error)
    )

    # 判定：半数以上失败，或任一子代理触发限流，都给 LLM 明确"不要重试"信号
    success = (completed > failed) and (rate_limited_count == 0)
    should_retry = False  # 无论如何都不建议重试：第 1 层已拒绝；重试也会撞同样限流

    summary_note = None
    if rate_limited_count > 0:
        summary_note = (
            f"{rate_limited_count}/{total} 个子代理触发上游 LLM 并发/QPS 限流，"
            "请基于已有产出继续回答，不要再次尝试 spawn_subagent（会继续被拒绝）。"
        )
    elif failed >= completed:
        summary_note = (
            f"子代理完成率 {completed}/{total} 偏低。请尽量基于已有产出回答，"
            "不建议再次调用 spawn_subagent。"
        )

    # 单个 subagent output 返给主 agent 的硬上限（字符数，≈ token×2.5）
    # 12000 × 3 agent ≈ 14k tokens，给 32k 模型留足余量；结论通常在头尾，
    # 超长时掐中间保头尾（头 2000 + 尾 10000），避免丢失 final answer
    _PER_AGENT_OUTPUT_CAP = 12000
    _HEAD_KEEP = 2000
    _TAIL_KEEP = _PER_AGENT_OUTPUT_CAP - _HEAD_KEEP

    def _shape_output(text: str) -> str:
        if not text:
            return ""
        if len(text) <= _PER_AGENT_OUTPUT_CAP:
            return text
        dropped = len(text) - _PER_AGENT_OUTPUT_CAP
        return (
            text[:_HEAD_KEEP]
            + f"\n\n[...中间省略 {dropped} 字，因长度超限截断 —— "
            f"如需完整内容请直接读源文件...]\n\n"
            + text[-_TAIL_KEEP:]
        )

    return {
        "success": success,
        "should_retry": should_retry,
        "note": summary_note,
        "subagents": [
            {
                "type": r.agent_type,
                "status": r.status,
                "elapsed": round(r.elapsed, 2),
                "tokens_used": r.tokens_used,
                "tools_called": r.tools_called,
                # output：完整（或截头尾）的 subagent 产出，给主 agent 读。
                # 之前这里叫 output_preview 且只切 200 字，导致主 agent 说"输出截断
                # 严重我直接读项目"然后就没下文——等于 subagent 白跑 1 分多钟。
                # 对标 Claude Code AgentTool 的 final assistant message 完整回传。
                "output": _shape_output(r.output),
                "output_preview": r.output[:300] + "..." if len(r.output) > 300 else r.output,
                "output_truncated": len(r.output) > _PER_AGENT_OUTPUT_CAP,
                "output_full_length": len(r.output),
                "error": r.error,
                "rate_limited": bool(r.error and is_rate_limit_error(r.error)),
            }
            for r in results
        ],
        "summary": {
            "total_elapsed": round(max(r.elapsed for r in results), 2),
            "total_tokens": sum(r.tokens_used for r in results),
            "total_tools": sum(r.tools_called for r in results),
            "completed": completed,
            "failed": failed,
            "rate_limited": rate_limited_count,
        }
    }

# spawn_subagent
registry.register(
    name="spawn_subagent",
    description=(
        "【本轮内】启动专家子代理并行查询信息，阻塞等待结果后，你要基于它们的产出继续回答用户。\n\n"
        "核心定位：这是『子函数并行』——你自己本轮还要答，只是临时请几个专家帮你搜集信息。\n\n"
        "中文术语：subagent 在用户对话里叫『专家』。用户说『请几个专家来 / 找专家分析 / 让专家看看 / 叫三个专家』——指的就是开 spawn_subagent（agent_types 里选对应类型 1~3 个）。\n\n"
        "可用类型分两类：\n"
        "【原生（代码工程领域）】\n"
        "- explore: 代码探索（扫描项目结构 / 技术栈 / 定位文件）\n"
        "- plan: 架构分析（依赖关系 / 设计评估）\n"
        "- code-reviewer: 代码审查（质量 / 改进建议）\n"
        "- instant-context: 精准检索（多轮验证后返回『文件:行区间』指针，回答\"X 在哪/谁调用了 X/改 X 影响哪些地方\"这类定位问题；比自己一行行 grep 更快更准）\n"
        "【插件 pack（已装的领域专家，namespace 形式 `<pack>:<agent>`）】\n"
        "- 例：`webnovel-writer:reviewer`、`webnovel-writer:context-agent`、`webnovel-writer:data-agent`、`webnovel-writer:deconstruction-agent`\n"
        "- 装其他 pack 后自动出现，规则同上（运行时校验，未识别类型会返回明确错误信息）\n\n"
        "如何对接 Claude Code 风格的 SKILL.md：\n"
        "- SKILL.md 写 `Agent(subagent_type=\"webnovel-writer:reviewer\", prompt=...)` 时，等价调用：\n"
        "  spawn_subagent(agent_types=[\"webnovel-writer:reviewer\"], task=...)\n"
        "- subagent_type 字符串原样可用（命名空间冒号不需转义）。\n\n"
        "什么时候用：\n"
        "- 用户明确请专家：『请专家 / 叫专家 / 找专家分析 / 让专家看看』\n"
        "- 用户问的问题需要先收集一手信息，你收到结果后要**在本轮继续组织回答**\n"
        "- 需要多个视角并行看同一个代码 / 章节 / 设定范围\n"
        "- SKILL.md 流程要求调 `Agent(subagent_type=\"...\")` → 用这个工具\n\n"
        "什么时候【不要】用：\n"
        "- 你不需要基于结果继续答，只是要委派一次交付 → 直接说明当前已不支持后台 worker 委派，或在本轮内完成可完成部分\n\n"
        "示例（代码工程，分析柳如烟自身）：\n"
        "spawn_subagent(\n"
        "  agent_types=['explore', 'plan'],\n"
        "  task='这个项目的鉴权模块是怎么组织的？'\n"
        ")\n\n"
        "示例（多智能体协同分析，调 pack 装的 reviewer）：\n"
        "spawn_subagent(\n"
        "  agent_types=['document-writer:reviewer'],\n"
        "  task='审查 docs/spec.md，按 reviewer schema 输出建议清单'\n"
        ")\n\n"
        "示例（分析另一个外部项目，传 working_directory）：\n"
        "spawn_subagent(\n"
        "  agent_types=['explore', 'plan', 'code-reviewer'],\n"
        "  task='完整分析该项目的架构与代码质量',\n"
        "  working_directory='/path/to/target/project'  # ← 分析非自身项目必给\n"
        ")"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "agent_types": {
                "type": "array",
                "items": {
                    # 取消 enum：动态支持 pack agents（namespace 形式 `<pack>:<agent>`）。
                    # 运行时由 handler 校验合法性（templates.is_valid_subagent_type）。
                    "type": "string",
                },
                "description": (
                    "要启动的子代理类型列表（可多选，建议 1-3 个）。"
                    "原生 4 个：'explore' / 'plan' / 'code-reviewer' / 'instant-context'；"
                    "pack 装的形如 'document-writer:reviewer'。"
                    "未识别类型会返回明确错误。"
                ),
                "minItems": 1,
                "maxItems": 3,
            },
            "task": {
                "type": "string",
                "description": "任务描述，清晰说明要分析什么（会传递给所有子代理）",
            },
            "working_directory": {
                "type": "string",
                "description": (
                    "可选——分析目标项目的根目录绝对路径（如 /path/to/target/project/）。"
                    "**当任务是分析柳如烟自身代码以外的另一个项目时必须传**，"
                    "否则子代理用相对路径会被错误解析到柳如烟自己的后端目录。"
                    "分析柳如烟自身时可不传。"
                ),
            },
        },
        "required": ["agent_types", "task"]
    },
    handler=_handle_spawn_subagent,
    is_async=True,
)


# ============================================================================
# cron_create / cron_list / cron_delete / sleep（FEATURE_CRON_TOOL 门控）
# ============================================================================
# 设计档：docs/cron_design.md（切片 D 落盘）
# 触发模式：fire 时直接 scheduler.submit() 起 worker（不模拟用户发问）
# 隔离：每条 job 自带 agent_id，CronList/Delete 强制 owner check
_FEATURE_CRON_TOOL = (
    (os.getenv("FEATURE_CRON_TOOL", "") or "").strip().lower()
    in ("1", "true", "yes", "on")
)


async def _handle_cron_create(
    cron: str,
    description: str,
    prompt: str,
    mode: str = "recurring",
    **context,
) -> dict:
    """注册一条 cron job。隶属当前 agent，到点自动起 worker。

    mode='recurring'（默认）= 按 cron 表达式持续触发，直到 cron_delete
    mode='once' = 一次性 reminder，触发一次后 _fire 自动 enabled=false
                  （仍可 cron_list 看历史；要彻底清理用 cron_delete）
    """
    agent_id = context.get("agent_id")
    session_id = context.get("session_id")
    if not agent_id:
        return {"ok": False, "error": "缺少 agent 上下文，无法创建 cron"}

    if mode not in ("recurring", "once"):
        return {"ok": False, "error": f"mode 仅支持 'recurring' 或 'once'，得到 {mode!r}"}

    from datetime import datetime
    from backend.cron_expr import next_fire_at, parse_cron
    from backend.cron_models import CronJob
    from backend.cron_scheduler import get_cron_scheduler

    try:
        ce = parse_cron(cron)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    # 描述 / prompt 长度兜底
    desc = (description or "").strip()
    if not desc:
        return {"ok": False, "error": "description 不能为空（≤200 字的人读说明）"}
    if len(desc) > 200:
        desc = desc[:200]
    prompt_text = (prompt or "").strip()
    if not prompt_text:
        return {"ok": False, "error": "prompt 不能为空（worker 收到的实际任务）"}

    job = CronJob(
        job_id=uuid.uuid4().hex[:12],
        agent_id=agent_id,
        parent_session_id=session_id or "",
        cron=cron.strip(),
        description=desc,
        prompt=prompt_text,
        created_at=time.time(),
        mode=mode,
    )

    sched = get_cron_scheduler()
    try:
        await sched.add(job)
    except Exception as e:
        return {"ok": False, "error": f"注册失败：{type(e).__name__}: {e}"}

    try:
        nxt = next_fire_at(ce, datetime.now())
        nxt_str = nxt.isoformat(timespec="minutes")
    except ValueError:
        nxt_str = "unknown"

    mode_note = "一次性提醒" if mode == "once" else "周期任务"
    return {
        "ok": True,
        "job_id": job.job_id,
        "next_fire_at": nxt_str,
        "mode": mode,
        "note": f"已建{mode_note} '{desc}'，下次触发 {nxt_str}。到点自动派分身跑，无需你介入。",
    }


async def _handle_cron_list(**context) -> dict:
    """仅列当前 agent 名下的 cron job。"""
    agent_id = context.get("agent_id")
    if not agent_id:
        return {"ok": False, "error": "缺少 agent 上下文"}

    from datetime import datetime
    from backend.cron_expr import next_fire_at, parse_cron
    from backend.cron_scheduler import get_cron_scheduler

    sched = get_cron_scheduler()
    jobs = await sched.list(agent_id=agent_id)
    now = datetime.now()
    items = []
    for j in jobs:
        try:
            ce = parse_cron(j.cron)
            nxt = next_fire_at(ce, now).isoformat(timespec="minutes")
        except ValueError:
            nxt = "unknown"
        items.append({
            "job_id": j.job_id,
            "cron": j.cron,
            "description": j.description,
            "mode": j.mode,
            "fire_count": j.fire_count,
            "last_fired_at": j.last_fired_at,
            "next_fire_at": nxt,
            "enabled": j.enabled,
        })
    return {"ok": True, "count": len(items), "jobs": items}


async def _handle_cron_delete(job_id: str, **context) -> dict:
    """删一条 cron job。owner check 强制：仅当前 agent 名下的可删。"""
    agent_id = context.get("agent_id")
    if not agent_id:
        return {"ok": False, "error": "缺少 agent 上下文"}
    if not job_id or not isinstance(job_id, str):
        return {"ok": False, "error": "job_id 必须是非空字符串"}
    from backend.cron_scheduler import get_cron_scheduler
    sched = get_cron_scheduler()
    ok = await sched.remove(job_id, agent_id=agent_id)
    if not ok:
        return {
            "ok": False,
            "error": f"job_id={job_id} 不存在或不属于当前 agent",
        }
    return {"ok": True, "removed": job_id}


async def _handle_cron_set_enabled(
    job_id: str,
    enabled: bool,
    **context,
) -> dict:
    """暂停 / 恢复 cron job。owner check 强制：仅当前 agent 名下的可改。"""
    agent_id = context.get("agent_id")
    if not agent_id:
        return {"ok": False, "error": "缺少 agent 上下文"}
    if not job_id or not isinstance(job_id, str):
        return {"ok": False, "error": "job_id 必须是非空字符串"}
    if not isinstance(enabled, bool):
        return {"ok": False, "error": "enabled 必须是 boolean"}
    from backend.cron_scheduler import get_cron_scheduler
    sched = get_cron_scheduler()
    ok = await sched.set_enabled(job_id, enabled, agent_id=agent_id)
    if not ok:
        return {
            "ok": False,
            "error": f"job_id={job_id} 不存在或不属于当前 agent",
        }
    state = "已恢复" if enabled else "已暂停"
    return {"ok": True, "job_id": job_id, "enabled": enabled, "note": f"{state}定时任务 {job_id}"}


async def _handle_sleep(seconds: int = 0, **context) -> dict:
    """阻塞 await asyncio.sleep。clamp 到 [1, 3600] 秒。"""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return {"ok": False, "error": "seconds 必须是整数"}
    s = max(1, min(s, 3600))
    await asyncio.sleep(s)
    return {"ok": True, "slept": s}


_CRON_CREATE_DESCRIPTION = (
    "你的内置定时工具（不是 OS cron/schtasks）。到点自动派分身跑 prompt，"
    "结果以新气泡进聊天流。\n"
    "cron 5 段：分 时 日 月 周（* / N / N,M / N-M / */N）。"
    "建议避开 0/30 整点（讯飞并发敏感）：早 9 点写 \"3 9 * * *\"，"
    "每小时写 \"7 * * * *\"。同时段多条错峰 5~10 分。\n"
    "mode='recurring'（默认）= 持续；'once' = 一次性，触发后自动 enabled=false。"
    "『明天/下周/一次性/提醒我』→ once；『每天/每周/每月』→ recurring。\n"
    "用户措辞像 shell（echo/ls/pwd）也调本工具，prompt 转成自然语言任务。\n"
    "作用域：仅当前 agent 可见可改。description ≤200 字给人读，prompt 是 worker 实际任务。"
)

_CRON_LIST_DESCRIPTION = (
    "【后台定时】列出当前 agent 名下的所有 cron job。其它 agent 的 job 看不到。\n"
    "返回每条的 job_id / cron / description / fire_count / last_fired_at / "
    "next_fire_at / enabled。\n\n"
    "用户问『有哪些定时任务 / 列一下 cron』时调用。"
)

_CRON_DELETE_DESCRIPTION = (
    "【后台定时】删除一条 cron job。job_id 来自 cron_list 输出。\n\n"
    "owner check 强制：只能删自己 agent 名下的 job；删不存在或他人的 job "
    "返回 ok=false（不会误删）。"
)

_CRON_SET_ENABLED_DESCRIPTION = (
    "【后台定时】暂停或恢复一条 cron job。job_id 来自 cron_list 输出。\n\n"
    "暂停（enabled=false）后 _run_loop tick 会跳过该 job 不再触发；恢复\n"
    "（enabled=true）立即重新进入触发循环，next_fire_at 按 cron 表达式\n"
    "重算（不补跑暂停期间漏的）。\n\n"
    "什么时候用：\n"
    "✓ 用户说『暂停那个定时任务』『先停一下别跑了』『恢复 / 重新启用』\n"
    "✓ 临时不想要触发，又不想丢失任务定义 / fire_count 历史\n\n"
    "owner check 强制：只能改自己 agent 名下的 job。"
)

_SLEEP_DESCRIPTION = (
    "【慎用】让本轮工具调用阻塞 await N 秒（最长 3600，超过 clamp）。\n\n"
    "什么时候用：\n"
    "✓ 等外部异步事件成熟（已知 ~30s 后第三方 webhook 会到）\n"
    "✓ 给 worker / 用户 一个反应窗口\n"
    "✓ 拉长 polling 间隔（避免高频打外部 API）\n\n"
    "什么时候【不要】用：\n"
    "✗ 想定时『每天 X 点跑一次』 → 永远用 cron_create，不要自己 sleep 等\n"
    "✗ 等用户回复 → 用 ask_user_question\n"
    "✗ 任意『等一会再说』场景没有具体外部事件需要等 → 直接做或拒绝\n\n"
    "成本提示：sleep 期间 prompt cache 5 分钟过期，>300s 会让下一轮 cache 重算。"
)

if _FEATURE_CRON_TOOL:
    registry.register(
        name="cron_create",
        description=_CRON_CREATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "required": ["cron", "description", "prompt"],
            "properties": {
                "cron": {
                    "type": "string",
                    "description": (
                        "5 段 cron 表达式（分 时 日 月 周）。"
                        "建议避开 0/30 分整点，例如 \"3 9 * * *\" 而非 \"0 9 * * *\"。"
                    ),
                    "pattern": r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$",
                },
                "description": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "≤200 字的人读说明，会出现在派发卡 [Cron] 前缀后。",
                },
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": "触发时 worker 收到的完整任务 prompt（实际指令）。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["recurring", "once"],
                    "default": "recurring",
                    "description": (
                        "'recurring'（默认）持续触发；'once' 一次性提醒，触发后自动 enabled=false。"
                        "用户说『明天 / 下周 / 一次性 / 提醒我』时用 once。"
                    ),
                },
            },
        },
        handler=_handle_cron_create,
        is_async=True,
    )

    registry.register(
        name="cron_list",
        description=_CRON_LIST_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {},
        },
        handler=_handle_cron_list,
        is_async=True,
    )

    registry.register(
        name="cron_delete",
        description=_CRON_DELETE_DESCRIPTION,
        input_schema={
            "type": "object",
            "required": ["job_id"],
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "cron_list 返回的 job_id",
                },
            },
        },
        handler=_handle_cron_delete,
        is_async=True,
    )

    registry.register(
        name="cron_set_enabled",
        description=_CRON_SET_ENABLED_DESCRIPTION,
        input_schema={
            "type": "object",
            "required": ["job_id", "enabled"],
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "cron_list 返回的 job_id",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "true=恢复触发，false=暂停（不删任务定义）",
                },
            },
        },
        handler=_handle_cron_set_enabled,
        is_async=True,
    )

    registry.register(
        name="sleep",
        description=_SLEEP_DESCRIPTION,
        input_schema={
            "type": "object",
            "required": ["seconds"],
            "properties": {
                "seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3600,
                    "description": "阻塞秒数。超出 [1, 3600] 范围会被 clamp。",
                },
            },
        },
        handler=_handle_sleep,
        is_async=True,
    )


# ============================================================================
# IDE 桥接 tool（C-2：后端反向调 VSCode 插件 bridge）
#
# 这些 tool handler 不直接做事——它们调 IdeToolBridge.request 让前端代执行
# runtime.ide.<x>(args) 然后回填结果。详见 backend/ide_tool_bridge.py 顶部 docstring。
#
# 命名前缀 ide_*：跟现有 backend 原生 tool（read_file / get_diagnostics / ...）区分；
# 后者 backend 自己能做，前者**必须**借助前端 webview。
#
# 协议：handler 异步等待结果（默认 30s 超时），结果是 dict {ok, result|error}。
# 给 LLM 的字符串：成功简短反馈（"Opened xxx.py"），失败 "Error: ..." 让模型看到
# 错误自己降级（比如 fallback 到 backend 原生 read_file 而不是 ide_open_file）。
# ============================================================================

from backend.ide_tool_bridge import call_ide_tool as _call_ide_tool


async def _handle_ide_open_file(**kwargs) -> str:
    session_id = kwargs.get("session_id")
    args = {
        k: kwargs.get(k)
        for k in ("uri", "fsPath", "line", "character", "preview", "viewColumn")
        if kwargs.get(k) is not None
    }
    if not args.get("uri") and not args.get("fsPath"):
        return "Error: ide_open_file requires uri or fsPath"
    resp = await _call_ide_tool(session_id, "open_file", args)
    if not resp.get("ok"):
        return f"Error: {resp.get('error', 'unknown')}"
    payload = resp.get("result") or {}
    line_info = ""
    if isinstance(args.get("line"), int):
        line_info = f" at line {args['line'] + 1}"
    return (
        f"Opened in IDE: {payload.get('uri') or payload.get('fsPath') or '(unknown)'}{line_info} "
        f"(lineCount={payload.get('lineCount', '?')})"
    )


async def _handle_ide_insert_snippet(**kwargs) -> str:
    session_id = kwargs.get("session_id")
    text = kwargs.get("text")
    if not isinstance(text, str):
        return "Error: ide_insert_snippet requires text (string)"
    args = {"text": text}
    for k in ("uri", "line", "character", "replace"):
        if kwargs.get(k) is not None:
            args[k] = kwargs.get(k)
    resp = await _call_ide_tool(session_id, "insert_snippet", args)
    if not resp.get("ok"):
        return f"Error: {resp.get('error', 'unknown')}"
    payload = resp.get("result") or {}
    return (
        f"Inserted into IDE: {payload.get('uri') or '(active editor)'} "
        f"(applied={payload.get('applied', '?')}, lineCount={payload.get('lineCount', '?')})"
    )


async def _handle_ide_get_diagnostics(**kwargs) -> str:
    session_id = kwargs.get("session_id")
    args: Dict[str, Any] = {}
    if kwargs.get("scope") == "all":
        args["scope"] = "all"
    elif isinstance(kwargs.get("uri"), str):
        args["uri"] = kwargs["uri"]
    resp = await _call_ide_tool(session_id, "get_diagnostics", args)
    if not resp.get("ok"):
        return f"Error: {resp.get('error', 'unknown')}"
    payload = resp.get("result") or []
    if not payload:
        return "IDE diagnostics: no issues."
    # 给 LLM 一个紧凑的 JSON list；schema 跟 vscode-extension/server/ide-routes.ts normalizeDiagnostic 对齐
    return json.dumps({"diagnostics": payload}, ensure_ascii=False)


async def _handle_ide_open_terminal(**kwargs) -> str:
    """在用户 VSCode 里打开 / 复用一个 terminal，返回 terminal_id 给后续调用。"""
    session_id = kwargs.get("session_id")
    args: Dict[str, Any] = {}
    if isinstance(kwargs.get("name"), str):
        args["name"] = kwargs["name"]
    if isinstance(kwargs.get("cwd"), str):
        args["cwd"] = kwargs["cwd"]
    if isinstance(kwargs.get("reuse"), bool):
        args["reuse"] = kwargs["reuse"]
    resp = await _call_ide_tool(session_id, "open_terminal", args)
    if not resp.get("ok"):
        return f"Error: {resp.get('error', 'unknown')}"
    payload = resp.get("result") or {}
    status = "reused" if not payload.get("created") else "created"
    return (
        f"Terminal {status}: name='{payload.get('name', '?')}' terminal_id={payload.get('terminal_id', '?')}. "
        f"Use this terminal_id in ide_run_command_in_terminal to target the same terminal."
    )


async def _handle_ide_run_command_in_terminal(**kwargs) -> str:
    """在 VSCode terminal 里跑命令。output 不保证回取（见 description 的 trade-off 说明）。"""
    session_id = kwargs.get("session_id")
    command = kwargs.get("command")
    if not isinstance(command, str) or not command.strip():
        return "Error: ide_run_command_in_terminal requires command (non-empty string)"
    args: Dict[str, Any] = {"command": command}
    if isinstance(kwargs.get("terminal_id"), str):
        args["terminal_id"] = kwargs["terminal_id"]
    if isinstance(kwargs.get("wait_for_output"), bool):
        args["wait_for_output"] = kwargs["wait_for_output"]
    if isinstance(kwargs.get("timeout"), int):
        args["timeout"] = kwargs["timeout"]
    # timeout 给够：默认 60s 命令 + 2s shell integration 激活窗口 + 缓冲
    call_timeout = float(args.get("timeout", 60)) + 15.0
    resp = await _call_ide_tool(session_id, "run_command_in_terminal", args, timeout=call_timeout)
    if not resp.get("ok"):
        return f"Error: {resp.get('error', 'unknown')}"
    payload = resp.get("result") or {}
    captured = payload.get("captured", False)
    terminal_id = payload.get("terminal_id", "?")
    if captured:
        output = payload.get("output", "")
        exit_code = payload.get("exitCode")
        exit_info = f" (exitCode={exit_code})" if exit_code is not None else ""
        # 给 LLM 紧凑结构
        return json.dumps(
            {
                "terminal_id": terminal_id,
                "captured": True,
                "exitCode": exit_code,
                "output": output[-8000:] if len(output) > 8000 else output,
                "output_truncated": len(output) > 8000,
                "note": f"Command completed in IDE terminal{exit_info}.",
            },
            ensure_ascii=False,
        )
    reason = payload.get("reason", "shell integration unavailable")
    return (
        f"Command sent to terminal {terminal_id}, but output NOT captured. "
        f"Reason: {reason} "
        f"If you need output, call `execute_command` (backend native) instead of this tool."
    )


async def _handle_ide_apply_edit(**kwargs) -> str:
    """让用户在 VSCode 里 review + accept 整文件修改。

    跟 edit / multi_edit（直接写盘）不同：这个 tool 走 inline diff（v2 默认）/ modal
    （v1 fallback）让用户 review。适合重要/大改/多文件，让用户参与确认的场景。

    超时：660s（11 分钟）。比 vscode-extension endpoint 的 600s 多 60s buffer，
    保证 endpoint 永远先 timeout 自动 abortSession，backend 才接收到错误，
    避免 backend 先 timeout 后 endpoint 还在等导致 ChangeRegistry 残留 / SSE 状态错乱。
    """
    session_id = kwargs.get("session_id")
    new_content = kwargs.get("newContent")
    if not isinstance(new_content, str):
        return "Error: ide_apply_edit requires newContent (string; the full file content after change)"
    args: Dict[str, Any] = {"newContent": new_content}
    if isinstance(kwargs.get("uri"), str):
        args["uri"] = kwargs["uri"]
    elif isinstance(kwargs.get("fsPath"), str):
        args["fsPath"] = kwargs["fsPath"]
    else:
        return "Error: ide_apply_edit requires uri or fsPath"
    if isinstance(kwargs.get("description"), str):
        args["description"] = kwargs["description"]

    # timeout 660s = vscode-extension endpoint 600s + 60s buffer（让 endpoint 先 timeout abortSession）
    resp = await _call_ide_tool(session_id, "apply_edit", args, timeout=660.0)
    if not resp.get("ok"):
        return f"Error: {resp.get('error', 'unknown')}"
    payload = resp.get("result") or {}
    applied = payload.get("applied")
    reason = payload.get("reason", "")
    uri_display = payload.get("uri") or payload.get("fsPath") or args.get("uri") or args.get("fsPath") or "(unknown)"
    # v2 inline diff hunk 级细节（v1 modal 路径下 accepted/rejected/total/status 都是 None）
    hunk_detail = ""
    accepted_n = payload.get("accepted")
    rejected_n = payload.get("rejected")
    total_n = payload.get("total")
    status = payload.get("status")
    if isinstance(accepted_n, int) and isinstance(rejected_n, int) and isinstance(total_n, int):
        hunk_detail = f" [{accepted_n} accepted / {rejected_n} rejected of {total_n} hunks; status={status}]"
    if applied:
        return f"Applied to IDE: {uri_display}.{hunk_detail} {reason}"
    # 用户拒绝或落盘失败 —— 返回清晰信号让 LLM 不要反复尝试
    return f"Rejected / not applied: {uri_display}.{hunk_detail} {reason}"


registry.register(
    name="ide_open_file",
    description=(
        "**Navigate the user's VSCode IDE cursor to a file (and optional line).** "
        "This makes the user's editor *jump to* the file —— it does NOT read content for you.\n\n"
        "**When to use (prefer over read_file)**:\n"
        "- User says '打开 X' / 'open X' / '跳到 X 第 N 行' / 'navigate to X' / 'show me X in my IDE' / "
        "'让我看看 X' / '让我跳过去看' —— anything about *the user seeing the file in their editor*.\n"
        "- You just modified a file and want the user to review it in IDE.\n\n"
        "**When NOT to use (use read_file instead)**:\n"
        "- You need to read file content yourself to answer / continue editing.\n"
        "- User asks 'what's in X' / '帮我看看 X 第 N 行写了什么' (they want YOU to read and tell).\n\n"
        "Decision rule: 'user wants to navigate' → ide_open_file; 'user wants to know content' → read_file.\n"
        "Both can be combined: read_file first to understand, then ide_open_file to take them there.\n\n"
        "Args: uri (file://...) OR fsPath (local path; forward slashes OK on Windows), "
        "line (0-indexed), character (0-indexed), preview (default false), viewColumn ('active'|'beside').\n\n"
        "Returns 'Opened in IDE: ...' on success, 'Error: ...' on failure "
        "(no VSCode runtime / file not found / timeout). Requires VSCode extension bridge connected."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "uri": {"type": "string", "description": "file:// URI of the target file (alternative to fsPath)"},
            "fsPath": {"type": "string", "description": "Local filesystem path (alternative to uri). Forward slashes OK on Windows."},
            "line": {"type": "integer", "description": "0-indexed line to jump cursor to. Optional."},
            "character": {"type": "integer", "description": "0-indexed character within the line. Optional."},
            "preview": {"type": "boolean", "description": "true = preview tab (italic, replaced by next preview); default false"},
            "viewColumn": {"type": "string", "enum": ["active", "beside"], "description": "'beside' splits editor"},
        },
    },
    handler=_handle_ide_open_file,
    is_async=True,
    arg_aliases=_FSPATH_ALIAS_MAP,
)

registry.register(
    name="ide_insert_snippet",
    description=(
        "Insert or replace text in the user's VSCode editor at cursor (or a specific line/char). "
        "Requires the user to have liuruyan VSCode extension running with bridge connected.\n\n"
        "Args: text (required), uri (default current editor), line/character (0-indexed, default cursor), "
        "replace (default false; if true and there's a non-empty selection, replaces it instead of inserting).\n\n"
        "Use sparingly — `edit` / `multi_edit` (which write file directly) is preferred when you can. "
        "This tool is for cases where the user explicitly asks to insert at cursor or you want them to "
        "see the change happen live in their editor. The user can Ctrl+Z to undo.\n\n"
        "Returns 'Inserted ...' on success, 'Error: ...' on failure."
    ),
    input_schema={
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string", "description": "Content to insert/replace. Multi-line OK."},
            "uri": {"type": "string", "description": "Target file URI. Default = active editor."},
            "line": {"type": "integer", "description": "0-indexed line. Default = cursor line."},
            "character": {"type": "integer", "description": "0-indexed character. Default = cursor column."},
            "replace": {"type": "boolean", "description": "true + non-empty selection → replace selection. Default false."},
        },
    },
    handler=_handle_ide_insert_snippet,
    is_async=True,
)

registry.register(
    name="ide_get_diagnostics",
    description=(
        "Get real-time diagnostics (LSP errors / warnings) from the user's VSCode IDE. "
        "Requires the user to have liuruyan VSCode extension running with bridge connected.\n\n"
        "Args: scope='current' (default; the active editor's diagnostics) | 'all' (entire workspace) | "
        "uri='file://...' (specific file).\n\n"
        "VS the existing `get_diagnostics` (Jedi-based, Python only): this tool sees ALL languages "
        "the IDE has language servers for (TS/Go/Rust/...) and gets fresher results (live LSP). "
        "Trade-off: requires VSCode IDE; falls back gracefully if not available.\n\n"
        "Returns JSON: {diagnostics: [{uri, fsPath, severity, message, source, code, range}, ...]}."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["current", "all"], "description": "'current' = active editor; 'all' = entire workspace"},
            "uri": {"type": "string", "description": "Specific file URI. Mutually exclusive with scope='all'."},
        },
    },
    handler=_handle_ide_get_diagnostics,
    is_async=True,
)

registry.register(
    name="ide_open_terminal",
    description=(
        "Open or reuse a terminal in the user's VSCode IDE. Returns a terminal_id you can pass "
        "to ide_run_command_in_terminal to target the same terminal across multiple commands.\n\n"
        "When to use: you want the user to see a sequence of commands run live in their IDE terminal "
        "(vs backend `execute_command` which runs silently). Useful for multi-step flows like "
        "'install deps → run tests → show results' where the user benefits from seeing each step.\n\n"
        "Args: name (default '柳如烟'), cwd (optional absolute path), reuse (default true — pick existing "
        "same-name terminal if any).\n\n"
        "Returns text including terminal_id. Requires VSCode extension bridge connected."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Terminal display name. Default '柳如烟'."},
            "cwd": {"type": "string", "description": "Working directory absolute path. Optional."},
            "reuse": {"type": "boolean", "description": "Default true — reuse existing same-name terminal if any."},
        },
    },
    handler=_handle_ide_open_terminal,
    is_async=True,
)

registry.register(
    name="ide_run_command_in_terminal",
    description=(
        "Run a command in a VSCode IDE terminal (visible to the user). **Output capture is best-effort**.\n\n"
        "**VS the native `execute_command` tool**:\n"
        "- `execute_command`: runs silently in backend, **guaranteed output capture**. Use this 90% of the time.\n"
        "- `ide_run_command_in_terminal`: runs in IDE terminal (**user sees it live**), output only captured "
        "if VSCode 1.93+ shell integration is active.\n\n"
        "**When to use this (rare)**:\n"
        "- User explicitly asks to run something 'in my terminal' / '在我的终端里跑'\n"
        "- Interactive commands where user wants to watch / intervene (long-running dev servers, watch modes)\n"
        "- Multi-step workflow where the user benefits from seeing each step in their IDE\n\n"
        "**When NOT to use**:\n"
        "- You need the output to continue reasoning → always `execute_command`\n"
        "- Silent background tasks → `execute_command`\n"
        "- Not in VSCode runtime → `execute_command` (this tool errors out)\n\n"
        "Args: command (required), terminal_id (optional — default auto open/reuse '柳如烟'), "
        "wait_for_output (default true, best-effort), timeout (seconds, default 60, cap 600).\n\n"
        "Returns JSON if output captured: {terminal_id, captured: true, exitCode, output, note}. "
        "Otherwise plain text '...output NOT captured. If you need output, call execute_command...'."
    ),
    input_schema={
        "type": "object",
        "required": ["command"],
        "properties": {
            "command": {"type": "string", "description": "Command to execute. Syntax matches the IDE terminal's shell."},
            "terminal_id": {"type": "string", "description": "Target terminal. Default = open/reuse '柳如烟' terminal."},
            "wait_for_output": {"type": "boolean", "description": "Default true (best-effort capture). false = fire-and-forget."},
            "timeout": {"type": "integer", "description": "Seconds, default 60, max 600."},
        },
    },
    handler=_handle_ide_run_command_in_terminal,
    is_async=True,
)

registry.register(
    name="ide_apply_edit",
    description=(
        "**Propose a full-file edit to the user's VSCode IDE with a diff review + user approval.**\n\n"
        "The user sees an inline diff (v2; per-hunk Accept/Reject CodeLens) or a side-by-side diff modal (v1 fallback). "
        "On accept, the file is written to disk. On reject or timeout (10 minutes), no change is made.\n\n"
        "**When to use (prefer over `edit` / `multi_edit`)**:\n"
        "- Significant rewrites / refactors where the user should review before commit\n"
        "- New file creation when the user asked you to 'create' / 'propose' / 'draft' something\n"
        "- When the user explicitly says 'show me the diff first' / 'review before applying' / '让我先看看'\n"
        "- Multi-step changes that benefit from user eyeballs mid-way\n\n"
        "**When NOT to use (use `edit` or `multi_edit` instead)**:\n"
        "- Small fixes (single-line typo, rename) where diff review is overkill\n"
        "- Your own internal scratch / intermediate states\n"
        "- Not in VSCode runtime (will fail; use edit/multi_edit which work everywhere)\n\n"
        "**Full-file semantics**: you pass the ENTIRE file content as `newContent` — not a patch. "
        "Read current content first (via read_file) if you need to base on existing code.\n\n"
        "Args: newContent (required, the full file after change), uri OR fsPath (required), "
        "description (optional, shown in diff view title and approval dialog).\n\n"
        "Returns 'Applied to IDE: <uri>. user accepted ...' or 'Rejected / not applied: <uri>. ...'. "
        "On rejection, do NOT immediately retry — explain to the user why you proposed this and let them "
        "steer the next step. Timeout (10 minutes) also counts as rejection."
    ),
    input_schema={
        "type": "object",
        "required": ["newContent"],
        "properties": {
            "newContent": {
                "type": "string",
                "description": "The full file content AFTER your proposed change. Multi-line OK.",
            },
            "uri": {
                "type": "string",
                "description": "Target file URI (file://...). Alternative to fsPath.",
            },
            "fsPath": {
                "type": "string",
                "description": "Target file local path. Alternative to uri. Forward slashes OK on Windows.",
            },
            "description": {
                "type": "string",
                "description": "One-line summary of the change, shown in diff title and approval dialog.",
            },
        },
    },
    handler=_handle_ide_apply_edit,
    is_async=True,
    arg_aliases=_FSPATH_ALIAS_MAP,
)


logger.info(f"Clean tool registry initialized with {len(registry.tools)} tools")


def get_tool_registry():
    """获取工具注册表（全局单例）"""
    return registry
