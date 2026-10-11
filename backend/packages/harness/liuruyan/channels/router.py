"""v2 · E-2a · InboundRouter — MessageBus consumer ↔ dispatch 胶水层

设计：docs/architecture_v2/01_master_plan.md Phase E + 02_runtime_core.md § 三

职责：消费 ``MessageBus.get_inbound()`` 拿到的 ``InboundMessage``，按 channel
配置解析 default_agent → 调 ``session_mapper`` 拿 SessionKey → 调 dispatch
跑 chain → 把 dispatch yield 的 OutboundMessage 反推回 ``MessageBus.publish_outbound``。

dispatch 是 backend 注入的（依赖注入解决 boundary 红线）。signature：

```python
DispatchFunc = Callable[[InboundMessage, SessionKey], AsyncIterator[OutboundMessage]]
```

dispatch 内部由 backend 实现：
1. 加载 session 历史（v1 SessionManager）
2. append user msg 到 messages
3. 调 ``LeadAgent.run_streaming(messages, ctx)`` 跑 chain
4. yield event → 转 OutboundMessage
5. 落盘 assistant msg 到 session

InboundRouter 不知道 LeadAgent / SessionManager / 任何 backend 细节——只看
协议（dispatch + InboundMessage + SessionKey + OutboundMessage）。

dispatch=None 时 metric_only 占位（log + drop），跟 mw 06/09 半激活模式对称。
方便先把骨架 + 测试 + lifecycle 跑通，后续再接 backend 实现。

并发模型：
- 一个 consumer task 跑 while loop（``_consume_loop``）
- 每条消息 spawn 独立 ``_handle_message`` task（不阻塞 consumer）——这样慢
  消息不会堆积住后面的快消息
- 单条消息异常 swallow + log（log + 不影响 consumer loop 继续）

生命周期：
- ``start()``：spawn consumer task，非阻塞返回
- ``stop()``：cancel consumer task + 等已 spawn 的 handle task 完成（带 timeout）
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Awaitable, Callable

from ..buses import InboundMessage, MessageBus, OutboundMessage
from ..multi_role import SessionKey, map_session
from .store import ChannelConfig, ChannelStore

log = logging.getLogger(__name__)


DispatchFunc = Callable[
    [InboundMessage, SessionKey],
    AsyncIterator[OutboundMessage],
]


class InboundRouter:
    """单 consumer + per-message handle task 模型。"""

    def __init__(
        self,
        bus: MessageBus,
        store: ChannelStore,
        *,
        dispatch: DispatchFunc | None = None,
        default_agent_fallback: str = "default",
    ) -> None:
        """``dispatch=None`` → metric_only 占位（log + drop）；
        生产用法由 backend 注入实际 dispatch。

        ``default_agent_fallback`` 在 channel config 没填 default_agent 且
        store 里找不到该 channel 配置时兜底——确保 InboundRouter 不会因为配置
        缺失就 crash 整个 consumer loop。
        """
        self.bus = bus
        self.store = store
        self.dispatch = dispatch
        self.default_agent_fallback = default_agent_fallback

        self._consumer_task: asyncio.Task | None = None
        self._handle_tasks: set[asyncio.Task] = set()
        self._running = False

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """spawn consumer task，立即返回。"""
        if self._running:
            log.warning("InboundRouter.start called twice; skipping")
            return
        self._running = True
        self._consumer_task = asyncio.create_task(
            self._consume_loop(), name="inbound_router_consume"
        )

    async def stop(self, *, drain_timeout: float = 10.0) -> None:
        """优雅关闭：cancel consumer + 等已 spawn 的 handle task 完成（带 timeout）。"""
        if not self._running:
            return
        self._running = False

        # 1. cancel consumer loop
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("InboundRouter consumer task crashed during stop")
            self._consumer_task = None

        # 2. 等已 spawn 的 handle tasks 完成
        if self._handle_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._handle_tasks, return_exceptions=True),
                    timeout=drain_timeout,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "InboundRouter.stop: %d handle task(s) still running after %.1fs; "
                    "cancelling",
                    sum(1 for t in self._handle_tasks if not t.done()),
                    drain_timeout,
                )
                for t in self._handle_tasks:
                    if not t.done():
                        t.cancel()

    # ── consumer loop ──────────────────────────────────────────────────

    async def _consume_loop(self) -> None:
        """从 MessageBus 拿 inbound，spawn handle task 处理。

        异常隔离：handle task 的异常不进 loop（自己 swallow）；loop 自身崩溃
        log + 短 backoff 重启（防 hot loop）。
        """
        while self._running:
            try:
                msg = await self.bus.get_inbound()
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("InboundRouter consume loop get_inbound failed")
                await asyncio.sleep(0.5)
                continue

            # spawn 独立 task；不阻塞 consumer loop
            t = asyncio.create_task(
                self._handle_message(msg),
                name=f"inbound_handle_{msg.channel}_{msg.chat_id}",
            )
            self._handle_tasks.add(t)
            t.add_done_callback(self._handle_tasks.discard)

    # ── 单条消息处理 ────────────────────────────────────────────────────

    async def _handle_message(self, msg: InboundMessage) -> None:
        """单条消息：channel config 解析 → session_mapper → dispatch → publish_outbound。

        所有异常 swallow + log（不影响 consumer loop）。
        """
        try:
            # 优先按 metadata.config_id 精确匹配（adapter 自报家门），fallback
            # 到"取该 channel 第一个 enabled"——多账号场景必须靠精确匹配，
            # 否则两个 dingtalk 配置时所有消息都被路由到第一个的 default_agent。
            cfg = self._resolve_channel_config(msg.channel, msg.metadata.get("config_id"))
            agent_id = self._extract_agent_id(cfg)
            log.info(
                "InboundRouter: msg_config_id=%r resolved cfg.id=%r agent_id=%r "
                "channel=%s chat=%s",
                msg.metadata.get("config_id"),
                cfg.id if cfg else None,
                agent_id,
                msg.channel, msg.chat_id,
            )
            sk = map_session(
                channel=msg.channel,
                chat_id=msg.chat_id,
                platform_user_id=msg.user_id,
                agent_id=agent_id,
                thread_ts=msg.thread_ts,
                topic_id=msg.topic_id,
            )

            if self.dispatch is None:
                # metric_only 占位
                log.info(
                    "InboundRouter (metric_only): would dispatch channel=%s "
                    "chat=%s user=%s sk.session=%s sk.agent=%s",
                    msg.channel, msg.chat_id, msg.user_id,
                    sk.session_id, sk.agent_id,
                )
                return

            async for outbound in self.dispatch(msg, sk):
                if outbound is None:  # 防 dispatch yield None
                    continue
                try:
                    await self.bus.publish_outbound(outbound)
                except Exception:
                    log.exception(
                        "InboundRouter publish_outbound failed channel=%s chat=%s",
                        outbound.channel, outbound.chat_id,
                    )
        except Exception:
            log.exception(
                "InboundRouter._handle_message failed channel=%s chat=%s user=%s",
                msg.channel, msg.chat_id, msg.user_id,
            )

    # ── 内部 helpers ────────────────────────────────────────────────────

    def _resolve_channel_config(
        self, channel: str, config_id: str | None = None,
    ) -> ChannelConfig | None:
        """优先按 ``config_id`` 精确匹配（adapter 自报家门）；fallback "取该 channel
        第一个 enabled"——精确匹配是多账号场景的正确路由方式，fallback 仅为
        老 adapter（未携带 config_id metadata）兜底。
        """
        try:
            if config_id:
                cfg = self.store.get(config_id)
                if cfg is not None and cfg.channel == channel:
                    return cfg
                log.warning(
                    "InboundRouter: config_id=%r not found / channel mismatch "
                    "(msg.channel=%r); falling back to first enabled",
                    config_id, channel,
                )
            for cfg in self.store.list_by_channel(channel):
                if cfg.enabled:
                    return cfg
        except Exception:
            log.exception("InboundRouter._resolve_channel_config failed channel=%s", channel)
        return None

    def _extract_agent_id(self, cfg: ChannelConfig | None) -> str:
        if cfg is None:
            return self.default_agent_fallback
        agent = cfg.config.get("default_agent") if cfg.config else None
        if not agent or not isinstance(agent, str):
            return self.default_agent_fallback
        return agent


__all__ = [
    "DispatchFunc",
    "InboundRouter",
]
