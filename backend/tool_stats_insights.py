"""
tool_stats 深度洞察模块

职责：从 ToolStatsCollector 累积的 records 里挖出"模型行为画像"信息，
比 get_summary() 更高阶——不只统计成功率，还分析趋势、连续失败、
回归、冷门工具等需要跨记录关联的指标。

被 /tools/stats/insights 接口和 scripts/analyze_tool_stats.py CLI 共用。
"""
from __future__ import annotations

import math
from collections import defaultdict, Counter
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from .tool_stats import ToolCallRecord, get_stats_collector


# ── 阈值（保守取值，可后续按使用情况调）──
MIN_CALLS_FOR_HEALTH = 5  # 调用次数太少无统计意义
LOW_SUCCESS_THRESHOLD = 0.80  # 成功率低于此视为"问题工具"
HIGH_REPAIR_THRESHOLD = 0.30  # JSON 修复率超过视为"参数模糊"
SLOW_P95_MS = 5000  # P95 执行时间超此视为慢工具
RECENT_WINDOW = 100  # "最近 N 次"窗口
REGRESSION_DROP_PP = 10.0  # 成功率掉超过 N 个百分点视为回归
STREAK_FAIL_THRESHOLD = 3  # 连续失败 N 次视为"模型卡住"
COLD_TOOL_MAX_CALLS = 3  # 历史调用 ≤ N 视为冷门
COLD_TOOL_LOOKBACK = 500  # 在最近 N 次范围内统计


@dataclass
class HealthRow:
    tool: str
    total_calls: int
    success_rate: float  # 0..1
    failure_count: int
    health_score: float  # 0..100
    avg_time_ms: Optional[float]
    p95_time_ms: Optional[float]


@dataclass
class Issue:
    """一条具体的问题洞察"""
    severity: str  # 'critical' | 'warning' | 'info'
    category: str  # 'low_success' / 'json_repair' / 'slow' / 'streak_fail' / 'regression' / 'cold'
    tool: str
    title: str
    detail: str
    suggestion: str
    metric: Dict[str, Any]


def _percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _compute_health(records: List[ToolCallRecord]) -> List[HealthRow]:
    """每个工具的健康分（综合成功率 + 调用频次）

    health_score = success_rate * 100 * (1 - exp(-total / 20))
      - 成功率 100% 但只调过 1 次 → 分数低（数据不足）
      - 调过 100 次成功率 95% → 接近 95 分
    """
    by_tool: Dict[str, List[ToolCallRecord]] = defaultdict(list)
    for r in records:
        by_tool[r.tool_name].append(r)

    rows: List[HealthRow] = []
    for tool, recs in by_tool.items():
        total = len(recs)
        succ = sum(1 for r in recs if r.success)
        fail = total - succ
        succ_rate = succ / total if total else 0.0
        confidence = 1 - math.exp(-total / 20.0)
        score = succ_rate * 100 * confidence

        times = [r.execution_time_ms for r in recs if r.execution_time_ms is not None]
        avg = sum(times) / len(times) if times else None
        p95 = _percentile(times, 0.95) if times else None

        rows.append(HealthRow(
            tool=tool,
            total_calls=total,
            success_rate=succ_rate,
            failure_count=fail,
            health_score=round(score, 1),
            avg_time_ms=round(avg, 1) if avg is not None else None,
            p95_time_ms=round(p95, 1) if p95 is not None else None,
        ))
    rows.sort(key=lambda r: r.health_score)
    return rows


def _detect_low_success(rows: List[HealthRow]) -> List[Issue]:
    issues = []
    for row in rows:
        if row.total_calls < MIN_CALLS_FOR_HEALTH:
            continue
        if row.success_rate >= LOW_SUCCESS_THRESHOLD:
            continue
        sev = 'critical' if row.success_rate < 0.5 else 'warning'
        issues.append(Issue(
            severity=sev,
            category='low_success',
            tool=row.tool,
            title=f"{row.tool} 成功率偏低 {row.success_rate*100:.1f}%",
            detail=f"共调用 {row.total_calls} 次，失败 {row.failure_count} 次。",
            suggestion=(
                "检查工具描述是否清晰、参数 schema 是否准确、错误信息是否包含定位线索。"
                "如果失败集中在某类参数，考虑加 prompt 提示典型用法。"
            ),
            metric={
                "total_calls": row.total_calls,
                "success_rate": row.success_rate,
                "failure_count": row.failure_count,
            },
        ))
    return issues


