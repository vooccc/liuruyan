"""v2 · C-1 · AgentEvent + EVENT_TYPES

EventBus 流转的事件契约。

设计：docs/architecture_v2/02_runtime_core.md § 四
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

EVENT_SCHEMA_VERSION = 1

# 受控字符串集 — 加新事件类型必须改这里 + 改文档（02_runtime_core.md § 4.1）
EVENT_TYPES: frozenset[str] = frozenset({
    # LLM 流
    "llm_chunk", "llm_done", "llm_error", "llm_error_observed",
    # 工具
    "tool_start", "tool_progress", "tool_end", "tool_error",
    # subagent
    "subagent_started", "subagent_progress", "subagent_done",
    "subagent_failed", "subagent_timeout", "subagent_cancelled",
    # 系统
    "memory_refine_requested", "memory_updated",
    "title_gen_requested", "title_generated", "session_archived",
    "summarize_threshold_reached", "summarize_plan_ready",
    "token_usage_recorded",
    # IM
    "im_inbound", "im_outbound_sent",
    # Cron — 跨 session 全局通知（toast 用）；payload schema 见
    # docs/cron_toast_design.md § 4.1：{job_id, description, cron, mode,
    # fire_count, next_fire_at, agent_id, parent_session_id}
    "cron_fired",
    # Cron 列表变更（add/remove/set_enabled）— 让前端面板免轮询实时刷新；
    # payload {action, job_id, agent_id}，UI 收到后自己 refetch
    "cron_jobs_changed",
    # Tasks 列表变更（task_create/update/delete/clear）— 前端 InlineTasksPanel
    # 订阅做免轮询实时刷新；payload {action, task_id?, session_id, agent_id}
    "tasks_changed",
    # 错误
    "error",
})


@dataclass(slots=True)
class AgentEvent:
    """Agent 主路径产生的事件，由 EventBus fan-out 给多消费者。

    `event_id` 由 publisher 生成（uuid4），用于幂等去重；
    `trace_id` 由调用层从 RunContext 拿；
    `payload` 自由结构，建议每种 type 有自己的 schema 约定（见 02_runtime_core.md）。
    """

    type: str
    trace_id: str
    session_id: str
    agent_id: str
    user_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = EVENT_SCHEMA_VERSION
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(
                f"AgentEvent.type must be in EVENT_TYPES, got {self.type!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """JSONL 落盘 / SSE 序列化用。"""
        return {
            "event_id": self.event_id,
            "schema_version": self.schema_version,
            "type": self.type,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "payload": self.payload,
            "ts": self.ts,
        }


__all__ = ["AgentEvent", "EVENT_TYPES", "EVENT_SCHEMA_VERSION"]
