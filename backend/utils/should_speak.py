"""TTS 朗读判定：把 LLM 输出过滤为"该用嘴说出口的部分"。

设计动机
========
柳如烟是工作场景 AI，回复动辄几千字。如果把整段都塞给 TTS，用户喝完一杯
咖啡都念不完。**关键洞察：TTS 不是"把 LLM 输出念出来"，而是给 LLM 加一个
"嘴" —— 它自己决定什么话用嘴说，什么话用文字给你看**。

核心机制：``<say>...</say>`` 标签 + 三档全局开关。

机制说明
========
1. LLM 在 system prompt 里被告知约定（用户复制粘贴到角色 system prompt 末尾）：
     - 默认所有内容只显示，不念
     - 用 <say>...</say> 标记你想"用嘴说"的关键句（结论 / 提醒 / 确认）
     - 一次回复通常 0-2 个 <say>，每个 ≤ 30 字
     - 长分析、代码、表格、文件路径绝不放进 <say>

2. 全局三档：
     - off: 完全不念（默认；工作场景默认安静）
     - key: 只念 <say> 标记 + ≤ 80 字短回复（推荐工作场景）
     - all: 长文本也念（陪伴 / 挂机模式）

3. 兜底规则（mode='key' 且无 <say> 标签时）：
     - 含代码块 ``` / 表格 | / len > 80 → 不念
     - len ≤ 80 → 全念（"好的"/"已完成"这种短反馈）

零污染纪律
==========
本模块只在 ``/tts/synthesize`` 路由内被调用，**不进 LLM chunk 流**。
<say> 标签的提取也只在前端 ChatBubble 渲染层做（M2 任务），后端只在用户
显式调用 /tts/test 或 /tts/synthesize 时才会用到这个判定。

设计文档：``_docs/handover_tts_design_2026-05-29.md``。
"""
from __future__ import annotations

import re
from typing import List, Literal, Optional

# mode 字面量类型，跟 TTSConfig.mode 保持同步。
SpeakMode = Literal["off", "key", "all"]

# 兜底规则的"短回复"上限：≤80 字直接念，> 80 字不念。
# 80 字大约对应中文一两句话或英文一段（150-200 chars 折算）。
_SHORT_REPLY_CHAR_LIMIT = 80

# <say>…</say> 抽取正则：非贪婪、跨行（DOTALL）。允许标签内含换行。
_SAY_TAG_RE = re.compile(r"<say>(.*?)</say>", flags=re.DOTALL | re.IGNORECASE)


def extract_say_segments(text: str) -> List[str]:
    """从原始文本里抽出所有 <say>...</say> 标记的内容。

    返回去掉首尾空白的片段列表。空 <say></say> 或纯空白片段会被丢弃。
    标签外的文本完全忽略。

    Examples:
        >>> extract_say_segments("blah <say>hello</say> blah")
        ['hello']
        >>> extract_say_segments("<say>第一句</say>中间忽略<say> 第二句 </say>")
        ['第一句', '第二句']
        >>> extract_say_segments("<say></say><say>   </say>")
        []
        >>> extract_say_segments("没有标签的纯文本")
        []
    """
    matches = _SAY_TAG_RE.findall(text or "")
    return [m.strip() for m in matches if m and m.strip()]


def strip_say_tags(text: str) -> str:
    """删除文本里的 <say>…</say> 包装，但**保留**标签内的内容。

    用途：mode='all' 时不只念 say 段，整段都念，但要把 say 标签字面去掉，
    避免 TTS 念出 "less-than say greater-than" 之类的字面。

    Examples:
        >>> strip_say_tags("hi <say>note</say> bye")
        'hi note bye'
        >>> strip_say_tags("纯文本不变")
        '纯文本不变'
    """
    if not text:
        return ""
    return _SAY_TAG_RE.sub(lambda m: m.group(1) or "", text)


