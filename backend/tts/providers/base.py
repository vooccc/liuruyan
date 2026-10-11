"""TTSProvider 抽象基类。

每个 provider 实现 ``async def synthesize(text, cfg)``，返回
``TTSResult(audio_bytes, content_type)``。

content_type 用 HTTP 标准 MIME，方便路由直接 ``Response(content=audio,
media_type=ct)`` 透传。

错误统一抛 ``TTSError`` —— 路由层捕获后转成 5xx + 友好 detail，方便前端展示。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..config import TTSConfig


@dataclass(frozen=True)
class TTSResult:
    """合成结果。"""

    audio: bytes
    """完整音频字节流。M1 一句一段，无流式分块。"""

    content_type: str
    """HTTP MIME，例 ``audio/mpeg`` / ``audio/wav`` / ``audio/opus``。"""


class TTSError(RuntimeError):
    """provider 抛出的合成错误。

    Args:
        message: 给用户展示的中文错误描述（可在 UI 直接展示）。
        upstream: 上游原始错误（用于日志，不展示给用户）。
    """

    def __init__(self, message: str, upstream: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.upstream = upstream


class TTSProvider(ABC):
    """TTS provider 协议。"""

    @abstractmethod
    async def synthesize(self, text: str, cfg: TTSConfig) -> TTSResult:
        """把文本合成为音频。

        Args:
            text: 已经过 should_speak 过滤的纯文本（不含 <say> 标签）。
            cfg: 完整 TTSConfig，provider 自取所需字段。

        Raises:
            TTSError: 配置缺失 / 网络错 / 上游 5xx 等。
        """
        raise NotImplementedError

    def supports_streaming(self) -> bool:
        """M1 全部 False。M4 加流式时这个 flag 才会有 True 实现。"""
        return False
