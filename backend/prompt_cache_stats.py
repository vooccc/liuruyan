"""Prompt cache 命中统计：每次 LLM 调用的 token 增量追加到 jsonl。

设计目的：
    `提示词缓存调度规范` § 五监控指标需要能算出
    `cache_hit_ratio`。session.meta 只存累加值，时间维度丢失；为了能：
    - 对比优化前后（commit e98fb91 / 1a4d2ab 落地前后）
    - 按 provider / model / agent 切片分析
    - 定位某次命中率骤降对应的改动
    需要一份**原始增量**的 append-only 日志。参考 `tool_stats.jsonl` 模式。

数据形态（每行 JSON）：
    {
        "ts": 1777700000.123,       # UNIX 秒（float）
        "session_id": "abc",
        "agent_id": "liuruyan",
        "model": "claude-sonnet-4",
        "input_tokens": 1234,       # 归一化后的 uncached input
        "output_tokens": 567,
        "cache_read": 8000,         # 命中缓存读入（Anthropic / OpenAI 兼容 cached_tokens）
        "cache_create": 0,          # 写入缓存（Anthropic only；OpenAI 协议无字段恒 0）
    }

调用方：`session.SessionManager.record_token_usage` 的副作用。失败静默。

非目标：
    - 不做实时聚合（跑的时候每次算 hit_ratio 太费）
    - 不做文件轮转（短期够用；如果 jsonl 涨到 100 MB 级别再考虑按日切片）
    - 不做并发安全强保证（append-only + 单行 JSON 足够；丢个别行不致命）
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 文件位置：项目根（跟 tool_stats.jsonl / backend_writes.jsonl 同级）
_DEFAULT_STATS_FILE = Path(__file__).resolve().parent.parent / "prompt_cache_stats.jsonl"

# 允许测试覆盖
_STATS_FILE: Path = _DEFAULT_STATS_FILE

# 进程内追加锁（跨进程场景 jsonl append-only 语义本身容错，不强要求）
_WRITE_LOCK = asyncio.Lock()


def _get_stats_file() -> Path:
    """测试路径可以通过环境变量或 set_stats_file 覆盖。"""
    env_override = os.environ.get("LRY_PROMPT_CACHE_STATS_FILE")
    if env_override:
        return Path(env_override)
    return _STATS_FILE


def set_stats_file(path: Optional[Path]) -> None:
    """测试专用：重置落盘目标。传 None 恢复默认。"""
    global _STATS_FILE
    _STATS_FILE = path if path is not None else _DEFAULT_STATS_FILE


async def record_cache_stat(
    *,
    session_id: str,
    agent_id: Optional[str],
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_create: int,
) -> None:
    """追加一行 JSON 到 prompt_cache_stats.jsonl。

    - 全 0 skip（跟 record_token_usage 的 no-op 对齐）
    - 失败静默（不抛到主 LLM 流程）
    """
    if not (input_tokens or output_tokens or cache_read or cache_create):
        return

    row = {
        "ts": time.time(),
        "session_id": session_id or "",
        "agent_id": agent_id or "",
        "model": model or "",
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "cache_read": int(cache_read or 0),
        "cache_create": int(cache_create or 0),
    }

    try:
        path = _get_stats_file()
        async with _WRITE_LOCK:
            # 用阻塞 I/O 在锁内同步写——jsonl 小行 append 不会成瓶颈
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 静默，跟 record_token_usage 约定一致


def load_recent_rows(limit: int = 1000) -> List[Dict[str, Any]]:
    """读最近 N 行供 API / 分析。限 1000 默认以免 OOM。"""
    try:
        path = _get_stats_file()
        if not path.exists():
            return []
        # 简单读全量再 tail——如果文件真的涨到百 MB 级别，改成倒序流式读
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        rows: List[Dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 坏行跳过
        return rows
    except Exception:
        return []


def compute_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """聚合命中率 / 省下的 token / 按 agent × model 切片。

    公式：
        total_read_or_billed = cache_read + cache_create + input_tokens
        hit_ratio = cache_read / total_read_or_billed
            (cache_create 算"付费首次写入"，不计命中；input 是未缓存的原 prompt)

        estimated_savings_tokens = cache_read * 0.9
            (Anthropic cache 读 0.1x，省 0.9x；OpenAI GPT 省 0.5x——这里取
            Anthropic 口径偏乐观，前端标注清楚语义即可)

    Returns:
        {
            "call_count": int,
            "total_input": int,
            "total_output": int,
            "total_cache_read": int,
            "total_cache_create": int,
            "hit_ratio": float,             # 0..1
            "estimated_savings_tokens": int,
            "by_agent_model": {
                "<agent_id>|<model>": {...同上...},
            },
        }
    """
    total_in = total_out = total_cr = total_cc = 0
    by_key: Dict[str, Dict[str, int]] = {}
    for r in rows:
        i = int(r.get("input_tokens", 0) or 0)
        o = int(r.get("output_tokens", 0) or 0)
        cr = int(r.get("cache_read", 0) or 0)
        cc = int(r.get("cache_create", 0) or 0)
        total_in += i
        total_out += o
        total_cr += cr
        total_cc += cc
        key = f"{r.get('agent_id', '')}|{r.get('model', '')}"
        slot = by_key.setdefault(
            key,
            {"call_count": 0, "total_input": 0, "total_output": 0,
             "total_cache_read": 0, "total_cache_create": 0},
        )
        slot["call_count"] += 1
        slot["total_input"] += i
        slot["total_output"] += o
        slot["total_cache_read"] += cr
        slot["total_cache_create"] += cc

    def _ratio(cr: int, cc: int, i: int) -> float:
        denom = cr + cc + i
        return (cr / denom) if denom > 0 else 0.0

    # 给每个 slot 补命中率字段
    for slot in by_key.values():
        slot["hit_ratio"] = _ratio(
            slot["total_cache_read"], slot["total_cache_create"], slot["total_input"],
        )
        slot["estimated_savings_tokens"] = int(slot["total_cache_read"] * 0.9)

    return {
        "call_count": len(rows),
        "total_input": total_in,
        "total_output": total_out,
        "total_cache_read": total_cr,
        "total_cache_create": total_cc,
        "hit_ratio": _ratio(total_cr, total_cc, total_in),
        "estimated_savings_tokens": int(total_cr * 0.9),
        "by_agent_model": by_key,
    }


__all__ = [
    "record_cache_stat",
    "load_recent_rows",
    "compute_summary",
    "set_stats_file",
]