def _detect_json_repair_hotspot(records: List[ToolCallRecord]) -> List[Issue]:
    by_tool: Dict[str, List[ToolCallRecord]] = defaultdict(list)
    for r in records:
        by_tool[r.tool_name].append(r)

    issues = []
    for tool, recs in by_tool.items():
        if len(recs) < MIN_CALLS_FOR_HEALTH:
            continue
        repair_count = sum(1 for r in recs if r.json_repair_layers)
        rate = repair_count / len(recs)
        if rate < HIGH_REPAIR_THRESHOLD:
            continue
        layer_counter: Counter = Counter()
        for r in recs:
            for layer in r.json_repair_layers:
                layer_counter[layer] += 1
        top_layers = layer_counter.most_common(3)
        layers_str = ", ".join(f"层{l}({c}次)" for l, c in top_layers)
        issues.append(Issue(
            severity='warning' if rate < 0.5 else 'critical',
            category='json_repair',
            tool=tool,
            title=f"{tool} JSON 修复触发率 {rate*100:.1f}%",
            detail=f"模型生成参数时频繁需要修复。主要修复层：{layers_str}",
            suggestion=(
                "工具的参数 schema 或描述对模型不友好。"
                "建议：1) 检查 description 是否给出清晰的字段含义和典型值"
                "2) 简化嵌套 schema，避免深层 dict/list "
                "3) 如果某个字段总修复，加 example 示范。"
            ),
            metric={"repair_rate": rate, "top_layers": dict(top_layers)},
        ))
    return issues


def _detect_slow_tools(rows: List[HealthRow]) -> List[Issue]:
    issues = []
    for row in rows:
        if row.p95_time_ms is None or row.p95_time_ms < SLOW_P95_MS:
            continue
        issues.append(Issue(
            severity='info' if row.p95_time_ms < 15000 else 'warning',
            category='slow',
            tool=row.tool,
            title=f"{row.tool} P95 执行时间 {row.p95_time_ms/1000:.1f}s",
            detail=f"平均 {row.avg_time_ms/1000:.1f}s，P95 {row.p95_time_ms/1000:.1f}s。",
            suggestion=(
                "考虑：1) 加超时 + 取消机制 2) 长任务拆分为可流式 / 可中断的子步 "
                "3) 在 prompt 里告知模型这是慢工具，避免在交互式场景滥用。"
            ),
            metric={"avg_ms": row.avg_time_ms, "p95_ms": row.p95_time_ms},
        ))
    return issues


def _detect_streak_failures(records: List[ToolCallRecord]) -> List[Issue]:
    """同一工具连续失败 N 次 = 模型卡住信号"""
    issues = []
    by_tool: Dict[str, List[ToolCallRecord]] = defaultdict(list)
    for r in records:
        by_tool[r.tool_name].append(r)

    for tool, recs in by_tool.items():
        recs_sorted = sorted(recs, key=lambda r: r.timestamp)
        streak = 0
        max_streak = 0
        last_streak_end_ts = 0.0
        last_error_types: List[str] = []
        cur_error_types: List[str] = []
        for r in recs_sorted:
            if r.success:
                if streak >= STREAK_FAIL_THRESHOLD and streak > max_streak:
                    max_streak = streak
                    last_streak_end_ts = recs_sorted[recs_sorted.index(r) - 1].timestamp
                    last_error_types = list(cur_error_types)
                streak = 0
                cur_error_types = []
            else:
                streak += 1
                if r.error_type:
                    cur_error_types.append(r.error_type)
        # 末尾未结束的 streak
        if streak >= STREAK_FAIL_THRESHOLD and streak > max_streak:
            max_streak = streak
            last_streak_end_ts = recs_sorted[-1].timestamp
            last_error_types = list(cur_error_types)

        if max_streak >= STREAK_FAIL_THRESHOLD:
            err_summary = ", ".join(Counter(last_error_types).keys()) if last_error_types else "未知"
            issues.append(Issue(
                severity='warning',
                category='streak_fail',
                tool=tool,
                title=f"{tool} 出现连续 {max_streak} 次失败",
                detail=f"主要错误类型：{err_summary}。模型可能陷入了同一种错误的循环。",
                suggestion=(
                    "检查工具的错误信息是否含有"
                    "可执行的修复线索（'参数 X 缺失'比'invalid input'有用）。"
                    "可以在工具实现里加入 retry hint。"
                ),
                metric={"max_consecutive_failures": max_streak, "errors": err_summary},
            ))
    return issues


def _detect_regression(records: List[ToolCallRecord]) -> List[Issue]:
    """对比最近 N 次 vs 之前 N 次，找成功率回归的工具"""
    by_tool: Dict[str, List[ToolCallRecord]] = defaultdict(list)
    for r in records:
        by_tool[r.tool_name].append(r)

    issues = []
    for tool, recs in by_tool.items():
        recs_sorted = sorted(recs, key=lambda r: r.timestamp)
        if len(recs_sorted) < RECENT_WINDOW * 2:
            continue
        recent = recs_sorted[-RECENT_WINDOW:]
        previous = recs_sorted[-RECENT_WINDOW * 2:-RECENT_WINDOW]
        recent_rate = sum(1 for r in recent if r.success) / len(recent)
        prev_rate = sum(1 for r in previous if r.success) / len(previous)
        drop_pp = (prev_rate - recent_rate) * 100
        if drop_pp < REGRESSION_DROP_PP:
            continue
        issues.append(Issue(
            severity='critical' if drop_pp > 25 else 'warning',
            category='regression',
            tool=tool,
            title=f"{tool} 最近成功率回归 -{drop_pp:.1f}pp",
            detail=(
                f"前 {RECENT_WINDOW} 次成功率 {prev_rate*100:.1f}%，"
                f"最近 {RECENT_WINDOW} 次降到 {recent_rate*100:.1f}%。"
            ),
            suggestion=(
                "排查最近的变更：工具实现是否动过、依赖是否升级、外部 API 是否变化、"
                "prompt 是否改了让模型用法变了。git log 看下相关文件最近的提交。"
            ),
            metric={"recent_rate": recent_rate, "previous_rate": prev_rate, "drop_pp": drop_pp},
        ))
    return issues


