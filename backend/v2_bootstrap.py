"""v2 · E-3.5 / E-4'b-1 · v2 channels 系统 lifespan 装配

设计：docs/architecture_v2/02_runtime_core.md § 11（优雅关闭顺序）+
01_master_plan.md Phase E（gateway lifespan wire）

把 ChannelStore + ChannelManager + InboundRouter 三件套接到 main.py lifespan：

```python
# main.py lifespan
async def lifespan(app):
    ...  # EventBus + MessageBus 已装配
    v2_channels = await setup_v2_channels(_message_bus)  # 默认 OFF
    try:
        yield
    finally:
        await teardown_v2_channels(v2_channels)
```

Feature flag：
- ``LIURUYAN_V2_CHANNELS`` 默认 OFF；显式 ``=1/true/yes/on`` 时启用整套
- ``LIURUYAN_V2_CHANNELS_WEB`` 默认 OFF；启用时 ephemeral 注册 WebChannel
  （内置自检通道，不入磁盘 store；进程退出即销毁）

OFF 时返回 None（main.py 直接判 None 跳过 teardown）。这种 flag 风格跟项目
其它 feature flag（例如 FEATURE_CRON_TOOL）一致。

ON 时构造路径：
1. ChannelStore（默认 base path）
2. ChannelManager（持 bus + store；factory 注册留给 channel adapter 实现层）
   - LIURUYAN_V2_CHANNELS_WEB=1 时本步注册 web factory + ephemeral upsert 配置
3. dispatch = make_dispatch(get_agent, get_session_manager)
4. InboundRouter（持 bus + store + dispatch；启动 consumer task）

异常隔离：
- setup 抛 → log + 返 None（main.py 启动不阻断；non-fatal）
- teardown 抛 → log + swallow（不阻断 main.py shutdown）

这跟其它 v2 lifecycle hook（EventBus / TraceJournal）的失败 non-fatal 风格一致。

routes 层访问 web_channel：
通过全局 getter ``get_v2_channels_handle()`` 拿到 handle.web_channel。setup 时
set，teardown 时 clear。这是 FastAPI 项目里"全局服务实例"的标准 pattern。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from liuruyan.buses import MessageBus
from liuruyan.channels import (
    ChannelConfig,
    ChannelManager,
    ChannelStore,
    InboundRouter,
)

log = logging.getLogger(__name__)


_FEATURE_FLAG_ENV = "LIURUYAN_V2_CHANNELS"
_WEB_CHANNEL_FLAG_ENV = "LIURUYAN_V2_CHANNELS_WEB"
_EPHEMERAL_WEB_CONFIG_ID = "_builtin_web"


@dataclass(slots=True)
class V2ChannelsHandle:
    """setup_v2_channels 的返回值；teardown 时一次传回。

    ``web_channel`` 仅在 LIURUYAN_V2_CHANNELS_WEB=1 时非 None；routes 层用它
    做 publish_inbound + stream。
    """

    store: ChannelStore
    manager: ChannelManager
    router: InboundRouter
    web_channel: Any | None = None


# 全局 handle（routes 层用 getter 拿）
_global_handle: V2ChannelsHandle | None = None


def get_v2_channels_handle() -> V2ChannelsHandle | None:
    """routes 层入口：拿当前 v2 channels handle（None 表 v2 未启用）。"""
    return _global_handle


def is_enabled() -> bool:
    """``LIURUYAN_V2_CHANNELS`` env 判 enabled。"""
    raw = (os.getenv(_FEATURE_FLAG_ENV) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def is_web_channel_enabled() -> bool:
    """``LIURUYAN_V2_CHANNELS_WEB`` env 判 web channel 启用。"""
    raw = (os.getenv(_WEB_CHANNEL_FLAG_ENV) or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


async def setup_v2_channels(
    bus: MessageBus,
    *,
    agent_resolver: Any | None = None,
    session_manager_resolver: Any | None = None,
) -> V2ChannelsHandle | None:
    """构造 channels 系统并启动。

    Feature flag OFF（默认） → 返 None（main.py 跳过 teardown）。

    ``agent_resolver`` / ``session_manager_resolver`` 可注入用于测试；
    None 时用 backend.agent.get_agent / backend.session.get_session_manager。

    任何异常 swallow + log + 返 None（启动不阻断 main.py）。
    """
    global _global_handle

    if not is_enabled():
        log.info(
            "[v2:channels] %s OFF; skipping channel system setup",
            _FEATURE_FLAG_ENV,
        )
        return None

    try:
        # 1. dispatch factory 用 v1 backend.agent + backend.session（默认）
        from backend.channels import make_dispatch

        if agent_resolver is None:
            from backend.agent import get_agent as agent_resolver  # type: ignore[assignment]
        if session_manager_resolver is None:
            from backend.session import get_session_manager as session_manager_resolver  # type: ignore[assignment]

        dispatch = make_dispatch(agent_resolver, session_manager_resolver)

        # 2. ChannelStore（{base}/channels/store.json；自动 load）
        store = ChannelStore()
        store.load()
        log.info(
            "[v2:channels] ChannelStore loaded: %d config(s) total, %d enabled",
            len(store.list_configs()), len(store.list_enabled()),
        )

        # 3. ChannelManager（无 adapter factory 时不启动任何 adapter；后续
        #    backend/channels/dingtalk.py 等实现层启动时再注册）
        manager = ChannelManager(bus, store)

        # 3.0. E-4: 注册 dingtalk factory（如 store 里有 dingtalk 配置）
        # lazy import：不装 dingtalk_stream SDK 的环境不会因 import 失败
        if any(c.channel == "dingtalk" for c in store.list_configs()):
            try:
                from backend.channels import _lazy_dingtalk
                manager.register_factory("dingtalk", _lazy_dingtalk())
                log.info("[v2:channels] DingTalkChannel factory registered")
            except Exception as e:
                log.exception(
                    "[v2:channels] DingTalkChannel factory registration failed: %s", e,
                )

        # 3.0.1. G'-3: 注册 matrix factory（如 store 里有 matrix 配置）
        # lazy import：不装 matrix-nio SDK 的环境不会因 import 失败
        if any(c.channel == "matrix" for c in store.list_configs()):
            try:
                from backend.channels import _lazy_matrix
                manager.register_factory("matrix", _lazy_matrix())
                log.info("[v2:channels] MatrixChannel factory registered")
            except Exception as e:
                log.exception(
                    "[v2:channels] MatrixChannel factory registration failed: %s", e,
                )

        # 3.0.2. G-feishu: 注册 feishu factory（如 store 里有 feishu 配置）
        # lazy import：不装 lark-oapi SDK 的环境不会因 import 失败
        if any(c.channel == "feishu" for c in store.list_configs()):
            try:
                from backend.channels import _lazy_feishu
                manager.register_factory("feishu", _lazy_feishu())
                log.info("[v2:channels] FeishuChannel factory registered")
            except Exception as e:
                log.exception(
                    "[v2:channels] FeishuChannel factory registration failed: %s", e,
                )

        # 3.0.3. G-wechat: 注册 wechat factory（如 store 里有 wechat 配置）
        if any(c.channel == "wechat" for c in store.list_configs()):
            try:
                from backend.channels import _lazy_wechat
                manager.register_factory("wechat", _lazy_wechat())
                log.info("[v2:channels] WeChatChannel factory registered")
            except Exception as e:
                log.exception(
                    "[v2:channels] WeChatChannel factory registration failed: %s", e,
                )

        # 3'. ephemeral 注册 WebChannel（自检通道）：LIURUYAN_V2_CHANNELS_WEB=1
        # 时本进程注册 web factory + 临时 upsert 一条 web ChannelConfig；不
        # 持久化到磁盘（``store.upsert`` 会写盘——这里我们绕过 store，直接
        # 操作内存 ``_configs``，避免污染用户的 channels/store.json）。进程
        # 退出即销毁。
        web_channel_enabled_for_run = is_web_channel_enabled()
        if web_channel_enabled_for_run:
            from backend.channels import WebChannel
            manager.register_factory("web", WebChannel)
            # 直接操作内存：避免 upsert 写盘 + 避免重启时残留
            if _EPHEMERAL_WEB_CONFIG_ID not in store._configs:
                store._configs[_EPHEMERAL_WEB_CONFIG_ID] = ChannelConfig(
                    id=_EPHEMERAL_WEB_CONFIG_ID,
                    channel="web",
                    config={"default_agent": _resolve_default_web_agent()},
                    enabled=True,
                )
            log.info(
                "[v2:channels] ephemeral WebChannel registered (config_id=%s)",
                _EPHEMERAL_WEB_CONFIG_ID,
            )

        await manager.startup()
        if not manager.registered_channels():
            log.info(
                "[v2:channels] ChannelManager started; no adapter factory "
                "registered yet (待 E-4 dingtalk 接入再 mgr.register_factory(...))"
            )

        # 4. InboundRouter（消费 MessageBus → dispatch）
        router = InboundRouter(bus, store, dispatch=dispatch)
        await router.start()

        # 5. 拿 web_channel 实例（如启用）暴露给 routes 层
        web_channel = None
        if web_channel_enabled_for_run:
            web_channel = manager.get_adapter(_EPHEMERAL_WEB_CONFIG_ID)
            if web_channel is None:
                # adapter 启动失败，但其它 channels 仍可工作
                log.warning(
                    "[v2:channels] ephemeral WebChannel failed to start; "
                    "routes 层 stream/inbound 不可用"
                )

        handle = V2ChannelsHandle(
            store=store,
            manager=manager,
            router=router,
            web_channel=web_channel,
        )
        _global_handle = handle
        log.info(
            "[v2:channels] setup complete: %d channel(s) running, "
            "InboundRouter consumer started, web_channel=%s",
            len(manager.list_status()),
            "ON" if web_channel else "OFF",
        )
        return handle
    except Exception as e:
        log.exception("[v2:channels] setup failed (non-fatal): %s", e)
        _global_handle = None
        return None


def _resolve_default_web_agent() -> str:
    """LIURUYAN_V2_CHANNELS_WEB_DEFAULT_AGENT env 优先；fallback 'default'。

    'default' 是柳如烟（如烟）的 agent_id；template_id 是 'liuruyan'。
    SessionManager.create_session 校验 agent_id 必须存在 → 用 'default' 兜底。
    """
    raw = (os.getenv("LIURUYAN_V2_CHANNELS_WEB_DEFAULT_AGENT") or "").strip()
    return raw or "default"


async def teardown_v2_channels(
    handle: V2ChannelsHandle | None,
    *,
    router_drain_timeout: float = 10.0,
    manager_shutdown_timeout_per_adapter: float = 5.0,
) -> None:
    """优雅关闭：先停 router 接收 → drain handle tasks → stop 所有 adapter。

    跟 02_runtime_core § 11 优雅关闭顺序一致：
    "先停 channels（停接收）→ drain MessageBus → shutdown_pools → stop FastAPI"。
    本函数只管 channels 内部部分（前两步）。
    """
    global _global_handle

    if handle is None:
        return

    # 1. 停 InboundRouter（先停接收，再 drain 已收消息）
    try:
        await handle.router.stop(drain_timeout=router_drain_timeout)
    except Exception:
        log.exception("[v2:channels] router.stop failed")

    # 2. 停 ChannelManager（所有 adapter 自报 stop）
    try:
        await handle.manager.shutdown(
            timeout_per_adapter=manager_shutdown_timeout_per_adapter,
        )
    except Exception:
        log.exception("[v2:channels] manager.shutdown failed")

    # 3. 清全局 handle，让 routes 层再请求时 503
    _global_handle = None

    log.info("[v2:channels] teardown complete")


__all__ = [
    "V2ChannelsHandle",
    "get_v2_channels_handle",
    "is_enabled",
    "is_web_channel_enabled",
    "setup_v2_channels",
    "teardown_v2_channels",
]
