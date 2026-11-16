"""realtime ASR provider 抽象。新增引擎（火山 / GLM-ASR / 本地 sherpa 等）时
实现 RealtimeProvider 并在 routes.py 的工厂里挂上即可，其余代码不动。"""
from __future__ import annotations

from .base import ProviderEvent, RealtimeProvider

__all__ = ["ProviderEvent", "RealtimeProvider"]
