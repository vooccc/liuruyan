"""5-field cron 表达式解析器（分 时 日 月 周）

仅支持 4 种语法（用户决策，不引 croniter）：
- `*`        整段
- `N`        单值
- `N,M[,K]`  列表（每项可以是单值或 N-M 范围）
- `N-M`      范围（含端点）
- `*/N`      步长（从 range lo 起每隔 N）

不支持：N-M/K（范围内步长）、L（last）、W（weekday）、#、@daily/mon 别名等扩展。
全部数字，dow 约定 0=周日 1=周一 ... 6=周六（标准 cron）。

dom 与 dow 关系按标准 cron 语义：任一是 `*` 时用另一个判定；都非 `*` 时
OR——dom 命中或 dow 命中即触发。

错误抛 ValueError，文案中文，便于回灌给 LLM 自调整。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import FrozenSet


_MINUTE_RANGE = (0, 59)
_HOUR_RANGE = (0, 23)
_DOM_RANGE = (1, 31)
_MONTH_RANGE = (1, 12)
_DOW_RANGE = (0, 6)


@dataclass(frozen=True)
class CronExpr:
    minute: FrozenSet[int]
    hour: FrozenSet[int]
    dom: FrozenSet[int]
    month: FrozenSet[int]
    dow: FrozenSet[int]
    dom_is_wildcard: bool
    dow_is_wildcard: bool


def _parse_segment(seg: str, lo: int, hi: int, seg_label: str) -> FrozenSet[int]:
    """解析单段。返回 [lo, hi] 范围内的取值集合（已去重）。"""
    seg = seg.strip()
    if not seg:
        raise ValueError(f"cron 表达式{seg_label}段不能为空")

    if seg.startswith("*/"):
        try:
            step = int(seg[2:])
        except ValueError:
            raise ValueError(f"cron 表达式{seg_label}段步长非数字：{seg}")
        if step <= 0:
            raise ValueError(f"cron 表达式{seg_label}段步长必须 >= 1：{seg}")
        return frozenset(range(lo, hi + 1, step))

    if seg == "*":
        return frozenset(range(lo, hi + 1))

    if "," in seg:
        result: set[int] = set()
        for part in seg.split(","):
            result |= _parse_segment(part, lo, hi, seg_label)
        return frozenset(result)

    if "-" in seg:
        parts = seg.split("-")
        if len(parts) != 2:
            raise ValueError(f"cron 表达式{seg_label}段范围格式非法：{seg}")
        try:
            a, b = int(parts[0]), int(parts[1])
        except ValueError:
            raise ValueError(f"cron 表达式{seg_label}段范围非数字：{seg}")
        if a > b:
            raise ValueError(f"cron 表达式{seg_label}段范围倒序：{seg}")
        if a < lo or b > hi:
            raise ValueError(f"cron 表达式{seg_label}段范围超界 [{lo},{hi}]：{seg}")
        return frozenset(range(a, b + 1))

    try:
        n = int(seg)
    except ValueError:
        raise ValueError(f"cron 表达式{seg_label}段非数字：{seg}")
    if n < lo or n > hi:
        raise ValueError(f"cron 表达式{seg_label}段超界 [{lo},{hi}]：{seg}")
    return frozenset({n})


def parse_cron(expr: str) -> CronExpr:
    """解析 5-field cron 表达式。

    错误抛 ValueError（中文文案），调用方可直接把 str(e) 回灌给 LLM。
    """
    if not isinstance(expr, str):
        raise ValueError(f"cron 表达式必须是字符串，得到 {type(expr).__name__}")
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"cron 表达式必须 5 段（分 时 日 月 周），得到 {len(parts)} 段：{expr!r}"
        )
    return CronExpr(
        minute=_parse_segment(parts[0], *_MINUTE_RANGE, seg_label="分钟"),
        hour=_parse_segment(parts[1], *_HOUR_RANGE, seg_label="小时"),
        dom=_parse_segment(parts[2], *_DOM_RANGE, seg_label="日"),
        month=_parse_segment(parts[3], *_MONTH_RANGE, seg_label="月"),
        dow=_parse_segment(parts[4], *_DOW_RANGE, seg_label="周"),
        dom_is_wildcard=(parts[2].strip() == "*"),
        dow_is_wildcard=(parts[4].strip() == "*"),
    )


def _python_weekday_to_cron(dt: datetime) -> int:
    """Python `weekday()` 返回 Mon=0..Sun=6；cron 约定 Sun=0..Sat=6。"""
    return (dt.weekday() + 1) % 7


def matches(ce: CronExpr, dt: datetime) -> bool:
    """检查 dt 是否命中 cron 表达式（精度到分钟）。

    dom/dow 标准 cron 语义：
    - 都是 `*` → 不约束日期
    - 一个是 `*` → 用另一个判定
    - 都不是 `*` → OR（任一命中即触发）
    """
    if dt.minute not in ce.minute:
        return False
    if dt.hour not in ce.hour:
        return False
    if dt.month not in ce.month:
        return False

    cron_dow = _python_weekday_to_cron(dt)
    if ce.dom_is_wildcard and ce.dow_is_wildcard:
        return True
    if ce.dom_is_wildcard:
        return cron_dow in ce.dow
    if ce.dow_is_wildcard:
        return dt.day in ce.dom
    return dt.day in ce.dom or cron_dow in ce.dow


def next_fire_at(
    ce: CronExpr,
    after: datetime,
    *,
    max_lookahead_days: int = 366,
) -> datetime:
    """从 `after` 之后（不含）找最近一次触发时刻。分钟粒度暴力推进。

    - after 应是 naive local datetime（与调度循环里 `datetime.now()` 一致）
    - 返回值 second/microsecond 一律置 0
    - 超过 max_lookahead_days 仍未命中抛 ValueError
    """
    cur = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    deadline = after + timedelta(days=max_lookahead_days)
    while cur <= deadline:
        if matches(ce, cur):
            return cur
        cur += timedelta(minutes=1)
    raise ValueError(
        f"cron 表达式在 {max_lookahead_days} 天内不会触发，请检查 dom/month 组合"
    )
