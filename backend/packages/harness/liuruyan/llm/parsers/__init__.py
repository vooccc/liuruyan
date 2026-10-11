"""v2 · H · Per-provider stream parsers.

每个 provider 一个文件——出站插件与中间件流水线模式的 Python 翻译。
"""

from .anthropic_parser import parse_anthropic_stream
from .openai_parser import parse_openai_stream

__all__ = [
    "parse_openai_stream",
    "parse_anthropic_stream",
]