def _detect_cold_tools(records: List[ToolCallRecord], available_tools: Optional[List[str]] = None) -> List[Issue]:
    """长期低使用的工具——模型可能根本不知道它存在或不会用"""
    if not available_tools:
        return []
    if not records:
        return [
            Issue(
                severity='info',
                category='cold',
                tool=t,
                title=f"{t} 历史无调用",
                detail="工具注册了但模型从未调用过。",
                suggestion="检查工具 description 是否清晰，是否在 prompt 触发场景里被提及。",
                metric={"call_count": 0},
            )
            for t in available_tools
        ]
    recent = records[-COLD_TOOL_LOOKBACK:]
    counter = Counter(r.tool_name for r in recent)
    issues = []
    for tool in available_tools:
        cnt = counter.get(tool, 0)
        if cnt > COLD_TOOL_MAX_CALLS:
            continue
        issues.append(Issue(
            severity='info',
            category='cold',
            tool=tool,
            title=f"{tool} 最近 {COLD_TOOL_LOOKBACK} 次中只调用 {cnt} 次",
            detail="该工具几乎不被使用。",
            suggestion=(
                "可能原因：1) 工具 description 不够清晰让模型知道何时用 "
                "2) 适用场景太窄/和其他工具重叠 "
                "3) 命名不直观。考虑改进 description 加 example，或合并到其他工具。"
            ),
            metric={"call_count": cnt, "lookback": COLD_TOOL_LOOKBACK},
        ))
    return issues


def compute_per_agent_breakdown(records: List[ToolCallRecord]) -> Dict[str, Any]:
    """按 agent_id 分桶统计每个角色的调用画像。

    没有 agent_id 的老记录归到 "_unknown" 桶。
    返回：{ agent_id: {total, success_rate, top_tools, top_errors} }
    """
    by_agent: Dict[str, List[ToolCallRecord]] = defaultdict(list)
    for r in records:
        by_agent[r.agent_id or "_unknown"].append(r)

    out: Dict[str, Any] = {}
    for agent, recs in by_agent.items():
        total = len(recs)
        succ = sum(1 for r in recs if r.success)
        tool_counter: Counter = Counter(r.tool_name for r in recs)
        err_counter: Counter = Counter(r.error_type for r in recs if r.error_type)
        out[agent] = {
            "total_calls": total,
            "success_rate": round(succ / total, 4) if total else 0.0,
            "top_tools": [
                {"tool": t, "count": c} for t, c in tool_counter.most_common(5)
            ],
            "top_errors": [
                {"error_type": e, "count": c} for e, c in err_counter.most_common(5)
            ],
        }
    return out


def compute_insights(
    records: Optional[List[ToolCallRecord]] = None,
    available_tools: Optional[List[str]] = None,
    include_per_agent: bool = False,
) -> Dict[str, Any]:
    """主入口：计算所有洞察。

    Args:
        records: 工具调用记录列表。None 则从 ToolStatsCollector 拿。
        available_tools: 当前已注册的工具名列表（用于检测冷门工具）。可选。

    Returns:
        {
            "summary": {总调用数、平均成功率、问题数等},
            "health": [HealthRow,...]（按 health_score 升序，最差在前）,
            "issues": [Issue,...]（按严重度排序）,
        }
    """
    if records is None:
        records = list(get_stats_collector().records)

    if not records:
        return {
            "summary": {
                "total_calls": 0,
                "tools_observed": 0,
                "issues_count": 0,
                "message": "无统计数据。",
            },
            "health": [],
            "issues": [],
        }

    health = _compute_health(records)

    issues: List[Issue] = []
    issues.extend(_detect_low_success(health))
    issues.extend(_detect_json_repair_hotspot(records))
    issues.extend(_detect_slow_tools(health))
    issues.extend(_detect_streak_failures(records))
    issues.extend(_detect_regression(records))
    issues.extend(_detect_cold_tools(records, available_tools))

    severity_order = {'critical': 0, 'warning': 1, 'info': 2}
    issues.sort(key=lambda i: (severity_order.get(i.severity, 99), i.tool))

    total_succ = sum(1 for r in records if r.success)
    summary = {
        "total_calls": len(records),
        "tools_observed": len(health),
        "overall_success_rate": round(total_succ / len(records), 4),
        "issues_count": len(issues),
        "issues_by_severity": dict(Counter(i.severity for i in issues)),
        "issues_by_category": dict(Counter(i.category for i in issues)),
    }

    result = {
        "summary": summary,
        "health": [asdict(r) for r in health],
        "issues": [asdict(i) for i in issues],
    }
    if include_per_agent:
        result["per_agent"] = compute_per_agent_breakdown(records)
    return result
