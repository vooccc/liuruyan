"""
tool_self_reflect 工具

让角色看自己的工具调用画像，发现自己的"坏习惯"。

为啥放在工具层而不是后台脚本：
- 角色可主动调用、按需反思（卡住循环时 / 大任务前自检）
- 输出是结构化建议，可以直接读进 LLM context 影响下一步决策
- 配合 per_agent breakdown，结果只关心自己（不被其他角色噪音干扰）
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 单次反思最多带回几条 issue（避免 prompt 爆）
_MAX_ISSUES_RETURNED = 6


def tool_self_reflect_tool(
    agent_id: Optional[str] = None,
    include_global: bool = False,
) -> str:
    """看自己的工具调用画像。

    Args:
        agent_id: 由 context 自动注入；None 时只能回退到全局画像
        include_global: True 时同时返回全局画像（用于对比"我和别人的差距"）

    Returns:
        JSON {"agent_id":..., "summary":..., "issues":[...], "top_tools":[...], "top_errors":[...]}
        没数据则 {"agent_id":..., "no_data": True, "message": "..."}
    """
    try:
        from backend.tool_stats import get_stats_collector
        from backend.tool_stats_insights import compute_insights, compute_per_agent_breakdown
    except Exception as e:
        return json.dumps({"error": f"insights module unavailable: {e}"})

    collector = get_stats_collector()
    all_records = list(collector.records)

    if not all_records:
        return json.dumps({
            "agent_id": agent_id,
            "no_data": True,
            "message": "No tool calls recorded yet. Call some tools, then try again.",
        })

    # 过滤本 agent 的记录
    if agent_id:
        my_records = [r for r in all_records if r.agent_id == agent_id]
    else:
        my_records = all_records  # 没 agent_id 时退化到全局

    if not my_records:
        return json.dumps({
            "agent_id": agent_id,
            "no_data": True,
            "message": (
                f"No records for agent_id={agent_id!r}. "
                "Either you haven't called tools yet this session, or stats predate the agent_id field."
            ),
        })

    # 跑 insights 仅作用于自己的记录
    try:
        my_insights = compute_insights(records=my_records, include_per_agent=False)
    except Exception as e:
        logger.exception("compute_insights failed in self_reflect")
        return json.dumps({"error": str(e)})

    # 截取最严重的 N 条 issue + 各列出 top 用具/错误
    issues = my_insights.get("issues", [])[:_MAX_ISSUES_RETURNED]

    breakdown = compute_per_agent_breakdown(my_records)
    my_breakdown = breakdown.get(agent_id or "_unknown") or next(iter(breakdown.values()), {})

    out: Dict[str, Any] = {
        "agent_id": agent_id,
        "summary": my_insights.get("summary", {}),
        "issues": issues,
        "top_tools": my_breakdown.get("top_tools", []),
        "top_errors": my_breakdown.get("top_errors", []),
        "self_advice": _generate_self_advice(my_insights, my_breakdown),
    }

    if include_global:
        try:
            global_insights = compute_insights(records=all_records, include_per_agent=False)
            out["global_summary"] = global_insights.get("summary", {})
        except Exception:
            pass

    return json.dumps(out, ensure_ascii=False)


def _generate_self_advice(insights: Dict[str, Any], breakdown: Dict[str, Any]) -> List[str]:
    """从 insights 提炼成 1-3 句"我应该怎么改"的自我建议（短、可执行）"""
    advice: List[str] = []
    issues = insights.get("issues", [])
    if not issues:
        advice.append("No major issues detected — keep current habits.")
        return advice

    sev_counts: Dict[str, int] = {}
    cat_counts: Dict[str, int] = {}
    for it in issues:
        sev_counts[it["severity"]] = sev_counts.get(it["severity"], 0) + 1
        cat_counts[it["category"]] = cat_counts.get(it["category"], 0) + 1

    if cat_counts.get("streak_fail", 0) > 0:
        advice.append(
            "I have streak failures: when a tool call fails, "
            "STOP and read the error carefully before retrying with a different approach."
        )
    if cat_counts.get("low_success", 0) > 0:
        # 找出成功率最低的工具
        worst_tool: Optional[str] = None
        worst_rate = 1.0
        for it in issues:
            if it["category"] == "low_success":
                m = it.get("metric") or {}
                rate = m.get("success_rate", 1.0)
                if rate < worst_rate:
                    worst_rate = rate
                    worst_tool = it.get("tool")
        if worst_tool:
            advice.append(
                f"I struggle with `{worst_tool}` ({worst_rate*100:.0f}% success). "
                "Re-read its description carefully; check arg types match the schema."
            )
    if cat_counts.get("json_repair", 0) > 0:
        advice.append(
            "JSON repair triggers a lot — check if my tool args contain unescaped "
            "newlines / quotes / control characters. Use proper JSON encoding."
        )
    if cat_counts.get("slow", 0) > 0:
        advice.append(
            "Some tools are slow on me: avoid using them in interactive contexts "
            "where the user is waiting; prefer the lighter alternative if exists."
        )
    if cat_counts.get("regression", 0) > 0:
        advice.append(
            "Recent regression detected — my tool habits got worse compared to before. "
            "Recall what changed in approach and revert what's not working."
        )

    return advice or ["Issues found but no specific advice template matched. Read the issues list manually."]


# ============================================================================
# handler 适配
# ============================================================================

def _handle_tool_self_reflect(**kwargs):
    # context 注入的 agent_id 是真实的；其他 kwargs 是工具参数
    agent_id = kwargs.get("agent_id")
    include_global = bool(kwargs.get("include_global", False))
    return tool_self_reflect_tool(agent_id=agent_id, include_global=include_global)
