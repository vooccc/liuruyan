"""CronJob 数据模型 + 序列化

落盘 schema 见 _docs/cron_design.md（切片 D 落盘）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class CronJob:
    job_id: str                          # uuid4 hex[:12]
    agent_id: str                        # cron 仅属此 agent；CronList/Delete 强制 owner check
    parent_session_id: str               # fire 时 worker 落地的 session；空串时 _ensure_main_session 兜底
    cron: str                            # 原 5-field 表达式（人读）
    description: str                     # ≤200 字，派发卡 [Cron] 前缀后展示
    prompt: str                          # fire 时 worker 接到的完整任务 prompt
    created_at: float                    # epoch seconds
    enabled: bool = True
    # mode='once' = 一次性 reminder：触发一次后 _fire 自动 enabled=false，等 cron_delete 清理
    # mode='recurring' = 默认按 cron 表达式持续触发，直到 cron_delete
    mode: str = "recurring"              # "recurring" | "once"
    last_fired_at: Optional[float] = None
    fire_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CronJob":
        # 容错未来字段：只挑 dataclass 已知字段，多余 key 静默忽略
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)
