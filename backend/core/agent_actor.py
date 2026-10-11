"""AgentActor：per-agent 全局常驻的 inbox + 心跳调度（handover §4.2）

设计要点：
1. **不替换现有 Agent**：AgentActor 是一层包装，持有 agent_id 引用，
   实际 LLM 调用由调用方注入 handler（避免本窗口产生跨模块依赖）。
2. **inbox + tick 复用单 task**：用 `asyncio.wait_for(inbox.get(), timeout=delay)`
   实现"有消息处理消息，超时即 tick"。
3. **生命周期**：start/stop 显式调用；崩溃由 watchdog 控制（_run 捕获所有异常并日志）。
4. **本窗口不接业务**：actor 只在单元测试中验证 inbox + tick + 安全阀；
   下个窗口接到 /chat 路径。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from backend.core.inbox_message import InboxMessage
from backend.core.safety.tick_guard import (
    TickGuard,
    TickGuardConfig,
    get_tick_guard,
)
from backend.core.tick_scheduler import (
    ActivityCounter,
    compute_next_delay,
)

logger = logging.getLogger(__name__)


# ─────── 处理器签名 ─────────

# 处理 inbox 消息的回调，返回 None。失败会被 actor 捕获并日志。
MessageHandler = Callable[["AgentActor", InboxMessage], Awaitable[None]]
TickHandler = Callable[["AgentActor"], Awaitable[None]]


@dataclass
class AgentActorConfig:
    agent_id: str
    tick: TickGuardConfig = field(default_factory=TickGuardConfig)
    activity_window_sec: float = 3600.0
    message_timeout_sec: float = 60.0   # 单条消息处理超时（watchdog）
    inbox_max_size: int = 0             # 0 = unbounded（asyncio.Queue 默认）


class AgentActor:
    """per-agent 常驻 actor"""

    def __init__(
        self,
        config: AgentActorConfig,
        *,
        on_message: Optional[MessageHandler] = None,
        on_tick: Optional[TickHandler] = None,
        guard: Optional[TickGuard] = None,
    ) -> None:
        self.config = config
        self.agent_id = config.agent_id
        self.inbox: asyncio.Queue[InboxMessage] = asyncio.Queue(
            maxsize=config.inbox_max_size
        )
        self.joined_sessions: Set[str] = set()
        self.joined_rooms: Set[str] = set()
        self.activity = ActivityCounter(window_sec=config.activity_window_sec)
        self.last_activity_at: float = time.time()

        self._guard = guard or get_tick_guard()
        self._on_message = on_message
        self._on_tick = on_tick
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._stop_event: Optional[asyncio.Event] = None

    # ─────── 生命周期 ─────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name=f"actor:{self.agent_id}")

    async def stop(self, *, timeout: float = 5.0) -> None:
        if not self._running:
            return
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "AgentActor[%s]: stop timeout, cancelling", self.agent_id
                )
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._task = None
        self._stop_event = None

    @property
    def running(self) -> bool:
        return self._running

    # ─────── 主循环 ─────────

    async def _run(self) -> None:
        try:
            while self._running:
                delay = self._next_tick_delay()
                try:
                    msg = await asyncio.wait_for(self.inbox.get(), timeout=delay)
                except asyncio.TimeoutError:
                    await self._handle_tick()
                    continue
                except asyncio.CancelledError:
                    raise
                else:
                    await self._handle_message(msg)
        except asyncio.CancelledError:
            logger.info("AgentActor[%s]: cancelled", self.agent_id)
            raise
        except Exception:  # noqa: BLE001
            logger.exception("AgentActor[%s]: crashed in _run", self.agent_id)
            self._running = False

    def _next_tick_delay(self) -> float:
        from backend.core.safety.tick_guard import TickGuard as _TG

        normalized = _TG.normalize_interval(self.config.tick.tick_interval_sec)
        return compute_next_delay(
            activity_count=self.activity.count(),
            base_interval_sec=normalized,
            min_sec=self.config.tick.tick_min_sec,
            max_sec=self.config.tick.tick_max_sec,
        )

    # ─────── 消息处理 ─────────

    async def _handle_message(self, msg: InboxMessage) -> None:
        self.activity.record()
        self.last_activity_at = time.time()

        if self._on_message is None:
            logger.debug(
                "AgentActor[%s]: no on_message handler, drop msg %s",
                self.agent_id,
                msg.msg_id,
            )
            return

        try:
            await asyncio.wait_for(
                self._on_message(self, msg),
                timeout=self.config.message_timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "AgentActor[%s]: message %s handler timeout (>%.1fs)",
                self.agent_id,
                msg.msg_id,
                self.config.message_timeout_sec,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "AgentActor[%s]: message %s handler raised",
                self.agent_id,
                msg.msg_id,
            )

    async def _handle_tick(self) -> None:
        if not self._guard.check_can_tick(self.agent_id, self.config.tick):
            return

        if self._on_tick is None:
            self._guard.record_tick(self.agent_id)
            return

        try:
            await self._on_tick(self)
        except Exception:  # noqa: BLE001
            logger.exception("AgentActor[%s]: tick handler raised", self.agent_id)
        finally:
            self._guard.record_tick(self.agent_id)


# ─────── AgentPool（process 单例）─────────

class AgentPool:
    """全局 actor 池：agent_id → AgentActor

    本窗口仅作为基础设施，不与 /chat 路径联通。
    """

    def __init__(self) -> None:
        self._actors: Dict[str, AgentActor] = {}

    def get(self, agent_id: str) -> Optional[AgentActor]:
        return self._actors.get(agent_id)

    def get_or_create(
        self,
        config: AgentActorConfig,
        *,
        on_message: Optional[MessageHandler] = None,
        on_tick: Optional[TickHandler] = None,
    ) -> AgentActor:
        existing = self._actors.get(config.agent_id)
        if existing is not None:
            return existing
        actor = AgentActor(
            config,
            on_message=on_message,
            on_tick=on_tick,
        )
        self._actors[config.agent_id] = actor
        return actor

    def all_actors(self) -> Dict[str, AgentActor]:
        return dict(self._actors)

    async def stop_all(self, *, timeout: float = 5.0) -> None:
        actors = list(self._actors.values())
        for actor in actors:
            await actor.stop(timeout=timeout)

    def reset_for_testing(self) -> None:
        self._actors.clear()


_pool_singleton: Optional[AgentPool] = None


def get_agent_pool() -> AgentPool:
    global _pool_singleton
    if _pool_singleton is None:
        _pool_singleton = AgentPool()
    return _pool_singleton


def reset_pool_for_testing() -> None:
    global _pool_singleton
    _pool_singleton = None
