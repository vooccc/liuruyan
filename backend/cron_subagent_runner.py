"""Cron subagent runner — execute scheduled prompts through the subagent runtime."""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .cron_models import CronJob

logger = logging.getLogger(__name__)


@dataclass
class CronSubagentRunResult:
    ok: bool
    output: str
    status: str
    subagent_id: str = ""
    agent_type: str = ""
    elapsed: float = 0.0
    tokens_used: int = 0
    tools_called: int = 0
    error: str | None = None


_CRON_SUBAGENT_SYSTEM_PROMPT = """你是柳如烟的后台定时任务 subagent。\n\n运行规则：\n- 你在后台执行，不要要求用户实时确认。\n- 直接完成用户预设的 prompt，并输出可以写回聊天气泡的最终结果。\n- 如果任务只是提醒、总结、写作或日程类，不要调用工具。\n- 如果任务明确要求检查项目/文件/代码，只能使用只读工具。\n- 不要创建、修改、删除文件，不要启动长期进程，不要调用会改变外部状态的工具。\n- 最终输出使用中文，简洁说明完成结果、关键发现和后续建议。\n"""

_CRON_ALLOWED_TOOLS = [
    "glob_search",
    "read_file",
    "grep",
    "code_intelligence",
]


def _build_task(job: CronJob, fire_ts: float) -> str:
    fired_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fire_ts))
    return (
        f"这是一个后台定时任务，请独立完成并输出可直接给用户阅读的结果。\n\n"
        f"任务说明：{job.description}\n"
        f"触发时间：{fired_at}\n"
        f"Cron 表达式：{job.cron}\n"
        f"触发次数：{job.fire_count + 1}\n\n"
        f"任务内容：\n{job.prompt}\n\n"
        f"要求：\n"
        f"1. 不要要求用户实时确认；这是定时后台执行。\n"
        f"2. 如果需要读取项目上下文，只使用允许的只读工具。\n"
        f"3. 最终输出用中文，总结完成结果、关键发现和后续建议。"
    )


def _format_result_message(job: CronJob, result: CronSubagentRunResult, fire_ts: float) -> str:
    fired_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fire_ts))
    title = f"## 定时任务完成：{job.description}"
    meta = (
        f"- 触发时间：{fired_at}\n"
        f"- Cron：`{job.cron}`\n"
        f"- 执行方式：cron-subagent `{result.subagent_id or 'unknown'}`\n"
        f"- 耗时：{result.elapsed:.1f}s\n"
    )
    body = (result.output or "").strip()
    if not body:
        body = "本次定时任务没有产生可展示结果。"
    return f"{title}\n\n{meta}\n---\n\n{body}"


def _format_failure_message(job: CronJob, result: CronSubagentRunResult, fire_ts: float) -> str:
    fired_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fire_ts))
    partial = (result.output or "").strip()
    partial_block = f"\n\n已产生的部分输出：\n\n{partial}" if partial else ""
    return (
        f"## 定时任务失败：{job.description}\n\n"
        f"- 触发时间：{fired_at}\n"
        f"- Cron：`{job.cron}`\n"
        f"- 执行方式：cron-subagent `{result.subagent_id or 'unknown'}`\n"
        f"- 状态：{result.status}\n"
        f"- 错误：{result.error or 'unknown error'}"
        f"{partial_block}"
    )


async def _append_result_to_session(
    *,
    session_id: str,
    agent_id: str,
    content: str,
    client_id: str,
) -> None:
    from .session import get_session_manager
    from .sse_manager import get_sse_manager

    session_mgr = get_session_manager()
    await session_mgr.append_assistant_structured(
        session_id,
        segments=[{"type": "text", "text": content}],
        text=content,
        client_id=client_id,
        agent_id=agent_id,
    )
    try:
        await get_sse_manager().send_event(
            session_id,
            {
                "type": "message_added",
                "role": "assistant",
                "client_id": client_id,
                "agent_id": agent_id,
                "content": content,
                "source": "cron_subagent",
            },
        )
    except Exception as exc:
        logger.warning("[CronSubagentRunner] message_added SSE failed: %s", exc)


