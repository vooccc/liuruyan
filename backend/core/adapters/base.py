"""InboundAdapter / OutboundAdapter 抽象基类（handover §4.5）

抽象目标：让 AgentActor 不知道平台、不知道传输层。

Phase 1 实现：WebSocketInboundAdapter / WebSocketOutboundAdapter（在 adapters/ws.py）
Phase 2 实现：WeChat / Feishu Adapter
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from backend.core.inbox_message import BusEvent


@dataclass
class DeliveryTarget:
    """OutboundAdapter 的投递目标（platform 无关）"""

    platform: str               # "web" / "wechat" / "feishu" / ...
    target_id: str              # WS 客户端 ID / 用户 openid / 群 ID 等
    channel: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class InboundAdapter(abc.ABC):
    """接入消息源（HTTP webhook / WebSocket / 平台 SDK），统一为内部 InboxMessage"""

    @property
    @abc.abstractmethod
    def platform(self) -> str:
        ...

    @abc.abstractmethod
    async def start(self) -> None:
        ...

    @abc.abstractmethod
    async def stop(self) -> None:
        ...


class OutboundAdapter(abc.ABC):
    """把 EventBus 事件转发到外部目的地"""

    @abc.abstractmethod
    def supports_platform(self, platform: str) -> bool:
        ...

    @abc.abstractmethod
    async def deliver(self, event: BusEvent, target: DeliveryTarget) -> None:
        ...
