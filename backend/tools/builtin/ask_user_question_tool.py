"""AskUserQuestion tool — 向用户发出带选项的问题。

v2 阻塞语义（基于交互式 AskUserQuestion 规范设计）：

- 工具 handler 立即返回 ``awaiting_user_choice``（前端据此渲染选项卡片），
  但 payload 会带 ``wait_for_answer: true`` 标记 + ``session_id``；
- 真正的"等用户"发生在 agent 工具循环（llm_client / anthropic_client 的
  ``_chat_with_tools``）：轮末检测到本期待回答的 ask → 挂起等待用户作答 →
  答案改写进该 tool_call 的 tool_result → **同一轮继续**，模型拿到的
  tool_result 就是用户答案（不再是"下一条 user message 回流"）；
- 非交互场景（渠道号 / cron / 子代理，未设 ``_INTERACTIVE_TURN``）不等待：
  payload 无 ``wait_for_answer``，循环据此**直接结束本轮**，不再把
  awaiting_user_choice 喂回模型（修复"提问完自己继续思考"）。

参数校验双重：input_schema 声明 + handler 运行时校验，防御性 defense in depth。
"""

import asyncio
import contextvars
import json
import logging
import os
import time
from typing import List, Dict, Any, Optional, Tuple

import uuid

logger = logging.getLogger(__name__)

# 由 main.py /chat 的流式分支（pump_agent）在调 agent.chat 前置 True：
# 只有网页端 SSE 会话才算"用户在场"，渠道号 / cron / 子代理一律视为不在场。
_INTERACTIVE_TURN: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "ask_user_interactive_turn", default=False
)

# 交互会话等待用户作答的最长时间（秒）。超时后本轮结束，tool_result 里留
# 超时说明；用户之后发的消息作为下一轮输入自然接续。
_DEFAULT_ASK_WAIT_TIMEOUT_SECONDS = 1800


def _ask_wait_timeout_seconds() -> float:
    try:
        return float(
            os.getenv("ASK_USER_WAIT_TIMEOUT_SECONDS")
            or _DEFAULT_ASK_WAIT_TIMEOUT_SECONDS
        )
    except (TypeError, ValueError):
        return float(_DEFAULT_ASK_WAIT_TIMEOUT_SECONDS)


class _PendingAsk:
    """一个待回答的 ask：Future 由工具循环 await，由 /chat/ask/answer resolve。"""

    __slots__ = ("future", "session_id", "ask_id", "tool_use_id", "created_at")

    def __init__(self, session_id: str, ask_id: str, tool_use_id: Optional[str]):
        self.future: "asyncio.Future" = asyncio.get_running_loop().create_future()
        self.session_id = session_id
        self.ask_id = ask_id
        self.tool_use_id = tool_use_id
        self.created_at = time.monotonic()


# key: (session_id, ask_id)。handler 注册（先于 tool_result SSE 事件到达前端，
# 因此前端提交时注册表里一定已有条目，无竞态）。
_PENDING_ASKS: "Dict[Tuple[str, str], _PendingAsk]" = {}


def _register_pending_ask(session_id: str, ask_id: str, tool_use_id: Optional[str]) -> _PendingAsk:
    entry = _PendingAsk(session_id, ask_id, tool_use_id)
    _PENDING_ASKS[(session_id, ask_id)] = entry
    return entry


def _pop_pending_ask(session_id: str, ask_id: str) -> Optional[_PendingAsk]:
    return _PENDING_ASKS.pop((session_id, ask_id), None)


def resolve_pending_ask(
    session_id: str,
    ask_id: str,
    answers: Optional[List[Dict[str, Any]]] = None,
    cancelled: bool = False,
) -> bool:
    """由 HTTP 端点调用：把用户的回答/取消投递给正在 await 的工具循环。

    返回 False 表示没有这个 pending ask（已超时/已取消/后端重启/页面刷新），
    调用方（前端）应降级走"答案作为普通消息回流"的旧路径。
    """
    entry = _pop_pending_ask(session_id, ask_id)
    if entry is None or entry.future.done():
        return False
    if cancelled:
        entry.future.set_result({"outcome": "cancelled", "answers": None})
    else:
        entry.future.set_result({"outcome": "answered", "answers": answers or []})
    return True


