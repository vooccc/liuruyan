"""backend/channels — IM 平台 adapter 实现层 + dispatch backend 实现。

按 v2 boundary 红线（docs/architecture_v2/01_master_plan.md § "harness 永不
import gateway/channels"），具体 IM SDK 接入（dingtalk-stream / lark-oapi）和
backend dispatch 实现都放在本包；harness 提供协议（harness.channels.base /
harness.channels.router 的 DispatchFunc）。

落地：
- E-3a: dispatch.make_dispatch(agent_resolver, session_manager_resolver) factory
- E-4'a: WebChannel — HTTP+SSE 风格的参考 adapter（不依赖外部 SDK）
"""

from .dispatch import make_dispatch
from .web import WebChannel

# DingTalkChannel 是 lazy import——不装 dingtalk_stream SDK 时也能 import
# 本模块（只是 register_factory 时 SDK 缺失会 ChannelStartError）
def _lazy_dingtalk():
    from .dingtalk import DingTalkChannel
    return DingTalkChannel


# MatrixChannel 同款 lazy——不装 matrix-nio 时也能 import 本模块
def _lazy_matrix():
    from .matrix import MatrixChannel
    return MatrixChannel


# FeishuChannel 同款 lazy——不装 lark-oapi 时也能 import 本模块
def _lazy_feishu():
    from .feishu import FeishuChannel
    return FeishuChannel


# WeChatChannel 同款 lazy
def _lazy_wechat():
    from .wechat import WeChatChannel
    return WeChatChannel


__all__ = [
    "WebChannel",
    "make_dispatch",
    "_lazy_dingtalk",
    "_lazy_matrix",
    "_lazy_feishu",
    "_lazy_wechat",
]
