"""v2 · E-4 · DingTalkChannel — 钉钉 Stream 模式 IM 接入

设计：_docs/architecture_v2/05_im_channels.md § 七 ADDING_A_PLATFORM

钉钉 Stream Mode（基于 ``dingtalk-stream`` SDK）：
- WSS 长连接（不需要公网 IP / webhook 暴露）
- 鉴权：client_id (AppKey) + client_secret (AppSecret)
- 接收：``ChatbotMessage`` 群 @机器人 / 单聊 消息
- 回复：``reply_markdown(title, text, incoming_message)``

config schema（``ChannelConfig.config``）：
- ``client_id``: str       钉钉应用 AppKey
- ``client_secret``: str   钉钉应用 AppSecret
- ``default_agent``: str   该 channel 默认路由到哪个 agent_id

平台行为约束（来自 dingtalk-stream SDK）：
- ``client.start()`` 是 async；阻塞循环 + 自动重连（断线时 sleep 10s 后重试）
- handler ``process(message)`` 必须 async；返 ``(AckMessage.STATUS_OK, '')``
  让钉钉知道处理成功
- handler 处理时间过长会让钉钉超时——所以 handler 内部只做"publish_inbound 进
  MessageBus"，dispatch + LLM 调用走 InboundRouter consumer 路径异步处理
- 流式回复钉钉不直接支持——本 adapter 把所有 OutboundMessage 缓冲累积，
  ``is_final=True`` 时 reply_markdown 一次性发完整内容

设计要点（vs WebChannel）：
- WebChannel 是被动接（routes 转 HTTP）；DingTalk 是主动连 WSS
- 流式 chunks 在钉钉 IM 没原生支持——只能合并一次性发
- ``send`` 时按 ``conversation_id`` 找原始 incoming message（last_incoming
  per chat_id），用它做 reply target

异常路径：
- start() WSS 连不上 → 抛 ChannelStartError；ChannelManager 标 ERROR
- send() 没找到 incoming message → 跳过 + log warning（极罕见，理论上 chat
  必须先有 inbound 才有 outbound）
- send() reply_markdown 抛 → ChannelSendError 上抛
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from liuruyan.buses import InboundMessage, OutboundMessage
from liuruyan.channels import (
    ChannelAdapter,
    ChannelSendError,
    ChannelStartError,
    ChannelStatus,
)

log = logging.getLogger(__name__)


class DingTalkChannel(ChannelAdapter):
    """钉钉 Stream Mode adapter。"""

    channel = "dingtalk"

    def __init__(self, bus: Any, config: dict[str, Any]) -> None:
        super().__init__(bus, config)

        client_id = config.get("client_id") or ""
        client_secret = config.get("client_secret") or ""
        if not client_id or not client_secret:
            raise ChannelStartError(
                "DingTalkChannel requires client_id + client_secret in config"
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._default_agent = str(config.get("default_agent") or "liuruyan")

        # SDK 状态
        self._client: Any | None = None
        self._client_task: asyncio.Task | None = None
        self._unsub: Any | None = None

        # outbound 流式合并：每 chat 累积 chunks，is_final 时一次性 reply
        # key: chat_id, value: {"text": str, "incoming": ChatbotMessage}
        self._buffers: dict[str, dict[str, Any]] = {}

        # 反查：chat_id → 最近一条 incoming message（用于 reply 定位）
        self._last_incoming: dict[str, Any] = {}

    # ── ChannelAdapter 钩子 ────────────────────────────────────────────

    async def start(self) -> None:
        try:
            from dingtalk_stream import (
                ChatbotMessage,
                Credential,
                DingTalkStreamClient,
            )
        except ImportError as e:
            raise ChannelStartError(
                f"dingtalk-stream SDK not installed: {e}; "
                "run: pip install dingtalk-stream"
            ) from e

        try:
            # 让 dingtalk_stream SDK 的 INFO log 流到 stdout（默认无 handler 看不到）
            _sdk_logger = logging.getLogger("dingtalk_stream")
            if not any(
                isinstance(h, logging.StreamHandler) for h in _sdk_logger.handlers
            ):
                _h = logging.StreamHandler()
                _h.setFormatter(logging.Formatter(
                    "[dingtalk_stream] %(levelname)s %(message)s"
                ))
                _sdk_logger.addHandler(_h)
                _sdk_logger.setLevel(logging.INFO)
                _sdk_logger.propagate = False

            credential = Credential(self._client_id, self._client_secret)
            self._client = DingTalkStreamClient(
                credential,
                logger=_sdk_logger,
            )

            # 注册 chatbot handler
            handler = _DingTalkInboundHandler(adapter=self)
            self._client.register_callback_handler(
                ChatbotMessage.TOPIC, handler,
            )

            # 注册 outbound listener（bus.publish_outbound fan-out 进来）
            self._unsub = self.bus.subscribe_outbound(self.send)

            # spawn WSS 长连接 task（client.start() 是 async + 阻塞 + 自动重连）
            self._client_task = asyncio.create_task(
                self._client.start(),
                name="dingtalk_stream_client",
            )
            self.status = ChannelStatus.RUNNING
            log.info(
                "[DingTalkChannel] started; client_id=%s default_agent=%s",
                self._client_id[:8] + "***",  # mask
                self._default_agent,
            )
        except Exception as e:
            self.last_error = str(e)
            raise ChannelStartError(f"DingTalkChannel.start failed: {e}") from e

    async def stop(self) -> None:
        # 1. unsubscribe outbound
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:
                log.exception("[DingTalkChannel] unsubscribe failed")
            self._unsub = None

        # 2. cancel WSS task
        if self._client_task is not None:
            self._client_task.cancel()
            try:
                await self._client_task
            except (asyncio.CancelledError, Exception):
                pass
            self._client_task = None

        # 3. 清缓冲
        self._buffers.clear()
        self._last_incoming.clear()
        self._client = None
        self.status = ChannelStatus.STOPPED
        log.info("[DingTalkChannel] stopped")

    async def send(self, msg: OutboundMessage) -> None:
        if msg.channel != self.channel:
            return  # 跨平台过滤

        chat_id = msg.chat_id
        incoming = self._last_incoming.get(chat_id)
        if incoming is None:
            log.debug(
                "[DingTalkChannel] send: no incoming message for chat=%s; skip",
                chat_id,
            )
            return

        # 累积 chunks
        buf = self._buffers.setdefault(chat_id, {"text": "", "incoming": incoming})
        if msg.text:
            buf["text"] += msg.text
        buf["incoming"] = incoming  # update reference 防 incoming 变了

        # is_final: flush 累积内容
        if msg.is_final:
            full_text = buf["text"].strip()
            self._buffers.pop(chat_id, None)

            if not full_text:
                # 空内容（仅终结帧）→ 不发，避免发空消息
                return

            try:
                title = "柳如烟"  # 卡片标题；后续可按 agent 动态
                handler = _DingTalkInboundHandler(adapter=self)
                # 用 client 的 SDK helper 直接调（reply_markdown 在 ChatbotHandler 上）
                # ChatbotHandler.reply_markdown 内部调 SDK reply API
                handler.reply_markdown(title, full_text, incoming)
            except Exception as e:
                raise ChannelSendError(
                    f"DingTalkChannel reply_markdown failed: {e}"
                ) from e

    # ── 内部：handler 调过来的 inbound publish 入口 ────────────────────

    async def _on_inbound(self, sdk_message: Any) -> None:
        """ChatbotHandler.process 调过来。把 SDK 消息转 InboundMessage 进 bus。"""
        try:
            text = self._extract_text(sdk_message)
            chat_id = str(getattr(sdk_message, "conversation_id", "") or "")
            user_id = str(getattr(sdk_message, "sender_id", "") or "")
            sender_nick = str(getattr(sdk_message, "sender_nick", "") or "")

            log.debug(
                "[DingTalkChannel] _on_inbound: nick=%r chat=%s... user=%s... text=%r",
                sender_nick, chat_id[:24], user_id[:16], text,
            )

            if not chat_id or not user_id:
                log.warning("[DingTalkChannel] missing chat_id/user_id; drop")
                return

            # 缓存 incoming 用于后续 reply
            self._last_incoming[chat_id] = sdk_message

            # 转 InboundMessage（config_id 让 InboundRouter 精确路由到本配置，
            # 避免多 dingtalk 账号场景下消息被路由到第一个 enabled 配置去）
            inbound = InboundMessage(
                channel=self.channel,
                chat_id=chat_id,
                user_id=user_id,
                text=text,
                metadata={
                    "config_id": self.config_key,
                    "sender_nick": sender_nick,
                    "conversation_type": str(
                        getattr(sdk_message, "conversation_type", "") or ""
                    ),
                    "robot_code": str(getattr(sdk_message, "robot_code", "") or ""),
                },
            )

            queued = await self.bus.publish_inbound(inbound)
            log.debug(
                "[DingTalkChannel] publish_inbound config_id=%r queued=%s",
                self.config_key, queued,
            )
            if not queued:
                log.warning(
                    "[DingTalkChannel] bus.publish_inbound dropped (queue full); "
                    "chat=%s user=%s",
                    chat_id, user_id,
                )
        except Exception:
            log.error("[DingTalkChannel] _on_inbound EXCEPTION:", exc_info=True)

    @staticmethod
    def _extract_text(sdk_message: Any) -> str:
        """从 ChatbotMessage 提文本——SDK 的 extract_text_from_incoming_message
        返 list；这里合并成单 string。"""
        try:
            text_attr = getattr(sdk_message, "text", None)
            # SDK 把 text 存在 ``message.text.content`` 或类似
            if text_attr is not None:
                if isinstance(text_attr, str):
                    return text_attr
                content = getattr(text_attr, "content", None)
                if isinstance(content, str):
                    return content
            # fallback：raw msgtype=text
            return ""
        except Exception:
            log.exception("[DingTalkChannel] _extract_text failed")
            return ""


# ── SDK Handler bridge ─────────────────────────────────────────────────


def _build_handler_class():
    """lazy import 防 dingtalk_stream 没装时模块 import 失败。"""
    from dingtalk_stream import AckMessage, ChatbotHandler

    class _DingTalkInboundHandler(ChatbotHandler):
        """SDK callback handler；转交给 DingTalkChannel._on_inbound。"""

        def __init__(self, adapter: "DingTalkChannel") -> None:
            super().__init__()
            self.adapter = adapter

        async def process(self, callback_message: Any):
            """SDK 调过来。``callback_message`` 是 CallbackMessage；
            真消息在 ``callback_message.data``（dict）。"""
            try:
                from dingtalk_stream import ChatbotMessage
                # 把 dict 转 ChatbotMessage 实例
                if hasattr(callback_message, "data"):
                    raw_data = callback_message.data
                else:
                    raw_data = callback_message  # 兼容直接传 dict
                sdk_message = ChatbotMessage.from_dict(raw_data)
                await self.adapter._on_inbound(sdk_message)
            except Exception:
                log.exception(
                    "[_DingTalkInboundHandler.process] failed",
                )
            return AckMessage.STATUS_OK, ""

    return _DingTalkInboundHandler


# 模块加载时不 import dingtalk_stream（让没装 SDK 的环境也能 import 本模块）
# 第一次实例化 handler 时才 lazy build
class _DingTalkInboundHandler:
    """proxy；首次实例化 build 真 handler class。"""

    _real_class: type | None = None

    def __new__(cls, *args, **kwargs):
        if cls._real_class is None:
            cls._real_class = _build_handler_class()
        instance = object.__new__(cls._real_class)
        cls._real_class.__init__(instance, *args, **kwargs)
        return instance


__all__ = ["DingTalkChannel"]
