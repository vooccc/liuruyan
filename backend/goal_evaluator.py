"""
Goal 独立客观验收评估器（Verification Loop）
文件：backend/goal_evaluator.py
"""
import json
import re
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("goal_evaluator")

EVALUATOR_PROMPT_TEMPLATE = """你是一个客观严苛的工程验收判官。请根据【任务目标】与【本轮工具调用的客观实据】，评估目标是否真正达成。

【任务目标】：
{objective}

【本轮工具调用的客观实据（命令输出/返回码/单测结果/文件修改）】：
{tool_evidence}

【助手本轮回复摘要】：
{assistant_reply}

【验收铁律】：
1. 严禁听信模型口头表态（如“我已全部修复好”但实据中没有任何命令验证或测试通过，必须判为 continue）。
2. 若存在未解决的明确报错、单测失败（failed）、退出码非0，绝对不可判为 completed。
3. 若存在明显的死锁、缺少关键外部依赖/权限无法继续，判为 blocked 并说明阻碍原因。
4. 只有当客观证据充分证明目标要求已达成时（例如全部单测通过、指定文件已按要求生成并校验通过），才可判为 completed。

必须输出严格合法的 JSON 对象，不要输出任何多余的解释、markdown 包裹或前缀：
{{
  "status": "continue" | "completed" | "blocked",
  "evidence": ["提取到的关键客观证据列表"],
  "next_action": "如果为 continue，指出下一轮最紧要的具体动作；如果为 blocked，指出卡点原因",
  "summary": "本轮推进的一句话客观摘要"
}}
"""


def _clean_json_text(raw_text: str) -> str:
    """提取并清洗可能的 markdown 代码块包裹的 JSON 字符串"""
    if not raw_text:
        return ""
    text = raw_text.strip()
    if text.startswith("```"):
        # 去掉开头的 ```json 或 ```
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        # 去掉结尾的 ```
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    # 尝试找到第一个 { 和最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def heuristic_evaluate_goal(
    objective: str,
    tool_evidence: str,
    assistant_reply: str = ""
) -> Dict[str, Any]:
    """
    轻量启发式规则评估器（离线/降级/单元测试使用）
    检测关键词与模式
    """
    evidence_lower = (tool_evidence or "").lower()
    
    # 1. 明显的阻碍/卡点信号
    if "permission denied" in evidence_lower or "command not found" in evidence_lower:
        return {
            "status": "blocked",
            "evidence": ["检测到权限或命令缺失错误"],
            "next_action": "需要解决环境权限或缺失依赖",
            "summary": "执行受阻：缺少必要环境依赖或权限"
        }

    # 2. 失败/报错信号
    has_test_failure = any(w in evidence_lower for w in ["failures=", "failed,", "failed in", "assertionerror", "error:"])
    if has_test_failure:
        return {
            "status": "continue",
            "evidence": ["检测到未通过的测试或报错信息"],
            "next_action": "排查并修复上述报错/失败断言",
            "summary": "发现报错，继续排查修复"
        }

    # 3. 成功通过信号（需结合目标包含测试等）
    has_test_success = any(w in evidence_lower for w in ["passed in", "100% passed", "all passed", "exit 0", "passed,"])
    if has_test_success:
        return {
            "status": "completed",
            "evidence": ["检测到测试或命令已成功通过 (exit 0/passed)"],
            "next_action": "",
            "summary": "验证通过，目标已达成"
        }

    # 4. 默认继续
    # 动态构建本轮 summary，提取输出关键尾部或回复关键动作，避免固定字符串误触发停滞熔断
    ev_lines = [l.strip() for l in tool_evidence.splitlines() if l.strip()] if tool_evidence else []
    ev_snippet = ev_lines[-1][:60] if ev_lines else ""
    reply_lines = [l.strip() for l in assistant_reply.splitlines() if l.strip()] if assistant_reply else []
    reply_snippet = reply_lines[0][:60] if reply_lines else ""

    if ev_snippet:
        dyn_summary = f"执行输出: {ev_snippet}"
    elif reply_snippet:
        dyn_summary = f"推进分析: {reply_snippet}"
    else:
        dyn_summary = "操作已执行，待进一步验证"

    return {
        "status": "continue",
        "evidence": ["本轮进行了操作，但尚无终态验收证据"],
        "next_action": "继续推进并运行命令或工具验证结果",
        "summary": dyn_summary
    }


async def evaluate_goal_turn(
    objective: str,
    tool_evidence: str,
    assistant_reply: str = "",
    llm_client: Any = None
) -> Dict[str, Any]:
    """
    执行单轮 Goal 客观核验。
    优先调用 LLMClient 进行严格审查，失败或无 client 时平滑降级到启发式核验。
    """
    prompt = EVALUATOR_PROMPT_TEMPLATE.format(
        objective=objective,
        tool_evidence=tool_evidence or "（本轮无工具调用输出）",
        assistant_reply=(assistant_reply or "")[:500]
    )

    if llm_client is not None:
        try:
            # 尝试使用模型生成结构化结果
            raw_response = ""
            if hasattr(llm_client, "chat_completion_async"):
                res = await llm_client.chat_completion_async([
                    {"role": "user", "content": prompt}
                ])
                raw_response = res.get("content", "") if isinstance(res, dict) else str(res)
            elif hasattr(llm_client, "one_off_chat"):
                raw_response = await llm_client.one_off_chat([
                    {"role": "user", "content": prompt}
                ])
            elif hasattr(llm_client, "generate_json_response"):
                raw_response = await llm_client.generate_json_response(prompt)
            
            cleaned = _clean_json_text(raw_response)
            if cleaned:
                data = json.loads(cleaned)
                status = data.get("status", "continue")
                if status not in ("continue", "completed", "blocked"):
                    status = "continue"
                return {
                    "status": status,
                    "evidence": data.get("evidence", []) if isinstance(data.get("evidence"), list) else [str(data.get("evidence", ""))],
                    "next_action": str(data.get("next_action", "")).strip(),
                    "summary": str(data.get("summary", "本轮完成部分操作")).strip()
                }
        except Exception as e:
            logger.warning("[GoalEvaluator] LLM 评估失败，降级到启发式核验: %s", e)

    # 降级到启发式规则评估
    return heuristic_evaluate_goal(objective, tool_evidence, assistant_reply)
