"""
Role-level skill growth and experience persistence.

This module adds a private mastery layer on top of the shared `skills/`
definitions. Shared skills describe the canonical procedure; role-local
growth captures how a specific character has practiced that skill, what
pitfalls it has hit, and how proficient it has become.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .agent_config import get_agent_manager


LEVELS: List[Dict[str, Any]] = [
    {"level": 0, "min_xp": 0, "label": "未掌握"},
    {"level": 1, "min_xp": 1, "label": "入门"},
    {"level": 2, "min_xp": 8, "label": "熟练"},
    {"level": 3, "min_xp": 20, "label": "精通"},
    {"level": 4, "min_xp": 40, "label": "专精"},
]

ERROR_PATTERNS: List[Tuple[str, str]] = [
    (r"权限|permission|forbidden|denied|不允许", "权限受限时先检查角色边界、目录范围和工具授权。"),
    (r"not found|不存在|no such file|找不到", "读取或修改前先确认路径、文件名和目录层级是否正确。"),
    (r"missing|required|参数|argument|unexpected keyword", "调用工具前先核对参数名、必填项和输入结构。"),
    (r"json|decode|parse|格式", "结构化输入要保持严格格式，必要时先把参数整理成最小合法对象。"),
    (r"预算|上限|limit|too many|循环|round", "连续调用同类工具前先汇总已有结果，避免空转和重复试错。"),
]


def _now_iso() -> str:
    return datetime.now().isoformat()


def _truncate(text: str, max_len: int = 120) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def get_level_info(xp: int) -> Dict[str, Any]:
    current = LEVELS[0]
    for info in LEVELS:
        if xp >= info["min_xp"]:
            current = info
        else:
            break

    next_level = None
    for info in LEVELS:
        if info["min_xp"] > xp:
            next_level = info
            break

    if next_level is None:
        progress_percent = 100
        xp_to_next = 0
        next_level_xp = xp
    else:
        span = max(1, next_level["min_xp"] - current["min_xp"])
        progress_percent = int(max(0.0, min(1.0, (xp - current["min_xp"]) / span)) * 100)
        xp_to_next = max(0, next_level["min_xp"] - xp)
        next_level_xp = next_level["min_xp"]

    return {
        "level": current["level"],
        "label": current["label"],
        "xp": xp,
        "next_level": next_level["level"] if next_level else current["level"],
        "next_level_label": next_level["label"] if next_level else current["label"],
        "next_level_xp": next_level_xp,
        "xp_to_next": xp_to_next,
        "progress_percent": progress_percent,
    }


def _build_default_record(skill_name: str) -> Dict[str, Any]:
    level_info = get_level_info(0)
    return {
        "skill_name": skill_name,
        "xp": 0,
        "level": level_info["level"],
        "level_label": level_info["label"],
        "total_uses": 0,
        "successful_uses": 0,
        "failed_uses": 0,
        "last_used_at": None,
        "last_leveled_at": None,
        "recent_tools": [],
        "heuristics": [],
        "pitfalls": [],
        "last_summary": "",
        "promotable": False,
    }


@dataclass
class SkillTurnSummary:
    loaded_skills: List[str]
    relevant_skills: List[str]
    tool_names: List[str]
    success_count: int
    failure_count: int
    failure_messages: List[str]
    heuristics: List[str]
    pitfalls: List[str]


class SkillGrowthManager:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id or "default"
        config = get_agent_manager().get_agent_config(self.agent_id)
        if not config:
            raise ValueError(f"agent '{self.agent_id}' does not exist")
        self.config = config
        self.mastery_file = config.agent_dir / "SKILL_MASTERY.json"
        self.experience_file = config.agent_dir / "SKILL_EXPERIENCE.md"

    def _default_state(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "agent_id": self.agent_id,
            "updated_at": None,
            "skills": {},
        }

    def _ensure_experience_file(self) -> None:
        if self.experience_file.exists():
            return
        self.experience_file.write_text(
            "# SKILL_EXPERIENCE.md\n\n## 技能成长记录\n",
            encoding="utf-8",
        )

    def load_state(self) -> Dict[str, Any]:
        if self.mastery_file.exists():
            try:
                data = json.loads(self.mastery_file.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    data.setdefault("version", 1)
                    data.setdefault("agent_id", self.agent_id)
                    data.setdefault("skills", {})
                    return data
            except Exception:
                pass
        return self._default_state()

    def save_state(self, state: Dict[str, Any]) -> None:
        state["updated_at"] = _now_iso()
        self.mastery_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def resolve_skill_name(self, raw_name: str) -> Optional[str]:
        if not raw_name:
            return None
        from .skill_loader import get_skill_loader

        loader = get_skill_loader()
        if raw_name in loader.skills:
            return raw_name

        lowered = raw_name.lower()
        for name in loader.skills:
            if name.lower() == lowered:
                return name
        for name in loader.skills:
            if lowered in name.lower():
                return name
        return None

    def get_skill_record(self, skill_name: str) -> Dict[str, Any]:
        state = self.load_state()
        raw = state["skills"].get(skill_name)
        if isinstance(raw, dict):
            record = dict(raw)
            record.setdefault("skill_name", skill_name)
        else:
            record = _build_default_record(skill_name)
        level_info = get_level_info(int(record.get("xp", 0) or 0))
        record["level"] = level_info["level"]
        record["level_label"] = level_info["label"]
        record["xp_to_next"] = level_info["xp_to_next"]
        record["next_level_xp"] = level_info["next_level_xp"]
        record["progress_percent"] = level_info["progress_percent"]
        record["promotable"] = bool(
            record.get("successful_uses", 0) >= 3 and record["level"] >= 3
        )
        return record

    def get_skill_records(self) -> Dict[str, Dict[str, Any]]:
        state = self.load_state()
        result: Dict[str, Dict[str, Any]] = {}
        for skill_name in state.get("skills", {}):
            result[skill_name] = self.get_skill_record(skill_name)
        return result

    def get_prompt_overlay(self, skill_name: str) -> str:
        record = self.get_skill_record(skill_name)
        if record["xp"] <= 0:
            return ""

        parts = [
            "## 你的个人技能经验",
            f"- 当前掌握度：Lv{record['level']} {record['level_label']}（XP {record['xp']}）",
            f"- 实战统计：总计 {record['total_uses']} 次，成功 {record['successful_uses']} 次，受阻 {record['failed_uses']} 次",
        ]
        heuristics = record.get("heuristics") or []
        pitfalls = record.get("pitfalls") or []
        if heuristics:
            parts.append(f"- 你的手感：{'；'.join(heuristics[:2])}")
        if pitfalls:
            parts.append(f"- 最近坑点：{'；'.join(pitfalls[:2])}")
        last_summary = _truncate(record.get("last_summary", ""), 160)
        if last_summary:
            parts.append(f"- 最近一次成长：{last_summary}")
        return "\n".join(parts)

    def _is_failure(self, result: str) -> bool:
        text = (result or "").lower()
        return any(token in text for token in [
            "错误",
            "error",
            "exception",
            "traceback",
            "权限不足",
            "denied",
            "forbidden",
            "not found",
            "不存在",
            "需要确认",
        ])

    def _derive_heuristics(self, tool_names: Iterable[str]) -> List[str]:
        names = list(tool_names)
        heuristics: List[str] = []
        if "grep" in names and "read_file" in names:
            heuristics.append("定位文件前先搜索再读取，可以减少无效试探。")
        if "list_directory" in names and "read_file" in names:
            heuristics.append("路径不熟时先看目录结构，再精读目标文件更稳。")
        if "read_file" in names and ("edit_file" in names or "write_file" in names):
            heuristics.append("修改前先读取上下文，能明显降低误改概率。")
        if "web_search" in names and "fetch_url" in names:
            heuristics.append("先搜索再抓取详情，更容易拿到可靠来源。")
        return heuristics[:3]

    def _derive_pitfalls(self, failure_messages: Iterable[str]) -> List[str]:
        pitfalls: List[str] = []
        for message in failure_messages:
            for pattern, hint in ERROR_PATTERNS:
                if re.search(pattern, message, flags=re.IGNORECASE):
                    if hint not in pitfalls:
                        pitfalls.append(hint)
        return pitfalls[:4]

    def _analyze_turn(
        self,
        turn_events: Iterable[Dict[str, Any]],
        equipped_skills: Optional[List[str]] = None,
    ) -> SkillTurnSummary:
        loaded_skills: List[str] = []
        tool_names: List[str] = []
        success_count = 0
        failure_count = 0
        failure_messages: List[str] = []

        for event in turn_events or []:
            etype = event.get("type")
            name = event.get("name") or ""
            if etype == "tool_call" and name == "load_skill":
                payload = event.get("input") or {}
                resolved = self.resolve_skill_name(str(payload.get("name", "")))
                if resolved and resolved not in loaded_skills:
                    loaded_skills.append(resolved)
                continue

            if etype == "tool_call" and name and name != "load_skill":
                tool_names.append(name)
                continue

            if etype == "tool_result" and name and name != "load_skill":
                result = str(event.get("result", ""))
                if self._is_failure(result):
                    failure_count += 1
                    failure_messages.append(_truncate(result, 180))
                else:
                    success_count += 1

        relevant_skills = list(loaded_skills)
        if not relevant_skills and equipped_skills:
            if len(equipped_skills) == 1 and tool_names:
                relevant_skills = list(equipped_skills)

        heuristics = self._derive_heuristics(tool_names)
        pitfalls = self._derive_pitfalls(failure_messages)
        return SkillTurnSummary(
            loaded_skills=loaded_skills,
            relevant_skills=relevant_skills,
            tool_names=tool_names,
            success_count=success_count,
            failure_count=failure_count,
            failure_messages=failure_messages,
            heuristics=heuristics,
            pitfalls=pitfalls,
        )

    def _compute_xp_delta(self, summary: SkillTurnSummary, assistant_response: str) -> int:
        if not summary.relevant_skills:
            return 0

        total = 0
        if summary.loaded_skills:
            total += 1
        if summary.tool_names:
            total += min(3, len(summary.tool_names))
        if summary.success_count > 0:
            total += 2
        if summary.failure_count > 0:
            total += 1
        if assistant_response.strip() and summary.success_count > 0:
            total += 1

        per_skill = max(1, math.ceil(total / max(1, len(summary.relevant_skills))))
        return min(per_skill, 6)

    def _merge_unique(self, existing: List[str], incoming: Iterable[str], limit: int = 5) -> List[str]:
        result = list(existing or [])
        for item in incoming:
            item = str(item).strip()
            if item and item not in result:
                result.append(item)
        return result[:limit]

    def _append_experience_entry(
        self,
        skill_name: str,
        record_before: Dict[str, Any],
        record_after: Dict[str, Any],
        summary: SkillTurnSummary,
        xp_delta: int,
        user_message: str,
    ) -> None:
        self._ensure_experience_file()
        old_level = record_before["level"]
        new_level = record_after["level"]
        level_change = (
            f"Lv{old_level} -> Lv{new_level}" if old_level != new_level else f"Lv{new_level}"
        )
        result_text = f"成功 {summary.success_count} 次，受阻 {summary.failure_count} 次"
        tool_text = "、".join(summary.tool_names[:6]) if summary.tool_names else "无工具链"
        lines = [
            f"### [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {skill_name} · {level_change}",
            f"- 任务：{_truncate(user_message, 80)}",
            f"- 本轮工具：{tool_text}",
            f"- 结果：{result_text}，经验 +{xp_delta}",
        ]
        if summary.heuristics:
            lines.append(f"- 新手感：{'；'.join(summary.heuristics[:2])}")
        if summary.pitfalls:
            lines.append(f"- 新坑点：{'；'.join(summary.pitfalls[:2])}")
        lines.append("")

        existing = self.experience_file.read_text(encoding="utf-8")
        self.experience_file.write_text(existing.rstrip() + "\n\n" + "\n".join(lines), encoding="utf-8")

    def record_turn(
        self,
        user_message: str,
        assistant_response: str,
        turn_events: Optional[List[Dict[str, Any]]] = None,
        equipped_skills: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        summary = self._analyze_turn(turn_events or [], equipped_skills=equipped_skills)
        if not summary.relevant_skills:
            return {"updated_skills": [], "leveled_up": []}

        xp_delta = self._compute_xp_delta(summary, assistant_response)
        if xp_delta <= 0:
            return {"updated_skills": [], "leveled_up": []}

        state = self.load_state()
        updated_skills: List[str] = []
        leveled_up: List[str] = []

        for skill_name in summary.relevant_skills:
            before = self.get_skill_record(skill_name)
            record = dict(before)
            record["xp"] = int(record.get("xp", 0)) + xp_delta
            record["total_uses"] = int(record.get("total_uses", 0)) + 1
            if summary.success_count > 0:
                record["successful_uses"] = int(record.get("successful_uses", 0)) + 1
            if summary.failure_count > 0:
                record["failed_uses"] = int(record.get("failed_uses", 0)) + 1
            record["last_used_at"] = _now_iso()
            record["recent_tools"] = list(dict.fromkeys(summary.tool_names))[:6]
            record["heuristics"] = self._merge_unique(record.get("heuristics") or [], summary.heuristics)
            record["pitfalls"] = self._merge_unique(record.get("pitfalls") or [], summary.pitfalls)
            record["last_summary"] = _truncate(
                f"本轮使用 {', '.join(summary.tool_names[:4]) or '无工具'}，"
                f"成功 {summary.success_count} 次、受阻 {summary.failure_count} 次。"
            )

            level_info = get_level_info(record["xp"])
            record["level"] = level_info["level"]
            record["level_label"] = level_info["label"]
            record["promotable"] = bool(
                record["successful_uses"] >= 3 and record["level"] >= 3
            )
            if record["level"] > before["level"]:
                record["last_leveled_at"] = _now_iso()
                leveled_up.append(skill_name)

            state["skills"][skill_name] = record
            updated_skills.append(skill_name)
            self._append_experience_entry(skill_name, before, record, summary, xp_delta, user_message)

        self.save_state(state)
        return {"updated_skills": updated_skills, "leveled_up": leveled_up}


_growth_managers: Dict[str, SkillGrowthManager] = {}


def get_skill_growth_manager(agent_id: str) -> SkillGrowthManager:
    agent_key = agent_id or "default"
    manager = _growth_managers.get(agent_key)
    if manager is None:
        manager = SkillGrowthManager(agent_key)
        _growth_managers[agent_key] = manager
    return manager
