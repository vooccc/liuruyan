"""v2 · H · Slice 4 · Python-call grammar.

识别 `name(args)` 形态的文本泄漏 tool_call ——
讯飞 GLM5 等 OpenAI 兼容协议把 native tool_call 当正文文本吐出来时常见。

例（用户真实截图，2026-05-02）：
    三个任务已立项。开始执行。task_update(task_id="192", status="in_progress")
    read_file("D:\\AI_Project\\智能体\\柳如烟\\backend\\main.py")

设计策略：
- **保守识别**：只匹配明确的 `name(...)` 完整闭合形态，括号嵌套用 brace counting
- **白名单 tool_name**：仅当 name 是已知 LLM 工具时才认为是 tool_call（`get_known_tool_names()`
  注入），否则当普通文本透传——避免误识别 `print("hi")` 这种代码块
- **args 翻译**：把 Python kwarg 形态 `key="value", key=value` 翻译成 JSON dict 字符串；
  单个位置参数（如 `read_file("path")`）不知道 key 名，直接报 ParseError 让消费层兜底
- **不救严重破损形态**：如 `task_updatetask_id "192"` 缺 paren 缺 `=` 这种，无解

跨 chunk 边界：
- buffer 跨 feed 累积
- 一次 feed 可能产出多个 ToolCallDelta（多个完整 `name(...)` 在同一段文本内）
- buffer 末尾若是 partial（未闭合括号）→ 留作 remaining，下次续

⚠️ 本 grammar 不做完整 Python AST parse —— 性能 + 误判风险高。
   仅用 `ast.parse` + `ast.literal_eval` 处理 args 字面量。
"""

from __future__ import annotations

import ast
import json
import re
import uuid
from typing import Callable

from backend.packages.harness.liuruyan.llm.events import ContentDelta, ParsedEvent, ToolCallDelta


# 匹配 `<identifier>(...)` 起始位置：
# - 工具名是 Python 标识符（字母/下划线开头，含字母/数字/下划线）
# - 紧跟 `(`，不允许之间有空格（避免匹配 `def foo (x)` 等代码）
_NAME_PAREN_START = re.compile(r"\b([A-Za-z_][A-Za-z_0-9]*)\(")


class PythonCallGrammar:
    """识别 `name(args)` 形态的文本泄漏 tool_call。

    Args:
        get_known_tool_names: callable 返回当前已注册的 tool name 集合。
            用于白名单过滤（只有已知工具才识别为 tool_call）。
            注入而非硬编码，保证测试隔离 + 工具集变化不影响 grammar。
    """

    name = "python_call"

    def __init__(self, get_known_tool_names: Callable[[], set[str]]):
        self._known = get_known_tool_names
        self._buffer = ""

    def feed(self, text: str) -> list[ParsedEvent]:
        """喂入一段文本，返回已识别的事件序列。

        未确定的尾部留在 self._buffer，下次 feed 续上。
        """
        self._buffer += text
        events: list[ParsedEvent] = []

        while True:
            match = _NAME_PAREN_START.search(self._buffer)
            if match is None:
                # 整个 buffer 没有 candidate → 全部作为 ContentDelta yield，清 buffer
                # 但若 buffer 末尾有半个 identifier（可能下次接上），保留末尾 50 字符
                if len(self._buffer) > 50:
                    safe = self._buffer[:-50]
                    if safe:
                        events.append(ContentDelta(text=safe))
                    self._buffer = self._buffer[-50:]
                break

            tool_name = match.group(1)
            paren_start = match.end() - 1  # `(` 位置

            # 工具名白名单检查
            if tool_name not in self._known():
                # 不是已知工具 → 把 `name(` 之前的 + `name(` 自身都当文本 yield
                pre = self._buffer[: match.end()]
                events.append(ContentDelta(text=pre))
                self._buffer = self._buffer[match.end():]
                continue

            # 找闭合 `)` —— 用 brace counting，跳过引号内
            close_idx = _find_matching_paren(self._buffer, paren_start)
            if close_idx is None:
                # 括号未闭合 → match 之前的文本 yield，match 之后的留 buffer
                pre = self._buffer[: match.start()]
                if pre:
                    events.append(ContentDelta(text=pre))
                self._buffer = self._buffer[match.start():]
                break

            # 完整闭合：提取 args
            args_text = self._buffer[paren_start + 1: close_idx]
            pre_text = self._buffer[: match.start()]
            if pre_text:
                events.append(ContentDelta(text=pre_text))

            args_json = _python_args_to_json(args_text)
            if args_json is None:
                # args 无法解析 → 整段 `name(args)` 当 ContentDelta 透传（不识别为 tool_call）
                events.append(
                    ContentDelta(text=self._buffer[match.start(): close_idx + 1])
                )
            else:
                synthetic_id = f"text_leaked_{uuid.uuid4().hex[:12]}"
                events.append(ToolCallDelta(
                    index=0,
                    id=synthetic_id,
                    name=tool_name,
                    arguments=args_json,
                ))

            # 推进 buffer
            self._buffer = self._buffer[close_idx + 1:]

        return events

    def flush(self) -> list[ParsedEvent]:
        """流末调一次：把 buffer 残余作为 ContentDelta 全部 yield 出来.

        partial 形态（如未闭合括号）此时也当普通文本 yield ——避免文本永久卡住。
        """
        events: list[ParsedEvent] = []
        if self._buffer:
            events.append(ContentDelta(text=self._buffer))
            self._buffer = ""
        return events


# ── helpers ────────────────────────────────────────────────────────────


def _find_matching_paren(text: str, open_idx: int) -> int | None:
    """从 `(` 位置开始找配对的 `)`，跳过字符串引号内的内容。

    支持单引号 / 双引号 / 反斜杠转义。不支持三引号字符串（讯飞 GLM5 通常不发）。
    返回 `)` 的 index，没找到返 None。
    """
    if text[open_idx] != "(":
        return None

    depth = 0
    in_str: str | None = None  # None / "'" / '"'
    escaped = False
    i = open_idx
    while i < len(text):
        c = text[i]
        if in_str is not None:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == in_str:
                in_str = None
        else:
            if c in ('"', "'"):
                in_str = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


def _python_args_to_json(args_text: str) -> str | None:
    """把 Python 函数调用的 args 翻译成 JSON 字符串。

    支持的形态：
    - kwargs only: `key="value", key2=value2` → `{"key": "value", "key2": ...}`
    - 单个 positional 字符串: `"path/to/file"` → 不知道 key 名，**返 None**（让 grammar
      不识别为 tool_call，避免无 key 的 args dict）
    - 混合 / 多 positional: 也返 None

    使用 `ast.parse` + 容错策略：失败返 None。
    """
    args_text = args_text.strip()
    if not args_text:
        return "{}"

    # 用 ast 解析成 Call 节点 args/kwargs
    try:
        tree = ast.parse(f"_({args_text})", mode="eval")
    except SyntaxError:
        return None

    if not isinstance(tree.body, ast.Call):
        return None
    call = tree.body

    if call.args:
        # 有 positional → 不知道参数名，无法转 JSON dict
        return None

    result: dict[str, object] = {}
    for kw in call.keywords:
        if kw.arg is None:
            return None  # **kwargs 解构，跳过
        try:
            value = ast.literal_eval(kw.value)
        except (ValueError, SyntaxError):
            return None
        result[kw.arg] = value

    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


__all__ = ["PythonCallGrammar"]
