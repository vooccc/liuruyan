"""运行预算 API（/runtime/budget）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
handler 均函数内惰性 import（runtime_budget_config / runtime_budget /
tool_loop_guard），零模块级依赖。

端点：
- GET /runtime/budget  读配置 + effective 三层 fallback 值
- PUT /runtime/budget  整套覆盖 runtime_budget.json
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["runtime-budget"])


@router.get("/runtime/budget")
async def get_runtime_budget():
    """读 runtime_budget.json 用户配置 + 当前 effective 值。

    config 是用户在前端调过的字段（缺则未调过 → env / 默认兜底）；
    effective 是经过 file → env → default 三层 fallback 后实际生效的值。
    """
    from backend.runtime_budget_config import load_runtime_budget_config
    from backend.runtime_budget import AggregateRuntimeBudget, get_effective_max_output_tokens, get_effective_stream_idle_timeout
    cfg = load_runtime_budget_config()
    from backend.tool_loop_guard import ToolLoopGuard
    eff = AggregateRuntimeBudget.from_env("openai")
    eff_guard = ToolLoopGuard.from_env("openai")
    return {
        "config": cfg,
        "effective": {
            "max_tool_calls": eff.max_tool_calls,
            "max_tool_rounds": eff_guard.max_rounds,
            "max_tool_output_chars": eff.max_tool_output_chars,
            "max_wall_clock_seconds": eff.max_wall_clock_seconds,
            "max_output_tokens": get_effective_max_output_tokens("openai"),
            "stream_idle_timeout_ms": int(get_effective_stream_idle_timeout("openai") * 1000),
            "warn_threshold_ratio": eff.warn_threshold_ratio,
        },
    }


@router.put("/runtime/budget")
async def set_runtime_budget(body: dict):
    """整套覆盖 runtime_budget.json。下次 chat 自动生效（_chat_with_tools 入口每次新建 budget 实例）。"""
    from backend.runtime_budget_config import (
        save_runtime_budget_config,
        validate_runtime_budget_payload,
        load_runtime_budget_config,
    )
    from backend.runtime_budget import AggregateRuntimeBudget, get_effective_max_output_tokens, get_effective_stream_idle_timeout
    from backend.tool_loop_guard import ToolLoopGuard
    try:
        validated = validate_runtime_budget_payload(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    save_runtime_budget_config(validated)
    eff = AggregateRuntimeBudget.from_env("openai")
    eff_guard = ToolLoopGuard.from_env("openai")
    return {
        "status": "success",
        "config": load_runtime_budget_config(),
        "effective": {
            "max_tool_calls": eff.max_tool_calls,
            "max_tool_rounds": eff_guard.max_rounds,
            "max_tool_output_chars": eff.max_tool_output_chars,
            "max_wall_clock_seconds": eff.max_wall_clock_seconds,
            "max_output_tokens": get_effective_max_output_tokens("openai"),
            "stream_idle_timeout_ms": int(get_effective_stream_idle_timeout("openai") * 1000),
            "warn_threshold_ratio": eff.warn_threshold_ratio,
        },
    }
