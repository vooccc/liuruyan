"""v2 · H · Slice 4 · 文本泄漏 tool_call 的 grammar 实现.

每个 grammar 知道一种"伪装成正文文本的 tool_call"形态：
- `python_call.py`: 讯飞 GLM5 / 部分 OpenAI 兼容路径的 `name(args)` 形态
- `dsml.py` (Slice 4.5): DeepSeek 的 `<｜｜DSML｜｜...>` 标签形态

Grammar 都是 stateful 状态机对象（per-stream 实例），由
`text_leaked_tool_call` plugin 工厂注入。
"""

from .python_call import PythonCallGrammar

__all__ = ["PythonCallGrammar"]
