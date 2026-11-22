"""用户画像增量提纯与演进引擎（USER.md）。

职责（低耦合纯模块）：
1. 将跨会话稳定成立的用户习惯、偏好、决策风格、审美或称呼认知，
   增量、去重、不毁格式地合并进 ``USER.md``。
2. 严守边界：过滤单项目具体事务细节与一次性琐事；保留已有结构与手写设定；
   遇到矛盾认知时推翻旧条目而非堆砌流水账；已有等价描述时返回 NO_CHANGE 跳过。
3. 变更留痕与安全护栏：防意外大幅缩水误删，每次自动修改前将上一版本快照
   记录到同目录 ``USER_HISTORY.jsonl``，支持随时查看与一键回滚。
"""
from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
import re
import threading
from typing import Any, Dict, List, Optional
import uuid

logger = logging.getLogger(__name__)

USER_PROFILE_MAX_CHARS = 5000
USER_HISTORY_MAX_ENTRIES = 30

_PROFILE_LOCKS: Dict[str, threading.RLock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()


def _get_lock(key: str) -> threading.RLock:
    with _PROFILE_LOCKS_GUARD:
        lock = _PROFILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROFILE_LOCKS[key] = lock
        return lock


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


class UserProfileEvolver:
    """USER.md 增量提纯、历史快照与回滚引擎。"""

    def __init__(self, llm_client: Any, memory_manager: Any):
        self.llm_client = llm_client
        self.memory_manager = memory_manager

    def _history_path(self) -> Optional[Path]:
        provider = getattr(self.memory_manager, "provider", None)
        user_file = getattr(provider, "user_file", None)
        if user_file is not None:
            return Path(user_file).parent / "USER_HISTORY.jsonl"
        return None

    def _lock_key(self) -> str:
        hp = self._history_path()
        if hp is not None:
            return str(hp.resolve())
        return f"mm:{id(self.memory_manager)}"

    def _read_profile(self) -> str:
        try:
            return self.memory_manager.read_user_profile() or ""
        except Exception:
            return ""

    def _write_profile(self, content: str) -> None:
        self.memory_manager.write_user_profile(content)

    def _default_template(self) -> str:
        try:
            from .agent_config import DEFAULT_USER_MD
            return DEFAULT_USER_MD
        except Exception:
            return (
                "# USER.md — 关于你的用户\n\n"
                "## 基本认知\n\n*（暂无）*\n\n"
                "## 我观察到的\n\n*（暂无观察）*\n"
            )

    def list_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """按时间倒序列出最近的 USER.md 演进记录（不含冗长 prev_content 原文，仅返回摘要元数据）。"""
        hp = self._history_path()
        if hp is None or not hp.exists():
            return []
        with _get_lock(self._lock_key()):
            records: List[Dict[str, Any]] = []
            try:
                for line in hp.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    records.append({
                        "id": rec.get("id", ""),
                        "ts": rec.get("ts", ""),
                        "insight": rec.get("insight", ""),
                        "reason": rec.get("reason", ""),
                        "session_id": rec.get("session_id", ""),
                        "kind": rec.get("kind", "auto"),
                        "prev_chars": len(rec.get("prev_content") or ""),
                        "new_chars": rec.get("new_chars", 0),
                    })
            except OSError:
                return []
            records.reverse()
            return records[:limit] if limit > 0 else records

    def _append_history(
        self,
        *,
        insight: str,
        reason: str,
        session_id: str,
        prev_content: str,
        new_chars: int,
        kind: str = "auto",
    ) -> Optional[str]:
        hp = self._history_path()
        if hp is None:
            return None
        entry_id = uuid.uuid4().hex[:10]
        record = {
            "id": entry_id,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "insight": insight.strip(),
            "reason": reason.strip(),
            "session_id": session_id.strip(),
            "kind": kind,
            "prev_content": prev_content,
            "new_chars": new_chars,
        }
        lines: List[str] = []
        if hp.exists():
            try:
                lines = [ln for ln in hp.read_text(encoding="utf-8").splitlines() if ln.strip()]
            except OSError:
                lines = []
        lines.append(json.dumps(record, ensure_ascii=False))
        if len(lines) > USER_HISTORY_MAX_ENTRIES:
            lines = lines[-USER_HISTORY_MAX_ENTRIES:]
        _atomic_write_text(hp, "\n".join(lines) + "\n")
        return entry_id

    def rollback(self, entry_id: str) -> Optional[str]:
        """将 USER.md 回滚到指定历史条目修改前的版本，并留一条回滚记录。"""
        hp = self._history_path()
        if hp is None or not hp.exists():
            return None
        with _get_lock(self._lock_key()):
            target: Optional[Dict[str, Any]] = None
            try:
                for line in hp.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("id") == entry_id:
                        target = rec
                        break
            except OSError:
                return None
            if target is None or "prev_content" not in target:
                return None

            current = self._read_profile()
            restored = str(target["prev_content"])
            self._append_history(
                insight=f"回滚至 {str(target.get('ts', ''))[:16]} 修改前版本",
                reason=f"撤销条目 {entry_id}",
                session_id="",
                prev_content=current,
                new_chars=len(restored),
                kind="rollback",
            )
            self._write_profile(restored)
            return restored

    def evolve(
        self,
        insight: str,
        *,
        session_id: str = "",
        reason: str = "",
        kind: str = "auto",
    ) -> Dict[str, Any]:
        """将一条跨会话用户认知/习惯增量合并进 USER.md。

        Returns:
            {"updated": bool, "history_id": str | None, "skipped_reason": str}
        """
        clean_insight = (insight or "").strip()
        if len(clean_insight) < 4:
            return {"updated": False, "history_id": None, "skipped_reason": "empty_insight"}
        if self.llm_client is None:
            return {"updated": False, "history_id": None, "skipped_reason": "no_llm_client"}

        with _get_lock(self._lock_key()):
            existing = self._read_profile()
            base_doc = existing if existing.strip() else self._default_template()

            prompt = f"""你是用户画像（USER.md）维护器。请判断【新观察】是否需要增量融入【现有 USER.md】，并在需要时输出更新后的完整 USER.md。

严格准入与合并规则：
1. **只沉淀跨会话长期成立的认知**：称呼、表达偏好、交互习惯、决策原则、审美本能、通用禁忌。
2. **禁止写入**：单个项目的具体业务细节/文件路径/金额/报错，一次性临时指令，或对用户的负面评判。若【新观察】不适合进用户画像，只输出 `NO_CHANGE`。
3. **去重与推翻重构**：
   - 若现有 USER.md 已经完整表达了相同意思，只输出 `NO_CHANGE`；
   - 若与现有某条认知冲突或更深入，**原地更新/提纯该条目**，不要堆砌重复流水账；
   - 优先融入现有对应章节（如 `## 交互与工作哲学`、`## 处事与决策格局`、`## 产品与审美本能`、`## 我观察到的`、`## 基本认知`），并替换 `*（暂无观察）*` 等占位符；
   - 保留原有的 frontmatter（如有 `---` 块）、标题结构、语气风格和末尾约定，绝不删改无关条目；
   - 总字数控制在 {USER_PROFILE_MAX_CHARS} 字符以内，措辞精炼准确。

【现有 USER.md】
{base_doc}

【新观察】
{clean_insight}
{f"（背景：{reason.strip()}）" if reason.strip() else ""}

若无需修改，只输出四个词：NO_CHANGE
否则只输出更新后的完整 USER.md Markdown 正文，不要解释。"""

            try:
                response = self.llm_client.chat_completion([
                    {"role": "user", "content": prompt}
                ])
                merged = str(response.get("content", "") or "").strip()
            except Exception as e:
                logger.warning("[UserProfileEvolver] LLM merge failed: %s", e)
                return {"updated": False, "history_id": None, "skipped_reason": f"llm_error:{e}"}

            if merged.startswith("```"):
                merged = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", merged).strip()

            if not merged or merged.upper() == "NO_CHANGE":
                return {"updated": False, "history_id": None, "skipped_reason": "no_change"}

            if merged.strip() == base_doc.strip():
                return {"updated": False, "history_id": None, "skipped_reason": "identical"}

            # 防护栏：禁止 LLM 意外截断或清空用户原有画像
            if len(base_doc.strip()) > 80 and len(merged) < len(base_doc.strip()) * 0.55:
                logger.warning(
                    "[UserProfileEvolver] rejected suspicious shrink (%d -> %d)",
                    len(base_doc),
                    len(merged),
                )
                return {"updated": False, "history_id": None, "skipped_reason": "sanity_guard_shrank"}

            if len(merged) > USER_PROFILE_MAX_CHARS:
                logger.warning(
                    "[UserProfileEvolver] rejected oversized profile (%d > %d)",
                    len(merged),
                    USER_PROFILE_MAX_CHARS,
                )
                return {"updated": False, "history_id": None, "skipped_reason": "exceeds_max_chars"}

            final_text = merged.rstrip() + "\n"
            history_id = self._append_history(
                insight=clean_insight,
                reason=reason,
                session_id=session_id,
                prev_content=existing,
                new_chars=len(final_text),
                kind=kind,
            )
            self._write_profile(final_text)
            logger.info("[UserProfileEvolver] updated USER.md (history=%s, chars=%d)", history_id, len(final_text))
            return {"updated": True, "history_id": history_id, "skipped_reason": ""}
