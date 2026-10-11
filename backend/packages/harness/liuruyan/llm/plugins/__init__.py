"""v2 · H · IR → IR 转换 plugin 链.

出站插件与中间件流水线模式的 Python 翻译。每个 plugin 解决一种
协议怪癖（tool_call 累积 / 文本泄漏 tool_call / reasoning 标签抽取 / ...），
作为 IR 流的纯 transformer，跟具体 vendor 解耦。

设计纪律（参考 protocols.py docstring）：
- 输入输出都是 ParsedEvent 流——不直接接触 vendor SDK chunk
- 无副作用、无 ContextVar
- plugin 顺序：先注册的在最外层（reverse 包装等价 Cherry PluginBuilder
  的 wrapLanguageModel reverse 语义；详见 base.py:apply_plugins）

Plugin 装载策略：
- OpenAI 路径默认装 [tool_call_accumulator]（Slice 3）
- Slice 4 加 text_leaked_tool_call（按 base_url=xf-yun.com / model=deepseek-* 路由）
- Slice 5 加 reasoning_extraction（按 model id 路由 tagName）
- Anthropic 路径默认空（用 final_message 作权威源；Slice 5+ 才可能加）
"""

from .base import apply_plugins
from .openrouter_redacted import openrouter_redacted
from .reasoning_extraction import make_reasoning_extraction_plugin
from .text_leaked_tool_call import make_text_leaked_tool_call_plugin
from .tool_call_accumulator import tool_call_accumulator

__all__ = [
    "apply_plugins",
    "tool_call_accumulator",
    "make_text_leaked_tool_call_plugin",
    "make_reasoning_extraction_plugin",
    "openrouter_redacted",
]
