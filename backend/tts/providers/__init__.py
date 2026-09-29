"""TTS provider 实现集合。

新增 provider 步骤：
1. 在本目录新建 ``<name>.py``，实现 ``TTSProvider`` 子类
2. 在 ``backend/tts/engine.py`` 的 dispatch 里加分支
3. 在 ``backend/tts/config.py`` 的 ``ProviderType`` 字面量加值
4. 前端 ``TTSPane.tsx`` 加配置 UI
"""
from .base import TTSProvider, TTSResult, TTSError
from .openai_compat import OpenAICompatProvider
from .gptsovits_compat import GPTSoVITSCompatProvider
from .tencent_compat import TencentCompatProvider
from .baidu_compat import BaiduCompatProvider
from .aliyun_compat import AliyunCompatProvider

__all__ = [
    "TTSProvider",
    "TTSResult",
    "TTSError",
    "OpenAICompatProvider",
    "GPTSoVITSCompatProvider",
    "TencentCompatProvider",
    "BaiduCompatProvider",
    "AliyunCompatProvider",
]
