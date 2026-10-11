"""liuruyan.tracing — 链路追踪与落盘。

TraceJournal: 订阅 EventBus 把 AgentEvent 落盘到 per-trace JSONL
new_trace_id / child_trace_id: 嵌套式 trace_id 生成

详见 docs/architecture_v2/02_runtime_core.md § 五
"""

from .journal import TraceJournal, child_trace_id, new_trace_id, top_trace_id

__all__ = ["TraceJournal", "child_trace_id", "new_trace_id", "top_trace_id"]

