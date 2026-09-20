"""百度云语音合成（TTS）provider。

使用百度智能云短文本语音合成接口：先通过 OAuth client_credentials
换取 access_token，再调用 ``https://tsn.baidu.com/text2audio`` 返回音频。

不缓存 token：provider 是全局单例，避免在单例上存 per-call / 可变状态；短文本
试听场景下多一次 token 请求可接受，后续如需缓存应使用带过期时间的独立 cache。
"""
from __future__ import annotations

import logging
import uuid

import httpx

from ..config import TTSConfig
from .base import TTSError, TTSProvider, TTSResult

logger = logging.getLogger(__name__)

_BAIDU_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
_BAIDU_TTS_URL = "https://tsn.baidu.com/text2audio"

_FORMAT_TO_AUE = {
    "mp3": 3,
    "wav": 6,
    "pcm": 4,
    "opus": 3,
}

_AUE_TO_MIME = {
    3: "audio/mpeg",
    4: "audio/pcm",
    6: "audio/wav",
}


class BaiduCompatProvider(TTSProvider):
    """百度云 TTS 适配器。"""

    async def synthesize(self, text: str, cfg: TTSConfig) -> TTSResult:
        if not text or not text.strip():
            raise TTSError("文本为空，无可合成内容")

        api_key = (cfg.baidu_api_key or "").strip()
        secret_key = (cfg.baidu_secret_key or "").strip()
        if not api_key or not secret_key:
            raise TTSError(
                "百度云 TTS 未配置 API Key 或 Secret Key。"
                "请到 设置 → 辅助功能 → 语音输出 → 百度云 填写凭证，"
                "并确认已开通语音合成服务。"
            )

        cuid = (cfg.baidu_cuid or "").strip() or f"liuruyan-{uuid.uuid4().hex[:16]}"
        aue = _FORMAT_TO_AUE.get(cfg.response_format, 3)
        if cfg.response_format == "opus":
            aue = 3

        try:
            async with httpx.AsyncClient(timeout=cfg.timeout_sec) as client:
                token = await self._fetch_token(client, api_key, secret_key)
                audio, content_type = await self._call_tts(client, text, cfg, token, cuid, aue)
        except TTSError:
            raise
        except Exception as e:
            raise TTSError(f"百度云 TTS 请求失败：{e}", upstream=e)

        logger.debug(
            "[TTS-BAIDU] ok %d bytes ct=%s voice=%s len=%d",
            len(audio), content_type, cfg.baidu_voice, len(text),
        )
        return TTSResult(audio=audio, content_type=content_type)

    async def _fetch_token(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        secret_key: str,
    ) -> str:
        try:
            resp = await client.post(
                _BAIDU_TOKEN_URL,
                params={
                    "grant_type": "client_credentials",
                    "client_id": api_key,
                    "client_secret": secret_key,
                },
            )
        except httpx.TimeoutException as e:
            raise TTSError("百度云 OAuth token 请求超时", upstream=e)
        except httpx.HTTPError as e:
            raise TTSError(f"百度云 OAuth token 网络错误：{e}", upstream=e)

        if resp.status_code != 200:
            raise TTSError(f"百度云 OAuth token 请求失败 ({resp.status_code})：{_short_body(resp)}")

        try:
            data = resp.json()
        except Exception as e:
            raise TTSError(f"百度云 OAuth token 响应不是 JSON：{_short_body(resp)}", upstream=e)

        token = data.get("access_token")
        if not token:
            msg = data.get("error_description") or data.get("error") or data
            raise TTSError(f"百度云 OAuth token 响应缺少 access_token：{msg}")
        return str(token)

    async def _call_tts(
        self,
        client: httpx.AsyncClient,
        text: str,
        cfg: TTSConfig,
        token: str,
        cuid: str,
        aue: int,
    ) -> tuple[bytes, str]:
        form = {
            "tex": text,
            "tok": token,
            "cuid": cuid,
            "ctp": 1,
            "lan": "zh",
            "spd": _map_baidu_slider(cfg.baidu_speed),
            "pit": _map_baidu_slider(cfg.baidu_pitch),
            "vol": _map_baidu_slider(cfg.baidu_volume),
            "per": cfg.baidu_voice or 0,
            "aue": aue,
        }

        try:
            resp = await client.post(
                _BAIDU_TTS_URL,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as e:
            raise TTSError("百度云 TTS 请求超时（试试缩短文本或增大 timeout）", upstream=e)
        except httpx.HTTPError as e:
            raise TTSError(f"百度云 TTS 网络错误：{e}", upstream=e)

        if resp.status_code != 200:
            raise TTSError(f"百度云 TTS 请求失败 ({resp.status_code})：{_short_body(resp)}")

        content_type = resp.headers.get("content-type", "").lower()
        body = resp.content or b""
        if "application/json" in content_type or body.startswith(b"{"):
            detail = _decode_json_error(resp)
            raise TTSError(f"百度云 TTS 合成失败：{detail}")

        if not body:
            raise TTSError("百度云 TTS 返回空音频（上游可能限流或参数错误）")

        return body, _AUE_TO_MIME.get(aue, "audio/mpeg")


def _map_baidu_slider(value: int | None) -> int:
    if value is None:
        return 5
    return max(0, min(15, int(value)))


def _short_body(resp: httpx.Response) -> str:
    text = (resp.text or "").strip()
    if len(text) > 300:
        text = text[:300] + "..."
    return text or "<empty body>"


def _decode_json_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return _short_body(resp)
    err_no = data.get("err_no") or data.get("error")
    err_msg = data.get("err_msg") or data.get("error_description") or data.get("error_msg")
    if err_no and err_msg:
        return f"{err_no}: {err_msg}"
    return str(data)
