"""v2 · G-wechat · WeChatChannel — 微信 / 企业微信 IM 接入适配器

参考企业微信官方标准协议：
通过企业微信自建应用及微信插件能力，将普通微信直接接入柳如烟 v2 channels。
用户可在手机普通微信与 Agent 进行无缝对话与指令交互，零封号风险。

config schema (ChannelConfig.config):
- corp_id: str          企业 ID (CorpID)
- corp_secret: str      应用 Secret (通过 MASK_VALUE 双向脱敏)
- agent_id: str | int   应用 AgentId (如 1000002)
- default_agent: str    该 channel 默认路由到哪个 agent_id (如 "default")
- touser: str           默认消息推送目标 (默认 "@all" 推送给所有应用可见人员)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from liuruyan.buses import InboundMessage, OutboundMessage
from liuruyan.channels import (
    ChannelAdapter,
    ChannelSendError,
    ChannelStartError,
    ChannelStatus,
)

log = logging.getLogger(__name__)

# 企业微信官方 API 地址
_WECOM_GET_TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
_WECOM_SEND_MSG_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"


class WeChatChannel(ChannelAdapter):
    """微信 / 企业微信 protocol adapter。"""

    channel = "wechat"

    def __init__(self, bus: Any, config: dict[str, Any]) -> None:
        super().__init__(bus, config)

        corp_id = (config.get("corp_id") or "").strip()
        corp_secret = (config.get("corp_secret") or "").strip()
        agent_id = str(config.get("agent_id") or "").strip()

        if not corp_id or not corp_secret:
            raise ChannelStartError(
                "WeChatChannel requires corp_id and corp_secret in config"
            )

        self._corp_id = corp_id
        self._corp_secret = corp_secret
        self._agent_id = int(agent_id) if agent_id.isdigit() else agent_id
        self._default_agent = str(config.get("default_agent") or "default")
        self._touser = str(config.get("touser") or "@all").strip()

        # Token 缓存
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._http_client: httpx.AsyncClient | None = None

    # ── 连通性与 Token 管理 ────────────────────────────────────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def get_access_token(self, force_refresh: bool = False) -> str:
        """获取并缓存企业微信 access_token（提前 300 秒刷新）。"""
        now = time.time()
        if not force_refresh and self._access_token and now < (self._token_expires_at - 300):
            return self._access_token

        async with self._token_lock:
            # 双检锁
            if not force_refresh and self._access_token and now < (self._token_expires_at - 300):
                return self._access_token

            client = await self._ensure_client()
            try:
                res = await client.get(
                    _WECOM_GET_TOKEN_URL,
                    params={"corpid": self._corp_id, "corpsecret": self._corp_secret},
                )
                if not res.is_success:
                    raise ChannelStartError(f"HTTP error fetching WeChat token: {res.status_code}")
                data = res.json()
            except Exception as e:
                raise ChannelStartError(f"网络异常，无法获取微信 Token: {e}") from e

            errcode = data.get("errcode", 0)
            if errcode != 0:
                errmsg = data.get("errmsg", "未知错误")
                raise ChannelStartError(f"企业微信凭证校验失败 ({errcode}): {errmsg}")

            token = data.get("access_token")
            expires_in = data.get("expires_in", 7200)
            if not token:
                raise ChannelStartError("企业微信返回了空的 access_token")

            self._access_token = str(token)
            self._token_expires_at = now + float(expires_in)
            log.info(
                "[v2:channels:wechat] token 刷新成功 (config_key=%s, ttl=%ds)",
                self.config_key,
                expires_in,
            )
            return self._access_token

    @classmethod
    async def verify_credentials(cls, corp_id: str, corp_secret: str) -> dict[str, Any]:
        """独立静态检测企业微信 CorpID 与 Secret 连通性。"""
        corp_id = corp_id.strip()
        corp_secret = corp_secret.strip()
        if not corp_id or not corp_secret:
            return {"ok": False, "message": "CorpID 与 Secret 均不能为空"}

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                res = await client.get(
                    _WECOM_GET_TOKEN_URL,
                    params={"corpid": corp_id, "corpsecret": corp_secret},
                )
                if not res.is_success:
                    return {"ok": False, "message": f"企业微信 API 请求失败 (HTTP {res.status_code})"}
                data = res.json()
                errcode = data.get("errcode", 0)
                if errcode == 0:
                    return {"ok": True, "message": "企业微信 API 认证成功！凭证有效。"}
                return {"ok": False, "message": f"企业微信错误 ({errcode}): {data.get('errmsg', '')}"}
        except Exception as e:
            return {"ok": False, "message": f"连接企业微信接口超时或异常: {e}"}

    # ── ChannelAdapter 生命周期 ────────────────────────────────────────

    async def start(self) -> None:
        """建立连接并验证凭证可用。"""
        self.status = ChannelStatus.STARTING
        try:
            token = await self.get_access_token(force_refresh=True)
            if not token:
                raise ChannelStartError("无法获取有效 Token")
            self.status = ChannelStatus.RUNNING
            self.last_error = ""
            log.info(
                "[v2:channels:wechat] WeChatChannel (%s) 启动成功，AgentId=%s",
                self.config_key,
                self._agent_id,
            )
        except Exception as e:
            self.status = ChannelStatus.ERROR
            self.last_error = str(e)
            log.error("[v2:channels:wechat] WeChatChannel 启动失败: %s", e)
            raise

    async def stop(self) -> None:
        """安全停止适配器。"""
        self.status = ChannelStatus.STOPPING
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None
        self.status = ChannelStatus.STOPPED
        log.info("[v2:channels:wechat] WeChatChannel (%s) 已停止", self.config_key)

    # ── 消息外发 ───────────────────────────────────────────────────────

    async def send(self, outbound: OutboundMessage) -> None:
        """将柳如烟 Agent 的回复发送至微信/企业微信。"""
        if self.status != ChannelStatus.RUNNING:
            raise ChannelSendError("WeChatChannel 未处于运行状态，无法发送消息")

        token = await self.get_access_token()
        client = await self._ensure_client()

        target_user = outbound.target or self._touser or "@all"
        text_content = outbound.text or ""

        # 企微支持 markdown 和 text
        payload: dict[str, Any] = {
            "touser": target_user,
            "msgtype": "markdown",
            "agentid": self._agent_id,
            "markdown": {
                "content": text_content,
            },
            "enable_duplicate_check": 0,
        }

        try:
            res = await client.post(
                _WECOM_SEND_MSG_URL,
                params={"access_token": token},
                json=payload,
            )
            data = res.json()
            errcode = data.get("errcode", 0)

            # 如果 markdown 格式不兼容，自动降级纯文本重试一次
            if errcode != 0 and payload["msgtype"] == "markdown":
                log.warning(
                    "[v2:channels:wechat] markdown 推送异常 (%s: %s)，降级为 text 重试",
                    errcode,
                    data.get("errmsg"),
                )
                payload["msgtype"] = "text"
                payload.pop("markdown", None)
                payload["text"] = {"content": text_content}
                res = await client.post(
                    _WECOM_SEND_MSG_URL,
                    params={"access_token": token},
                    json=payload,
                )
                data = res.json()
                errcode = data.get("errcode", 0)

            if errcode != 0:
                raise ChannelSendError(
                    f"微信消息发送失败 ({errcode}): {data.get('errmsg', '')}"
                )

            log.info(
                "[v2:channels:wechat] 消息已送达微信 (target=%s, msg_id=%s)",
                target_user,
                data.get("msgid"),
            )
        except ChannelSendError:
            raise
        except Exception as e:
            raise ChannelSendError(f"请求微信发送接口异常: {e}") from e
