"""Cron jobs REST API（/cron/jobs*）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
仅做 list/delete/setEnabled，create 仍走 LLM（cron 表达式让 LLM 翻译
自然语言更顺手）。详见 docs/cron_design.md 切片 UI-A。
handler 均函数内惰性 import cron_scheduler / cron_expr，零模块级依赖。

端点：
- GET    /cron/jobs                  列出（按 agent 过滤；FEATURE_CRON_TOOL 关时返空）
- DELETE /cron/jobs/{job_id}         删除（owner check）
- PATCH  /cron/jobs/{job_id}/enabled 暂停/恢复
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["cron"])


@router.get("/cron/jobs")
async def list_cron_jobs(agent_id: Optional[str] = None):
    """列出 cron jobs。agent_id 非空 → 仅当前 agent 名下；否则全部（管理用）。"""
    if not (os.getenv("FEATURE_CRON_TOOL", "") or "").strip().lower() in ("1", "true", "yes", "on"):
        return {"ok": True, "count": 0, "jobs": [], "feature_enabled": False}
    from backend.cron_scheduler import get_cron_scheduler
    from backend.cron_expr import next_fire_at, parse_cron
    from datetime import datetime
    sched = get_cron_scheduler()
    jobs = await sched.list(agent_id=agent_id)
    now = datetime.now()
    items = []
    for j in jobs:
        try:
            nxt = next_fire_at(parse_cron(j.cron), now).isoformat(timespec="minutes")
        except ValueError:
            nxt = None
        items.append({
            "job_id": j.job_id,
            "agent_id": j.agent_id,
            "cron": j.cron,
            "description": j.description,
            "prompt": j.prompt,
            "mode": j.mode,
            "enabled": j.enabled,
            "fire_count": j.fire_count,
            "last_fired_at": j.last_fired_at,
            "next_fire_at": nxt,
            "created_at": j.created_at,
        })
    return {"ok": True, "count": len(items), "jobs": items, "feature_enabled": True}


@router.delete("/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str, agent_id: Optional[str] = None):
    """删 cron job。agent_id 非空 → owner check（防误删别 agent 的）。"""
    from backend.cron_scheduler import get_cron_scheduler
    sched = get_cron_scheduler()
    ok = await sched.remove(job_id, agent_id=agent_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job 不存在或不属于该 agent")
    return {"ok": True, "removed": job_id}


class _CronEnabledPayload(BaseModel):
    enabled: bool
    agent_id: Optional[str] = None


@router.patch("/cron/jobs/{job_id}/enabled")
async def set_cron_job_enabled(job_id: str, payload: _CronEnabledPayload):
    """暂停/恢复 cron job。owner check 同 delete。"""
    from backend.cron_scheduler import get_cron_scheduler
    sched = get_cron_scheduler()
    ok = await sched.set_enabled(job_id, payload.enabled, agent_id=payload.agent_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job 不存在或不属于该 agent")
    return {"ok": True, "job_id": job_id, "enabled": payload.enabled}
