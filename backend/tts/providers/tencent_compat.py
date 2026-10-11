"""腾讯云语音合成（TTS）provider。

使用 Tencent Cloud API 3.0（TC3-HMAC-SHA256 签名），
不依赖 tencentcloud-sdk-python，仅用 httpx + hashlib + hmac。

API 文档
========
- TextToVoice: 短文本合成（≤150 字），实时返 base64 音频
- 端点: POST https://tts.tencentcloudapi.com
- 签名: TC3-HMAC-SHA256

配置字段（TTSConfig）
====================
- tencent_secret_id:     腾讯云 API 密钥 ID（访问管理 → API 密钥）
- tencent_secret_key:    腾讯云 API 密钥 Key
- tencent_voice_type:    音色 ID（默认 101001 情感女声·温柔）
- tencent_region:        地域（默认 ap-guangzhou）
- response_format:       mp3 / wav / pcm（opus 不被支持→降级 mp3）

设计文档：``docs/architecture_spec.md``
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Optional

import httpx

from ..config import TTSConfig
from .base import TTSError, TTSProvider, TTSResult

logger = logging.getLogger(__name__)

# Tencent Cloud TTS API 固定端点
_TENCENT_API_HOST = "tts.tencentcloudapi.com"
_TENCENT_API_URL = f"https://{_TENCENT_API_HOST}"
_TENCENT_VERSION = "2019-08-23"
_TENCENT_SERVICE = "tts"
_ALGORITHM = "TC3-HMAC-SHA256"

# response_format → Tencent Codec 参数映射
_FORMAT_TO_TENCENT_CODEC = {
    "mp3": "mp3",
    "wav": "wav",
    "opus": "mp3",  # 腾讯云不支持 opus，降级 mp3
    "pcm": "pcm",
}

# response_format → Content-Type 映射
_FORMAT_TO_MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}


class TencentCompatProvider(TTSProvider):
    """腾讯云 TTS 适配器。用 TC3-HMAC-SHA256 走 API 3.0。"""

    async def synthesize(self, text: str, cfg: TTSConfig) -> TTSResult:
        if not text or not text.strip():
            raise TTSError("文本为空，无可合成内容")

        sid = (cfg.tencent_secret_id or "").strip()
        key = (cfg.tencent_secret_key or "").strip()
        if not sid or not key:
            raise TTSError(
                "腾讯云 TTS 未配置 SecretId 或 SecretKey。"
                "请到 设置 → 辅助功能 → 语音输出 → 腾讯云 填写凭证，"
                "或在腾讯云控制台（访问管理 → API 密钥管理）获取。"
            )

        # 构造请求参数
        codec = _FORMAT_TO_TENCENT_CODEC.get(cfg.response_format, "mp3")
        tspeed = _map_speed(cfg.speed)
        session_id = str(uuid.uuid4())

        payload = {
            "Text": text,
            "SessionId": session_id,
            "VoiceType": cfg.tencent_voice_type or 101001,
            "Codec": codec,
            "SampleRate": 16000,
            "Speed": tspeed,
            "Volume": 0,
        }

        region = (cfg.tencent_region or "ap-guangzhou").strip()

        try:
            audio, ct = await self._call_tencent_api(sid, key, region, payload, cfg.timeout_sec)
        except TTSError:
            raise
        except Exception as e:
            raise TTSError(f"腾讯云 TTS 请求失败：{e}", upstream=e)

        logger.debug(
            "[TTS-TENCENT] ok %d bytes ct=%s voice=%s region=%s len=%d",
            len(audio), ct, cfg.tencent_voice_type, region, len(text),
        )
        return TTSResult(audio=audio, content_type=ct)

    # ── 内部：TC3-HMAC-SHA256 签名 + HTTP 请求 ──────────────────────

    async def _call_tencent_api(
        self,
        secret_id: str,
        secret_key: str,
        region: str,
        payload: dict,
        timeout_sec: int,
    ) -> tuple[bytes, str]:
        """发 signed POST 到腾讯云 TTS API，返 (audio_bytes, content_type)。"""
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        timestamp = str(int(time.time()))
        date_str = _ts_to_date(timestamp)

        # 1) CanonicalRequest
        canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{_TENCENT_API_HOST}\n"
        signed_headers = "content-type;host"
        hashed_payload = _sha256_hex(body_bytes)
        canonical_request = (
            f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"
        )

        # 2) StringToSign
        credential_scope = f"{date_str}/{_TENCENT_SERVICE}/tc3_request"
        hashed_canonical = _sha256_hex(canonical_request.encode("utf-8"))
        string_to_sign = f"{_ALGORITHM}\n{timestamp}\n{credential_scope}\n{hashed_canonical}"

        # 3) SigningKey
        secret = _hmac_sha256(b"TC3" + secret_key.encode("utf-8"), date_str.encode("utf-8"))
        secret = _hmac_sha256(secret, _TENCENT_SERVICE.encode("utf-8"))
        secret = _hmac_sha256(secret, b"tc3_request")

        # 4) Signature
        signature = _hmac_sha256_hex(secret, string_to_sign.encode("utf-8"))

        # 5) Authorization header
        auth = (
            f"{_ALGORITHM} Credential={secret_id}/{credential_scope},"
            f" SignedHeaders={signed_headers}, Signature={signature}"
        )

        headers = {
            "Authorization": auth,
            "Content-Type": "application/json; charset=utf-8",
            "Host": _TENCENT_API_HOST,
            "X-TC-Action": "TextToVoice",
            "X-TC-Timestamp": timestamp,
            "X-TC-Version": _TENCENT_VERSION,
            "X-TC-Region": region,
        }

        # 发起请求
        try:
            async with httpx.AsyncClient(timeout=timeout_sec) as client:
                resp = await client.post(
                    _TENCENT_API_URL,
                    content=body_bytes,
                    headers=headers,
                )
        except httpx.TimeoutException as e:
            raise TTSError("腾讯云 TTS 请求超时（试试缩短文本或增大 timeout）", upstream=e)
        except httpx.HTTPError as e:
            raise TTSError(f"腾讯云 TTS 网络错误：{e}", upstream=e)

        if resp.status_code != 200:
            await self._raise_for_status(resp)

        # 腾讯云 TTS API 返回 JSON 响应，音频以 base64 编码在 Response.Audio 中
        body = resp.json()
        inner = body.get("Response", {})
        b64_audio = inner.get("Audio")
        if not b64_audio:
            err_msg = inner.get("Error", {}).get("Message", "") or inner.get("Error", "")
            request_id = inner.get("RequestId", "")
            detail = f"腾讯云 API 返回中缺少 Audio 字段"
            if err_msg:
                detail += f"；错误：{err_msg}"
            if request_id:
                detail += f"（RequestId: {request_id}）"
            raise TTSError(detail)

        try:
            audio = base64.b64decode(b64_audio)
        except Exception as e:
            raise TTSError(f"腾讯云音频 base64 解码失败：{e}")

        if not audio:
            raise TTSError("腾讯云 TTS 返回空音频（上游可能限流或参数错误）")

        # Content-Type 由 Codec 参数决定，不依赖 HTTP 响应头
        ct = _FORMAT_TO_MIME.get(payload.get("Codec", "mp3"), "audio/mpeg")
        return audio, ct

    @staticmethod
    async def _raise_for_status(resp):
        """把 Tencent API HTTP 错误转成中文 TTSError。"""
        code = resp.status_code
        body = await _try_decode_error_async(resp)

        if code == 401:
            raise TTSError(f"鉴权失败 (401)：检查 SecretId 和 SecretKey 是否正确。上游：{body}")
        if code == 403:
            raise TTSError(
                f"权限不足 (403)：检查是否已开通语音合成服务（腾讯云控制台→语音合成→开通），"
                f"以及 SecretId 是否有权限。上游：{body}"
            )
        if code == 429:
            raise TTSError(f"限流 (429)：降低调用频率。上游：{body}")
        if 500 <= code < 600:
            raise TTSError(f"腾讯云服务端错误 ({code})：{body}")
        raise TTSError(f"腾讯云 API 异常 ({code})：{body}")


# ── 工具函数 ──────────────────────────────────────────────────


def _map_speed(speed: Optional[float]) -> int:
    """将 TTSConfig.speed（0.25~4.0，默认 1.0）映射到腾讯云 Speed（-2~2，默认 0）。

    映射关系：
        speed 1.0 → 0（正常）
        0.5      → -1（稍慢）
        2.0      →  1（稍快）
        0.25     → -2（最慢）
        4.0      →  2（最快）
    """
    s = speed or 1.0
    s = max(0.25, min(4.0, s))
    return max(-2, min(2, int(round((s - 1.0) * 2))))


def _ts_to_date(ts: str) -> str:
    """Unix 时间戳 → YYYY-MM-DD 日期字符串。"""
    import datetime

    return datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def _hmac_sha256_hex(key: bytes, msg: bytes) -> str:
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


async def _try_decode_error_async(resp) -> str:
    """尽力把错误响应体解成字符串展示。"""
    try:
        text = resp.text or ""
    except Exception:
        return "<binary or unreadable body>"
    text = text.strip()
    if len(text) > 200:
        text = text[:200] + "…"
    return text or "<empty body>"
