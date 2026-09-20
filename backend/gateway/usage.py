"""Gateway 用量统计聚合（M4-C）。

数据源：`backend/gateway/audit.py` 写的 `gateway_audit.jsonl`
聚合方式：每次请求 / 每次刷新都重新扫一遍 jsonl，内存里聚合。
为什么不存 SQLite：
  - 本地网关流量上限保守估计 < 100k/月，10MB 量级 jsonl 扫描 < 100ms
  - 不引数据库依赖、无需迁移
  - jsonl 本身是「单一事实源」，前端聚合只是 view，可随意扩展维度

防御：
  - 任何坏行（半 JSON / 残行）skip + warn，不抛
  - 文件不存在 → 返空结果
  - 边界过滤用字符串比较 ts（ISO 8601 字典序就是时间序）
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .. import paths


# 支持的聚合维度
GROUP_BY_DIMS = {"provider", "model", "token", "day", "endpoint", "client_ip"}


@dataclass
class _Bucket:
    """单个聚合桶的累计量。"""
    key: str
    calls: int = 0
    errors: int = 0
    stream_calls: int = 0
    nonstream_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fallback_jumps_in: int = 0   # 别人切到我（我作为备胎被启用次数）
    fallback_jumps_out: int = 0  # 我自己失败被切走（我作为故障源）
    latencies_ms: List[int] = field(default_factory=list)
    first_seen_ts: str = ""
    last_seen_ts: str = ""

    def to_dict(self) -> Dict[str, Any]:
        lat_sorted = sorted(self.latencies_ms) if self.latencies_ms else []
        return {
            "key": self.key,
            "calls": self.calls,
            "errors": self.errors,
            "error_rate": round(self.errors / self.calls, 4) if self.calls else 0.0,
            "stream_calls": self.stream_calls,
            "nonstream_calls": self.nonstream_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "fallback_jumps_in": self.fallback_jumps_in,
            "fallback_jumps_out": self.fallback_jumps_out,
            "p50_latency_ms": _percentile(lat_sorted, 0.50),
            "p95_latency_ms": _percentile(lat_sorted, 0.95),
            "first_seen_ts": self.first_seen_ts,
            "last_seen_ts": self.last_seen_ts,
        }


def _percentile(sorted_vals: List[int], p: float) -> Optional[int]:
    if not sorted_vals:
        return None
    idx = max(0, min(len(sorted_vals) - 1, int(round(p * (len(sorted_vals) - 1)))))
    return int(sorted_vals[idx])


def _iter_audit_lines() -> Iterable[Dict[str, Any]]:
    """逐行读 audit jsonl，损坏行 skip。"""
    p = paths.gateway_audit_path()
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    # 半 JSON / 损坏行：跳过即可，不影响别的行
                    continue
    except OSError:
        return


def _key_for(event: Dict[str, Any], dim: str) -> str:
    """把一条 event 投影到指定维度的 key。"""
    if dim == "provider":
        return event.get("provider") or "(unknown)"
    if dim == "model":
        # 优先用 provider:vendor_model 形式（最可读），退回 model_requested
        prov = event.get("provider") or ""
        vm = event.get("vendor_model") or ""
        if prov and vm:
            return f"{prov}:{vm}"
        return event.get("model_requested") or "(unknown)"
    if dim == "token":
        return event.get("token_id") or "(unknown)"
    if dim == "day":
        ts = event.get("ts") or ""
        return ts[:10] if len(ts) >= 10 else "(unknown)"
    if dim == "endpoint":
        return event.get("endpoint") or "(unknown)"
    if dim == "client_ip":
        return event.get("client_ip") or "(unknown)"
    return "(unknown)"


def _accumulate_bucket(b: _Bucket, ev: Dict[str, Any]) -> None:
    """把一条 event 累加到 bucket。"""
    b.calls += 1
    status = int(ev.get("status") or 0)
    if status == 0 or status >= 400 or ev.get("error"):
        b.errors += 1
    if ev.get("stream"):
        b.stream_calls += 1
    else:
        b.nonstream_calls += 1
    pt = ev.get("prompt_tokens")
    ct = ev.get("completion_tokens")
    if isinstance(pt, int):
        b.prompt_tokens += pt
    if isinstance(ct, int):
        b.completion_tokens += ct
    lat = ev.get("upstream_latency_ms")
    if isinstance(lat, int) and lat >= 0:
        b.latencies_ms.append(lat)

    # M3 fallback chain 信息（audit 里 extra={"fallback_chain": {...}}）
    fc = ev.get("fallback_chain") or {}
    attempts = fc.get("attempts") or []
    final_pid = fc.get("final_provider") or ""
    if len(attempts) > 1 and final_pid:
        # 最终落到非首位 provider → 算 jumps_in 给最终 provider，jumps_out 给首位
        # 但这里我们只统计当前 bucket（按维度可能是 provider）；具体归属在 build_usage 里统一处理
        pass

    ts = ev.get("ts") or ""
    if ts:
        if not b.first_seen_ts or ts < b.first_seen_ts:
            b.first_seen_ts = ts
        if not b.last_seen_ts or ts > b.last_seen_ts:
            b.last_seen_ts = ts


def _accumulate_fallback_jumps(
    buckets: Dict[str, _Bucket],
    ev: Dict[str, Any],
    dim: str,
) -> None:
    """fallback chain 里 jumps_in/jumps_out 必须按 *provider* 维度归属，
    所以仅当 group_by=provider 时才往 buckets 里加；其他维度只在 attempts 数量上间接体现（不重复累加）。"""
    if dim != "provider":
        return
    fc = ev.get("fallback_chain") or {}
    attempts = fc.get("attempts") or []
    final_pid = fc.get("final_provider") or ""
    if not attempts:
        return
    # 先把所有非最终的 attempt 算 jumps_out（失败被切走）
    for at in attempts[:-1]:
        out_pid = at.get("provider") or ""
        if not out_pid:
            continue
        b = buckets.setdefault(out_pid, _Bucket(key=out_pid))
        b.fallback_jumps_out += 1
    # 最终 provider 如果不是首位 → 它是被启用的备胎，算 jumps_in
    if final_pid and len(attempts) > 1:
        first_pid = (attempts[0] or {}).get("provider") or ""
        if final_pid != first_pid:
            b = buckets.setdefault(final_pid, _Bucket(key=final_pid))
            b.fallback_jumps_in += 1


def _ts_in_range(ts: str, start_iso: Optional[str], end_iso: Optional[str]) -> bool:
    """ISO 8601 字符串字典序 = 时间序，支持只传 YYYY-MM-DD。"""
    if start_iso and ts < start_iso:
        return False
    if end_iso and ts > end_iso:
        return False
    return True


def build_usage(
    *,
    group_by: str = "provider",
    start: Optional[str] = None,
    end: Optional[str] = None,
    top_n: int = 100,
) -> Dict[str, Any]:
    """聚合用量。

    Args:
      group_by: provider / model / token / day / endpoint / client_ip
      start: ISO 起始时间戳（含），可只传 'YYYY-MM-DD'。None=不限。
      end:   ISO 终止时间戳（不含次日 0 点；推荐用户 UI 上传 'YYYY-MM-DD' 后端拼 'T23:59:59'）
      top_n: 返回的最大桶数（按 calls 降序截断）

    Returns:
      {
        "range": {"from": ..., "to": ...},
        "group_by": ...,
        "total_calls": ...,
        "total_errors": ...,
        "total_prompt_tokens": ...,
        "total_completion_tokens": ...,
        "buckets": [ {key, calls, errors, ...}, ... ]
      }
    """
    if group_by not in GROUP_BY_DIMS:
        raise ValueError(f"unsupported group_by '{group_by}'; valid: {sorted(GROUP_BY_DIMS)}")

    buckets: Dict[str, _Bucket] = {}
    total_calls = 0
    total_errors = 0
    total_pt = 0
    total_ct = 0
    earliest = ""
    latest = ""

    for ev in _iter_audit_lines():
        ts = ev.get("ts") or ""
        if not _ts_in_range(ts, start, end):
            continue

        total_calls += 1
        status = int(ev.get("status") or 0)
        if status == 0 or status >= 400 or ev.get("error"):
            total_errors += 1
        pt = ev.get("prompt_tokens")
        ct = ev.get("completion_tokens")
        if isinstance(pt, int):
            total_pt += pt
        if isinstance(ct, int):
            total_ct += ct

        if ts:
            if not earliest or ts < earliest:
                earliest = ts
            if not latest or ts > latest:
                latest = ts

        key = _key_for(ev, group_by)
        b = buckets.setdefault(key, _Bucket(key=key))
        _accumulate_bucket(b, ev)
        _accumulate_fallback_jumps(buckets, ev, group_by)

    sorted_buckets = sorted(buckets.values(), key=lambda b: b.calls, reverse=True)[:top_n]

    return {
        "range": {"from": start or earliest, "to": end or latest},
        "group_by": group_by,
        "total_calls": total_calls,
        "total_errors": total_errors,
        "error_rate": round(total_errors / total_calls, 4) if total_calls else 0.0,
        "total_prompt_tokens": total_pt,
        "total_completion_tokens": total_ct,
        "total_tokens": total_pt + total_ct,
        "buckets": [b.to_dict() for b in sorted_buckets],
    }


def list_recent_fallback_jumps(*, limit: int = 20) -> List[Dict[str, Any]]:
    """列最近 N 条「确实发生了 fallback 切换」的事件（attempts > 1）。

    给 UI 用：用户能直观看到「最近 20 次自动切备胎都是为啥」。
    返回按时间倒序排。
    """
    out: List[Dict[str, Any]] = []
    for ev in _iter_audit_lines():
        fc = ev.get("fallback_chain") or {}
        attempts = fc.get("attempts") or []
        if len(attempts) <= 1:
            continue
        first = attempts[0] or {}
        final_pid = fc.get("final_provider") or ""
        first_pid = first.get("provider") or ""
        if final_pid == first_pid:
            # 第一就成功，没真正切（防御，正常 attempts==1 已 skip）
            continue
        out.append({
            "ts": ev.get("ts"),
            "endpoint": ev.get("endpoint"),
            "model_requested": ev.get("model_requested"),
            "from_provider": first_pid,
            "from_status": first.get("status"),
            "from_result": first.get("result"),
            "to_provider": final_pid,
            "attempts_count": len(attempts),
            "stream": ev.get("stream"),
        })
    # 按时间倒序，取前 limit
    out.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return out[:limit]


__all__ = ["build_usage", "list_recent_fallback_jumps", "GROUP_BY_DIMS"]
