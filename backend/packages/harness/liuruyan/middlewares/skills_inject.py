"""v2 · B-8 · SkillsInjectMiddleware

把 enabled skills 列表注入 system prompt，让 LLM 知道可用技能。

参考：_docs/architecture_v2/03_middlewares.md § 04

钩子：before_model

输入约定（state 字段）：
- `enabled_skills`: list[dict] 或 list[str]
  - dict 形态：{name: str, description?: str, allowed_tools?: list[str]}
  - str 形态：仅 skill name
  - 上游（ConfigLoader / SkillsScanner）填充；middleware 不直接读 extensions.json
  - 一次注入 + per-state 跟踪 `skills_injected`，避免重复注入

输出：
- 有 skills 且未注入 → 注入 system message + 标记 skills_injected=True
- 无 skills / 已注入 → return None

注入位置：插在第一条 user message 之前（system 段尾）。

异常隔离：内部 try/except；任何异常 swallow + log。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


class SkillsInjectMiddleware(AgentMiddleware):
    name = "skills_inject"

    async def before_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            if state.get("skills_injected"):
                return None

            skills = self._extract_skills(state.get("enabled_skills"))
            if not skills:
                return None

            block = self._build_skills_block(skills)
            new_messages = self._inject_before_first_user(
                state.get("messages") or [], block
            )

            return {"messages": new_messages, "skills_injected": True}
        except Exception:
            log.exception(
                "SkillsInjectMiddleware swallowed exception (run=%s)",
                ctx.run_id,
            )
            return None

    # ── extract ──────────────────────────────────────────────────────────

    @staticmethod
    def _extract_skills(raw: Any) -> list[dict[str, Any]]:
        """统一成 list[dict{name, description?, allowed_tools?}]，过滤非法项。"""
        if not isinstance(raw, (list, tuple)):
            return []
        out: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for item in raw:
            entry: dict[str, Any] | None = None
            if isinstance(item, str):
                if item.strip():
                    entry = {"name": item}
            elif isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    entry = {"name": name}
                    desc = item.get("description")
                    if isinstance(desc, str) and desc.strip():
                        entry["description"] = desc
                    tools = item.get("allowed_tools")
                    if isinstance(tools, (list, tuple)):
                        clean_tools = [t for t in tools if isinstance(t, str) and t]
                        if clean_tools:
                            entry["allowed_tools"] = clean_tools
            if entry and entry["name"] not in seen_names:
                out.append(entry)
                seen_names.add(entry["name"])
        return out

    # ── build ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_skills_block(skills: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for s in skills:
            line = f"- {s['name']}"
            if "description" in s:
                line += f": {s['description']}"
            if "allowed_tools" in s:
                line += f" (allowed-tools: {', '.join(s['allowed_tools'])})"
            lines.append(line)
        body = "\n".join(lines)
        return (
            "<skills>\n"
            "你可以使用以下技能（通过 SkillTool 调用）：\n"
            f"{body}\n"
            "</skills>"
        )

    # ── inject ──────────────────────────────────────────────────────────

    @staticmethod
    def _inject_before_first_user(
        messages: list[Any], block: str
    ) -> list[Any]:
        """插入 system message：在第一条 user 之前。无 user 则末尾追加。"""
        new_msgs = list(messages)
        insert_idx = len(new_msgs)
        for i, m in enumerate(new_msgs):
            if isinstance(m, dict) and m.get("role") == "user":
                insert_idx = i
                break
        new_msgs.insert(insert_idx, {"role": "system", "content": block})
        return new_msgs


__all__ = ["SkillsInjectMiddleware"]
