"""`delegate_acp_agent` 工具：让柳如烟 LLM 调外部 ACP agent（如 Claude Code）干活。

注册方式：本模块导入时执行 register()。但**不强制 import**——只有显式调
`from backend.acp.tools import register_delegate_acp_agent_tool; register_delegate_acp_agent_tool()`
才生效，避免污染默认 tool 集（柳如烟主桌面端不需要这个工具）。

启用方式：
1. 主桌面端启用 → 在 backend 启动时显式调 `register_delegate_acp_agent_tool()`
2. ACP server 模式 → 不启用（柳如烟在 Zed 里时不需要再调外部 agent）
3. CLI 测试 → 显式调

LLM 使用建议（写进 description）：
- 仅在用户明确要求"用 Claude Code"或想横向对比时调
- 注意外部 agent 跑可能慢（>60s），有 token / API 成本
- 不要拿来做日常任务，柳如烟自己能干就自己干
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from ..client.service import EXTERNAL_AGENTS, run_external_agent_turn

logger = logging.getLogger(__name__)


TOOL_NAME = "delegate_acp_agent"


# ────────────────────────────────────────────────────────────────────
# 取消机制：delegate_id → 正在运行的 Task
# ────────────────────────────────────────────────────────────────────
# 路由 POST /acp/delegate/{delegate_id}/cancel 调 cancel_delegate(did) → task.cancel()
# tool handler 自己捕获 CancelledError，emit end{cancelled=True}，返回失败 JSON。
# 这样：① LLM 看到的 tool_result 是结构化失败而不是异常抛穿；② 前端卡片照常落 end 状态。
_ACTIVE_DELEGATES: Dict[str, asyncio.Task] = {}


def cancel_delegate(delegate_id: str) -> bool:
    """取消正在跑的 delegate。返回 True 表示找到并发了 cancel，False 表示 id 不在册。

    幂等：重复调对同一个 did 第二次直接返回 False（task 已 pop）。
    """
    task = _ACTIVE_DELEGATES.get(delegate_id)
    if task is None or task.done():
        return False
    task.cancel()
    logger.info("acp delegate cancel requested: delegate_id=%s", delegate_id)
    return True


def list_active_delegates() -> list[str]:
    """调试用：当前在跑的所有 delegate_id。"""
    return [did for did, t in _ACTIVE_DELEGATES.items() if not t.done()]


def _resolve_session_id(injected: Optional[str]) -> Optional[str]:
    """三级 fallback 取 session_id（同 execute_command）：dispatch context kwarg →
    LLMClient ContextVar → harness ContextVar。任一有就够。
    """
    if injected:
        return injected
    try:
        from backend.llm_client import _current_session_id_var
        sid = _current_session_id_var.get()
        if sid:
            return sid
    except Exception:
        pass
    try:
        from backend.packages.harness.liuruyan.runtime.context import get_session_id
        return get_session_id()
    except Exception:
        return None


def _make_chunk_emitter(session_id: str, delegate_id: str, agent: str):
    """返一个 async (kind: str, payload: dict) -> None 的 callback，
    每次调把 payload 推到当前 session 的 SSE 流里。失败 swallow，绝不阻塞 ACP。
    """
    try:
        from backend.sse_manager import get_sse_manager
    except Exception:
        return None
    sse_manager = get_sse_manager()

    async def _emit(kind: str, payload: dict) -> None:
        try:
            await sse_manager.send_event(session_id, {
                "type": "system_notice",
                "kind": "acp_delegate_chunk",
                "content": "",  # 必填壳；真正数据在 payload
                "payload": {
                    "delegate_id": delegate_id,
                    "agent": agent,
                    "chunk_kind": kind,  # "start" | "text" | "thought" | "tool_call" | "tool_done" | "end"
                    **payload,
                },
            })
        except Exception:
            pass  # SSE 出错绝不阻塞外部 agent 收流

    return _emit


def _build_description() -> str:
    available = list(EXTERNAL_AGENTS.keys())
    return (
        "Delegate a task to an external ACP-compatible agent (e.g. Claude Code, Gemini CLI) "
        "and return their output.\n\n"
        f"Available agents: {available}\n\n"
        "WHEN TO USE:\n"
        "- User explicitly says 'use Claude Code' / 'ask gemini' / 'let external agent do this'\n"
        "- You want to A/B compare your answer against a reference implementation\n"
        "- The task is highly specialized and the external agent is known to be better at it\n\n"
        "WHEN NOT TO USE:\n"
        "- Routine tasks—you (柳如烟) handle those directly, it's faster and cheaper\n"
        "- You're already in the middle of executing a tool plan—finish that first\n\n"
        "COST/LATENCY WARNING:\n"
        "- External agent spawns a subprocess on every call (no connection reuse in MVP)\n"
        "- Typical turn takes 30-120s; may consume the external agent's API quota\n"
        "- Set `cwd` to the project root or relevant subdir; default is the current working dir\n\n"
        "RESULT FORMAT:\n"
        "Returns JSON with: success, agent, output (markdown summary of what external agent did/said), "
        "raw_update_count, delegate_id, optional error.\n"
    )


def _build_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "enum": list(EXTERNAL_AGENTS.keys()),
                "description": "Name of the external ACP agent to delegate to.",
            },
            "prompt": {
                "type": "string",
                "description": "The full task prompt to send. Be concrete; the external agent has no context about this conversation.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for the external agent (absolute path). Defaults to current process cwd.",
            },
            "timeout_sec": {
                "type": "integer",
                "description": (
                    "Hard timeout for the whole turn in seconds. Default 600. "
                    "Bump to 900~1800 when delegating tasks that read large files (>100KB) "
                    "or require many tool rounds—external agent first-token latency on large "
                    "context can easily exceed 60~120s on relayed Anthropic endpoints."
                ),
                "default": 600,
            },
        },
        "required": ["agent", "prompt"],
    }


async def _handle_delegate_acp_agent(**kwargs: Any) -> str:
    """工具 handler。返 JSON 字符串（dispatch 层会把它当 tool_result）。

    Phase 5.5：取当前柳如烟 session_id，构造 SSE chunk emitter，让外部 agent
    一边干活一边把 text/thought/tool_call 推到前端流式卡片。
    """
    # context 注入键单独取（session_id 走 SSE 推送），其它丢
    injected_sid = kwargs.get("session_id")
    _context_keys = {"agent_id", "session_id", "llm_client", "trace_id"}
    args = {k: v for k, v in kwargs.items() if k not in _context_keys}

    agent = args.get("agent")
    prompt = args.get("prompt")
    cwd = args.get("cwd")
    timeout_sec = args.get("timeout_sec", 600)

    if not agent or not prompt:
        return json.dumps({"error": "Missing required parameter: agent or prompt"})

    try:
        timeout_f = float(timeout_sec)
    except Exception:
        timeout_f = 600.0

    # SSE emitter（session_id 找不到就降级——LLM 还能拿结果，只是没流式卡片）
    sse_session_id = _resolve_session_id(injected_sid)
    delegate_id = f"acp_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    emitter = _make_chunk_emitter(sse_session_id, delegate_id, str(agent)) if sse_session_id else None

    if emitter:
        await emitter("start", {
            "prompt_preview": str(prompt)[:200],
            "cwd": str(cwd) if cwd else None,
            "timeout_sec": timeout_f,
            "delegate_id": delegate_id,  # 前端 cancel 按钮要这个 did 才能 POST
        })

    # 把 run_external_agent_turn 包成 Task 注册到 _ACTIVE_DELEGATES，
    # 路由层 cancel_delegate(did) 就能 task.cancel() 把整条链路掐掉。
    turn_task: asyncio.Task = asyncio.ensure_future(run_external_agent_turn(
        agent=str(agent),
        prompt=str(prompt),
        cwd=str(cwd) if cwd else None,
        timeout_sec=timeout_f,
        on_chunk=emitter,
    ))
    _ACTIVE_DELEGATES[delegate_id] = turn_task
    cancelled = False
    try:
        result = await turn_task
    except asyncio.CancelledError:
        cancelled = True
        result = {
            "success": False,
            "agent": str(agent),
            "error": "Cancelled by user",
            "output": "(已被用户取消)",
            "raw_update_count": 0,
            "cancelled": True,
        }
    finally:
        # 用 pop(..., None) 防止 task 没注册成功时 KeyError；幂等
        _ACTIVE_DELEGATES.pop(delegate_id, None)

    if emitter:
        await emitter("end", {
            "success": bool(result.get("success")),
            "raw_update_count": result.get("raw_update_count", 0),
            "error": result.get("error") or "",
            "cancelled": cancelled,
        })

    # 把 delegate_id 写进结果，方便 LLM 引用前面卡片
    result["delegate_id"] = delegate_id
    return json.dumps(result, ensure_ascii=False)


_registered = False


def register() -> None:
    """注册到 clean_registry。幂等。"""
    global _registered
    if _registered:
        return
    from backend.tools.builtin.clean_registry import registry

    registry.register(
        name=TOOL_NAME,
        description=_build_description(),
        input_schema=_build_schema(),
        handler=_handle_delegate_acp_agent,
        is_async=True,
        concurrency_class="exclusive",  # 外部 agent 长阻塞 + 跟外部强交互
    )
    _registered = True
    logger.info("Registered tool: %s (delegates to external ACP agent)", TOOL_NAME)