def cancel_pending_asks_for_session(session_id: str, reason: str = "turn_ended") -> int:
    """会话级清理：本轮被停止/断连时由循环 finally 调用，防注册表泄漏。"""
    removed = 0
    for key in [k for k in _PENDING_ASKS if k[0] == session_id]:
        entry = _PENDING_ASKS.pop(key)
        if not entry.future.done():
            entry.future.set_result({"outcome": "cancelled", "answers": None})
        removed += 1
    if removed:
        logger.info(
            "[ask_user_question] cancelled %d pending ask(s) for session=%s (%s)",
            removed, session_id, reason,
        )
    return removed


def _validate_option(opt: Any, q_idx: int, o_idx: int) -> Dict[str, Any]:
    if not isinstance(opt, dict):
        raise ValueError(f"questions[{q_idx}].options[{o_idx}] 必须是 dict")
    label = (opt.get("label") or "").strip()
    if not label:
        raise ValueError(f"questions[{q_idx}].options[{o_idx}].label 不能为空")
    if len(label) > 60:
        raise ValueError(
            f"questions[{q_idx}].options[{o_idx}].label 过长（{len(label)}>60）"
        )
    description = (opt.get("description") or "").strip()
    if not description:
        raise ValueError(
            f"questions[{q_idx}].options[{o_idx}].description 不能为空"
        )
    preview = opt.get("preview")
    if preview is not None and not isinstance(preview, str):
        raise ValueError(
            f"questions[{q_idx}].options[{o_idx}].preview 必须是字符串或省略"
        )
    return {
        "label": label,
        "description": description,
        "preview": preview,
    }


def _validate_question(q: Any, idx: int) -> Dict[str, Any]:
    if not isinstance(q, dict):
        raise ValueError(f"questions[{idx}] 必须是 dict")
    question_text = (q.get("question") or "").strip()
    if not question_text:
        raise ValueError(f"questions[{idx}].question 不能为空")
    header = (q.get("header") or "").strip()
    if not header:
        raise ValueError(f"questions[{idx}].header 不能为空")
    if len(header) > 12:
        raise ValueError(
            f"questions[{idx}].header 长度须 ≤12（当前 {len(header)}）"
        )
    options = q.get("options")
    if not isinstance(options, list) or not 2 <= len(options) <= 4:
        raise ValueError(f"questions[{idx}].options 必须是 2-4 个选项")
    validated_options = [
        _validate_option(opt, idx, j) for j, opt in enumerate(options)
    ]
    # 禁止 LLM 自己塞 Other（UI 会兜底追加）
    for opt in validated_options:
        if opt["label"].lower() in ("other", "其他", "else"):
            raise ValueError(
                f"questions[{idx}] 不要自己塞 'Other' 选项，UI 会自动追加兜底项"
            )
    return {
        "question": question_text,
        "header": header,
        "options": validated_options,
        "multiSelect": bool(q.get("multiSelect", False)),
    }


async def ask_user_question(
    questions: List[Dict[str, Any]],
    session_id: Optional[str] = None,
    **_unused,
) -> Dict[str, Any]:
    """Ask the user 1-4 multiple-choice questions.

    交互会话（网页端）：返回带 ``wait_for_answer`` 的 payload；工具循环会挂起
    等待用户在卡片上作答，答案稍后**作为本工具的 tool_result** 回流——模型在
    同一轮拿到答案后继续执行。发出提问后不要再调用其他工具或输出正文。

    非交互场景（渠道号 / cron / 子代理）：立即返回，无 ``wait_for_answer``；
    工具循环会直接结束本轮，用户的下一次回复作为新消息接续。
    """
    if not isinstance(questions, list):
        raise ValueError("questions 必须是列表")
    if not 1 <= len(questions) <= 4:
        raise ValueError(f"questions 必须包含 1-4 个问题（当前 {len(questions)}）")

    validated = [_validate_question(q, i) for i, q in enumerate(questions)]

    payload: Dict[str, Any] = {
        "status": "awaiting_user_choice",
        "ask_id": uuid.uuid4().hex[:12],
        "questions": validated,
    }

    interactive = bool(_INTERACTIVE_TURN.get(False)) and bool(session_id)
    if interactive:
        ask_id = payload["ask_id"]
        entry = _register_pending_ask(session_id, ask_id, None)
        payload["session_id"] = session_id
        payload["wait_for_answer"] = True
        payload["wait_timeout_seconds"] = int(_ask_wait_timeout_seconds())
        payload["note"] = (
            "问题已推送到前端卡片。本轮在此挂起等待用户作答；用户的回答将作为"
            "本工具的 tool_result 返回，拿到后继续执行。"
        )
        logger.info(
            "[ask_user_question] registered pending ask session=%s ask_id=%s",
            session_id, ask_id,
        )
    else:
        payload["note"] = (
            "问题已发出。当前场景用户不在线交互，本轮到此结束；"
            "用户的下一次回复会作为新消息接续。"
        )
    return payload