async def run_cron_subagent(
    *,
    job: CronJob,
    session_id: str,
    fire_ts: float,
) -> CronSubagentRunResult:
    from .llm_client import get_llm_client
    from .subagent.engine import SubAgentEngine, SubAgentStatus

    task = _build_task(job, fire_ts)
    route_id = f"cron:{job.job_id}:{int(fire_ts)}"
    logger.info(
        "[CronSubagentRunner] start job=%s agent=%s session=%s route=%s",
        job.job_id,
        job.agent_id,
        session_id,
        route_id,
    )

    try:
        engine = SubAgentEngine(session_id, job.agent_id, get_llm_client())
        engine.templates["cron"] = {
            "name": "Cron Subagent",
            "icon": "clock",
            "system_prompt": _CRON_SUBAGENT_SYSTEM_PROMPT,
            "tools": list(_CRON_ALLOWED_TOOLS),
            "disallowed_tools": [],
            "timeout": 600,
            "max_tokens": 4000,
            "max_tool_rounds": 8,
            "idle_timeout": 90,
            "model": "inherit",
            "thinking_config": "disabled",
        }
        results = await engine.spawn_parallel(["cron"], task)
        first = results[0] if results else None
        if first is None:
            result = CronSubagentRunResult(
                ok=False,
                output="",
                status="failed",
                agent_type="cron",
                error="subagent returned no result",
            )
        else:
            status = getattr(first.status, "value", str(first.status))
            ok = first.status == SubAgentStatus.COMPLETED and bool((first.output or "").strip())
            result = CronSubagentRunResult(
                ok=ok,
                output=first.output or "",
                status=status,
                subagent_id=first.agent_id,
                agent_type=first.agent_type,
                elapsed=float(first.elapsed or 0.0),
                tokens_used=int(first.tokens_used or 0),
                tools_called=int(first.tools_called or 0),
                error=first.error if not ok else None,
            )
            if not result.ok and not result.error:
                result.error = "cron subagent produced empty output"
    except Exception as exc:
        result = CronSubagentRunResult(
            ok=False,
            output="",
            status="failed",
            agent_type="cron",
            error=f"{type(exc).__name__}: {exc}",
        )

    message = (
        _format_result_message(job, result, fire_ts)
        if result.ok
        else _format_failure_message(job, result, fire_ts)
    )
    await _append_result_to_session(
        session_id=session_id,
        agent_id=job.agent_id,
        content=message,
        client_id=f"cron-{job.job_id}-{uuid.uuid4().hex[:8]}",
    )
    logger.info(
        "[CronSubagentRunner] done job=%s ok=%s status=%s elapsed=%.2fs tools=%d",
        job.job_id,
        result.ok,
        result.status,
        result.elapsed,
        result.tools_called,
    )
    return result


async def publish_cron_run_event(
    *,
    event_type: str,
    job: CronJob,
    session_id: str,
    route_id: str,
    result: CronSubagentRunResult | None = None,
    fire_ts: float | None = None,
) -> None:
    try:
        from liuruyan.buses import AgentEvent, get_event_bus
        bus = get_event_bus()
        if bus is None:
            return
        payload: dict[str, Any] = {
            "job_id": job.job_id,
            "description": job.description,
            "cron": job.cron,
            "mode": job.mode,
            "fire_count": job.fire_count,
            "route_id": route_id,
        }
        if fire_ts is not None:
            payload["fire_ts"] = fire_ts
        if result is not None:
            payload.update({
                "ok": result.ok,
                "status": result.status,
                "subagent_id": result.subagent_id,
                "subagent_type": result.agent_type,
                "elapsed": result.elapsed,
                "tools_called": result.tools_called,
                "error": result.error,
            })
        event = AgentEvent(
            type=event_type,
            trace_id=f"cron-run-{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            agent_id=job.agent_id,
            user_id="local",
            payload=payload,
        )
        await bus.publish(event)
    except Exception as exc:
        logger.warning("[CronSubagentRunner] %s publish failed: %s", event_type, exc)
