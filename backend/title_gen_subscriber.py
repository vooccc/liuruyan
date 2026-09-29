"""v2 · B-11 · title_gen_subscriber

订阅 EventBus 的 ``title_gen_requested`` 事件 → 调便宜模型生成 ≤8 字
会话标题 → 写回 session.title → 发 SSE 让前端实时刷新。

mw 14 TitleGenMiddleware（在 chain 里跑）emit 事件，本订阅者承接。
设计上跟 TraceJournal 同模式：lifespan 注册 + EventBus.subscribe + detach。

为什么 mw 不直接做：harness 不能 import backend.* —— SessionManager / LLMClient
都在 backend；通过 EventBus 解耦是 boundary CI 红线下的唯一正确路径。

为什么 fire-and-forget：标题生成失败不应影响主对话；LLM 调用最多 1-2 秒，
跟前端 first round 完成是同时刻——稍后到达的 ``session_title_updated`` SSE
事件让前端把"用户消息前 30 字"傻截断标题替换成 LLM 生成的智能标题。

异常处理（全 swallow + log）：
- LLM 调用失败 → 保留 v1 已写的傻截断标题（不动）
- update_title 失败 → log；session 文件可能锁/IO 错，下次再试机会很多
- send_event 失败 → log；前端下次 refresh session list 时仍能看到新标题
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)


# 生成提示词——简短 + 中文 + 字数约束
_SYSTEM_PROMPT = (
    "你是会话标题生成助手。"
    "根据用户首条消息和 AI 首条响应，生成一个 4 到 8 个字的中文标题。"
    "标题要点出对话核心主题，不要标点符号，不要引号，不要书名号，不要表情符号。"
    "只输出标题文本本身，不要任何前后缀（如「标题：」「答：」之类）。"
)

# 标题长度上限（字符数）
_TITLE_MIN_CHARS = 2
_TITLE_MAX_CHARS = 12

# LLM 调用超时（秒）— 标题不重要，超时直接放弃
_LLM_TIMEOUT_SEC = 10.0


class TitleGenSubscriber:
    """EventBus 订阅者——title_gen_requested → LLM → SessionManager → SSE。"""

    EVENT_TYPE = "title_gen_requested"

    def __init__(self) -> None:
        self._unsubscribe: Optional[Any] = None  # EventBus.subscribe 返的 callable

    def attach_to(self, event_bus: Any) -> None:
        """注册到 EventBus。lifespan startup 时调一次。"""
        if self._unsubscribe is not None:
            log.warning("TitleGenSubscriber: already attached, skipping")
            return
        self._unsubscribe = event_bus.subscribe(
            self.EVENT_TYPE, self._handle, slow_threshold_ms=20000
        )
        log.info("TitleGenSubscriber attached to EventBus")

    def detach(self) -> None:
        """lifespan shutdown 时调。"""
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                log.exception("TitleGenSubscriber detach failed")
            self._unsubscribe = None

    async def _handle(self, event: Any) -> None:
        """收到事件 → fire-and-forget 启动后台任务。

        我们不在订阅回调里 await LLM——EventBus.publish 串行 fan-out，慢消费
        会拖累其它订阅者（虽然有 slow_threshold_ms 警告）。这里直接
        ``asyncio.create_task`` 把工作丢出去。
        """
        try:
            asyncio.create_task(self._do_generate(event))
        except Exception:
            log.exception("TitleGenSubscriber: failed to schedule task")

    async def _do_generate(self, event: Any) -> None:
        """实际工作：调 LLM 生成 → 写 session → 发 SSE。"""
        try:
            session_id = getattr(event, "session_id", None) or ""
            agent_id = getattr(event, "agent_id", None) or ""
            payload = getattr(event, "payload", None) or {}
            user_message = (payload.get("user_message") or "").strip()
            ai_excerpt = (payload.get("ai_response_excerpt") or "").strip()

            if not session_id:
                log.debug("TitleGen: empty session_id, skip")
                return
            if not user_message:
                log.debug("TitleGen: empty user_message for session=%s", session_id)
                return

            from backend.session import get_session_manager
            sm = get_session_manager()
            session = await sm.get_session(session_id)

            title = await self._call_llm(
                user_message,
                ai_excerpt,
                session_id=session_id,
                agent_id=agent_id,
                session=session,
            )
            if not title:
                log.debug("TitleGen: empty title from LLM session=%s", session_id)
                return

            cleaned = _clean_title(title)
            if not cleaned:
                return

            # 写 session.title
            try:
                await sm.update_title(session_id, cleaned)
            except Exception:
                log.exception(
                    "TitleGen: update_title failed session=%s", session_id
                )
                return

            # 发 SSE
            try:
                from backend.sse_manager import get_sse_manager
                sse = get_sse_manager()
                await sse.send_event(
                    session_id,
                    {
                        "type": "session_title_updated",
                        "payload": {
                            "session_id": session_id,
                            "title": cleaned,
                        },
                    },
                )
            except Exception:
                log.exception(
                    "TitleGen: SSE send_event failed session=%s", session_id
                )

            log.info(
                "TitleGen: session=%s agent=%s title=%r",
                session_id, agent_id, cleaned,
            )
        except Exception:
            log.exception("TitleGenSubscriber._do_generate failed")

    async def _call_llm(
        self,
        user_message: str,
        ai_excerpt: str,
        session_id: str = "",
        agent_id: str = "",
        session: Any = None,
    ) -> str:
        """调当前会话/角色的模型生成标题（限制超时 10s）。失败返空串。"""
        call_cfg = None
        try:
            from backend.llm_call_config import resolve_call_model_config
            call_cfg = resolve_call_model_config(
                agent_id=agent_id or None,
                session=session,
            )
        except Exception as exc:
            log.warning("TitleGen: resolve_call_model_config failed (agent=%s session=%s): %s", agent_id, session_id, exc)

        try:
            from backend.llm_client import get_llm_client, use_call_config
            client = get_llm_client()
        except Exception:
            log.exception("TitleGen: get_llm_client failed")
            return ""

        # 构造 messages：system prompt + 一段 user 把"用户消息+AI 响应"粘进去
        user_block = (
            f"【用户首条消息】\n{user_message}\n\n"
            f"【AI 首条响应（节选）】\n{ai_excerpt or '（无文本响应）'}"
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_block},
        ]
        try:
            with use_call_config(call_cfg):
                # chat_completion_async 内部已写死 use_tools=False / stream=False
                result = await asyncio.wait_for(
                    client.chat_completion_async(messages),
                    timeout=_LLM_TIMEOUT_SEC,
                )
        except asyncio.TimeoutError:
            log.warning("TitleGen: LLM call timeout (%.1fs)", _LLM_TIMEOUT_SEC)
            return ""
        except Exception:
            log.exception("TitleGen: LLM call failed")
            return ""

        if isinstance(result, dict):
            content = result.get("content") or ""
        else:
            content = str(result or "")
        return content.strip()


# 模块级单例 — main.py lifespan 引用
_singleton: Optional[TitleGenSubscriber] = None


def get_title_gen_subscriber() -> TitleGenSubscriber:
    global _singleton
    if _singleton is None:
        _singleton = TitleGenSubscriber()
    return _singleton


# ── 标题清洗 ────────────────────────────────────────────────────────────


_PUNCT_STRIP_RE = re.compile(r"[\s　\"'`「」『』《》【】\[\]\(\)<>:：，。、！？!?.,;；]")
_LEADING_LABEL_RE = re.compile(
    r"^(标题|主题|题目|答|输出|结果|title)\s*[:：]\s*",
    re.IGNORECASE,
)


def _clean_title(raw: str) -> str:
    """清洗 LLM 输出：剥前缀、剥引号、收紧空白、长度兜底。"""
    s = raw.strip()
    if not s:
        return ""
    # 去 "标题：" 这类前缀
    s = _LEADING_LABEL_RE.sub("", s).strip()
    # 多行只取第一行
    if "\n" in s:
        s = s.split("\n", 1)[0].strip()
    # 去首尾标点 / 空白
    while s and _PUNCT_STRIP_RE.match(s[0]):
        s = s[1:]
    while s and _PUNCT_STRIP_RE.match(s[-1]):
        s = s[:-1]
    # 长度兜底
    if len(s) < _TITLE_MIN_CHARS:
        return ""
    if len(s) > _TITLE_MAX_CHARS:
        s = s[:_TITLE_MAX_CHARS]
    return s


__all__ = [
    "TitleGenSubscriber",
    "get_title_gen_subscriber",
]
