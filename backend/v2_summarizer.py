"""v2 · C5-c-3 · backend summarizer 实现 — 包装 LLMClient 跑摘要

设计：docs/architecture_v2/03_middlewares.md § 09 + handover_v2_phase_c5c_apply_summary_landed.md

实现 ``Summarizer`` 协议（``async (text_to_summarize: str) -> str``）：用
``LLMClient.chat_completion_async``（已经是非流式 + use_tools=False 不会触发
工具循环）跑摘要 prompt。

**当前用 LLMClient 默认 model**——03_middlewares.md § 09 设计建议用 cheaper
模型，但 v1 LLMClient 是全局单例，``set_model`` 切换会污染其它 chat 调用的
context。最小改动：summarizer 用默认 model；如果要 cheaper，env
``LIURUYAN_SUMMARIZE_MODEL`` 配独立 client（v3 升级再做）。

异常处理：LLM 抛异常 / 返空 → 返空字符串；mw 09 收到空 summary 走 fallback
drop（不插空 summary 减少噪音）。这跟 ``apply_summary`` 的契约对齐。

prompt 设计要点：
- 第三人称客观陈述（避免摘要里出现"你""我"误导后续 LLM）
- 限长 ≤ 500 字（中文混合）
- 突出："用户身份/目标、关键决定、未解决问题"
- 禁止编造、客套、引号原话

不在 ``backend/channels/`` 是因为 summarizer 跟 channels 没关系——它是
v2 chain 的辅助。放在 backend/ 顶层跟 v2_bootstrap.py 一类。

routes 集成：v2_bootstrap setup 时通过 ``LeadAgent(agent, summarizer=...)``
注入；agent.py ``_ensure_v2_lead`` 用 ``make_summarizer(self.llm_client)``
构造。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


_DEFAULT_SUMMARY_PROMPT = (
    "你是对话历史压缩助手。把下面这段对话历史压缩成 ≤ 500 字的核心摘要。\n\n"
    "保留以下信息：\n"
    "- 用户的身份、目标、当前关注的话题\n"
    "- 已经达成的关键决定 / 给出的关键事实\n"
    "- 用户提的问题如果还没解决，要明确点出来\n"
    "- 工具调用如果产生过重要发现（文件位置、错误根因等）\n\n"
    "禁止：\n"
    "- 编造没出现过的信息\n"
    "- 给读者的客套话（'好的'、'我会'）\n"
    "- 引号包原话；用第三人称客观陈述\n"
    "- 加 markdown 标题 / 列表（输出 1~3 段连续文字即可）"
)


SummarizerFunc = Callable[[str], Awaitable[str]]


def make_summarizer(
    llm_client: Any,
    *,
    system_prompt: str | None = None,
) -> SummarizerFunc:
    """工厂：返符合 ``Summarizer`` 协议的 async 函数。

    参数：
    - ``llm_client``: v1 LLMClient 实例（必须有 ``chat_completion_async`` 方法）
    - ``system_prompt``: 自定义摘要 prompt；默认见 ``_DEFAULT_SUMMARY_PROMPT``

    生产用法：
    ```python
    from backend.agent import get_agent
    from backend.v2_summarizer import make_summarizer

    agent = get_agent("liuruyan")
    summarizer = make_summarizer(agent.llm_client)
    lead = LeadAgent(agent, summarizer=summarizer)
    ```
    """
    prompt = system_prompt or _DEFAULT_SUMMARY_PROMPT

    async def _summarize(text_to_summarize: str) -> str:
        if not text_to_summarize or not text_to_summarize.strip():
            return ""

        prompt_messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text_to_summarize},
        ]
        try:
            result = await llm_client.chat_completion_async(prompt_messages)
        except Exception:
            log.exception("[v2:summarizer] chat_completion_async raised")
            return ""

        if not isinstance(result, dict):
            log.warning(
                "[v2:summarizer] unexpected return type: %s",
                type(result).__name__,
            )
            return ""

        content = result.get("content") or ""
        return content.strip() if isinstance(content, str) else ""

    return _summarize


def is_summarize_activated() -> bool:
    """env LIURUYAN_SUMMARIZE_ACTIVATE 判 mw 09 是否真接管。

    默认 OFF 保守——避免 mw 09 真摘要在没充分验证的部署上影响对话连续性。
    """
    raw = (os.getenv("LIURUYAN_SUMMARIZE_ACTIVATE") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


__all__ = ["make_summarizer", "is_summarize_activated", "SummarizerFunc"]
