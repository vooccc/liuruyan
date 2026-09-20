"""liuruyan.channels — IM 平台 adapter 协议层 + 注册表 + store（核）。

具体 adapter 实现（dingtalk / feishu / wecom / wechat）放在 backend/channels/，
它们 import 本模块拿协议；harness 永不 import 具体 adapter（boundary 红线）。

落地：
- E-0b: ChannelAdapter ABC（4 个钩子：start / stop / send / receive） +
        ChannelInfo / ChannelStatus / ChannelStartError / ChannelSendError
- E-1a: ChannelStore（{base}/channels/store.json 原子读写）
- E-1b: ChannelManager（factory 注册 + adapter 生命周期 + 状态查询）
- E-2a: InboundRouter（MessageBus consumer + session_mapper + dispatch DI 胶水）
"""

from .base import (
    ChannelAdapter,
    ChannelInfo,
    ChannelSendError,
    ChannelStartError,
    ChannelStatus,
)
from .manager import AdapterFactory, ChannelManager
from .router import DispatchFunc, InboundRouter
from .store import (
    CHANNEL_STORE_SCHEMA_VERSION,
    ChannelConfig,
    ChannelStore,
    channels_dir,
    store_path,
)

__all__ = [
    "AdapterFactory",
    "CHANNEL_STORE_SCHEMA_VERSION",
    "ChannelAdapter",
    "ChannelConfig",
    "ChannelInfo",
    "ChannelManager",
    "ChannelSendError",
    "ChannelStartError",
    "ChannelStatus",
    "ChannelStore",
    "DispatchFunc",
    "InboundRouter",
    "channels_dir",
    "store_path",
]