# ============================================================================
# 工具循环侧：轮末暂停等待（由 llm_client / anthropic_client._chat_with_tools 调）
# ============================================================================

_ASK_TOOL_NAME = "ask_user_question"


def _parse_ask_payload(result_str: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(result_str, str):
        return None
    if "awaiting_user_choice" not in result_str:
        return None
    try:
        parsed = json.loads(result_str)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(parsed, dict) and parsed.get("status") == "awaiting_user_choice":
        return parsed
    return None


def _format_answer_text(questions: List[Dict[str, Any]], answers: List[Dict[str, Any]]) -> str:
    """把用户的回答格式化成喂给模型的可读文本。"""
    by_question: Dict[str, str] = {}
    for ans in answers or []:
        if isinstance(ans, dict) and ans.get("question"):
            by_question[str(ans["question"])] = str(ans.get("answer", "")).strip()
    parts = []
    for idx, q in enumerate(questions or []):
        q_text = str(q.get("question", ""))
        a_text = by_question.get(q_text, "")
        if not a_text:
            a_text = "（用户未回答此题）"
        parts.append(f"{idx + 1}. {q_text}\n   用户回答：{a_text}")
    return (
        "用户已回答你的提问：\n"
        + "\n".join(parts)
        + "\n\n请基于以上回答继续执行。"
    )


def _answered_ui_payload(payload: Dict[str, Any], answers: List[Dict[str, Any]]) -> str:
    """给前端/持久化的 tool_result_update 载荷：卡片刷新后仍能渲染"已回答"态。"""
    return json.dumps(
        {
            "status": "answered_user_choice",
            "ask_id": payload.get("ask_id"),
            "questions": payload.get("questions") or [],
            "answers": answers or [],
        },
        ensure_ascii=False,
    )


def _timeout_ui_payload(payload: Dict[str, Any]) -> str:
    return json.dumps(
        {
            "status": "ask_user_timeout",
            "ask_id": payload.get("ask_id"),
            "note": "用户长时间未回答，本轮已结束；用户之后的回复会作为新消息接续。",
        },
        ensure_ascii=False,
    )


def _update_round_tool_messages(round_done: Dict[str, Any], tool_use_id: str, final_text: str) -> None:
    """把等待期间已 yield 出去的 awaiting tool_result，就地改写为最终内容。

    兼容两条循环的消息形状：
    - llm_client：tool_result_msgs = [{"role":"tool","tool_call_id":...,"content":...}]
    - anthropic_client：tool_result_msg = {"role":"user","content":[{type:"tool_result","tool_use_id":...,"content":...}]}
    """
    for msg in round_done.get("tool_result_msgs") or []:
        if isinstance(msg, dict) and msg.get("tool_call_id") == tool_use_id:
            msg["content"] = final_text
    anth_msg = round_done.get("tool_result_msg")
    if isinstance(anth_msg, dict) and isinstance(anth_msg.get("content"), list):
        for block in anth_msg["content"]:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == tool_use_id
            ):
                block["content"] = final_text


