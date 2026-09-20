"""OpenAI 兼容 ``/v1/audio/speech`` provider。

为什么 MVP 选这个
================
Open WebUI 最近加了 "Custom OpenAPI Compatible" —— 证实业界共识：**一个
``/v1/audio/speech`` 客户端能通吃**：

- OpenAI 官方：tts-1 / tts-1-hd / gpt-4o-mini-tts
- SiliconFlow：FunAudioLLM/CosyVoice2-0.5B
- Groq：playai-tts
- ElevenLabs：提供 OpenAI 兼容端点
- Edge TTS：社区 ``openai-edge-tts`` wrapper（pip 一行）
- 任何聚合 router 实现的 ``/v1/audio/speech``

请求/响应契约
=============
请求::

    POST {base_url}/audio/speech
    Authorization: Bearer {api_key}
    Content-Type: application/json
    {
      "model": "tts-1",
      "voice": "alloy",
      "input": "...",
      "response_format": "mp3",   # mp3 | wav | opus | pcm | flac | aac
      "speed": 1.0
    }

响应::

    Content-Type: audio/mpeg  (或 audio/wav / audio/opus 视 response_format)
    binary body

错误处理
========
- 401/403: api_key 无效或被风控
- 404: model 名错（很多聚合站对 tts-1 不支持）
- 5xx: 上游故障

全部转 ``TTSError`` 抛出，路由层捕获返 502。

UA 伪装
=======
复用 ``backend.utils.llm_headers.merge_default_llm_headers()`` —— 聚合 router 见
Python SDK 默认 UA 会直接 403，必须伪装成 Chrome。
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from backend.utils.llm_headers import merge_default_llm_headers
from ..config import TTSConfig
from ..credentials import resolve_credentials
from .base import TTSError, TTSProvider, TTSResult

logger = logging.getLogger(__name__)


# response_format → Content-Type 映射
_FORMAT_TO_MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/opus",
    "pcm": "audio/pcm",
}


class OpenAICompatProvider(TTSProvider):
    """``/v1/audio/speech`` 通用客户端。"""

    async def synthesize(self, text: str, cfg: TTSConfig) -> TTSResult:
        if not text or not text.strip():
            raise TTSError("文本为空，无可合成内容")

        # 凭证解析：优先 provider_id 走 providers store，fallback 旧字段
        creds = resolve_credentials(cfg)
        if not creds.base_url:
            raise TTSError(
                "TTS Provider 未配置。请到 设置 → 辅助功能 → 语音输出 选一个 OpenAI 兼容 Provider，"
                "或先在 设置 → 模型服务 添加自定义 Provider（如 http://127.0.0.1:5050/v1）。"
            )

        url = self._build_url(creds.base_url)
        headers = self._build_headers(creds.api_key, creds.extra_headers)
        payload = {
            "model": cfg.model or "tts-1",
            "voice": cfg.voice or "alloy",
            "input": text,
            "response_format": cfg.response_format or "mp3",
            "speed": float(cfg.speed or 1.0),
        }

        try:
            async with httpx.AsyncClient(timeout=cfg.timeout_sec) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as e:
            raise TTSError("TTS 请求超时（试试缩短文本或增大 timeout）", upstream=e)
        except httpx.HTTPError as e:
            raise TTSError(f"TTS 网络错误：{e}", upstream=e)

        if resp.status_code != 200:
            self._raise_for_status(resp)

        audio = resp.content
        if not audio:
            raise TTSError("TTS 返回空音频（上游可能限流 / 模型未授权）")

        # 优先用响应里的 Content-Type；某些聚合站不带，按 response_format 推断
        ct = resp.headers.get("content-type", "").split(";")[0].strip()
        if not ct or not ct.startswith("audio/"):
            ct = _FORMAT_TO_MIME.get(cfg.response_format, "audio/mpeg")

        logger.debug(
            "[TTS-OPENAI] ok %d bytes ct=%s model=%s voice=%s len=%d",
            len(audio), ct, payload["model"], payload["voice"], len(text),
        )
        return TTSResult(audio=audio, content_type=ct)

    # ── 内部工具 ──────────────────────────────────────────────

    @staticmethod
    def _build_url(base_url: str) -> str:
        """拼接 ``/audio/speech``。

        兼容用户填 ``https://api.openai.com/v1`` 或 ``https://.../v1/`` 两种写法。
        某些聚合站没有 /v1 前缀直接挂 /audio/speech 的也支持——只要 base_url 已经
        指向"上一级"路径，本函数就把 /audio/speech 拼上去。
        """
        b = base_url.rstrip("/")
        # 已经指到 /audio/speech 的特殊情形也兼容
        if b.endswith("/audio/speech"):
            return b
        return f"{b}/audio/speech"

    @staticmethod
    def _build_headers(api_key: str, extra: Optional[dict] = None) -> dict:
        h = merge_default_llm_headers(extra)
        h["Content-Type"] = "application/json"
        h["Accept"] = "audio/mpeg, audio/wav, audio/opus, audio/*;q=0.5, */*;q=0.1"
        if api_key:
            h["Authorization"] = f"Bearer {api_key}"
        return h

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """把上游 HTTP 错误转成中文 TTSError。"""
        code = resp.status_code
        body_preview = _try_decode_error(resp)

        if code in (401, 403):
            raise TTSError(f"鉴权失败 ({code})：检查 API Key 是否正确 / 是否过期。上游：{body_preview}")
        if code == 404:
            raise TTSError(f"端点或模型不存在 ({code})：检查 base_url 末尾路径和 model 名。上游：{body_preview}")
        if code == 429:
            raise TTSError(f"上游限流 ({code})：降低频率或换 Key。上游：{body_preview}")
        if 500 <= code < 600:
            raise TTSError(f"上游服务错误 ({code})：{body_preview}")
        raise TTSError(f"上游异常 ({code})：{body_preview}")


def _try_decode_error(resp: httpx.Response) -> str:
    """尽力把错误响应解成短字符串展示（截断 200 字符防爆 UI）。"""
    try:
        text = resp.text or ""
    except Exception:
        return "<binary or unreadable body>"
    text = text.strip()
    if len(text) > 200:
        text = text[:200] + "…"
    return text or "<empty body>"
