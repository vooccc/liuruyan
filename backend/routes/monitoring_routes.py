"""监控 / 诊断 API（工具统计 · prompt cache · debug flags · 系统自检）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
所有 handler 均在函数体内惰性 import，不依赖 main.py 模块级全局，
故无循环 import 风险。

端点：
- /tools/stats（GET 查 / DELETE 清空 / DELETE {tool_name} 单删）
- /tools/registered_names（GET registry 实际注册名）
- /tools/stats/insights（GET 深度洞察）
- /stats/prompt_cache（GET 命中率）
- /debug/flags（GET / PUT 调试通道开关）
- /system/health（GET 系统自检）
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["monitoring"])


@router.get("/tools/stats")
async def get_tool_stats(tool_name: Optional[str] = None, last_n: Optional[int] = None):
    """获取工具执行统计

    参数:
    - tool_name: 可选，只查看指定工具的统计
    - last_n: 可选，只统计最近 N 次调用
    """
    from backend.tool_stats import get_stats_collector
    stats = get_stats_collector()
    summary = stats.get_summary(tool_name=tool_name, last_n=last_n)
    return summary


@router.delete("/tools/stats")
async def clear_tool_stats():
    """清空工具统计历史"""
    from backend.tool_stats import get_stats_collector
    stats = get_stats_collector()
    stats.clear_history()
    return {"message": "统计历史已清空"}


@router.delete("/tools/stats/{tool_name}")
async def clear_tool_stats_one(tool_name: str):
    """删除某个工具的所有调用记录（内存 + tool_stats.jsonl）。

    用例：
    - 工具改名后老名留下 0% 成功率的"幽灵行"，单条删干净
    - 模型一阵子调坏了某工具，想重置该工具的成功率视图

    注意：这只删历史记录，不影响 registry 注册状态。
    """
    if not tool_name or not tool_name.strip():
        raise HTTPException(status_code=400, detail="tool_name required")
    from backend.tool_stats import get_stats_collector
    stats = get_stats_collector()
    removed = stats.clear_tool(tool_name.strip())
    return {"tool_name": tool_name, "removed": removed}


@router.get("/tools/registered_names")
async def list_registered_tool_names():
    """返回当前 registry 里实际注册的所有工具名（含 MCP / IDE）。

    前端监测面板用此判断"统计里出现但 registry 没有"的幽灵工具——给它们打废弃标 +
    醒目的删除入口，避免占位 0%。
    """
    from backend.tools.builtin.clean_registry import registry
    return {"names": sorted(registry.tools.keys())}


@router.get("/stats/prompt_cache")
async def get_prompt_cache_stats(
    last_n: int = 1000,
    include_rows: bool = False,
):
    """Prompt cache 命中率统计。

    参数:
    - last_n: 取最近多少次 LLM 调用（默认 1000，上限保护 OOM）
    - include_rows: true 时返回原始 rows（前端调试用；默认只返 summary）

    Returns:
        {
            "summary": {call_count, hit_ratio, total_cache_read, ...,
                        by_agent_model: {<agent|model>: {...}}},
            "rows": [...]  # 仅 include_rows=true 时
        }
    """
    from backend.prompt_cache_stats import load_recent_rows, compute_summary
    last_n_clamped = max(1, min(int(last_n or 1000), 5000))
    rows = load_recent_rows(limit=last_n_clamped)
    summary = compute_summary(rows)
    resp: Dict[str, Any] = {"summary": summary}
    if include_rows:
        resp["rows"] = rows
    return resp


@router.get("/debug/flags")
async def get_debug_flags():
    """读当前 debug 通道开关状态。"""
    from backend.debug_flags import DEBUG
    return {"flags": DEBUG.state()}


@router.put("/debug/flags")
async def set_debug_flags(body: dict):
    """切换 debug 通道。body 形如 {"dispatch": true, "sse": false}。"""
    from backend.debug_flags import DEBUG, _ALL_CHANNELS
    for key, val in (body or {}).items():
        if key not in _ALL_CHANNELS:
            continue
        if val:
            DEBUG.enable(key)
        else:
            DEBUG.disable(key)
    return {"flags": DEBUG.state()}


@router.get("/system/health")
async def get_system_health():
    """系统自检：AST/工具洞察/审计错误/磁盘/TODO 一条龙报告

    返回 overall_severity ∈ {ok, warning, critical}，前端可据此红绿灯展示。
    """
    from backend.system_health import run_health_check, report_to_dict
    return report_to_dict(run_health_check())


@router.get("/tools/stats/insights")
async def get_tool_stats_insights(per_agent: bool = False):
    """获取工具调用深度洞察（健康分 + 问题清单 + 改进建议）

    比 /tools/stats 高一档：除了原始数字还给出：
    - 每个工具的健康分（综合成功率 + 调用频次）
    - 自动发现的问题（成功率低 / JSON 修复多 / 慢 / 连续失败 / 回归 / 冷门）
    - 每条问题的可执行建议
    - per_agent=true 时附加按角色分桶的画像（top_tools / top_errors）
    """
    from backend.tool_stats_insights import compute_insights
    try:
        from backend.tools import get_tool_registry
        available = list(get_tool_registry().tools.keys())
    except Exception:
        available = None
    return compute_insights(available_tools=available, include_per_agent=per_agent)