def _resolve_tool_use_id(round_done: Dict[str, Any], index: int) -> str:
    """按调用序号拿 tool_use_id，兼容两条循环的消息形状：
    - llm_client：executed_tool_calls[i] 与 tool_result_msgs[i]（含 tool_call_id）同序；
    - anthropic_client：executed_tool_calls[i] 与 tool_result_msg.content[i]
      （含 tool_use_id）同序；executed_tool_calls 本身不带 id。
    """
    calls = round_done.get("executed_tool_calls") or []
    if index < len(calls):
        call = calls[index]
        if isinstance(call, dict):
            cid = call.get("id") or call.get("tool_use_id")
            if cid:
                return str(cid)
    msgs = round_done.get("tool_result_msgs") or []
    if index < len(msgs) and isinstance(msgs[index], dict):
        tid = msgs[index].get("tool_call_id")
        if tid:
            return str(tid)
    anth = round_done.get("tool_result_msg")
    if isinstance(anth, dict) and isinstance(anth.get("content"), list):
        blocks = [b for b in anth["content"] if isinstance(b, dict)]
        if index < len(blocks):
            tid = blocks[index].get("tool_use_id")
            if tid:
                return str(tid)
    return ""


def _iter_round_asks(round_done: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """[(tool_use_id, ask_payload)]：本轮所有 ask_user_question 调用及其结果载荷。"""
    calls = round_done.get("executed_tool_calls") or []
    results = round_done.get("executed_tool_results") or []
    pairs: List[Tuple[str, Dict[str, Any]]] = []
    for i, call in enumerate(calls):
        if not isinstance(call, dict) or call.get("name") != _ASK_TOOL_NAME:
            continue
        result_str = results[i] if i < len(results) else None
        payload = _parse_ask_payload(result_str)
        if payload is None:
            continue
        pairs.append((_resolve_tool_use_id(round_done, i), payload))
    return pairs


async def pause_for_ask_user_answers(
    round_done: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], str]:
    """工具循环轮末调用：本轮若发出了 ask_user_question，在此挂起等用户。

    返回 ``(updates, action)``：
    - updates: [{"tool_use_id", "result"}] —— 每条对应一个 SSE
      ``tool_result_update`` 事件（前端就地刷新卡片 + 持久化段改写为终态）；
    - action: "continue"（答案已写回 tool_result_msgs / tool_result_msg，
      正常继续下一轮）或 "append_then_stop"（超时 / 取消 / 非交互——循环应
      append 本轮 messages 后立即结束本轮，**不得**再把 awaiting 喂回模型）。
    """
    asks = _iter_round_asks(round_done)
    if not asks:
        return [], "continue"

    updates: List[Dict[str, Any]] = []
    action = "continue"

    for tool_use_id, payload in asks:
        if not payload.get("wait_for_answer"):
            # 非交互场景（渠道号 / cron / 子代理）：问完即停轮。
            action = "append_then_stop"
            continue

        session_id = str(payload.get("session_id") or "")
        ask_id = str(payload.get("ask_id") or "")
        entry = _PENDING_ASKS.get((session_id, ask_id))
        if entry is None or entry.future.done():
            # 注册表条目丢失（后端重启等极端情况）：按取消处理，停轮兜底。
            action = "append_then_stop"
            continue

        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(entry.future), timeout=_ask_wait_timeout_seconds()
            )
        except asyncio.TimeoutError:
            _pop_pending_ask(session_id, ask_id)
            timeout_text = (
                "用户长时间未回答（等待超时）。本轮到此结束，不要假设用户的偏好；"
                "用户的下一次回复会作为新消息接续，届时再继续。"
            )
            _update_round_tool_messages(round_done, tool_use_id, timeout_text)
            updates.append(
                {"tool_use_id": tool_use_id, "result": _timeout_ui_payload(payload)}
            )
            action = "append_then_stop"
            continue
        except asyncio.CancelledError:
            # 本轮被停止 / SSE 断连：清掉注册条目再上抛，保证注册表不泄漏。
            _pop_pending_ask(session_id, ask_id)
            raise

        if outcome.get("outcome") == "answered":
            answers = outcome.get("answers") or []
            final_text = _format_answer_text(payload.get("questions") or [], answers)
            _update_round_tool_messages(round_done, tool_use_id, final_text)
            updates.append(
                {
                    "tool_use_id": tool_use_id,
                    "result": _answered_ui_payload(payload, answers),
                }
            )
        else:  # cancelled
            cancel_text = "用户取消了本次回答。本轮到此结束；用户之后的回复会作为新消息接续。"
            _update_round_tool_messages(round_done, tool_use_id, cancel_text)
            updates.append(
                {"tool_use_id": tool_use_id, "result": _timeout_ui_payload(payload)}
            )
            action = "append_then_stop"

    return updates, action
