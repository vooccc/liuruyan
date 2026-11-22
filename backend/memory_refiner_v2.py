"""
记忆提炼模块 V2 - 使用 LLM 智能提炼对话
从对话中提取真正重要的内容，生成有情感标记的沉淀记忆
V2.1: 新增行为洞察提取 → 写入 REFLECTIONS.md
V3 (2026-10-24): 命名空间五路分流。启用项目命名空间（REGISTRY.json 落盘）后，
提炼产物不再写扁平 MEMORY.md，而是按 target_scope 分流：
- relationship → GLOBAL.md（全局层：关系里程碑/跨项目认知，常驻注入）
- project      → {ns}/timeline.jsonl 追加（事实之源）+ LLM 增量合并状态卡
- user         → 走既有 REFLECTIONS.md 行为洞察链路
- discard      → 直接丢弃
未启用命名空间时保持 V2 行为不变（MEMORY.md 追加）。
"""

from datetime import datetime
from typing import Callable, List, Dict, Optional, Tuple
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# REFLECTIONS.md 容量限制（行数 / 字符数）
REFLECTIONS_MAX_LINES = 50
REFLECTIONS_MAX_CHARS = 5000

TEMPERATURE_ORDER = {"burning": 3, "warm": 2, "cold": 1}

# 状态卡单次 LLM 合并的 prompt 上限（防长卡撑爆 flash 模型）
CARD_MERGE_MAX_CHARS = 8000


