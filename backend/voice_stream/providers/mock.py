"""Mock provider：无 key / 无网环境下的确定性假识别，测试与联调用。

行为：start → started；每收到 4 段音频帧吐一句（1 句中间 + 1 句最终交替）；
finish → 把剩余未结句收尾再 finished。文本取 CANNED 循环，与帧数对应，
保证测试可断言（同一输入序列 → 同一输出序列）。
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from ..config import VoiceStreamConfig
from .base import ProviderEvent, ProviderEventCallback, RealtimeProvider

CANNED_SENTENCES = [
    "今天天气不错，",
    "我们去公园散步吧。",
    "顺便买一杯咖啡，",
    "然后回家写代码。",
]


class MockRealtimeProvider(RealtimeProvider):
    """一个实例 = 一次识别任务。不可复用。"""

    FRAMES_PER_FINAL = 4  # 每 N 个音频帧出一个最终句

    def __init__(self, cfg: VoiceStreamConfig) -> None:
        self._cfg = cfg
        self._on_event: Optional[ProviderEventCallback] = None
        self._frames = 0
        self._emitted_finals = 0
        self._sentences: List[str] = []
        self._pending_partial: Optional[str] = None
        self._closed = False
        self._finished = False

    async def start(self, on_event: ProviderEventCallback) -> None:
        self._on_event = on_event
        await self._emit(ProviderEvent.started())

    async def feed(self, frame: bytes) -> None:
        if self._closed or self._finished:
            return
        self._frames += 1
        # 每帧都吐当前句的"中间结果"（模拟真实流式修订）
        idx = self._emitted_finals % len(CANNED_SENTENCES)
        current = CANNED_SENTENCES[idx]
        partial_text = current[: max(1, (self._frames % self.FRAMES_PER_FINAL) * 2)]
        await self._emit(ProviderEvent.partial(partial_text))
        if self._frames % self.FRAMES_PER_FINAL == 0:
            await self._emit(ProviderEvent.final(current))
            self._sentences.append(current)
            self._emitted_finals += 1

    async def finish(self) -> None:
        if self._closed or self._finished:
            return
        # 收尾：把当前未结束的句子按最终结果吐出（模拟上游 flush）
        if self._frames % self.FRAMES_PER_FINAL != 0:
            idx = self._emitted_finals % len(CANNED_SENTENCES)
            current = CANNED_SENTENCES[idx]
            await self._emit(ProviderEvent.final(current))
            self._sentences.append(current)
            self._emitted_finals += 1
        self._finished = True
        await self._emit(ProviderEvent.finished("".join(self._sentences)))

    async def close(self) -> None:
        self._closed = True

    async def _emit(self, ev: dict) -> None:
        if self._on_event is not None:
            await self._on_event(ev)
