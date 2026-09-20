"""AskUserQuestion tool — 向用户发出带选项的问题。

即发即返模型：工具不挂起 LLM stream。handler 立刻返回 awaiting_user_choice
作 tool_result，前端识别此形态渲染选项卡片；用户点选后作为下一轮 user
message 回流，LLM 看到答案继续执行。

参数校验双重：input_schema 声明 + handler 运行时校验，防御性 defense in depth。
"""

from typing import List, Dict, Any
import uuid


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
    **_unused,
) -> Dict[str, Any]:
    """Ask the user 1-4 multiple-choice questions.

    Returns awaiting_user_choice immediately. Model MUST end its response
    after calling this tool — do not call other tools, do not write more
    text. The user's answers will arrive as the next user message.
    """
    if not isinstance(questions, list):
        raise ValueError("questions 必须是列表")
    if not 1 <= len(questions) <= 4:
        raise ValueError(f"questions 必须包含 1-4 个问题（当前 {len(questions)}）")

    validated = [_validate_question(q, i) for i, q in enumerate(questions)]

    return {
        "status": "awaiting_user_choice",
        "ask_id": uuid.uuid4().hex[:12],
        "questions": validated,
        "note": (
            "问题已发送到前端卡片。本工具已返回，模型应立即结束本轮响应，"
            "等待用户在前端完成选择后作为下一轮消息回流。"
        ),
    }