def should_speak(text: str, mode: SpeakMode) -> Optional[str]:
    """根据 mode 与文本特征决定要不要念，以及念什么。

    Args:
        text: LLM 原始输出（含 <say> 标签的全文）。
        mode: 三档全局开关。

    Returns:
        要送给 TTS 的纯文本（已剥去 <say> 标签）。None 表示不念。

    判定流程（按 mode 分支）：

    - ``off``：永远返 None（最高优先级，连 <say> 都忽略）。
    - ``key``：
        * 有 <say> 标签 → 拼接所有 say 段返回（用户显式标的，最听话）
        * 无 say 标签且含代码块 ```、表格 |、长度 > 80 → None（兜底拒绝）
        * 无 say 标签且 ≤ 80 字 → 念全文（短回复直接念，避免漏掉"好的""完成"）
    - ``all``：
        * 有 <say> 标签 → 念整段（带 say 内容），但去掉标签字面
        * 无 say 标签 → 念整段
        无论如何长文本都念。陪伴 / 挂机场景用。

    Examples:
        >>> should_speak("任意文本", "off")
        >>> should_speak("...<say>修好了</say>...", "key")
        '修好了'
        >>> should_speak("好的", "key")
        '好的'
        >>> should_speak("a" * 200, "key")
        >>> should_speak("```py\\ncode\\n```", "key")
        >>> should_speak("a" * 200, "all")  # doctest: +ELLIPSIS
        'aaa...'
    """
    if mode == "off":
        return None

    text = text or ""
    if not text.strip():
        return None

    say_segments = extract_say_segments(text)

    if mode == "all":
        # 全念：把 <say> 标签字面剥掉，保留内容
        cleaned = strip_say_tags(text).strip()
        return cleaned or None

    # mode == "key"
    if say_segments:
        # LLM 显式标了 say —— 完全听话，只念这些段
        joined = " ".join(say_segments).strip()
        return joined or None

    # 没标 say —— 走兜底规则
    if _looks_like_long_or_structural(text):
        return None
    if len(text) > _SHORT_REPLY_CHAR_LIMIT:
        return None
    # 短回复 → 直接念
    return text.strip() or None


def _looks_like_long_or_structural(text: str) -> bool:
    """判断文本是否含"绝对不念"的结构化特征。

    触发以下任一即拒绝：
    - 三反引号代码块（```py / ``` / ```bash 之类）
    - markdown 表格（含至少一行包含两个以上 |）
    - 多个文件路径（出现 2+ 个 / 或 \\ 风格路径片段）—— 念路径只会念出 slash slash
      让人崩溃，且工作场景路径很常见。

    实现保守：宁可错过几个该不念的（让 80 字上限兜底），不要把"好的，
    分隔符是 |"这种短句误判。所以表格判定要求至少一行有两个 | 而不是出现 |。

    Examples:
        >>> _looks_like_long_or_structural("```py\\nprint(1)\\n```")
        True
        >>> _looks_like_long_or_structural("| col1 | col2 | col3 |")
        True
        >>> _looks_like_long_or_structural("好的，我用 | 分隔")
        False
        >>> _looks_like_long_or_structural("文件在 /a/b 和 /c/d 还有 /e/f")
        True
        >>> _looks_like_long_or_structural("普通短句")
        False
    """
    if "```" in text:
        return True

    # 表格：至少一行包含 ≥ 2 个 |
    for line in text.splitlines():
        if line.count("|") >= 2:
            return True

    # 多文件路径：≥ 2 个 / 风格或 \\ 风格路径片段
    # 用最朴素的启发式：连续的 [/或\\] + 字母数字下划线/点
    path_hits = re.findall(r"[/\\][\w.\-]+(?:[/\\][\w.\-]+)+", text)
    if len(path_hits) >= 2:
        return True

    return False


__all__ = [
    "SpeakMode",
    "extract_say_segments",
    "strip_say_tags",
    "should_speak",
]
