"""RealtimeProvider 抽象基类 + 事件类型。

事件（ProviderEvent，dict）由 provider 产生、service 转发给浏览器：
    {"type": "started"}                          任务已启动，可发音频
    {"type": "partial", "text": str}              当前句中间结果（会被后续修订）
    {"type": "final",  "text": str}               一句最终结果（sentence_end）
    {"type": "finished", "text": str}             任务正常结束，text=累计全文
    {"type": "failed", "code": str, "message": str} 任务失败

生命周期：start() → (feed()*) → finish() → (finished|failed) → close()
浏览器异常断开时 service 直接调 close()，不保证走 finish()。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

# 事件回调：await on_event(dict)
ProviderEventCallback = Callable[[dict], Awaitable[None]]


class RealtimeProvider(ABC):
    """一个 provider 实例 = 一次识别任务。不可复用。"""

    @abstractmethod
    async def start(self, on_event: ProviderEventCallback) -> None:
        """建立上游连接并发起任务。返回后调用方可开始 feed()。

        抛异常 = 启动失败（握手失败 / 鉴权失败 / 上游拒绝）。
        任务成功启动必须 emit {"type": "started"}。
        """

    @abstractmethod
    async def feed(self, frame: bytes) -> None:
        """转发一段二进制音频（PCM 单声道）。"""

    @abstractmethod
    async def finish(self) -> None:
        """通知上游音频发送完毕。finished / failed 事件随后到达。"""

    @abstractmethod
    async def close(self) -> None:
        """立即释放连接（取消场景）。幂等。"""


class ProviderEvent:
    """事件构造小工具（也当文档用）。"""

    @staticmethod
    def started() -> dict[str, Any]:
        return {"type": "started"}

    @staticmethod
    def partial(text: str) -> dict[str, Any]:
        return {"type": "partial", "text": text}

    @staticmethod
    def final(text: str) -> dict[str, Any]:
        return {"type": "final", "text": text}

    @staticmethod
    def finished(text: str) -> dict[str, Any]:
        return {"type": "finished", "text": text}

    @staticmethod
    def failed(code: str, message: str) -> dict[str, Any]:
        return {"type": "failed", "code": code, "message": message}
