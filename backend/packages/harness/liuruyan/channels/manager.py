"""v2 · E-1b · ChannelManager — adapter 注册表 + 生命周期

设计：_docs/architecture_v2/01_master_plan.md Phase E §1（manager.py）+
02_runtime_core.md § 十一（优雅关闭顺序）

职责：
1. 持有 ``MessageBus`` + ``ChannelStore``
2. 注册 adapter factory（``{channel: AdapterClass}``，依赖注入）
3. 生命周期：``startup()`` 从 store 读 enabled 实例 → 实例化 adapter → start()；
   ``shutdown()`` stop 所有运行中的 adapter
4. 状态查询：``status()`` 给 ``/healthz`` / channels admin / 健康检查用

依赖注入：
adapter 实现层（DingTalkChannel / FeishuChannel）放在 ``backend/channels/``，
依赖具体 IM SDK；harness 不能 import 它们。所以 ChannelManager 通过
``register_factory(channel, AdapterClass)`` 接受 AdapterClass，启动时根据
``ChannelConfig.channel`` 找 factory 创建实例。

inbound consumer loop 不做：
当前不接 LeadAgent——MessageBus.get_inbound 拿到消息后路由到 LeadAgent 是
E-2 的事（要解决 session_mapper → LeadAgent.run_streaming 接通）。本切片
ChannelManager 只管"启动 adapter + 让它把消息 publish 进 MessageBus"，谁
消费 MessageBus 由 gateway 启动时另外接。

异常隔离：
- 单个 adapter start() 失败：标记该 instance ERROR，不阻断其它 adapter
- 单个 adapter stop() 失败：log + 继续 stop 其它（确保 shutdown 路径不卡）
- 找不到 channel 的 factory：log warning + 跳过该 instance（不抛——配置允许
  指向未注册的 channel，方便用户先配后实现）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Type

from ..buses import MessageBus
from .base import (
    ChannelAdapter,
    ChannelInfo,
    ChannelStartError,
    ChannelStatus,
)
from .store import ChannelConfig, ChannelStore

log = logging.getLogger(__name__)


AdapterFactory = Callable[[MessageBus, dict[str, Any]], ChannelAdapter]


class ChannelManager:
    """统一管理所有 channel adapter 实例的生命周期。"""

    def __init__(
        self,
        bus: MessageBus,
        store: ChannelStore | None = None,
    ) -> None:
        self.bus = bus
        self.store = store or ChannelStore()
        self._factories: dict[str, AdapterFactory] = {}
        # config_id → adapter 实例（startup 之后填）
        self._adapters: dict[str, ChannelAdapter] = {}
        # config_id → ChannelInfo（含 ERROR 等不在 _adapters 的状态）
        self._infos: dict[str, ChannelInfo] = {}
        self._started = False
        # 串行化 hot-reload 写操作（add_and_start / stop_and_remove / restart）
        # 避免同 config_id 并发改 store + adapter 字典造成中间态
        self._lock = asyncio.Lock()

    # ── 注册 ────────────────────────────────────────────────────────────

    def register_factory(
        self, channel: str, factory: AdapterFactory | Type[ChannelAdapter]
    ) -> None:
        """注册 channel name → adapter factory（或 class）。

        factory 签名 ``(bus, config_dict) -> ChannelAdapter``；ChannelAdapter
        子类的 ``__init__`` 已经是这个签名，所以直接传 class 即可。
        """
        if not channel:
            raise ValueError("channel name must be non-empty")
        if channel in self._factories:
            log.warning(
                "ChannelManager.register_factory: channel %r already registered, overwriting",
                channel,
            )
        self._factories[channel] = factory

    def registered_channels(self) -> list[str]:
        return sorted(self._factories.keys())

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """从 store 读 enabled 实例 → 按 factory 创建 adapter → start()。

        单个 adapter start 失败标 ERROR，不阻断其它。
        """
        if self._started:
            log.warning("ChannelManager.startup called twice; skipping")
            return
        self._started = True

        for cfg in self.store.list_enabled():
            await self._start_one(cfg)

    async def shutdown(self, timeout_per_adapter: float = 5.0) -> None:
        """stop 所有运行中 adapter。每个有独立超时；不阻塞其它。"""
        if not self._started:
            return
        for config_id, adapter in list(self._adapters.items()):
            await self._stop_one(config_id, adapter, timeout=timeout_per_adapter)
        self._adapters.clear()
        self._started = False

    # ── 状态 ────────────────────────────────────────────────────────────

    def list_status(self) -> list[ChannelInfo]:
        """所有已知 instance 的状态（含 ERROR / STOPPED 的）。"""
        merged: dict[str, ChannelInfo] = dict(self._infos)
        for cid, adapter in self._adapters.items():
            merged[cid] = adapter.info(config_key=cid)
        return list(merged.values())

    def get_adapter(self, config_id: str) -> ChannelAdapter | None:
        return self._adapters.get(config_id)

    def _info_for(self, config_id: str) -> ChannelInfo | None:
        """返 config_id 当前的 ChannelInfo（在 _adapters 优先；fallback _infos）"""
        adapter = self._adapters.get(config_id)
        if adapter is not None:
            return adapter.info(config_key=config_id)
        return self._infos.get(config_id)

    # ── hot-reload（admin routes 用）────────────────────────────────────

    async def add_and_start(
        self, cfg: ChannelConfig, *, stop_timeout: float = 5.0
    ) -> ChannelInfo | None:
        """upsert config + 启动 adapter（如 enabled）。返当前状态。

        如该 id 已有 running adapter，先 stop 旧的（覆盖语义；用户改了配置
        重新创建会走这条路径）。
        """
        async with self._lock:
            old = self._adapters.pop(cfg.id, None)
            if old is not None:
                await self._stop_one(cfg.id, old, timeout=stop_timeout)
            self._infos.pop(cfg.id, None)

            self.store.upsert(cfg)

            if cfg.enabled:
                await self._start_one(cfg)
            else:
                self._infos[cfg.id] = ChannelInfo(
                    channel=cfg.channel,
                    status=ChannelStatus.STOPPED,
                    config_key=cfg.id,
                )
            return self._info_for(cfg.id)

    async def stop_and_remove(
        self, config_id: str, *, stop_timeout: float = 5.0
    ) -> bool:
        """停 adapter（如在跑）+ 删 store + 清内部 dict。返 True 如命中。

        - 即使 store 没这条记录但内存里有 ERROR 残留，也清掉（防御性）
        - 返 False 表示既不在 store 也不在内存——nothing to do
        """
        async with self._lock:
            cfg_in_store = self.store.get(config_id) is not None
            adapter = self._adapters.pop(config_id, None)
            if adapter is not None:
                await self._stop_one(config_id, adapter, timeout=stop_timeout)
            had_info = self._infos.pop(config_id, None) is not None
            removed_from_store = (
                self.store.remove(config_id) if cfg_in_store else False
            )
            return removed_from_store or adapter is not None or had_info

    async def restart(
        self, config_id: str, *, stop_timeout: float = 5.0
    ) -> ChannelInfo | None:
        """stop 旧 adapter + 从 store 重读 cfg + start。配置改后立即生效用。

        - cfg 不在 store → 返 None（调用方应 404）
        - cfg.enabled=False → 只 stop，不 start；返 STOPPED info
        """
        async with self._lock:
            cfg = self.store.get(config_id)
            if cfg is None:
                return None
            old = self._adapters.pop(config_id, None)
            if old is not None:
                await self._stop_one(config_id, old, timeout=stop_timeout)
            self._infos.pop(config_id, None)
            if cfg.enabled:
                await self._start_one(cfg)
            else:
                self._infos[cfg.id] = ChannelInfo(
                    channel=cfg.channel,
                    status=ChannelStatus.STOPPED,
                    config_key=cfg.id,
                )
            return self._info_for(config_id)

    # ── 内部 ────────────────────────────────────────────────────────────

    async def _start_one(self, cfg: ChannelConfig) -> None:
        factory = self._factories.get(cfg.channel)
        if factory is None:
            log.warning(
                "ChannelManager: no factory registered for channel=%r (id=%r); "
                "skipping (configure factory before startup or remove this config)",
                cfg.channel, cfg.id,
            )
            self._infos[cfg.id] = ChannelInfo(
                channel=cfg.channel,
                status=ChannelStatus.ERROR,
                config_key=cfg.id,
                last_error=f"no factory for channel={cfg.channel!r}",
            )
            return

        try:
            adapter = factory(self.bus, cfg.config)
            # 注入 config_key 让 adapter 能在 publish_inbound 时带上自己的
            # config_id，InboundRouter 据此精确路由（多账号场景关键）。
            adapter.config_key = cfg.id
        except Exception as exc:
            log.exception(
                "ChannelManager: factory failed for channel=%r id=%r",
                cfg.channel, cfg.id,
            )
            self._infos[cfg.id] = ChannelInfo(
                channel=cfg.channel,
                status=ChannelStatus.ERROR,
                config_key=cfg.id,
                last_error=f"{type(exc).__name__}: {exc}",
            )
            return

        adapter.status = ChannelStatus.STARTING
        try:
            await adapter.start()
        except ChannelStartError as exc:
            log.warning(
                "ChannelManager: adapter start failed channel=%r id=%r: %s",
                cfg.channel, cfg.id, exc,
            )
            adapter.status = ChannelStatus.ERROR
            adapter.last_error = str(exc)
            self._infos[cfg.id] = adapter.info(config_key=cfg.id)
            return
        except Exception as exc:
            log.exception(
                "ChannelManager: adapter start raised unexpected channel=%r id=%r",
                cfg.channel, cfg.id,
            )
            adapter.status = ChannelStatus.ERROR
            adapter.last_error = f"{type(exc).__name__}: {exc}"
            self._infos[cfg.id] = adapter.info(config_key=cfg.id)
            return

        # adapter.start() 成功——adapter 内部应该已经把 status 设为 RUNNING
        # 防御性兜底：如果 adapter 忘了设，这里替它设
        if adapter.status == ChannelStatus.STARTING:
            adapter.status = ChannelStatus.RUNNING
        self._adapters[cfg.id] = adapter
        self._infos.pop(cfg.id, None)  # 清掉之前可能的 ERROR 状态
        log.info(
            "ChannelManager: started channel=%r id=%r status=%s",
            cfg.channel, cfg.id, adapter.status.value,
        )

    async def _stop_one(
        self, config_id: str, adapter: ChannelAdapter, *, timeout: float
    ) -> None:
        adapter.status = ChannelStatus.STOPPING
        try:
            await asyncio.wait_for(adapter.stop(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning(
                "ChannelManager: adapter stop timeout id=%r (%.1fs)",
                config_id, timeout,
            )
            adapter.status = ChannelStatus.ERROR
            adapter.last_error = f"stop timeout {timeout}s"
        except Exception as exc:
            log.exception(
                "ChannelManager: adapter stop raised id=%r", config_id,
            )
            adapter.status = ChannelStatus.ERROR
            adapter.last_error = f"{type(exc).__name__}: {exc}"
        else:
            adapter.status = ChannelStatus.STOPPED


__all__ = [
    "ChannelManager",
    "AdapterFactory",
]
