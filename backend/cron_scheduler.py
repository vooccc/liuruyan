"""CronScheduler — 后台 tick 循环，按 cron 表达式触发 subagent

设计要点：
- 30s tick + 1 分钟去重：避免双触发
- 每个 job 自带 agent_id + parent_session_id，list/remove 强制 owner check
- _fire 触发前 ContextVar 防御性 set + _ensure_main_session 兜底
  （cron 是 detached task，必须显式绑定 session_id 才能写回会话）
- 持久化 / lifespan 接入分别在切片 B2 / B3 加，本切片仅内存版

dom/dow 标准 cron OR 语义；time 用 datetime.now() 本地时间（与 cron 表达式
约定一致，跨时区跑 1 个实例不在本期范围内）。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Dict, List, Optional

from .cron_expr import next_fire_at, parse_cron
from .cron_models import CronJob

logger = logging.getLogger(__name__)


_TICK_SECONDS = 30           # 30s 检查一次；最小粒度仍是 1 分钟
_DEDUP_WINDOW_SEC = 60       # 距上次 fire <60s 视为同一分钟，跳过
_PERSIST_VERSION = 1         # 落盘 schema 版本


class CronScheduler:
    def __init__(self) -> None:
        self._jobs: Dict[str, CronJob] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._stopping = False

    # ============================================================
    # 生命周期
    # ============================================================

    async def start(self) -> None:
        """启动 tick 循环 + 加载持久化 jobs。幂等。"""
        if self._loop_task and not self._loop_task.done():
            return
        self._stopping = False
        await self._load()
        self._loop_task = asyncio.create_task(self._run_loop(), name="cron-scheduler-loop")
        logger.info("[CronScheduler] started, %d jobs in memory", len(self._jobs))

    async def stop(self) -> None:
        """取消 tick 循环。不删 jobs（持久化在 B2，重启可恢复）。"""
        self._stopping = True
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
        self._loop_task = None
        logger.info("[CronScheduler] stopped")

    # ============================================================
    # CRUD
    # ============================================================

    async def add(self, job: CronJob) -> None:
        """注册一条 job。重复 job_id 直接覆盖（B2 持久化也会 atomic 写）。"""
        # parse 失败先抛——上层 cron_create handler 应该已验过，这里是最后兜底
        parse_cron(job.cron)
        self._jobs[job.job_id] = job
        await self._persist()
        await self._emit_changed("add", job.job_id, job.agent_id)

    async def remove(self, job_id: str, *, agent_id: Optional[str] = None) -> bool:
        """删除 job。

        agent_id 非空 → owner check：仅 job.agent_id == agent_id 时才删，
        防止 LLM 跨 agent 误删；返回 False 表示拒绝或不存在。
        """
        job = self._jobs.get(job_id)
        if not job:
            return False
        if agent_id is not None and job.agent_id != agent_id:
            return False
        owner = job.agent_id
        del self._jobs[job_id]
        await self._persist()
        await self._emit_changed("remove", job_id, owner)
        return True

    async def list(self, *, agent_id: Optional[str] = None) -> List[CronJob]:
        """列出 jobs。agent_id 非空 → 仅返回该 agent 名下。"""
        if agent_id is None:
            return list(self._jobs.values())
        return [j for j in self._jobs.values() if j.agent_id == agent_id]

    async def get(self, job_id: str) -> Optional[CronJob]:
        return self._jobs.get(job_id)

    async def set_enabled(
        self,
        job_id: str,
        enabled: bool,
        *,
        agent_id: Optional[str] = None,
    ) -> bool:
        """切换 job.enabled。owner check 同 remove。

        返回 True = 成功更新；False = job 不存在或跨 agent 拒绝。
        enabled=False 后下一次 _run_loop tick 跳过该 job；enabled=True 立即恢复。
        """
        job = self._jobs.get(job_id)
        if not job:
            return False
        if agent_id is not None and job.agent_id != agent_id:
            return False
        job.enabled = bool(enabled)
        await self._persist()
        await self._emit_changed("set_enabled", job_id, job.agent_id)
        return True

    async def _emit_changed(self, action: str, job_id: str, agent_id: str) -> None:
        """前端 CronJobsPanel 订阅这个事件做免轮询刷新。bus 没装配（test/CI）静默跳过。"""
        try:
            from liuruyan.buses import AgentEvent, get_event_bus
            bus = get_event_bus()
            if bus is None:
                return
            event = AgentEvent(
                type="cron_jobs_changed",
                trace_id=f"cron-{action}-{uuid.uuid4().hex[:6]}",
                session_id="",  # 全局事件不绑 session
                agent_id=agent_id,
                user_id="local",
                payload={"action": action, "job_id": job_id},
            )
            await bus.publish(event)
        except Exception as e:
            logger.warning("[CronScheduler] cron_jobs_changed publish failed: %s", e)

    # ============================================================
    # 内部
    # ============================================================

    async def _run_loop(self) -> None:
        """每 _TICK_SECONDS 扫一次所有 job，命中 + 未在去重窗口内则 fire。"""
        while not self._stopping:
            try:
                now = datetime.now()
                now_ts = now.timestamp()
                # 拷贝一份，避免迭代过程中其他协程改动
                snapshot = list(self._jobs.values())
                for job in snapshot:
                    if not job.enabled:
                        continue
                    if job.last_fired_at and (now_ts - job.last_fired_at) < _DEDUP_WINDOW_SEC:
                        continue
                    try:
                        ce = parse_cron(job.cron)
                    except ValueError as e:
                        logger.warning("[CronScheduler] job %s 表达式损坏跳过：%s", job.job_id, e)
                        continue
                    from .cron_expr import matches as _matches
                    if _matches(ce, now):
                        try:
                            await self._fire(job, now_ts)
                        except Exception as e:
                            logger.exception("[CronScheduler] _fire %s 异常：%s", job.job_id, e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[CronScheduler] tick 异常：%s", e)
            try:
                await asyncio.sleep(_TICK_SECONDS)
            except asyncio.CancelledError:
                raise

    async def _fire(self, job: CronJob, fire_ts: float) -> None:
        """触发一条 job：set ContextVar + 兜底 session + cron subagent + 计数。"""
        # ContextVar 防御性 set —— cron tick 是 detached task，不在 chat 请求 context 里
        try:
            from . import llm_client as _llm
            _llm._current_agent_id_var.set(job.agent_id)
            _llm._current_session_id_var.set(job.parent_session_id or None)
        except Exception:
            pass  # set 失败不能阻断 fire

        psid = job.parent_session_id
        if not psid:
            psid = await self._ensure_main_session(job.agent_id)
            # 落回 job 上，下次直接用
            job.parent_session_id = psid

        route_id = f"cron:{job.job_id}:{int(fire_ts)}"
        logger.info(
            "[CronScheduler] firing cron subagent job=%s agent=%s session=%s route=%s",
            job.job_id, job.agent_id, psid, route_id,
        )

        job.fire_count += 1
        job.last_fired_at = fire_ts
        # once 模式：触发一次即自动 disable，避免下次 tick 再 fire
        # 不直接 remove：留 cron_list 可见 + fire_count/last_fired_at 历史，
        # 用户通过 cron_delete 主动清理
        if job.mode == "once":
            job.enabled = False
        await self._persist()
        logger.info(
            "[CronScheduler] fired job=%s agent=%s session=%s route=%s count=%d mode=%s",
            job.job_id,
            job.agent_id,
            psid,
            route_id,
            job.fire_count,
            job.mode,
        )

        # v2 EventBus 推 cron_fired 给跨 session 订阅者（toast / 审计）
        # 见 _docs/cron_toast_design.md § 4。失败 swallow 不阻断主路径；
        # bus 未装配（lifespan 未起 / FEATURE_CRON_TOOL=off 单测路径）也直接跳过
        try:
            from liuruyan.buses import AgentEvent, get_event_bus
            bus = get_event_bus()
            if bus is not None:
                # cron tick 是 detached task，没现成 trace_id，自造一个
                # 标识来源是 cron_scheduler，便于审计回查
                trace_id = f"cron-tick-{uuid.uuid4().hex[:8]}"
                # next_fire_at 失败不阻断 publish；payload 给前端展示用
                try:
                    from .cron_expr import next_fire_at, parse_cron
                    nxt_iso = next_fire_at(parse_cron(job.cron), datetime.now()).isoformat(timespec="minutes")
                except Exception:
                    nxt_iso = None
                event = AgentEvent(
                    type="cron_fired",
                    trace_id=trace_id,
                    session_id=psid,
                    agent_id=job.agent_id,
                    user_id="local",  # 单机产品固定值，与 Agent.RunContext 一致
                    payload={
                        "job_id": job.job_id,
                        "description": job.description,
                        "cron": job.cron,
                        "mode": job.mode,
                        "fire_count": job.fire_count,
                        "next_fire_at": nxt_iso,
                        "route_id": route_id,
                    },
                )
                await bus.publish(event)
        except Exception as e:
            logger.warning("[CronScheduler] cron_fired publish failed: %s", e)

        job_snapshot = copy.deepcopy(job)

        async def _run_detached() -> None:
            try:
                from .cron_subagent_runner import publish_cron_run_event, run_cron_subagent
                result = await run_cron_subagent(job=job_snapshot, session_id=psid, fire_ts=fire_ts)
                await publish_cron_run_event(
                    event_type="cron_completed" if result.ok else "cron_failed",
                    job=job_snapshot,
                    session_id=psid,
                    route_id=route_id,
                    result=result,
                    fire_ts=fire_ts,
                )
            except Exception as e:
                logger.exception("[CronScheduler] cron subagent job=%s failed: %s", job_snapshot.job_id, e)

        asyncio.create_task(_run_detached(), name=f"cron-subagent-{job.job_id}")

    async def _ensure_main_session(self, agent_id: str) -> str:
        """找到该 agent 最近的 main session，没有就新建。

        这是关键风险兜底——worker._notify_parent_notice 在 session is None
        时静默 return（worker.py:2421-2433），cron 触发的 worker 必须有真实
        parent_session 才能让前端派发卡显示。
        """
        from .session import get_session_manager
        mgr = get_session_manager()
        items = await mgr.list_sessions(agent_id=agent_id)
        if items:
            # list_sessions 已按 updated_at desc 排序
            return items[0]["id"]
        # 没有任何 main session → 创建一个
        new_session = await mgr.create_session(
            agent_id=agent_id,
            title="Cron 自动会话",
            session_type="main",
        )
        return new_session.id

    # ============================================================
    # 持久化
    # ============================================================

    async def _persist(self) -> None:
        """全量 atomic 写 cron_jobs.json：tmp + os.replace。

        失败仅 logger.warning 不抛——内存 jobs 仍在跑，不应阻断 add/remove/_fire。
        路径每次重新 resolve（paths.cron_jobs_path() 无 cache），LIURUYAN_DATA_DIR
        切换会自然落新位置。
        """
        try:
            from .paths import cron_jobs_path
            target = cron_jobs_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": _PERSIST_VERSION,
                "jobs": [j.to_dict() for j in self._jobs.values()],
            }
            data = json.dumps(payload, ensure_ascii=False, indent=2)
            # tmp 写在同目录，保证 os.replace 是同分区原子操作
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(target.parent),
                prefix=".cron_jobs.",
                suffix=".tmp",
                delete=False,
            ) as f:
                f.write(data)
                tmp_path = f.name
            os.replace(tmp_path, str(target))
        except Exception as e:
            logger.warning("[CronScheduler] _persist 失败：%s", e)

    async def _load(self) -> None:
        """从 cron_jobs.json 加载 jobs 到内存。文件不存在 / 损坏 → 空启动 + warning。"""
        try:
            from .paths import cron_jobs_path
            target = cron_jobs_path()
            if not target.exists():
                self._jobs = {}
                return
            with open(target, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            logger.warning("[CronScheduler] _load 损坏 / 不可读：%s，空启动", e)
            self._jobs = {}
            return

        loaded: Dict[str, CronJob] = {}
        for item in payload.get("jobs", []):
            try:
                job = CronJob.from_dict(item)
                # 校验 cron 表达式；坏的跳过不阻断启动
                parse_cron(job.cron)
                loaded[job.job_id] = job
            except Exception as e:
                logger.warning(
                    "[CronScheduler] 跳过损坏 job %s：%s",
                    item.get("job_id", "?"), e,
                )
        self._jobs = loaded
        logger.info("[CronScheduler] _load 加载 %d 条 job", len(self._jobs))


# ============================================================
# 单例
# ============================================================

_instance: Optional[CronScheduler] = None


def get_cron_scheduler() -> CronScheduler:
    global _instance
    if _instance is None:
        _instance = CronScheduler()
    return _instance


def reset_cron_scheduler_for_testing() -> None:
    """测试用：重置单例 + 内存 jobs。"""
    global _instance
    _instance = None
