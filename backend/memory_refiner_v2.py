"""
记忆提炼模块 V2 - 使用 LLM 智能提炼对话
从对话中提取真正重要的内容，生成有情感标记的沉淀记忆
V2.1: 新增行为洞察提取 → 写入 REFLECTIONS.md
"""

from datetime import datetime
from typing import List, Dict, Optional
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# REFLECTIONS.md 容量限制（行数 / 字符数）
REFLECTIONS_MAX_LINES = 50
REFLECTIONS_MAX_CHARS = 5000

TEMPERATURE_ORDER = {"burning": 3, "warm": 2, "cold": 1}


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

    def refine_conversation(
        self,
        user_message: str,
        assistant_message: str,
        session_id: Optional[str] = None,
        message_span: Optional[List[int]] = None,
    ) -> Optional[Dict]:
        """提炼单轮对话，判断是否值得沉淀。返回结构化记忆或 None。"""
        today = datetime.now().strftime("%Y-%m-%d")
        source_hint = {
            "session_id": session_id,
            "date": today,
            "message_span": message_span or [],
        }
        prompt = f"""你是一个记忆提炼器。请分析以下对话，判断是否值得沉淀为长期记忆。

用户说：
{user_message}

助手回复：
{assistant_message}

来源线索：
{json.dumps(source_hint, ensure_ascii=False)}

请判断：
1. 这段对话是否包含重要信息？（重要决定、情感时刻、新认知、关系变化等）
2. 如果重要，请提取关键内容并生成沉淀记忆
3. 只在真正重要时使用 warm / burning；普通事实或一次性信息用 cold

请以 JSON 格式返回：
{{
    "is_important": true/false,
    "reason": "为什么重要/不重要",
    "event": "发生了什么（一句话）",
    "feeling": "当时的感受（如：感动、被信任、焦虑、开心）",
    "cognition": "形成了什么新的理解，没有则写空字符串",
    "decision": "做出了什么决定（如果有）",
    "tags": ["#标签1", "#标签2"],
    "temperature": "burning|warm|cold",
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
                return self._normalize_refined_memory(refined, session_id=session_id, message_span=message_span)
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
    ) -> Dict:
        refined = dict(refined or {})
        refined.setdefault("event", "对话片段")
        refined.setdefault("feeling", "")
        refined.setdefault("cognition", "")
        refined.setdefault("decision", "无")
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

    def process_and_save(
        self,
        user_message: str,
        assistant_message: str,
        session_id: Optional[str] = None,
        message_span: Optional[List[int]] = None,
    ) -> Dict[str, bool]:
        """完整流程：提炼 → 判断 → 保存（MEMORY.md + REFLECTIONS.md）

        Returns:
            {"memory_saved": bool, "reflection_saved": bool}
        """
        result = {"memory_saved": False, "reflection_saved": False}

        # 1. 提炼长期记忆
        refined = self.refine_conversation(
            user_message,
            assistant_message,
            session_id=session_id,
            message_span=message_span,
        )
        if refined and refined.get("is_important", False):
            result["memory_saved"] = self.save_to_memory(refined)

        # 温度衰减保鲜——无条件触发，重要/不重要都过这道
        # 闸做在 apply_temp_decay() 内部（TEMP_DECAY_MIN_INTERVAL_SEC=1h），多余调用零成本早退
        # 兜的盲区：连续重要对话 → save_to_memory 一直走 → 之前 else 分支永远进不去
        # 现在无论走哪条分支都补位，A 启动钩子 + C 这里 = 真闭环
        try:
            self.memory_manager.apply_temp_decay()
        except Exception as e:
            logger.warning("[TEMP_DECAY] refiner-side pass failed (non-fatal): %s", e)

        # 2. 提取行为洞察（概率触发）
        import random
        should_reflect = random.random() < 0.3 or (refined and refined.get("is_important"))
        if should_reflect:
            reflection = self.extract_reflection(user_message, assistant_message)
            if reflection:
                result["reflection_saved"] = self.save_reflection(reflection)

        return result


if __name__ == "__main__":
    print("MemoryRefinerV2 模块加载成功")
    print("使用方法：")
    print("  refiner = MemoryRefinerV2(llm_client, memory_manager)")
    print("  result = refiner.process_and_save(user_message, assistant_message)")
