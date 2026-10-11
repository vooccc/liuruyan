"""阿里云 NLS 语音合成（TTS）provider。

使用阿里云智能语音交互 REST API：先通过 POP RPC ``CreateToken`` 生成
NLS token，再调用 ``/stream/v1/tts`` 返回音频。

provider 是全局单例；这里不在实例上缓存 token，避免全局可变状态污染并发会话。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import quote
from xml.sax.saxutils import escape

import httpx

from ..config import TTSConfig
from .base import TTSError, TTSProvider, TTSResult

logger = logging.getLogger(__name__)

_ALIYUN_TOKEN_HOST = "nls-meta.cn-shanghai.aliyuncs.com"
_ALIYUN_TOKEN_URL = f"https://{_ALIYUN_TOKEN_HOST}/"

_FORMAT_TO_MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
    "opus": "audio/opus",
}


class AliyunCompatProvider(TTSProvider):
    """阿里云 NLS TTS 适配器。"""

    async def synthesize(self, text: str, cfg: TTSConfig) -> TTSResult:
        if not text or not text.strip():
            raise TTSError("文本为空，无可合成内容")

        access_key_id = (cfg.aliyun_access_key_id or "").strip()
        access_key_secret = (cfg.aliyun_access_key_secret or "").strip()
        appkey = (cfg.aliyun_appkey or "").strip()
        if not access_key_id or not access_key_secret or not appkey:
            raise TTSError(
                "阿里云 TTS 未配置 AccessKey ID、AccessKey Secret 或 AppKey。"
                "请到 设置 → 辅助功能 → 语音输出 → 阿里云 填写凭证，"
                "并确认已开通智能语音交互服务。"
            )

        region = (cfg.aliyun_region or "cn-shanghai").strip() or "cn-shanghai"
        gateway = _gateway_url(region)
        audio_format = _normalize_format(cfg.response_format)

        try:
            async with httpx.AsyncClient(timeout=cfg.timeout_sec) as client:
                token = await self._fetch_token(client, access_key_id, access_key_secret)
                audio, content_type = await self._call_tts(
                    client, gateway, text, cfg, appkey, token, audio_format
                )
        except TTSError:
            raise
        except Exception as e:
            raise TTSError(f"阿里云 TTS 请求失败：{e}", upstream=e)

        logger.debug(
            "[TTS-ALIYUN] ok %d bytes ct=%s voice=%s len=%d",
            len(audio), content_type, cfg.aliyun_voice, len(text),
        )
        return TTSResult(audio=audio, content_type=content_type)

    async def _fetch_token(
        self,
        client: httpx.AsyncClient,
        access_key_id: str,
        access_key_secret: str,
    ) -> str:
        params = _signed_create_token_params(access_key_id, access_key_secret)
        try:
            resp = await client.get(_ALIYUN_TOKEN_URL, params=params)
        except httpx.TimeoutException as e:
            raise TTSError("阿里云 CreateToken 请求超时", upstream=e)
        except httpx.HTTPError as e:
            raise TTSError(f"阿里云 CreateToken 网络错误：{e}", upstream=e)

        if resp.status_code != 200:
            raise TTSError(f"阿里云 CreateToken 请求失败 ({resp.status_code})：{_short_body(resp)}")

        try:
            data = resp.json()
        except Exception as e:
            raise TTSError(f"阿里云 CreateToken 响应不是 JSON：{_short_body(resp)}", upstream=e)

        token = data.get("Token", {}).get("Id")
        if not token:
            detail = data.get("Message") or data.get("Code") or data
            raise TTSError(f"阿里云 CreateToken 响应缺少 Token.Id：{detail}")
        return str(token)

    async def _call_tts(
        self,
        client: httpx.AsyncClient,
        gateway: str,
        text: str,
        cfg: TTSConfig,
        appkey: str,
        token: str,
        audio_format: str,
    ) -> tuple[bytes, str]:
        emotion = (cfg.aliyun_emotion or "").strip()
        request_text = _wrap_emotion_ssml(text, emotion) if emotion else text
        params = {
            "appkey": appkey,
            "token": token,
            "text": request_text,
            "format": audio_format,
            "voice": (cfg.aliyun_voice or "xiaoyun").strip() or "xiaoyun",
            "sample_rate": cfg.aliyun_sample_rate or 16000,
            "volume": _clamp(cfg.aliyun_volume, 0, 100),
            "speech_rate": _clamp(cfg.aliyun_speech_rate, -500, 500),
            "pitch_rate": _clamp(cfg.aliyun_pitch_rate, -500, 500),
        }

        try:
            resp = await client.get(gateway, params=params)
        except httpx.TimeoutException as e:
            raise TTSError("阿里云 TTS 请求超时（试试缩短文本或增大 timeout）", upstream=e)
        except httpx.HTTPError as e:
            raise TTSError(f"阿里云 TTS 网络错误：{e}", upstream=e)

        if resp.status_code != 200:
            raise TTSError(f"阿里云 TTS 请求失败 ({resp.status_code})：{_short_body(resp)}")

        content_type = resp.headers.get("content-type", "").lower()
        body = resp.content or b""
        if "application/json" in content_type or body.startswith(b"{"):
            raise TTSError(f"阿里云 TTS 合成失败：{_decode_json_error(resp)}")
        if not body:
            raise TTSError("阿里云 TTS 返回空音频（上游可能限流或参数错误）")

        return body, _FORMAT_TO_MIME.get(audio_format, "audio/mpeg")


def _signed_create_token_params(access_key_id: str, access_key_secret: str) -> dict[str, str]:
    params = {
        "AccessKeyId": access_key_id,
        "Action": "CreateToken",
        "Format": "JSON",
        "RegionId": "cn-shanghai",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": str(uuid.uuid4()),
        "SignatureVersion": "1.0",
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2019-02-28",
    }
    canonical = "&".join(
        f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in sorted(params.items())
    )
    string_to_sign = f"GET&%2F&{_percent_encode(canonical)}"
    digest = hmac.new(
        (access_key_secret + "&").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    params["Signature"] = base64.b64encode(digest).decode("ascii")
    return params


def _percent_encode(value: object) -> str:
    encoded = quote(str(value), safe="")
    return encoded.replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


def _gateway_url(region: str) -> str:
    if region == "cn-shanghai":
        return "https://nls-gateway-cn-shanghai.aliyuncs.com/stream/v1/tts"
    if region == "cn-beijing":
        return "https://nls-gateway-cn-beijing.aliyuncs.com/stream/v1/tts"
    return f"https://nls-gateway-{region}.aliyuncs.com/stream/v1/tts"


def _normalize_format(value: str) -> str:
    if value in _FORMAT_TO_MIME:
        return value
    return "mp3"


def _wrap_emotion_ssml(text: str, emotion: str) -> str:
    safe_emotion = escape(emotion, {'"': '&quot;'})
    safe_text = escape(text)
    return f'<speak><emotion category="{safe_emotion}">{safe_text}</emotion></speak>'


def _clamp(value: int | None, low: int, high: int) -> int:
    if value is None:
        return 0 if low < 0 else low
    return max(low, min(high, int(value)))


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
    code = data.get("Code") or data.get("code") or data.get("status")
    message = data.get("Message") or data.get("message") or data.get("error_message")
    request_id = data.get("RequestId") or data.get("request_id")
    parts = [str(x) for x in (code, message, request_id) if x]
    return " | ".join(parts) if parts else json.dumps(data, ensure_ascii=False)
