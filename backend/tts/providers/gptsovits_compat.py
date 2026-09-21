"""GPT-SoVITS api_v1/api_v2 风格 provider。

兼容
====
- 原生 GPT-SoVITS ``api.py`` / ``api_v2.py`` （RVC-Boss/GPT-SoVITS）
- 衍生服务：Qwen3TTS-API (api-qwen.py)、各类二次封装的 GPT-SoVITS Web UI
- 用户本地跑 ``run-1.7b.bat`` / ``run-0.6b.bat`` 起来的 Qwen3TTS 服务（POST 根路径）

请求/响应契约
=============

请求::

    POST {base_url}/         (POST 根路径，GPT-SoVITS / Qwen3TTS 风格)
    Content-Type: application/json
    {
      "refer_wav_path": "<参考音频绝对路径>",
      "prompt_text": "<参考音频对应文字>",   # 可选；zh 通常省略
      "text": "<要合成的文本>",
      "text_language": "Chinese" | "Japanese" | "English",
      "cut_punc": ",.;:。;：!！?？",
      "media_type": "wav",
      "stream_mode": "close",      # M3 暂不支持 framed 流式（M4 上线）
      "temperature": 0.9
    }

响应::

    Content-Type: audio/wav
    binary body

错误处理
========
- 400/422: 参考音频路径不存在、prompt_text 跟参考音频不匹配
- 500/502: 上游模型加载失败 / OOM
- ConnectionError: 用户本地服务没启动

全部转 ``TTSError`` 抛出。

跟 openai_compat 的差异
========================
- **不需要** api_key（本地服务无认证）
- **必须** refer_wav_path（zero-shot 风格的参考音频文件路径）
- **不复用** providers store，因为本地 GPT-SoVITS server 不是 LLM 凭证范畴
- 用户在 TTSPane 直接填 base_url + refer_wav_path 等本地服务参数

设计文档：``_docs/handover_tts_design_2026-05-29.md`` §M3
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from ..config import TTSConfig
from .base import TTSError, TTSProvider, TTSResult

logger = logging.getLogger(__name__)


# 配置里的 ISO 简码 → GPT-SoVITS 内部使用的全名
_LANG_MAP = {
    "zh": "Chinese",
    "ja": "Japanese",
    "en": "English",
    # 兼容用户可能填的其它写法
    "Chinese": "Chinese",
    "Japanese": "Japanese",
    "English": "English",
    "中文": "Chinese",
    "日文": "Japanese",
    "英文": "English",
}


class GPTSoVITSCompatProvider(TTSProvider):
    """GPT-SoVITS / Qwen3TTS-API 客户端。"""

    async def synthesize(self, text: str, cfg: TTSConfig) -> TTSResult:
        if not text or not text.strip():
            raise TTSError("文本为空，无可合成内容")

        base_url = (cfg.gptsovits_base_url or "").strip()
        if not base_url:
            raise TTSError(
                "GPT-SoVITS base URL 未配置。请到 设置 → 辅助功能 → 语音输出 填本地服务地址，"
                "例如 http://127.0.0.1:9880"
            )

        refer = (cfg.gptsovits_refer_wav_path or "").strip()
        if not refer:
            raise TTSError(
                "GPT-SoVITS 必须提供参考音频路径（refer_wav_path）。"
                "请在 设置 → 语音输出 填一个本地参考音频文件的绝对路径（mp3/wav/flac 等）。"
            )
        if not os.path.exists(refer):
            raise TTSError(
                f"参考音频文件不存在：{refer}。"
                f"请检查路径是否正确（必须是绝对路径，例如 D:\\audio\\reference.mp3）。"
            )

        text_language = _LANG_MAP.get(
            (cfg.gptsovits_text_lang or "zh").strip(),
            "Chinese",
        )

        url = self._build_url(base_url)
        payload = {
            "refer_wav_path": refer,
            "text": text,
            "text_language": text_language,
            "cut_punc": ",.;:。;：!！?？",
            "media_type": "wav",
            # M3 暂用 close（非流式 wav 整段返回），M4 再上 framed streaming
            "stream_mode": "close",
            # GPT-SoVITS 默认温度 0.9（参考 BandoriPet）
            "temperature": 0.9,
        }
        prompt_text = (cfg.gptsovits_prompt_text or "").strip()
        if prompt_text:
            payload["prompt_text"] = prompt_text

        try:
            async with httpx.AsyncClient(timeout=cfg.timeout_sec) as client:
                resp = await client.post(url, json=payload)
        except httpx.ConnectError as e:
            raise TTSError(
                f"连不上 GPT-SoVITS 服务（{base_url}）。"
                f"请确认本地服务已启动（例如运行 run-1.7b.bat）。",
                upstream=e,
            )
        except httpx.TimeoutException as e:
            raise TTSError(
                "GPT-SoVITS 合成超时（试试缩短文本，或增大 timeout）",
                upstream=e,
            )
        except httpx.HTTPError as e:
            raise TTSError(f"GPT-SoVITS 网络错误：{e}", upstream=e)

        if resp.status_code != 200:
            self._raise_for_status(resp)

        audio = resp.content
        if not audio:
            raise TTSError("GPT-SoVITS 返回空音频（参考音频或文本可能有问题）")

        ct = resp.headers.get("content-type", "").split(";")[0].strip()
        if not ct or not ct.startswith("audio/"):
            ct = "audio/wav"  # GPT-SoVITS close 模式默认 wav

        logger.debug(
            "[TTS-GPTSOVITS] ok %d bytes ct=%s lang=%s len=%d",
            len(audio), ct, text_language, len(text),
        )
        return TTSResult(audio=audio, content_type=ct)

    # ── 内部工具 ──────────────────────────────────────────────

    @staticmethod
    def _build_url(base_url: str) -> str:
        """GPT-SoVITS 官方 api.py / Qwen3TTS-API 都是 POST 根路径。

        兼容用户多种填法：
        - ``http://127.0.0.1:9880`` → POST /
        - ``http://127.0.0.1:9880/`` → POST /
        - ``http://127.0.0.1:9880/tts`` → 保留（api_v2 路径）

        如果用户填了具体路径就尊重；否则默认走根路径。
        """
        b = base_url.rstrip("/")
        # 已经带 /tts 等具体路径就保留
        if "/tts" in b.split("//", 1)[-1]:
            return b
        return b + "/"

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """把上游 HTTP 错误转成中文 TTSError。"""
        code = resp.status_code
        body_preview = _try_decode_error(resp)

        if code in (400, 422):
            raise TTSError(
                f"参数错误 ({code})：检查参考音频路径、prompt_text 是否跟参考音频匹配。"
                f"上游：{body_preview}"
            )
        if code == 404:
            raise TTSError(
                f"端点不存在 ({code})：base_url 末尾可能要去掉 /tts 改用根路径。"
                f"上游：{body_preview}"
            )
        if 500 <= code < 600:
            raise TTSError(
                f"GPT-SoVITS 服务错误 ({code})：可能是模型加载失败 / OOM。"
                f"上游：{body_preview}"
            )
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
