"""ACP 反向能力 + permission：把柳如烟工具调用通过 ACP 协议交给 client（如 Zed）执行。

设计原则（@docs/acp_integration_design.md §6.4.3）：
- ACP server 启动后，根据 client 声明的 capabilities 挑选性 wrap 已有 tool handlers
- wrap 而不是替换：conn 不可用时（脚本调用 / ContextVar 没设）走原 handler，保证向后兼容
- ContextVar 隔离：每次 prompt 进入设 conn + acp_session_id，prompt 结束 reset；多 ACP 连接并存安全
- permission：destructive 工具（write_file / edit）执行前先 request_permission，被拒返错误给 LLM

Phase 4 范围：
- fs/read_text_file：替换 read_file（仅当 client 声明 fs.read_text_file=true）
- fs/write_text_file：替换 write_file 和 edit（仅当 client 声明 fs.write_text_file=true，先 request_permission）
- session/request_permission：暴露 acp_request_permission 给 destructive tools 用

Phase 4.5（暂留）：
- terminal/create + terminal/output：替换 execute_command（流式输出复杂，单独迭代）
- elicitation：替换 ask_user_question（client 声明 elicitation 时启用）
"""

from __future__ import annotations

import contextvars
import json
import logging
from typing import Any, Optional

import acp
from acp.schema import AllowedOutcome, PermissionOption, ToolCallUpdate

logger = logging.getLogger(__name__)


# ============================================================
# ContextVars：每次 prompt 进入设值，结束 reset
# ============================================================

_acp_conn_var: "contextvars.ContextVar[Optional[acp.Client]]" = contextvars.ContextVar(
    "acp_conn", default=None
)
"""当前 ACP prompt 调用的 client 反向通道。设 None 表示非 ACP 路径（走原 handler）。"""

_acp_session_id_var: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "acp_session_id", default=None
)
"""当前 ACP session_id（不是柳如烟 lry_sid，是 ACP 协议层 id）。反向 fs/permission 调用要带这个。"""

_client_caps_var: "contextvars.ContextVar[Optional[Any]]" = contextvars.ContextVar(
    "acp_client_caps", default=None
)
"""client 在 initialize 时声明的 capabilities。反向调用前要看是否声明了对应能力。"""


def set_acp_context(conn: acp.Client, acp_session_id: str, client_capabilities: Any) -> tuple:
    """在 prompt 入口设上下文，返回 token tuple 用于 reset。"""
    t1 = _acp_conn_var.set(conn)
    t2 = _acp_session_id_var.set(acp_session_id)
    t3 = _client_caps_var.set(client_capabilities)
    return (t1, t2, t3)


def reset_acp_context(tokens: tuple) -> None:
    """prompt 结束时还原。"""
    t1, t2, t3 = tokens
    _acp_conn_var.reset(t1)
    _acp_session_id_var.reset(t2)
    _client_caps_var.reset(t3)


# ============================================================
# 能力探测
# ============================================================

def _client_has_fs_read() -> bool:
    caps = _client_caps_var.get()
    if caps is None:
        return False
    fs = getattr(caps, "fs", None)
    if fs is None:
        return False
    return bool(getattr(fs, "read_text_file", False))


def _client_has_fs_write() -> bool:
    caps = _client_caps_var.get()
    if caps is None:
        return False
    fs = getattr(caps, "fs", None)
    if fs is None:
        return False
    return bool(getattr(fs, "write_text_file", False))


# ============================================================
# 反向能力实现
# ============================================================

async def acp_request_permission(
    *,
    tool_name: str,
    title: str,
    description: str = "",
    raw_input: Any = None,
    options: Optional[list[dict]] = None,
) -> Optional[str]:
    """请求 ACP client 弹批准 UI；返被选 option_id，被拒返 None。

    options 默认提供"允许一次/拒绝"两选；调用方可自定义。
    被拒时 caller 应该给 LLM 返一个明确的"用户拒绝"结果，避免 LLM 卡等。
    """
    conn = _acp_conn_var.get()
    sid = _acp_session_id_var.get()
    if conn is None or sid is None:
        # 非 ACP 路径：默认允许（保持向后兼容）
        return "allow_once"

    if options is None:
        options = [
            {"option_id": "allow_once", "name": "允许一次", "kind": "allow_once"},
            {"option_id": "reject", "name": "拒绝", "kind": "reject_once"},
        ]
    perm_options = [PermissionOption(**o) for o in options]

    # ToolCallUpdate 是 ACP 协议 tool_call 的轻量描述（client 弹 UI 用）
    tool_call = ToolCallUpdate(
        tool_call_id=f"perm_{tool_name}",
        title=title or tool_name,
        kind="other",
        raw_input=raw_input,
    )

    try:
        resp = await conn.request_permission(
            options=perm_options,
            session_id=sid,
            tool_call=tool_call,
        )
    except Exception:
        logger.exception("acp_request_permission: client error, defaulting to deny")
        return None

    outcome = getattr(resp, "outcome", None)
    if isinstance(outcome, AllowedOutcome):
        return getattr(outcome, "option_id", "allow_once")
    return None  # DeniedOutcome 或解析失败