class MemoryRefinerV2:
    """智能记忆提炼器"""

    def __init__(self, llm_client, memory_manager):
        """
        Args:
            llm_client: LLM 客户端，用于智能提炼
            memory_manager: 记忆管理器，用于读写记忆文件
        """
        self.llm_client = llm_client
        self.memory_manager = memory_manager

    @property
    def _namespaces_active(self) -> bool:
        try:
            return self.memory_manager.namespace_manager.is_active()
        except Exception:
            return False

    def _namespace_manager(self):
        return self.memory_manager.namespace_manager

    def refine_conversation(
        self,
        user_message: str,
        assistant_message: str,
        session_id: Optional[str] = None,
        message_span: Optional[List[int]] = None,
        session_ns: Optional[str] = None,
        tool_summary: Optional[str] = None,
    ) -> Optional[Dict]:
        """提炼单轮对话，判断是否值得沉淀。返回结构化记忆或 None。"""
        from .refine_signals import unwrap_assistant_payload

        clean_assistant, _ = unwrap_assistant_payload(assistant_message)
        today = datetime.now().strftime("%Y-%m-%d")
        source_hint = {
            "session_id": session_id,
            "date": today,
            "message_span": message_span or [],
        }

        # v3：命名空间模式下附带项目注册表，让 LLM 把 project_name 归到
        # 已有 canonical 名上；新项目允许自拟短名（注册时自动建）。
        project_catalog = ""
        if self._namespaces_active:
            ns_mgr = self._namespace_manager()
            entries = ns_mgr.list_namespaces(include_archived=False)
            catalog_lines = [
                f"- {e['name']}" + (f"（别名：{'、'.join(e.get('aliases', []))}）" if e.get("aliases") else "")
                for e in entries
            ]
            session_hint = ""
            topic_hint = ""
            target_hint_ns = None
            if session_ns:
                bound_entry = ns_mgr.get_namespace(session_ns)
                if bound_entry and bound_entry.get("status") != "archived":
                    target_hint_ns = bound_entry["id"]
                    session_hint = (
                        f"\n当前会话已绑定项目：「{bound_entry['name']}」"
                        f"（若本轮讨论属于该项目，project_name 请直接填「{bound_entry['name']}」；"
                        "若明确讨论了其他项目再填对应项目名）"
                    )
            if target_hint_ns is None and user_message:
                target_hint_ns = ns_mgr.resolve_from_text(user_message)
            if target_hint_ns:
                from .namespace_topics import MAX_TOPICS_SOFT_LIMIT
                existing_topics = ns_mgr.list_topics(target_hint_ns)
                if existing_topics:
                    t_names = "、".join(t["name"] for t in existing_topics)
                    if len(existing_topics) >= MAX_TOPICS_SOFT_LIMIT - 2:
                        topic_hint = (
                            f"\n该项目已有树叉（{len(existing_topics)} 个）：{t_names}。"
                            "树叉已接近上限，请优先归入以上最贴近的已有树叉，不要新建零散树叉。"
                        )
                    else:
                        topic_hint = (
                            f"\n该项目已有树叉：{t_names}（topic_name 优先从中选；确实属于全新专题再自拟 ≤8 字新树叉名）"
                        )
                else:
                    topic_hint = "\n该项目尚无专题树叉，若内容属于可独立归纳的专题（如 架构与边界 / 证据链 / 进度与待办 / 策略红线），可自拟 ≤8 字 topic_name。"
            project_catalog = (
                (
                    "\n已注册的项目（project_name 尽量从中选；确实没有再自拟 ≤10 字新名）：\n"
                    + "\n".join(catalog_lines)
                    if catalog_lines
                    else ""
                )
                + session_hint
                + topic_hint
            )
            scope_spec = '''    "target_scope": "relationship|project|user|discard",
    "project_name": "target_scope=project 时填写项目名，其余留空",
    "topic_name": "target_scope=project 时填写归属的专题树叉名（≤8字，优先选已有树叉；整体状态变化可留空）",'''
        else:
            scope_spec = ''

        tool_block = f"\n\n本轮关键工具动作与产物：\n{tool_summary}" if tool_summary else ""

        prompt = f"""你是一个记忆提炼器。请分析以下对话，判断是否值得沉淀为长期记忆。

用户说：
{user_message}

助手回复：
{clean_assistant}{tool_block}

来源线索：
{json.dumps(source_hint, ensure_ascii=False)}{project_catalog}

请判断：
1. 这段对话是否包含重要信息？（重要决定、情感时刻、新认知、关系变化、关键文件/方案落地等）
2. 如果重要，请提取关键内容并生成沉淀记忆（若有关键文件落盘或工具确认结果，保留结论与路径指针，不抄大段代码正文）
3. 只在真正重要时使用 warm / burning；普通事实或一次性信息用 cold
4. target_scope 分流规则（重要时必填）：
   - relationship：两人关系的变化、情感时刻、陪伴承诺、跨项目的相处认知
   - project：某个具体项目/事务的事实与进展（案件、代码库、投资、运营…）
   - user：对用户跨会话仍成立的习惯、偏好、决策风格、审美或称呼认知（将增量融入 USER.md 画像）
   - discard：日常闲聊、一次性操作细节、无长期价值

请以 JSON 格式返回：
{{
    "is_important": true/false,
    "reason": "为什么重要/不重要",
    "event": "发生了什么（一句话）",
    "feeling": "当时的感受（如：感动、被信任、焦虑、开心）",
    "cognition": "形成了什么新的理解，没有则写空字符串",
    "decision": "做出了什么决定（如果有）",
    "user_habit": "若本轮同时体现了用户跨项目通用的习惯/偏好/审美/禁忌，用一句话精炼概括；没有必须留空",
    "tags": ["#标签1", "#标签2"],
    "temperature": "burning|warm|cold",{scope_spec}
    "source": {{
        "session_id": "{session_id or ''}",
        "date": "{today}",
        "message_span": {message_span or []}
    }}
}}

只返回 JSON，不要其他内容。"""

        try:
            response = self.llm_client.chat_completion([
                {"role": "user", "content": prompt}
            ])
            content = response.get("content", "")
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                refined = json.loads(json_match.group())
                return self._normalize_refined_memory(
                    refined,
                    session_id=session_id,
                    message_span=message_span,
                    session_ns=session_ns,
                )
        except Exception as e:
            logger.error("[MemoryRefinerV2] 提炼失败: %s", e)
        return None

    def extract_reflection(self, user_message: str, assistant_message: str) -> Optional[str]:
        """从对话中提取行为洞察（用于写入 REFLECTIONS.md）"""
        existing = self._read_existing_reflections()
        # P0-1: 由调用方拼好真实日期，避免 LLM 把占位符原样抄进文件
        now_str = datetime.now().strftime("%Y-%m-%d")

        prompt = f"""你是一个行为洞察分析器。请从以下对话中提取「行为洞察」——不是发生了什么，而是学到了什么。

用户说：
{user_message}

助手回复：
{assistant_message}

现有反思记录：
{existing if existing else '（尚无记录）'}

请分析这段对话，判断是否有值得记录的行为洞察：
- 关于用户的：你对他/她有了什么新的理解？
- 关于自己的：你的反应模式有什么发现？
- 关于互动的：你们之间的相处方式有什么新的认知？

如果没有值得记录的内容，返回空字符串。
如果有，返回一段简洁的话（1-3句话），格式如下：

## [{now_str}] 洞察主题

**关于{{对象}}**：具体洞察内容

只返回洞察文本，不要其他解释。"""

        try:
            response = self.llm_client.chat_completion([
                {"role": "user", "content": prompt}
            ])
            content = response.get("content", "").strip()
            if len(content) > 20 and not content.startswith("没有") and not content.startswith("暂无"):
                return content
        except Exception as e:
            logger.error("[MemoryRefinerV2] 反思提取失败: %s", e)
        return None

    def _read_existing_reflections(self) -> str:
        """读取现有的 REFLECTIONS.md 内容"""
        try:
            return self.memory_manager.read_reflections()
        except Exception:
            return ""

    def save_reflection(self, reflection_text: str) -> bool:
        """将洞察追加到 REFLECTIONS.md"""
        try:
            new_content = ""
            existing = self.memory_manager.read_reflections()
            if existing:
                new_content = existing.rstrip()

            new_content += "\n\n" + reflection_text + "\n"

            # 容量控制：超限时压缩
            lines = new_content.split("\n")
            if len(lines) > REFLECTIONS_MAX_LINES:
                # 找到 body 部分（声明之后）
                header_end = 0
                for i, line in enumerate(lines):
                    if line.startswith("---") or (line.strip() == "" and i > 5):
                        header_end = i + 1
                        break
                keep_lines = lines[:header_end] + lines[-(REFLECTIONS_MAX_LINES - header_end):]
                new_content = "\n".join(keep_lines)

            if len(new_content) > REFLECTIONS_MAX_CHARS:
                # P0-2: 按段落边界（## 标题）回退，避免把标题切成半截
                truncated = new_content[-REFLECTIONS_MAX_CHARS:]
                idx = truncated.find("\n## ")
                if idx >= 0:
                    new_content = truncated[idx + 1:]
                else:
                    nl_idx = truncated.find("\n")
                    new_content = truncated[nl_idx + 1:] if nl_idx >= 0 else truncated

            self.memory_manager.write_reflections(new_content)
            logger.info("[MemoryRefinerV2] 已保存行为洞察到 REFLECTIONS.md")
            return True
        except Exception as e:
            logger.error("[MemoryRefinerV2] 保存反思失败: %s", e)
            return False

    def _normalize_temperature(self, refined: Dict) -> str:
        temperature = str(refined.get("temperature", "")).strip().lower()
        if temperature not in TEMPERATURE_ORDER:
            temperature = "warm" if refined.get("is_important") else "cold"

        text = " ".join([
            str(refined.get("event", "")),
            str(refined.get("feeling", "")),
            str(refined.get("cognition", "")),
            str(refined.get("decision", "")),
            " ".join(refined.get("tags", []) or []),
        ])
        strong_signals = ["认定", "承诺", "救命恩人", "伙伴", "成长", "忠诚", "命运", "长期"]
        if any(signal in text for signal in strong_signals):
            temperature = "warm" if TEMPERATURE_ORDER[temperature] < TEMPERATURE_ORDER["warm"] else temperature
        return temperature

    def _normalize_refined_memory(
        self,
        refined: Dict,
        session_id: Optional[str] = None,
        message_span: Optional[List[int]] = None,
        session_ns: Optional[str] = None,
    ) -> Dict:
        refined = dict(refined or {})
        refined.setdefault("event", "对话片段")
        refined.setdefault("feeling", "")
        refined.setdefault("cognition", "")
        refined.setdefault("decision", "无")
        refined["user_habit"] = str(refined.get("user_habit") or "").strip()
        tags = refined.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        refined["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()]

        source = refined.get("source") if isinstance(refined.get("source"), dict) else {}
        source.setdefault("session_id", session_id or "")
        source.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
        if message_span:
            source["message_span"] = message_span
        elif not isinstance(source.get("message_span"), list):
            source["message_span"] = []
        refined["source"] = source
        refined["temperature"] = self._normalize_temperature(refined)

        # v3: target_scope 归一化。LLM 漏填时的兜底规则：
        # 项目注册表能按文本命中 → project；带强情感 → relationship；已绑定项目会话兜底 → project；否则 discard
        if self._namespaces_active:
            from .namespace_manager import normalize_refiner_scope
            scope = normalize_refiner_scope(refined.get("target_scope"))
            if not scope:
                hit = self._namespace_manager().resolve_from_text(
                    refined["event"] + " " + " ".join(refined["tags"])
                )
                if hit:
                    scope = "project"
                elif str(refined.get("feeling", "")).strip():
                    scope = "relationship"
                elif session_ns and refined.get("is_important"):
                    scope = "project"
                else:
                    scope = "discard"
            refined["target_scope"] = scope
            from .namespace_topics import sanitize_topic_name
            raw_topic = str(refined.get("topic_name") or "").strip()
            if raw_topic.lower() in ("", "无", "空", "none", "null", "n/a"):
                refined["topic_name"] = ""
            else:
                refined["topic_name"] = sanitize_topic_name(raw_topic)
        return refined

    def _format_source(self, source: Dict) -> str:
        session_id = source.get("session_id") or ""
        date = source.get("date") or ""
        span = source.get("message_span") or []
        parts = []
        if session_id:
            parts.append(f"session={session_id}")
        if date:
            parts.append(f"date={date}")
        if isinstance(span, list) and len(span) == 2:
            parts.append(f"span={span[0]}-{span[1]}")
        return ", ".join(parts) if parts else "无"

    def _ensure_memory_sections(self, memory_content: str) -> str:
        content = memory_content.strip()
        if not content:
            content = "# 记忆"
        if "## 事件日志" not in content:
            content += "\n\n## 事件日志\n"
        if "## 沉淀记忆" not in content:
            content += "\n\n## 沉淀记忆\n"
        return content.rstrip() + "\n"

    def generate_memory_entry(self, refined: Dict) -> str:
        """生成沉淀记忆的 Markdown 格式"""
        date = datetime.now().strftime("%Y-%m-%d")
        source_text = self._format_source(refined.get("source") or {})
        cognition = refined.get("cognition", "") or "无"
        entry = f"""
<!-- temp:{refined.get('temperature', 'warm')} -->
### [{date}] {refined.get('event', '对话片段')}

**事件**：{refined.get('event', '')}

**感受**：{refined.get('feeling', '')}

**认知**：{cognition}

**决定**：{refined.get('decision', '无')}

**标签**：{' '.join(refined.get('tags', []))}

**来源**：{source_text}

---
"""
        return entry

    def save_to_memory(self, refined: Dict) -> bool:
        """保存沉淀记忆到 MEMORY.md"""
        try:
            entry = self.generate_memory_entry(refined).strip()
            memory_content = self._ensure_memory_sections(self.memory_manager.read_long_term_memory())
            lines = memory_content.split("\n")
            new_lines = []
            inserted = False

            for i, line in enumerate(lines):
                new_lines.append(line)
                if line.startswith("## 沉淀记忆") and not inserted:
                    next_is_section = False
                    for j in range(i + 1, len(lines)):
                        if lines[j].startswith("## "):
                            next_is_section = True
                            break
                    new_lines.append("")
                    new_lines.append(entry)
                    inserted = True
                    if next_is_section:
                        continue

            if not inserted:
                new_lines.extend(["", "## 沉淀记忆", "", entry])

            self.memory_manager.write_long_term_memory(
                "\n".join(new_lines).rstrip() + "\n"
            )
            logger.info("[MemoryRefinerV2] 已保存沉淀记忆: %s", refined.get('event', ''))
            return True
        except Exception as e:
            logger.error("[MemoryRefinerV2] 保存失败: %s", e)
            return False

    # ===== v3 命名空间分流（2026-10-24）=====

    def _resolve_project_namespace(
        self,
        refined: Dict,
        session_ns: Optional[str] = None,
    ) -> Optional[str]:
        """project 记忆归属解析：LLM 显式已注册项目 > 文本命中 > 会话绑定项目 > 按 LLM 自拟名建新项目。"""
        ns_id, _ = self._resolve_project_namespace_with_created(refined, session_ns=session_ns)
        return ns_id

    def _resolve_project_namespace_with_created(
        self,
        refined: Dict,
        session_ns: Optional[str] = None,
    ) -> Tuple[Optional[str], bool]:
        ns = self._namespace_manager()
        project_name = str(refined.get("project_name", "") or "").strip()

        # 1. LLM 明确给出了已注册（未归档）项目名或别名 → 优先匹配
        if project_name:
            matched = ns.resolve_id_or_alias(project_name, include_archived=False)
            if matched:
                return matched["id"], False

        # 2. 事件/标签文本命中已注册项目
        text = refined.get("event", "") + " " + " ".join(refined.get("tags", []))
        hit = ns.resolve_from_text(text)
        if hit:
            return hit, False

        # 3. 当前会话已显式绑定有效项目：
        #    - 若 LLM 未填新名，或所填名字与当前绑定项目名称/别名存在包含重叠（防改写误建重复项目）
        #      → 直接归属到会话绑定项目
        if session_ns:
            bound_entry = ns.get_namespace(session_ns)
            if bound_entry and bound_entry.get("status") != "archived":
                if not project_name:
                    return bound_entry["id"], False
                pn_lc = project_name.lower()
                bound_tokens = [bound_entry.get("name", "")] + list(bound_entry.get("aliases") or [])
                if any(
                    len(t.strip()) >= 2 and (pn_lc in t.strip().lower() or t.strip().lower() in pn_lc)
                    for t in bound_tokens
                ):
                    return bound_entry["id"], False

        if not project_name:
            return None, False

        # 4. 新项目：LLM 自拟名自动注册（用户可在面板改名/注销）
        entry = ns.create_namespace(name=project_name[:20])
        logger.info("[MemoryRefinerV2] auto-created namespace %s for %r", entry["id"], project_name)
        return entry["id"], True

    def _merge_project_card(self, ns_id: str, refined: Dict) -> bool:
        """状态卡增量合并：现有卡 + 新事实 → LLM 覆写更新后的卡。
        失败兜底 append_card_line 原文进卡，保证信息不丢。"""
        from .namespace_manager import _truncate_card, extract_card_summary

        ns = self._namespace_manager()
        card = ns.read_card(ns_id)
        entry_text = self.generate_memory_entry(refined).strip()
        # 保头保尾裁剪送入合并，避免超长卡片截尾丢掉标题和 ### 当前状态
        prompt_card = _truncate_card(card, CARD_MERGE_MAX_CHARS)

        try:
            prompt = f"""你是项目状态卡维护器。把【新事实】合并进【现有状态卡】，输出更新后的完整卡片。

规则：
- 这是"当前状态卡"不是流水账：新事实推翻旧状态时直接覆写，不要保留过时表述
- 保持/沿用现有卡片结构（当前状态 / 关键事实 / 红线与禁忌 / 情感基调），没有的节可以新建
- 保留现有的标题与未被推翻的红线与禁忌
- 情感基调 = 与该项目相关的相处氛围，一两行以内
- 不确定的旧信息宁可不删；禁止编造未提及的内容

【现有状态卡】
{prompt_card}

【新事实】
{entry_text}

只输出更新后的完整 Markdown 卡片，不要解释。"""
            response = self.llm_client.chat_completion([
                {"role": "user", "content": prompt}
            ])
            merged = str(response.get("content", "") or "").strip()
            if merged.startswith("```"):
                merged = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", merged).strip()
            if not merged or len(merged) < 20:
                raise ValueError("merged card too short")
            ns.write_card(ns_id, merged + "\n")
        except Exception as e:
            logger.warning("[MemoryRefinerV2] card merge failed (%s), fallback append", e)
            ns.append_card_line(ns_id, entry_text)

        summary = extract_card_summary(ns.read_card(ns_id), max_len=100)
        ns.update_namespace(ns_id, summary=summary)
        return True

    def _merge_project_topic(self, ns_id: str, topic_name: str, refined: Dict) -> bool:
        """局部树叉增量合并：只更新 topics/{topic_name}.md，不重写整张根卡也不碰其他树叉。"""
        from .namespace_manager import _has_substantive_content, extract_card_summary
        from .namespace_topics import TOPIC_MERGE_MAX_CHARS, extract_topic_summary, sanitize_topic_name

        ns = self._namespace_manager()
        safe_topic = sanitize_topic_name(topic_name)
        if not safe_topic:
            return self._merge_project_card(ns_id, refined)

        existing = ns.read_topic(ns_id, safe_topic).strip()
        if not existing:
            existing = f"# {safe_topic}\n"
        prompt_topic = existing[-TOPIC_MERGE_MAX_CHARS:] if len(existing) > TOPIC_MERGE_MAX_CHARS else existing
        entry_text = self.generate_memory_entry(refined).strip()

        try:
            prompt = f"""你是项目专题树叉维护器。把【新事实】增量合并进专题「{safe_topic}」的【现有树叉内容】，输出更新后的完整专题 Markdown。

规则：
- 只维护「{safe_topic}」这一专题，保留精炼结构（可用小标题或要点列表），不要写成聊天流水账
- 新事实推翻旧结论时直接更新/替换过时表述，保持内容紧致准确
- 不确定的旧事实宁可保留；禁止编造未提及的内容

【现有树叉内容】
{prompt_topic}

【新事实】
{entry_text}

只输出更新后的完整 Markdown 内容，不要解释。"""
            response = self.llm_client.chat_completion([
                {"role": "user", "content": prompt}
            ])
            merged = str(response.get("content", "") or "").strip()
            if merged.startswith("```"):
                merged = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", merged).strip()
            if not merged or len(merged) < 10:
                raise ValueError("merged topic too short")
            ns.write_topic(ns_id, safe_topic, merged + "\n")
        except Exception as e:
            logger.warning("[MemoryRefinerV2] topic merge failed (%s), fallback append", e)
            fallback = existing.rstrip() + f"\n\n- {entry_text}\n"
            ns.write_topic(ns_id, safe_topic, fallback)

        # 若项目根卡尚无实质内容，初始化精炼根卡骨架并同步注册表摘要
        card = ns.read_card(ns_id)
        entry_meta = ns.get_namespace(ns_id) or {}
        if not _has_substantive_content(card):
            proj_name = entry_meta.get("name") or ns_id
            ev = str(refined.get("event") or "").strip()
            dec = str(refined.get("decision") or "").strip()
            status_line = ev + (f"（决定：{dec}）" if dec and dec != "无" else "")
            root_card = (
                f"# {proj_name} · 项目状态卡\n\n"
                f"### 当前状态\n{status_line or '专题知识持续沉淀中。'}\n\n"
                "### 红线与禁忌\n- （暂无）\n"
            )
            ns.write_card(ns_id, root_card)
        if not (entry_meta.get("summary") or "").strip():
            topic_sum = extract_topic_summary(ns.read_topic(ns_id, safe_topic), max_len=100)
            card_sum = extract_card_summary(ns.read_card(ns_id), max_len=100)
            ns.update_namespace(ns_id, summary=card_sum or topic_sum)
        return True

    def rebuild_card_from_timeline(self, ns_id: str, use_llm: bool = True) -> str:
        """从 timeline.jsonl（事实之源）重算项目状态卡（物化视图）。

        LLM 不可用或失败时自动降级到 NamespaceManager 确定性骨架重建，保证永远可用。
        """
        from .namespace_manager import _extract_markdown_section, extract_card_summary

        ns = self._namespace_manager()
        entry = ns.get_namespace(ns_id)
        if entry is None:
            raise ValueError(f"项目命名空间不存在: {ns_id}")

        records = ns.read_timeline(ns_id, limit=None)
        if not use_llm or not records or self.llm_client is None:
            return ns.rebuild_card_skeleton_from_timeline(ns_id)

        existing_card = ns.read_card(ns_id)
        preserved_red_lines = _extract_markdown_section(existing_card, ("红线", "禁忌"))
        preserved_tone = _extract_markdown_section(existing_card, ("情感基调",))

        # 取最近 60 条时间线事件，控制 prompt 体积
        recent = records[-60:]
        tl_lines: List[str] = []
        for idx, rec in enumerate(recent, 1):
            ts_str = str(rec.get("ts", ""))[:19]
            ev = str(rec.get("event", "")).strip()
            dec = str(rec.get("decision", "")).strip()
            cog = str(rec.get("cognition", "")).strip()
            detail = str(rec.get("detail", "")).strip()
            parts = [f"{idx}. [{ts_str}] {ev}"]
            if dec and dec != "无":
                parts.append(f"决定：{dec}")
            if cog:
                parts.append(f"认知：{cog}")
            if detail:
                parts.append(f"细节：{detail[:200]}")
            tl_lines.append(" | ".join(parts))

        red_block = f"\n\n【需要保留的现有红线与禁忌】\n{preserved_red_lines}" if preserved_red_lines else ""
        tone_block = f"\n\n【现有情感基调参考】\n{preserved_tone}" if preserved_tone else ""
        existing_topics = ns.list_topics(ns_id)
        topics_block = (
            "\n\n【已有专题树叉（细节已单独存放，根卡只需保持总览与红线）】\n"
            + "\n".join(f"- {t['name']}：{t.get('summary', '')}" for t in existing_topics)
            if existing_topics
            else ""
        )

        prompt = f"""你是项目状态卡维护器。请基于项目「{entry['name']}」的时间线事实记录，重新整理一份干净、准确、结构化的项目状态卡。

规则：
- 严格以时间线事实为依据，新事实推翻旧状态时只保留最新有效状态
- 必须使用结构：
  # {entry['name']} · 项目状态卡
  ### 当前状态
  ### 关键事实
  ### 红线与禁忌
  ### 情感基调
- 如有现有红线与禁忌，务必完整保留且不要弱化
- 禁止编造时间线中不存在的事实

【时间线记录（共 {len(records)} 条，展示最近 {len(recent)} 条）】
{chr(10).join(tl_lines)}{red_block}{tone_block}{topics_block}

只输出完整 Markdown 状态卡，不要解释。"""

        try:
            response = self.llm_client.chat_completion([
                {"role": "user", "content": prompt}
            ])
            rebuilt = str(response.get("content", "") or "").strip()
            if rebuilt.startswith("```"):
                rebuilt = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", rebuilt).strip()
            if not rebuilt or len(rebuilt) < 20:
                raise ValueError("rebuilt card too short")
            ns.write_card(ns_id, rebuilt + "\n")
            summary = extract_card_summary(rebuilt, max_len=100)
            ns.update_namespace(ns_id, summary=summary)
            return rebuilt + "\n"
        except Exception as e:
            logger.warning("[MemoryRefinerV2] LLM rebuild card failed (%s), fallback skeleton", e)
            return ns.rebuild_card_skeleton_from_timeline(ns_id)

    def _save_namespaced(
        self,
        refined: Dict,
        session_ns: Optional[str] = None,
        on_namespace_created: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """v3 分流写入。返回是否写入了长期记忆层（global 或项目卡）。"""
        ns = self._namespace_manager()
        scope = refined.get("target_scope")

        if scope == "relationship":
            parts = [refined.get("event", "")]
            if refined.get("cognition"):
                parts.append(f"认知：{refined['cognition']}")
            if refined.get("feeling"):
                parts.append(f"感受：{refined['feeling']}")
            ns.append_global_entry("。".join(p for p in parts if p))
            return True

        if scope == "project":
            ns_id, created = self._resolve_project_namespace_with_created(
                refined, session_ns=session_ns
            )
            if ns_id is None:
                # 归属解析失败：带情感的进全局层，纯事实的丢弃
                if str(refined.get("feeling", "")).strip():
                    ns.append_global_entry(refined.get("event", ""))
                    return True
                logger.info("[MemoryRefinerV2] project 记忆无法归属且无情感，丢弃: %s", refined.get("event", "")[:50])
                return False
            if created and on_namespace_created is not None:
                try:
                    on_namespace_created(ns_id)
                except Exception:
                    logger.debug("[MemoryRefinerV2] on_namespace_created callback failed", exc_info=True)
            # 解析目标树叉（若已达软上限且新树叉不在已有列表中，优先归并到最相关已有树叉）
            topic_name = str(refined.get("topic_name") or "").strip()
            if topic_name:
                from .namespace_topics import MAX_TOPICS_SOFT_LIMIT, select_relevant_topics

                existing_topics = ns.list_topics(ns_id)
                existing_names = {t["name"] for t in existing_topics}
                if topic_name not in existing_names and len(existing_topics) >= MAX_TOPICS_SOFT_LIMIT:
                    query = f"{topic_name} {refined.get('event', '')} {' '.join(refined.get('tags', []))}"
                    matched_topics = select_relevant_topics(ns._ns_dir(ns_id), query, max_count=1)
                    if matched_topics:
                        topic_name = matched_topics[0]["name"]
                    else:
                        topic_name = ""

            # timeline 永远追加（事实之源），卡片/树叉增量合并（物化视图）
            tl_record = {
                "event": refined.get("event", ""),
                "decision": refined.get("decision", ""),
                "cognition": refined.get("cognition", ""),
                "tags": refined.get("tags", []),
                "temperature": refined.get("temperature", "warm"),
                "session_id": (refined.get("source") or {}).get("session_id", ""),
            }
            if topic_name:
                tl_record["topic"] = topic_name
            ns.append_timeline(ns_id, tl_record)
            if topic_name:
                return self._merge_project_topic(ns_id, topic_name, refined)
            return self._merge_project_card(ns_id, refined)

        # user / discard：不占项目或全局长期记忆层（user 另走 USER.md 画像演进与 REFLECTIONS 洞察链路）
        return False

    def _try_evolve_user_profile(
        self,
        insight: str,
        *,
        session_id: str = "",
        reason: str = "",
    ) -> bool:
        """将跨会话用户习惯/偏好增量合并进 USER.md（不破坏现有结构）。"""
        if not insight or len(insight.strip()) < 4:
            return False
        if not hasattr(self.memory_manager, "write_user_profile"):
            return False
        try:
            from .user_profile_evolver import UserProfileEvolver

            res = UserProfileEvolver(self.llm_client, self.memory_manager).evolve(
                insight,
                session_id=session_id,
                reason=reason,
            )
            return bool(res.get("updated"))
        except Exception as e:
            logger.warning("[MemoryRefinerV2] user profile evolve failed: %s", e)
            return False

    def process_and_save(
        self,
        user_message: str,
        assistant_message: str,
        session_id: Optional[str] = None,
        message_span: Optional[List[int]] = None,
        session_ns: Optional[str] = None,
        on_namespace_created: Optional[Callable[[str], None]] = None,
        tool_summary: Optional[str] = None,
    ) -> Dict[str, bool]:
        """完整流程：提炼 → 判断 → 分流保存

        v2（命名空间未启用）: MEMORY.md + REFLECTIONS.md，行为不变
        v3（命名空间已启用）: GLOBAL.md / 项目卡+timeline / REFLECTIONS.md + USER.md 画像演进，
        **不再写 MEMORY.md**（存量文件冻结为归档）

        Returns:
            {"memory_saved": bool, "reflection_saved": bool, "profile_updated": bool}
        """
        from .refine_signals import unwrap_assistant_payload

        clean_assistant, _ = unwrap_assistant_payload(assistant_message)
        result: Dict[str, bool] = {
            "memory_saved": False,
            "reflection_saved": False,
            "profile_updated": False,
        }

        # 1. 提炼长期记忆
        refined = self.refine_conversation(
            user_message,
            clean_assistant,
            session_id=session_id,
            message_span=message_span,
            session_ns=session_ns,
            tool_summary=tool_summary,
        )
        if refined and refined.get("is_important", False):
            if self._namespaces_active:
                result["memory_saved"] = self._save_namespaced(
                    refined,
                    session_ns=session_ns,
                    on_namespace_created=on_namespace_created,
                )
            else:
                result["memory_saved"] = self.save_to_memory(refined)

            # 2. 用户画像增量演进：target_scope=user 直接提纯，或项目/关系对话附带 user_habit 顺手反哺
            habit_insight = str(refined.get("user_habit") or "").strip()
            if refined.get("target_scope") == "user":
                parts = [
                    habit_insight,
                    str(refined.get("cognition") or "").strip(),
                    str(refined.get("event") or "").strip(),
                ]
                dec = str(refined.get("decision") or "").strip()
                if dec and dec != "无":
                    parts.append(f"偏好/决定：{dec}")
                combined = "；".join(dict.fromkeys(p for p in parts if p))
                result["profile_updated"] = self._try_evolve_user_profile(
                    combined,
                    session_id=session_id or "",
                    reason=str(refined.get("reason") or ""),
                )
            elif habit_insight:
                result["profile_updated"] = self._try_evolve_user_profile(
                    habit_insight,
                    session_id=session_id or "",
                    reason=f"跨项目观察（{refined.get('project_name') or refined.get('event', '')[:30]}）",
                )
        elif refined and refined.get("user_habit"):
            # 即使本轮事务本身不重要（如临时小事），若体现了稳定习惯也可尝试增量沉淀
            result["profile_updated"] = self._try_evolve_user_profile(
                str(refined.get("user_habit") or ""),
                session_id=session_id or "",
                reason=str(refined.get("reason") or ""),
            )

        # 温度衰减保鲜——无条件触发，重要/不重要都过这道
        # 闸做在 apply_temp_decay() 内部（TEMP_DECAY_MIN_INTERVAL_SEC=1h），多余调用零成本早退
        # 兜的盲区：连续重要对话 → save_to_memory 一直走 → 之前 else 分支永远进不去
        # 现在无论走哪条分支都补位，A 启动钩子 + C 这里 = 真闭环
        if not self._namespaces_active:
            try:
                self.memory_manager.apply_temp_decay()
            except Exception as e:
                logger.warning("[TEMP_DECAY] refiner-side pass failed (non-fatal): %s", e)

        # 2. 提取行为洞察（概率触发；v3 下 user 洞察必过这道）
        import random
        should_reflect = (
            random.random() < 0.3
            or (refined and refined.get("is_important"))
            or (refined is not None and refined.get("target_scope") == "user")
        )
        if should_reflect:
            reflection = self.extract_reflection(user_message, clean_assistant)
            if reflection:
                result["reflection_saved"] = self.save_reflection(reflection)

        return result


if __name__ == "__main__":
    print("MemoryRefinerV2 模块加载成功")
    print("使用方法：")
    print("  refiner = MemoryRefinerV2(llm_client, memory_manager)")
    print("  result = refiner.process_and_save(user_message, assistant_message)")
