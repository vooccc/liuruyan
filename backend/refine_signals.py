"""记忆提炼高信号检测与工具动作摘要提取（低耦合纯分析模块）。

职责：
1. 从单轮 `turn_events` / `assistant_segments` 中配对提取精简的工具动作摘要，
   让 `MemoryRefinerV2` 能感知本轮修改了哪些文件、更新了哪些项目档案或获得了什么用户确认；
2. 判定本轮是否命中“高信号”（关键落盘工具、明确红线/决定/定稿表达、重大外部进展），
   命中时允许穿透常规 5 轮冷却立即触发后台提炼，避免重要信息漏摘。

本模块不依赖 LLM、SessionManager 或 Agent 单例，可独立单测与演进。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


# 写/状态变更/用户拍板类工具：成功执行时视为高信号
HIGH_SIGNAL_WRITE_TOOLS = frozenset({
    "write_file",
    "edit",
    "multi_edit",
    "ide_apply_edit",
    "ide_insert_snippet",
    "set_project_binding",
    "add_project_rule",
    "update_project_notes",
    "save_project_memory",
    "archive_lry_file",
    "restore_lry_file",
    "create_memory",
    "save_memory",
    "ask_user_question",
    "cron_create",
    "cron_delete",
})

# project_archive 仅写操作算高信号，list/read 不穿透冷却
_PROJECT_ARCHIVE_WRITE_ACTIONS = frozenset({
    "create",
    "append_timeline",
    "update_card",
    "update_meta",
    "update_topic",
    "delete_topic",
    "merge_topics",
})

# 纯探索/只读工具：默认不穿透冷却，摘要中也尽量精简
_LOW_NOISE_READ_TOOLS = frozenset({
    "read_file",
    "glob_search",
    "grep",
    "fast_context_search",
    "outline_file",
    "code_intelligence",
    "git_status",
    "ide_get_diagnostics",
    "ide_open_file",
    "recall_memory",
    "session_search",
    "list_project_rules",
    "recall_project_memory",
    "get_project_workflow",
})

# 用户明确指令 / 红线 / 拍板词（即使字数不长也应穿透冷却）
_USER_RULE_OR_DECISION_KEYWORDS = (
    "记住",
    "以后",
    "下次",
    "别再",
    "千万不能",
    "红线",
    "底线",
    "禁忌",
    "铁律",
    "就这么定了",
    "就按这个",
    "按这个方案",
    "按方案",
    "拍板",
    "定稿",
    "最终决定",
    "确认采用",
    "切到",
    "改绑",
    "绑定到",
    "不喜欢",
    "偏好",
)

# 重大外部世界反馈 / 里程碑进展词
_MILESTONE_KEYWORDS = (
    "已立案",
    "立案了",
    "已签",
    "签了",
    "打款",
    "回款",
    "驳回",
    "裁定",
    "判决",
    "已上线",
    "发布了",
    "通过测试",
    "已解决",
    "已定稿",
)

# 纯寒暄/短应答（无高信号工具时不穿透）
_TRIVIAL_USER_MESSAGES = frozenset({
    "好", "好的", "好滴", "嗯", "嗯嗯", "行", "可以", "继续", "谢谢", "收到",
    "明白", "了解", "知道了", "ok", "okay", "yes", "no", "再看看", "试试",
})


def unwrap_assistant_payload(assistant_response: str) -> Tuple[str, List[Dict[str, Any]]]:
    """剥离历史 JSON 包装或 `## 思考过程` 前缀，返回 `(纯文本回复, 内嵌segments)`。"""
    text = str(assistant_response or "")
    segments: List[Dict[str, Any]] = []

    stripped = text.strip()
    if stripped.startswith("{") and '"segments"' in stripped[:300]:
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                raw_segs = obj.get("segments")
                if isinstance(raw_segs, list):
                    segments = [s for s in raw_segs if isinstance(s, dict)]
                content = obj.get("content")
                if isinstance(content, str) and content.strip():
                    text = content
                elif segments:
                    text = "".join(
                        str(s.get("content") or "")
                        for s in segments
                        if s.get("type") == "text"
                    )
        except Exception:
            pass

    if text.startswith("## 思考过程\n"):
        marker = "\n\n---\n\n"
        idx = text.find(marker)
        if idx != -1:
            text = text[idx + len(marker):]

    return text.strip(), segments


def _coerce_input_dict(raw_input: Any) -> Dict[str, Any]:
    if isinstance(raw_input, dict):
        return raw_input
    if isinstance(raw_input, str) and raw_input.strip().startswith("{"):
        try:
            parsed = json.loads(raw_input)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _is_tool_error(result_text: str) -> bool:
    if not result_text:
        return False
    low = result_text.strip().lower()
    if low.startswith("error:") or low.startswith("[tool=") or "guardrail_denied" in low:
        return True
    if low.startswith("{"):
        try:
            obj = json.loads(result_text)
            if isinstance(obj, dict) and obj.get("error"):
                return True
        except Exception:
            pass
    if "exit code" in low and "exit code 0" not in low:
        return True
    return False


def _pair_tool_events(
    turn_events: Optional[Iterable[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """将 turn_events / assistant_segments 中的 tool_call 与 tool_result 配对。"""
    calls: List[Dict[str, Any]] = []
    by_id: Dict[str, Dict[str, Any]] = {}

    for ev in turn_events or []:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "tool_call":
            name = str(ev.get("name") or ev.get("tool_name") or "").strip()
            if not name:
                continue
            tid = str(ev.get("tool_use_id") or ev.get("id") or "")
            inp = _coerce_input_dict(ev.get("input") if ev.get("input") is not None else ev.get("arguments"))
            item = {
                "name": name,
                "input": inp,
                "tool_use_id": tid,
                "result": "",
                "has_result": False,
                "is_error": False,
            }
            calls.append(item)
            if tid:
                by_id[tid] = item
        elif etype == "tool_result":
            tid = str(ev.get("tool_use_id") or "")
            name = str(ev.get("name") or "").strip()
            raw_res = ev.get("result") if ev.get("result") is not None else ev.get("content", "")
            res_str = raw_res if isinstance(raw_res, str) else json.dumps(raw_res, ensure_ascii=False)
            target = by_id.get(tid) if tid else None
            if target is None and name:
                for cand in reversed(calls):
                    if cand["name"] == name and not cand["has_result"]:
                        target = cand
                        break
            if target is not None:
                target["result"] = res_str
                target["has_result"] = True
                target["is_error"] = _is_tool_error(res_str)

    return calls


def _format_single_tool_action(item: Dict[str, Any]) -> Optional[str]:
    name = item["name"]
    inp = item["input"]
    res = item["result"].strip()
    err_tag = "（失败）" if item["is_error"] else ""

    if name in ("write_file", "edit", "multi_edit", "ide_apply_edit"):
        path = str(inp.get("path") or inp.get("fsPath") or "").strip()
        return f"- 写入/修改文件{err_tag}：{path or '(未知路径)'}"

    if name == "project_archive":
        action = str(inp.get("action") or "").strip()
        if action in ("list", "read", "list_topics", "read_topic") and not item["is_error"]:
            return None
        proj = str(inp.get("project") or "当前项目").strip()
        topic = str(inp.get("topic") or "").strip()
        detail = str(inp.get("event") or inp.get("summary") or inp.get("append_line") or "").strip()
        topic_part = f"/{topic}" if topic else ""
        tail = f" - {detail[:80]}" if detail else ""
        return f"- 更新项目档案({action}){err_tag}：{proj}{topic_part}{tail}"

    if name == "set_project_binding":
        proj = str(inp.get("project") or "解绑").strip()
        return f"- 切换会话项目绑定{err_tag}：{proj}"

    if name in ("add_project_rule", "update_project_notes", "save_project_memory", "archive_lry_file", "restore_lry_file"):
        target = str(inp.get("name") or inp.get("topic") or inp.get("title") or "").strip()
        return f"- 工作区文档({name}){err_tag}：{target[:80]}"

    if name in ("create_memory", "save_memory"):
        content = str(inp.get("content") or inp.get("title") or "").strip()
        return f"- 写入长期记忆{err_tag}：{content[:80]}"

    if name == "ask_user_question":
        ans = re.sub(r"\s+", " ", res)[:120]
        return f"- 用户交互确认：{ans}" if ans else "- 发起用户确认提问"

    if name in ("execute_command", "ide_run_command_in_terminal"):
        cmd = str(inp.get("command") or "").strip()
        desc = str(inp.get("description") or "").strip()
        label = desc or cmd[:70]
        return f"- 执行命令{err_tag}：{label}"

    if name in ("web_search", "web_extract", "recall_memory", "session_search"):
        q = str(inp.get("query") or inp.get("urls") or "").strip()
        return f"- 检索({name}){err_tag}：{q[:70]}"

    if name == "cron_create":
        prompt = str(inp.get("prompt") or inp.get("name") or "").strip()
        return f"- 创建定时任务{err_tag}：{prompt[:70]}"

    if name in _LOW_NOISE_READ_TOOLS:
        if item["is_error"]:
            return f"- {name} 失败：{res[:80]}"
        return None

    return f"- 调用工具 {name}{err_tag}"


def extract_tool_actions_summary(
    turn_events: Optional[Iterable[Dict[str, Any]]],
    max_items: int = 8,
    max_chars: int = 1200,
) -> str:
    """将本轮工具调用浓缩为紧凑摘要，供 MemoryRefinerV2 感知工具动作与产物。"""
    paired = _pair_tool_events(turn_events)
    if not paired:
        return ""

    lines: List[str] = []
    seen = set()
    for item in paired:
        line = _format_single_tool_action(item)
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
        if len(lines) >= max_items:
            break

    # 若全部都是只读工具，仅在总数很少时给一句轻量提示，避免空有工具调用却零上下文
    if not lines:
        read_targets: List[str] = []
        for item in paired[:4]:
            p = item["input"].get("path") or item["input"].get("query") or item["input"].get("pattern")
            if p:
                read_targets.append(f"{item['name']}({str(p)[:40]})")
            else:
                read_targets.append(item["name"])
        if read_targets:
            lines.append("- 本轮只读检索：" + "、".join(read_targets))

    summary = "\n".join(lines)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1] + "…"
    return summary


def detect_high_signal_turn(
    user_message: str,
    assistant_response: str,
    turn_events: Optional[Iterable[Dict[str, Any]]] = None,
) -> Tuple[bool, List[str]]:
    """判断本轮是否为高信号轮次（命中则返回 `(True, reasons)`，可穿透提炼冷却）。"""
    reasons: List[str] = []
    user_clean = str(user_message or "").strip()
    asst_clean, embedded_segs = unwrap_assistant_payload(assistant_response)
    events = list(turn_events) if turn_events is not None else embedded_segs

    # 1. 关键落盘 / 状态变更工具调用（且未失败）
    paired = _pair_tool_events(events)
    hit_tools: List[str] = []
    for item in paired:
        if item["is_error"]:
            continue
        name = item["name"]
        if name == "project_archive":
            action = str(item["input"].get("action") or "").strip()
            if action in _PROJECT_ARCHIVE_WRITE_ACTIONS:
                hit_tools.append(f"project_archive:{action}")
        elif name in HIGH_SIGNAL_WRITE_TOOLS:
            hit_tools.append(name)
    if hit_tools:
        reasons.append("tool:" + ",".join(dict.fromkeys(hit_tools))[:60])

    # 2. 用户明确指令 / 红线 / 拍板词
    user_low = user_clean.lower()
    is_trivial = len(user_clean) <= 6 and user_low in _TRIVIAL_USER_MESSAGES
    if user_clean and not is_trivial:
        matched_user_kw = [kw for kw in _USER_RULE_OR_DECISION_KEYWORDS if kw in user_clean]
        if matched_user_kw:
            reasons.append("user_decision:" + ",".join(matched_user_kw[:3]))

        matched_milestone = [kw for kw in _MILESTONE_KEYWORDS if kw in user_clean]
        if matched_milestone:
            reasons.append("milestone:" + ",".join(matched_milestone[:3]))

    # 3. 助手明确确认定稿/落盘且对话具备实质信息量
    if not is_trivial and len(user_clean) + len(asst_clean) >= 160:
        asst_hits = [kw for kw in ("已定稿", "已落盘", "最终方案", "已达成", "已写入") if kw in asst_clean]
        if asst_hits:
            reasons.append("assistant_milestone:" + ",".join(asst_hits[:2]))

    return bool(reasons), reasons
