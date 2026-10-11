"""Adapters：把外部平台的消息流接进/送出 EventBus

抽象基类在 base.py，具体实现按平台分文件（ws.py / wechat.py / feishu.py）。
"""

from backend.core.adapters.base import (
    DeliveryTarget,
    InboundAdapter,
    OutboundAdapter,
)

__all__ = [
    "DeliveryTarget",
    "InboundAdapter",
    "OutboundAdapter",
]
