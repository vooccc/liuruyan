"""v2 · E-0b · ChannelAdapter — IM 平台适配器抽象协议

设计：_docs/architecture_v2/01_master_plan.md Phase E

每个 IM 平台（dingtalk / feishu / wecom / wechat / web）实现一个 ChannelAdapter
子类，提供 4 个钩子：

- ``start()``：建立连接（dingtalk-stream WSS / lark callback / ...）
- ``stop()``：断开连接 + 清理
- ``send(msg: OutboundMessage)``：把柳如烟 → 平台
- ``receive_loop()``：从平台 → 柳如烟，put 进 MessageBus.publish_inbound

实现层不在 harness 里——按 boundary 红线（_docs/architecture_v2/01_master_plan.md
§ "harness 永不 import gateway/channels"），具体 adapter 实现放在 backend/channels/
（dingtalk.py / feishu.py / ...），它们 import harness.channels.base 拿协议。

本文件只定义协议 + 数据契约（ChannelAdapter ABC + ChannelStatus），不依赖
任何 IM SDK，纯 stdlib——这样 harness 仍是"核"，可独立打包。

每个 adapter 行为约束：

1. ``start()`` 应该是非阻塞返回——如果有长跑后台任务（如 WSS 监听循环），
   adapter 自己 spawn ``asyncio.Task``；调用方（ChannelManager）不应该期待
   ``start()`` 阻塞到平台连接成功
2. ``send()`` 必须 await 完成（成功 / 失败抛 ChannelSendError）；调用方据此
   决定是否重发
3. 平台连接异常（断网 / token 过期）adapter 自己重连——不让上层感知
4. ``stop()`` 应该 idempotent（多次调不出错）

异常隔离：
- ChannelSendError：send 时上抛，让调用方决定 retry / dead letter
- ChannelStartError：start 失败时抛，ChannelManager 标记该 channel "down"
- 其它异常 adapter 内部 swallow + log（不让一个平台抽风影响其它）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..buses import MessageBus, OutboundMessage


class ChannelStatus(str, Enum):
    """Channel adapter 生命周期状态（用于 /channels admin / 健康检查）。"""

    INIT = "init"           # 刚 __init__ 完，未 start
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"         # start 失败 / 致命异常


class ChannelStartError(Exception):
    """Channel 启动失败（如 dingtalk client_id 错 / 网络不通）。"""


class ChannelSendError(Exception):
    """单条 OutboundMessage 发送失败。"""


@dataclass(slots=True)
class ChannelInfo:
    """ChannelManager 看 adapter 状态用。"""

    channel: str            # "dingtalk" / "feishu" / ...
    status: ChannelStatus
    config_key: str = ""    # 该实例的配置 key（多账号场景区分）
    last_error: str = ""    # ERROR 时的最近异常 str
    metadata: dict[str, Any] = field(default_factory=dict)


class ChannelAdapter(ABC):
    """IM 平台适配器抽象基类。

    每个具体 adapter（DingTalkChannel / FeishuChannel / ...）实现 4 个钩子。
    协议层（本类）+ MessageBus 已经在 harness；具体实现在 backend/channels/。
    """

    #: 子类必须设置：平台名（"dingtalk" / "feishu" / ...）
    channel: str = ""

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        """``config`` 是该 channel 实例的配置（client_id / secret / 默认 agent / ...）；
        具体 schema 由子类定义。``bus`` 是共享的 MessageBus。"""
        if not self.channel:
            raise TypeError(
                f"{self.__class__.__name__} must set class-level `channel` attribute"
            )
        self.bus = bus
        self.config = config
        self.status: ChannelStatus = ChannelStatus.INIT
        self.last_error: str = ""
        # 由 ChannelManager._start_one 在 factory 之后注入；adapter 在
        # publish_inbound 时把它塞进 InboundMessage.metadata["config_id"]
        # 让 InboundRouter 精确路由到对应 ChannelConfig（多账号场景关键）。
        self.config_key: str = ""

    @abstractmethod
    async def start(self) -> None:
        """建立平台连接 + 启动 receive loop。

        正常返回 → ``self.status = RUNNING``。
        失败 → 抛 ChannelStartError；调用方应捕获并把 status 标 ERROR。
        """

    @abstractmethod
    async def stop(self) -> None:
        """断开平台连接 + 清理后台 task。

        必须 idempotent。返回时 ``self.status = STOPPED``。
        """

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """把柳如烟产出的消息发到 IM 平台。

        失败抛 ChannelSendError；调用方决定重发 / dead letter。
        """

    def info(self, config_key: str = "") -> ChannelInfo:
        """返当前 adapter 状态（ChannelManager 报告 / 健康检查用）。"""
        return ChannelInfo(
            channel=self.channel,
            status=self.status,
            config_key=config_key or self.config_key,
            last_error=self.last_error,
        )


__all__ = [
    "ChannelAdapter",
    "ChannelInfo",
    "ChannelStatus",
    "ChannelStartError",
    "ChannelSendError",
]