async def _acp_read_file_reverse(**kwargs) -> str:
    """走 ACP fs/read_text_file 让 client 读。

    柳如烟 read_file 有 line_offset/limit 参数（int），ACP 接口字段是 line/limit。
    映射：path 直传；offset → line（ACP 是 1-based 起始行）；limit 直传。
    """
    conn = _acp_conn_var.get()
    sid = _acp_session_id_var.get()
    if conn is None or sid is None:
        return json.dumps({"error": "ACP context not set"})

    path = kwargs.get("path") or kwargs.get("file_path")
    if not path:
        return json.dumps({"error": "missing path"})

    line = kwargs.get("offset")
    if line is not None:
        try:
            line = int(line)
            if line <= 0:
                line = None
        except Exception:
            line = None

    limit = kwargs.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except Exception:
            limit = None

    try:
        resp = await conn.read_text_file(
            path=path,
            session_id=sid,
            line=line,
            limit=limit,
        )
    except Exception as exc:
        logger.exception("acp_read_file_reverse: client error")
        return json.dumps({"error": f"ACP fs/read_text_file failed: {exc}"})

    content = getattr(resp, "content", "") or ""
    # 跟柳如烟 read_file_tool 输出对齐：纯文本内容
    return content


async def _acp_write_file_reverse(**kwargs) -> str:
    """走 ACP fs/write_text_file 让 client 写（先 request_permission）。"""
    conn = _acp_conn_var.get()
    sid = _acp_session_id_var.get()
    if conn is None or sid is None:
        return json.dumps({"error": "ACP context not set"})

    path = kwargs.get("path") or kwargs.get("file_path")
    content = kwargs.get("content")
    if not path or content is None:
        return json.dumps({"error": "missing path or content"})

    # 先 request_permission（destructive 操作）
    decision = await acp_request_permission(
        tool_name="write_file",
        title=f"Write file: {path}",
        description=f"Agent wants to write {len(content)} chars to {path}",
        raw_input={"path": path, "content_preview": content[:200]},
    )
    if decision is None:
        return json.dumps({"error": "User denied write_file via ACP permission"})

    try:
        await conn.write_text_file(
            path=path,
            content=content,
            session_id=sid,
        )
    except Exception as exc:
        logger.exception("acp_write_file_reverse: client error")
        return json.dumps({"error": f"ACP fs/write_text_file failed: {exc}"})

    lines_written = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return json.dumps({
        "success": True,
        "path": path,
        "lines_written": lines_written,
        "via": "acp_fs_write_text_file",
    })


# ============================================================
# Handler 包装器：根据 ContextVar 决定走 ACP 还是原路径
# ============================================================

def _make_fs_read_wrapper(original_handler):
    """包装 read_file handler。conn 在且 client 支持 fs.read → 走 ACP，否则走原 handler。"""
    import asyncio

    is_async_orig = asyncio.iscoroutinefunction(original_handler)

    async def wrapper(**kwargs):
        if _acp_conn_var.get() is not None and _client_has_fs_read():
            try:
                return await _acp_read_file_reverse(**kwargs)
            except Exception:
                logger.exception("ACP fs/read failed, falling back to original handler")
        # 兜底：原 handler
        if is_async_orig:
            return await original_handler(**kwargs)
        return original_handler(**kwargs)

    wrapper.__name__ = "acp_aware_read_file"
    return wrapper


def _make_fs_write_wrapper(original_handler):
    """包装 write_file handler。conn 在且 client 支持 fs.write → 走 ACP（含 permission），否则原 handler。"""
    import asyncio

    is_async_orig = asyncio.iscoroutinefunction(original_handler)

    async def wrapper(**kwargs):
        if _acp_conn_var.get() is not None and _client_has_fs_write():
            try:
                return await _acp_write_file_reverse(**kwargs)
            except Exception:
                logger.exception("ACP fs/write failed, falling back to original handler")
        if is_async_orig:
            return await original_handler(**kwargs)
        return original_handler(**kwargs)

    wrapper.__name__ = "acp_aware_write_file"
    return wrapper


# ============================================================
# 安装入口（agent.py 在握手后调）
# ============================================================

_installed = False


def install_acp_handler_overrides() -> None:
    """把柳如烟 read_file / write_file 的 handler 包一层 ACP-aware 逻辑。

    幂等：重复调只装一次。ACP server 是单进程子进程，全局注册表改一次即可。
    Backward-compatible：ContextVar 没设（脚本/直接调用）一律走原 handler，柳如烟主桌面端不受影响。
    """
    global _installed
    if _installed:
        return

    from backend.tools.builtin.clean_registry import registry

    targets = {
        "read_file": _make_fs_read_wrapper,
        "write_file": _make_fs_write_wrapper,
    }
    wrapped = []
    for tool_name, factory in targets.items():
        tool = registry.tools.get(tool_name)
        if tool is None:
            logger.warning("install_acp_handler_overrides: tool %s not registered, skipping", tool_name)
            continue
        original = tool["handler"]
        tool["handler"] = factory(original)
        # is_async 必须改成 True，因为 wrapper 是 async
        tool["is_async"] = True
        wrapped.append(tool_name)

    _installed = True
    logger.info("ACP reverse handlers installed for: %s", wrapped)
