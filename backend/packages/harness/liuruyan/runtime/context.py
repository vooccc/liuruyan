"""ContextVar 全集 — per-task 上下文传播。

设计原则（docs/architecture_v2/02_runtime_core.md § 1）：

1. 任何"可变状态"先归类，分类决定怎么存。
2. **跨 await 自动传播**（asyncio 原生），**跨 thread 边界必须显式捕获**。
3. 禁止 `self.user_id` / `self.session_id` 等实例变量；只能从这里读。

跨 thread 边界捕获模板：

    captured = get_effective_user_id()
    threading.Timer(30, lambda: do_work(user_id=captured)).start()

绝不要写：

    threading.Timer(30, lambda: do_work(user_id=get_effective_user_id())).start()

contextvar 在新线程拿到的是 default，不是当前作用域。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterator, Optional

if TYPE_CHECKING:
    from ..subagents import CancelEvent

# 类型别名 — pre/post hook 回调签名（v1 dispatch_batch 注入点契约）
# 设计成纯 dict / 字符串接口，跟 harness 的 v2 ToolCall/ToolResult 解耦——
# 让 backend.tools 不需要 import harness 的 dataclass，只 import 这两个 getter。
PreToolHook = Callable[[str, dict, str], Awaitable[Optional[dict]]]
PostToolHook = Callable[[str, dict, str, Any, bool], Awaitable[Optional[dict]]]

# === 五元组：所有 LLM 调用 / 工具 / 日志 / metrics 都带这五个 ===
_user_id: ContextVar[str] = ContextVar("user_id", default="default")
_agent_id: ContextVar[str] = ContextVar("agent_id", default="liuruyan")
_session_id: ContextVar[str | None] = ContextVar("session_id", default=None)
_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)

# === 业务侧 ===
_channel: ContextVar[str | None] = ContextVar("channel", default=None)
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# === v1 dispatch_batch 注入点 ===
# v2 chain（LeadAgent）想在 v1 工具执行前后插钩子时，用 with_tool_hooks 把
# 回调写到这两个 ContextVar；v1 backend.tools.builtin.clean_registry.dispatch_batch
# 内部 get 出来执行。不传时 ContextVar 默认 None，dispatch_batch 行为完全不变。
#
# Pre hook 返回值约定：
#   None                       → 放行（按原 args 执行工具）
#   {"short_circuit": str|dict}→ 跳过工具执行，用该值作为 result（mw 11 GuardrailChain 短路用）
#   {"replaced_args": dict}    → 用新 args 执行（mw 11/12 改写参数用，比如虚拟路径替换）
#
# Post hook 返回值约定：
#   None                       → result 不变
#   {"replaced_result": str|dict} → 替换 result（mw 13 ToolErrorRecovery 归一化用）
#
# 任何 hook 抛异常 → swallow + log，不影响工具执行（dispatch_batch 内部兜底）。
_pre_tool_hook: ContextVar[Optional[PreToolHook]] = ContextVar(
    "pre_tool_hook", default=None
)
_post_tool_hook: ContextVar[Optional[PostToolHook]] = ContextVar(
    "post_tool_hook", default=None
)

# === F-2c: 顶层 cancel_event ===
# LeadAgent 在每次 run 入口 set；subagent / worker runtime 拿到后调
# event.child(trace_id=...) 派子 event 透给 v1 scheduler.submit(cancel_event=...)。
# 父 event cancel 时所有派出去的子 worker 自动级联取消（CancelEvent.child 的
# 自动级联能力）。
# 不传 / chain shim 未启用 → 工具拿 None 走老路径（行为不变）。
_cancel_event: ContextVar[Optional["CancelEvent"]] = ContextVar(
    "cancel_event", default=None
)


# ── getter（永不 None 的两个）──────────────────────────────────────────


def get_effective_user_id() -> str:
    """user_id 永不返 None；无认证模式默认 'default'。"""
    return _user_id.get()


def get_effective_agent_id() -> str:
    """agent_id 永不返 None；用户没切角色时默认 'liuruyan'。"""
    return _agent_id.get()


# ── getter（可能 None 的五个）─────────────────────────────────────────


def get_session_id() -> str | None:
    return _session_id.get()


def get_run_id() -> str | None:
    return _run_id.get()


def get_trace_id() -> str | None:
    return _trace_id.get()


def get_channel() -> str | None:
    return _channel.get()


def get_request_id() -> str | None:
    return _request_id.get()


def get_pre_tool_hook() -> Optional[PreToolHook]:
    """v1 dispatch_batch 用：拿当前 run 的 pre_tool_hook，没设返 None。"""
    return _pre_tool_hook.get()


def get_post_tool_hook() -> Optional[PostToolHook]:
    """v1 dispatch_batch 用：拿当前 run 的 post_tool_hook，没设返 None。"""
    return _post_tool_hook.get()


def get_cancel_event() -> Optional["CancelEvent"]:
    """F-2c 用：拿当前 run 的顶层 CancelEvent，未设返 None。

    subagent / worker runtime 调它判断"是否在 v2 chain 内"——
    返 None 时走老路径（v1 scheduler.submit 不传 cancel_event）；返 event 时
    调 ``event.child(trace_id=worker.task_id)`` 派子 event 透给 submit。
    """
    return _cancel_event.get()


# ── setter ──────────────────────────────────────────────────────────────


@contextmanager
def with_context(
    *,
    user_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    channel: str | None = None,
    request_id: str | None = None,
) -> Iterator[None]:
    """临时设置 context，作用域结束自动还原。

    传 None 表示"不改这个字段"；要主动把字段设回 None，传一个特殊空串再处理。
    （目前没有"改回 None"的场景，需要时单独加 API。）

    嵌套安全：内层 with 不影响外层。
    """
    tokens: list[Token] = []
    if user_id is not None:
        tokens.append(_user_id.set(user_id))
    if agent_id is not None:
        tokens.append(_agent_id.set(agent_id))
    if session_id is not None:
        tokens.append(_session_id.set(session_id))
    if run_id is not None:
        tokens.append(_run_id.set(run_id))
    if trace_id is not None:
        tokens.append(_trace_id.set(trace_id))
    if channel is not None:
        tokens.append(_channel.set(channel))
    if request_id is not None:
        tokens.append(_request_id.set(request_id))
    try:
        yield
    finally:
        # 反序还原（LIFO）
        for tok in reversed(tokens):
            tok.var.reset(tok)


def snapshot() -> dict[str, str | None]:
    """快照当前所有 contextvar 值。

    跨 thread 边界使用时，先调这个捕获，然后传给新线程的入口函数。
    """
    return {
        "user_id": _user_id.get(),
        "agent_id": _agent_id.get(),
        "session_id": _session_id.get(),
        "run_id": _run_id.get(),
        "trace_id": _trace_id.get(),
        "channel": _channel.get(),
        "request_id": _request_id.get(),
    }


@contextmanager
def restore(snap: dict[str, str | None]) -> Iterator[None]:
    """从快照还原 context。新线程入口处用。"""
    with with_context(**{k: v for k, v in snap.items() if v is not None}):
        yield


@contextmanager
def with_cancel_event(event: Optional["CancelEvent"]) -> Iterator[None]:
    """F-2c：临时把顶层 CancelEvent set 到 ContextVar，作用域结束自动还原。

    用法（LeadAgent run_streaming/run_collected 入口）：

        cancel_event = CancelEvent(trace_id=ctx.trace_id)
        with with_cancel_event(cancel_event):
            ...  # 派出去的 worker 自动透传 child event

    传 None 是 no-op（不改 ContextVar）。嵌套安全：内层 with 不影响外层。
    """
    if event is None:
        yield
        return
    token = _cancel_event.set(event)
    try:
        yield
    finally:
        _cancel_event.reset(token)


@contextmanager
def with_tool_hooks(
    *,
    pre: PreToolHook | None = None,
    post: PostToolHook | None = None,
) -> Iterator[None]:
    """临时把 v1 dispatch_batch 的 pre/post hook set 到 ContextVar，作用域结束自动还原。

    用法（LeadAgent 调 v1 chat 前）：

        with with_tool_hooks(pre=my_pre_hook, post=my_post_hook):
            async for event in agent.llm_client.chat(messages, ...):
                yield event

    单独传 pre 或 post 也合法（另一个保持 None）。嵌套安全：
    内层 with 不影响外层 — LIFO reset。
    """
    tokens: list[Token] = []
    if pre is not None:
        tokens.append(_pre_tool_hook.set(pre))
    if post is not None:
        tokens.append(_post_tool_hook.set(post))
    try:
        yield
    finally:
        for tok in reversed(tokens):
            tok.var.reset(tok)


__all__ = [
    "PreToolHook",
    "PostToolHook",
    "get_effective_user_id",
    "get_effective_agent_id",
    "get_session_id",
    "get_run_id",
    "get_trace_id",
    "get_channel",
    "get_request_id",
    "get_pre_tool_hook",
    "get_post_tool_hook",
    "get_cancel_event",
    "with_context",
    "with_tool_hooks",
    "with_cancel_event",
    "snapshot",
    "restore",
]
