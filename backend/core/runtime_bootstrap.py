"""AgentActor 运行时启动器（Phase 1B-1）

职责：
1. 进程启动时为现有所有 agent 创建 AgentActor 并 start
2. 进程关闭时优雅 stop_all
3. 提供环境开关 LIURUYAN_AGENT_TICK 控制是否启动（默认 OFF）
4. 默认 tick handler 仅推 panorama 心跳占位事件，不真调 LLM

设计原则：
- 与 agent.py 解耦：通过 agent_id 列表驱动；测试可注入
- 失败隔离：单 agent 启动失败不影响其他 actor 与 lifespan
- 默认关闭：handover §4.1 强调"先观察 1 周烧钱量"，所以接 LLM 推迟到下一刀
"""

from __future__ import annotations

import logging
import os
import time
from typing import Iterable, List, Optional

from backend.core.agent_actor import (
    AgentActor,
    AgentActorConfig,
    AgentPool,
    get_agent_pool,
)
from backend.core.event_bus import (
    PANORAMA_CHANNEL,
    EventBus,
    get_default_bus,
)
from backend.core.inbox_message import BusEvent
from backend.core.safety.tick_guard import (
    DAILY_QUOTA_DEFAULT,
    TICK_HARD_MIN_SEC,
    TickGuardConfig,
)

logger = logging.getLogger(__name__)


ENV_FLAG = "LIURUYAN_AGENT_TICK"
ENV_PROD = "LIURUYAN_PROD"


def _is_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "0").strip().lower() in ("1", "true", "yes", "on")


def _is_prod() -> bool:
    return os.environ.get(ENV_PROD) == "1"


def resolve_tick_config() -> TickGuardConfig:
    """根据环境决定 dev / prod 模式 tick 参数（handover §4.1）

    - dev：5min 间隔（硬下限），日配额 5（方便观察、低烧钱）
    - prod：1h 间隔，日配额 100
    """
    if _is_prod():
        return TickGuardConfig(
            tick_interval_sec=3600,
            daily_tick_quota=DAILY_QUOTA_DEFAULT,
        )
    return TickGuardConfig(
        tick_interval_sec=TICK_HARD_MIN_SEC,
        daily_tick_quota=5,
    )


async def default_on_tick(actor: AgentActor) -> None:
    """默认 tick handler（占位心跳，不调 LLM）

    输出：往 panorama channel 推一条 agent_tick_heartbeat 事件，
    便于 dev 观察 actor 真的在跑。下窗口接 LLM 时替换。
    """
    bus: EventBus = get_default_bus()
    await bus.publish(
        PANORAMA_CHANNEL,
        BusEvent(
            type="agent_tick_heartbeat",
            payload={
                "agent_id": actor.agent_id,
                "at": time.time(),
                "activity_count": actor.activity.count(),
            },
        ),
    )


def _list_agent_ids_safe() -> List[str]:
    """读 agent 列表（失败返回空，不污染 lifespan）"""
    try:
        from backend.agent_config import get_agent_manager
        manager = get_agent_manager()
        agents = manager.list_agents()
        return [a.get("id") for a in agents if isinstance(a, dict) and a.get("id")]
    except Exception:  # noqa: BLE001
        logger.exception("runtime_bootstrap: list_agents failed; skip")
        return []


async def bootstrap_agent_actors(
    *,
    pool: Optional[AgentPool] = None,
    agent_ids: Optional[Iterable[str]] = None,
    on_tick=None,
    tick_config: Optional[TickGuardConfig] = None,
    force_enable: bool = False,
) -> AgentPool:
    """启动所有 agent 的 AgentActor。

    参数：
      pool:          自定义 pool，默认全局单例
      agent_ids:     自定义 id 列表，默认从 agent_config 读
      on_tick:       自定义 tick handler，默认 `default_on_tick`
      tick_config:   自定义 tick 参数，默认按环境推断（dev/prod）
      force_enable:  跳过 LIURUYAN_AGENT_TICK 检查（仅测试用）

    返回：使用的 pool。

    安全：
      - 未启用 → 直接返回空 pool（不创建 actor）
      - 单个 actor start 失败 → 日志，继续启其他
    """
    if pool is None:
        pool = get_agent_pool()

    if not (force_enable or _is_enabled()):
        logger.info(
            "runtime_bootstrap: actor heartbeat disabled (set %s=1 to enable)",
            ENV_FLAG,
        )
        return pool

    ids = list(agent_ids) if agent_ids is not None else _list_agent_ids_safe()
    if not ids:
        logger.info("runtime_bootstrap: no agent found, skip")
        return pool

    cfg = tick_config or resolve_tick_config()
    handler = on_tick if on_tick is not None else default_on_tick

    started: List[str] = []
    for aid in ids:
        try:
            actor = pool.get_or_create(
                AgentActorConfig(agent_id=aid, tick=cfg),
                on_tick=handler,
            )
            await actor.start()
            started.append(aid)
        except Exception:  # noqa: BLE001
            logger.exception(
                "runtime_bootstrap: failed to start actor for agent_id=%s", aid
            )

    logger.info(
        "runtime_bootstrap: started %d/%d actors (interval=%ds quota=%d/day)",
        len(started),
        len(ids),
        cfg.tick_interval_sec,
        cfg.daily_tick_quota,
    )
    return pool


async def shutdown_agent_actors(
    *,
    pool: Optional[AgentPool] = None,
    timeout: float = 5.0,
) -> None:
    """优雅关闭（lifespan shutdown 钩子）"""
    target = pool or get_agent_pool()
    await target.stop_all(timeout=timeout)
