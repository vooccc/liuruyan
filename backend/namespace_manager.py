"""项目命名空间记忆管理（v5 · 2026-10-24）。

背景：MEMORY.md 是无命名空间的扁平时间线，多项目（维权案 / 美股 / 柳如烟
开发 / 自媒体…）混沉淀 + 温度排序全量前缀注入，导致跨项目实体漂移与
"张冠李戴"。本模块把长期记忆拆成三层：

1. 全局层 ``GLOBAL.md`` —— 关系里程碑、跨项目习惯与认知。**情感记忆
   永远落这里**，不分命名空间（情感天生跨项目），常驻注入。
2. 项目状态卡 ``{ns}/FACTS.md`` —— 一个项目一张"当前状态卡"
   （当前状态 / 关键事实 / 红线禁忌 / 情感基调），新进展覆写更新而非追加。
3. 事件溯源 ``{ns}/timeline.jsonl`` —— append-only 事实之源；状态卡是
   它的物化视图，可随时重算恢复，卡片写坏不丢数据。

目录布局（per-agent）::

    agents/{agent_id}/namespaces/
    ├── REGISTRY.json      # 项目注册表（canonical name + aliases + workspace 锚）
    ├── GLOBAL.md          # 全局层
    └── {ns_id}/
        ├── FACTS.md
        └── timeline.jsonl

**启用判定（平滑降级）**：REGISTRY.json 存在 → 命名空间模式；不存在 →
走 legacy MEMORY.md 老路径，行为与 v4 完全一致。迁移是纯增量操作：
删掉 namespaces/ 目录即回滚。

本模块只做文件 IO、解析、渲染与确定性重建，不调 LLM——LLM 分流与智能合并在
MemoryRefinerV2，存量迁移在 scripts/migrate_memory_namespaces.py。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 状态卡各节的 token 软上限（注入预算内截断，卡片本体不限）
CARD_MAX_CHARS = 6000
# 全局层注入上限。关系里程碑不是"越新越重要"——5 月的契约和今天的承诺同权，
# 尾部截断会把老里程碑切掉（迁移后 GLOBAL.md ≈ 8.5K chars 全量注入约 4K tokens，
# 仍在 8K 记忆预算内）。增长到超限前应做一次"关系层合并"压缩。
GLOBAL_MAX_CHARS = 20000
TIMELINE_TAIL_DEFAULT = 50

_SLUG_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_ARCHIVE_SUFFIX_RE = re.compile(r"-(\d{14})$")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def slugify_namespace_id(name: str) -> str:
    """项目名 → 文件系统安全的 ns_id。中文保留、空白/符号折叠为 '-'。"""
    slug = _SLUG_RE.sub("-", (name or "").strip()).strip("-").lower()
    return slug or "ns"


def _atomic_write_text(path: Path, content: str) -> None:
    """原子写文本文件（写临时文件再 replace），避免并发读或中断产生半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _clean_markdown_inline(text: str) -> str:
    """清理单行内联 Markdown 符号，避免摘要截出半截 `**` 或反引号。"""
    cleaned = _MD_LINK_RE.sub(r"\1", text or "")
    cleaned = re.sub(r"^\s*(?:[-*+]\s+|\d+\.\s+)", "", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def extract_card_summary(card: str, max_len: int = 100) -> str:
    """从状态卡提取干净的一行摘要（供注册表与项目索引展示）。"""
    meaningful_lines: List[str] = []
    for raw in (card or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("<!--") or s.startswith("---"):
            continue
        cleaned = _clean_markdown_inline(s)
        if not cleaned:
            continue
        meaningful_lines.append(cleaned)
        if len(" ".join(meaningful_lines)) >= max_len + 20:
            break

    if not meaningful_lines:
        return ""

    # 若首行只是“当前状态：”之类短标签，拼上下一行正文
    first = meaningful_lines[0]
    if len(first) <= 12 and first.endswith(("：", ":")) and len(meaningful_lines) > 1:
        combined = f"{first} {meaningful_lines[1]}"
    else:
        combined = first

    if len(combined) <= max_len:
        return combined

    # 优先在句末标点边界截断，避免半截断词
    slice_text = combined[:max_len]
    for sep in ("。", "；", ";", "，", ","):
        pos = slice_text.rfind(sep)
        if pos >= max_len // 2:
            return slice_text[: pos + 1].rstrip("，, ")
    return slice_text.rstrip() + "…"


def _extract_markdown_section(card: str, heading_keywords: Tuple[str, ...]) -> str:
    """提取指定 ### 标题节的完整正文（不含标题行）。"""
    lines = (card or "").splitlines()
    captured: List[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("### ") or stripped.startswith("## "):
            title = stripped.lstrip("#").strip()
            if in_section:
                break
            if any(kw in title for kw in heading_keywords):
                in_section = True
            continue
        if in_section:
            captured.append(line)
    return "\n".join(captured).strip()


class NamespaceManager:
    """项目命名空间注册表 + 状态卡 + timeline 的文件后端。

    线程安全：REGISTRY.json、状态卡、GLOBAL.md 与 timeline.jsonl 读写全程持 RLock
    （refiner 在后台线程跑，主线程同时可能建项目/绑定会话/工具写入）。
    """

    def __init__(self, namespaces_dir: Path):
        self.dir = Path(namespaces_dir)
        self.registry_file = self.dir / "REGISTRY.json"
        self.global_file = self.dir / "GLOBAL.md"
        self._lock = threading.RLock()

    # ── 启用判定 ──────────────────────────────────────────────

    def is_active(self) -> bool:
        """命名空间模式是否启用（REGISTRY.json 落盘即启用）。"""
        return self.registry_file.exists()

    def ensure_registry(self) -> None:
        """确保注册表落盘（启用命名空间模式）。已有注册表时为 no-op。
        场景：某角色只有关系记忆、暂无项目时，迁移/首次写入也要启用模式，
        否则全局层写入了却不注入（legacy 路径继续读 MEMORY.md）。"""
        with self._lock:
            if not self.is_active():
                self._write_registry({"version": 1, "namespaces": []})

    # ── 注册表 CRUD ───────────────────────────────────────────

    def list_namespaces(self, include_archived: bool = True) -> List[Dict[str, Any]]:
        with self._lock:
            data = self._read_registry()
        entries = [dict(entry) for entry in data.get("namespaces", [])]
        if not include_archived:
            entries = [e for e in entries if e.get("status") != "archived"]
        return entries

    def get_namespace(self, ns_id: str) -> Optional[Dict[str, Any]]:
        for entry in self.list_namespaces():
            if entry.get("id") == ns_id:
                return dict(entry)
        return None

    def resolve_id_or_alias(self, project: str, include_archived: bool = False) -> Optional[Dict[str, Any]]:
        """按 id / canonical name / alias 精确解析项目（不区分大小写）。"""
        target = (project or "").strip().lower()
        if not target:
            return None
        for entry in self.list_namespaces(include_archived=include_archived):
            candidates = [
                entry.get("id", ""),
                entry.get("name", ""),
                *(entry.get("aliases") or []),
            ]
            if any((c or "").strip().lower() == target for c in candidates):
                return entry
        return None

    def create_namespace(
        self,
        name: str,
        aliases: Optional[List[str]] = None,
        workspace_path: Optional[str] = None,
        ns_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """注册新项目。重名（id 撞车）时自动加序号后缀。"""
        name = (name or "").strip()
        if not name:
            raise ValueError("项目名不能为空")

        with self._lock:
            data = self._read_registry()
            existing_ids = {e.get("id") for e in data.get("namespaces", [])}

            base_id = ns_id or slugify_namespace_id(name)
            final_id, seq = base_id, 2
            while final_id in existing_ids:
                final_id = f"{base_id}-{seq}"
                seq += 1

            entry = {
                "id": final_id,
                "name": name,
                "aliases": [a.strip() for a in (aliases or []) if a and a.strip()],
                "workspace_path": (workspace_path or "").strip() or None,
                "summary": "",
                "status": "active",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            data.setdefault("namespaces", []).append(entry)
            self._write_registry(data)

            ns_dir = self.dir / final_id
            ns_dir.mkdir(parents=True, exist_ok=True)
            card = ns_dir / "FACTS.md"
            if not card.exists():
                _atomic_write_text(
                    card,
                    f"# {name} · 项目状态卡\n\n"
                    "<!-- 状态卡：新进展覆写更新，不要追加流水账。"
                    "结构建议：当前状态 / 关键事实 / 红线与禁忌 / 情感基调 -->\n",
                )
            (ns_dir / "timeline.jsonl").touch()
        logger.info("[NamespaceManager] created namespace %s (%s)", final_id, name)
        return entry

    def update_namespace(
        self,
        ns_id: str,
        name: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        workspace_path: Optional[str] = None,
        summary: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._read_registry()
            for entry in data.get("namespaces", []):
                if entry.get("id") != ns_id:
                    continue
                if name is not None and name.strip():
                    entry["name"] = name.strip()
                if aliases is not None:
                    entry["aliases"] = [a.strip() for a in aliases if a and a.strip()]
                if workspace_path is not None:
                    entry["workspace_path"] = workspace_path.strip() or None
                if summary is not None:
                    entry["summary"] = summary.strip()[:200]
                if status is not None and status in ("active", "archived"):
                    entry["status"] = status
                entry["updated_at"] = _now_iso()
                self._write_registry(data)
                return dict(entry)
        return None

    def delete_namespace(self, ns_id: str) -> bool:
        """注销项目：只摘注册表，磁盘数据整体挪到 _archived/ 留底。"""
        with self._lock:
            data = self._read_registry()
            remaining = [e for e in data.get("namespaces", []) if e.get("id") != ns_id]
            if len(remaining) == len(data.get("namespaces", [])):
                return False
            data["namespaces"] = remaining
            self._write_registry(data)

            ns_dir = self.dir / ns_id
            if ns_dir.exists():
                archived = self.dir / "_archived"
                archived.mkdir(exist_ok=True)
                target = archived / f"{ns_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                ns_dir.rename(target)
        return True

    def list_archived_backups(self) -> List[Dict[str, Any]]:
        """列出 _archived/ 中被注销留底的项目备份。"""
        with self._lock:
            archived_dir = self.dir / "_archived"
            if not archived_dir.exists():
                return []
            items: List[Dict[str, Any]] = []
            for child in sorted(archived_dir.iterdir(), reverse=True):
                if not child.is_dir():
                    continue
                folder_name = child.name
                base_id = _ARCHIVE_SUFFIX_RE.sub("", folder_name)
                card_file = child / "FACTS.md"
                tl_file = child / "timeline.jsonl"
                display_name = base_id
                summary = ""
                if card_file.exists():
                    try:
                        card_text = card_file.read_text(encoding="utf-8")
                        for line in card_text.splitlines():
                            s = line.strip()
                            if s.startswith("# "):
                                display_name = s[2:].split("·")[0].strip() or base_id
                                break
                        summary = extract_card_summary(card_text, max_len=80)
                    except OSError:
                        pass
                tl_count = 0
                if tl_file.exists():
                    try:
                        with open(tl_file, encoding="utf-8") as fh:
                            tl_count = sum(1 for ln in fh if ln.strip())
                    except OSError:
                        pass
                m = _ARCHIVE_SUFFIX_RE.search(folder_name)
                archived_at = m.group(1) if m else ""
                items.append({
                    "folder_name": folder_name,
                    "base_id": base_id,
                    "name": display_name,
                    "summary": summary,
                    "timeline_count": tl_count,
                    "archived_at": archived_at,
                })
            return items

    def restore_archived_backup(self, folder_name: str) -> Optional[Dict[str, Any]]:
        """从 _archived/ 恢复被注销的项目并重新注册到 REGISTRY.json。"""
        clean_name = Path(folder_name).name
        if not clean_name or clean_name != folder_name:
            return None
        with self._lock:
            archived_dir = self.dir / "_archived"
            src = archived_dir / clean_name
            if not src.exists() or not src.is_dir():
                return None

            base_id = _ARCHIVE_SUFFIX_RE.sub("", clean_name) or "restored-ns"
            data = self._read_registry()
            existing_ids = {e.get("id") for e in data.get("namespaces", [])}
            final_id, seq = base_id, 2
            while final_id in existing_ids or (self.dir / final_id).exists():
                final_id = f"{base_id}-{seq}"
                seq += 1

            dest = self.dir / final_id
            src.rename(dest)

            card_file = dest / "FACTS.md"
            card_text = card_file.read_text(encoding="utf-8") if card_file.exists() else ""
            display_name = final_id
            for line in card_text.splitlines():
                s = line.strip()
                if s.startswith("# "):
                    display_name = s[2:].split("·")[0].strip() or final_id
                    break
            summary = extract_card_summary(card_text, max_len=100)

            entry = {
                "id": final_id,
                "name": display_name,
                "aliases": [],
                "workspace_path": None,
                "summary": summary,
                "status": "active",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            data.setdefault("namespaces", []).append(entry)
            self._write_registry(data)
            logger.info("[NamespaceManager] restored archived backup %s -> %s", clean_name, final_id)
            return dict(entry)

    # ── 别名/工作区解析（refiner 路由 + 会话锚定共用） ─────────

    def resolve_from_text(self, text: str) -> Optional[str]:
        """从一段记忆/对话文本里匹配项目：canonical name + aliases 子串命中。

        过滤已归档项目；多个命中时最长 token 优先（同长度按注册表顺序先建优先），
        防止短别名抢先误命中更长更精确的项目名。
        """
        if not text:
            return None
        best: Optional[Tuple[int, int, str]] = None
        for idx, entry in enumerate(self.list_namespaces(include_archived=False)):
            candidates = [entry.get("name", "")] + list(entry.get("aliases") or [])
            for token in candidates:
                token = (token or "").strip()
                if len(token) >= 2 and token in text:
                    score = (len(token), -idx)
                    if best is None or score > (best[0], best[1]):
                        best = (len(token), -idx, entry.get("id"))
        return best[2] if best else None

    def resolve_from_workspace(self, workspace: Optional[str]) -> Optional[str]:
        """工作区目录 → 命名空间（编码项目自动锚定用）。前缀匹配，长路径优先，忽略已归档项目。"""
        if not workspace:
            return None
        try:
            target = Path(workspace).expanduser().resolve()
        except Exception:
            return None
        best: Optional[Tuple[int, str]] = None
        for entry in self.list_namespaces(include_archived=False):
            anchor = entry.get("workspace_path")
            if not anchor:
                continue
            try:
                anchor_resolved = Path(anchor).expanduser().resolve()
            except Exception:
                continue
            if target == anchor_resolved or anchor_resolved in target.parents:
                depth = len(anchor_resolved.parts)
                if best is None or depth > best[0]:
                    best = (depth, entry.get("id"))
        return best[1] if best else None

    def resolve_session_binding(
        self,
        session: Any,
        user_text: Optional[str] = None,
        autobind: bool = True,
    ) -> Tuple[Optional[str], Optional[str]]:
        """解析当前会话的项目绑定与工作区目录（低耦合统一入口）。

        优先级：
        1. session.project_namespace（存在且未归档）；
        2. session 工作区目录 → workspace 锚定（命中且未绑定时自动落盘绑定）；
        3. 开篇文本自动绑定（autobind_project）。
        """
        workspace = None
        try:
            get_wd = getattr(session, "get_pinned_workspace_directory", None)
            if callable(get_wd):
                workspace = get_wd()
        except Exception:
            workspace = None

        if not self.is_active() or session is None:
            return None, workspace

        bound = getattr(session, "project_namespace", None)
        if bound:
            entry = self.get_namespace(bound)
            if entry and entry.get("status") != "archived":
                return bound, workspace

        ws_ns = self.resolve_from_workspace(workspace)
        if ws_ns:
            if autobind and not bound and hasattr(session, "set_project_namespace"):
                try:
                    session.set_project_namespace(ws_ns)
                    logger.info(
                        "[NamespaceManager] workspace autobind session=%s -> %s",
                        getattr(session, "id", "?"), ws_ns,
                    )
                except Exception:
                    logger.debug("[NamespaceManager] workspace autobind failed", exc_info=True)
            return ws_ns, workspace

        if autobind and user_text:
            hit = autobind_project(session, self, user_text)
            if hit:
                return hit, workspace

        return None, workspace

    # ── 全局层（关系记忆） ────────────────────────────────────

    def read_global(self) -> str:
        with self._lock:
            if self.global_file.exists():
                return self.global_file.read_text(encoding="utf-8")
            return ""

    def write_global(self, content: str) -> None:
        with self._lock:
            _atomic_write_text(self.global_file, content)

    def append_global_entry(self, entry_text: str, date: Optional[str] = None) -> None:
        """追加一条全局层记忆（关系里程碑 / 跨项目认知）。"""
        with self._lock:
            existing = self.read_global().rstrip()
            block = f"### [{date or datetime.now().strftime('%Y-%m-%d')}] {entry_text.strip()}"
            self.write_global((existing + "\n\n" + block).lstrip() + "\n")

    # ── 项目状态卡 + timeline ─────────────────────────────────

    def _ns_dir(self, ns_id: str) -> Path:
        return self.dir / ns_id

    def read_card(self, ns_id: str) -> str:
        with self._lock:
            card = self._ns_dir(ns_id) / "FACTS.md"
            if card.exists():
                return card.read_text(encoding="utf-8")
            return ""

    def write_card(self, ns_id: str, content: str) -> None:
        with self._lock:
            ns_dir = self._ns_dir(ns_id)
            ns_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(ns_dir / "FACTS.md", content)

    def append_card_line(self, ns_id: str, line: str) -> None:
        """LLM 卡片合并失败时的兜底：原文追加到卡底，保证信息不丢。"""
        with self._lock:
            existing = self.read_card(ns_id).rstrip()
            self.write_card(ns_id, existing + "\n\n- " + line.strip() + "\n")

    def append_timeline(self, ns_id: str, entry: Dict[str, Any]) -> None:
        """timeline 追加一条事件（事实之源，永不覆写）。"""
        record = {
            "ts": _now_iso(),
            **entry,
        }
        with self._lock:
            ns_dir = self._ns_dir(ns_id)
            ns_dir.mkdir(parents=True, exist_ok=True)
            with open(ns_dir / "timeline.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _read_all_timeline(self, ns_id: str, query: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            path = self._ns_dir(ns_id) / "timeline.jsonl"
            if not path.exists():
                return []
            records: List[Dict[str, Any]] = []
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        if query and query.strip():
            q = query.strip().lower()
            filtered: List[Dict[str, Any]] = []
            for rec in records:
                haystack = " ".join([
                    str(rec.get("event", "")),
                    str(rec.get("decision", "")),
                    str(rec.get("cognition", "")),
                    str(rec.get("detail", "")),
                    " ".join(str(t) for t in (rec.get("tags") or [])),
                ]).lower()
                if q in haystack:
                    filtered.append(rec)
            return filtered
        return records

    def count_timeline(self, ns_id: str, query: Optional[str] = None) -> int:
        return len(self._read_all_timeline(ns_id, query=query))

    def read_timeline(
        self,
        ns_id: str,
        limit: Optional[int] = TIMELINE_TAIL_DEFAULT,
        offset: int = 0,
        query: Optional[str] = None,
        newest_first: bool = False,
    ) -> List[Dict[str, Any]]:
        records = self._read_all_timeline(ns_id, query=query)
        if newest_first:
            records = list(reversed(records))
            if offset > 0:
                records = records[offset:]
            if limit is not None and limit > 0:
                return records[:limit]
            return records
        # 默认保持向后兼容：返回最尾部的 limit 条（时间正序）
        if offset > 0:
            records = records[:-offset] if offset < len(records) else []
        if limit is not None and limit > 0:
            return records[-limit:]
        return records

    def rebuild_card_skeleton_from_timeline(self, ns_id: str) -> str:
        """确定性物化视图重建：从 timeline.jsonl 全量重算状态卡骨架（不依赖 LLM）。

        保留现有卡片中的 `### 红线与禁忌` 与 `### 情感基调`（防止人工维护的红线丢失）。
        """
        with self._lock:
            entry = self.get_namespace(ns_id)
            name = (entry or {}).get("name") or ns_id
            existing_card = self.read_card(ns_id)
            records = self._read_all_timeline(ns_id)

            red_lines = _extract_markdown_section(existing_card, ("红线", "禁忌"))
            tone = _extract_markdown_section(existing_card, ("情感基调",))

            latest = records[-1] if records else None
            status_lines: List[str] = []
            if latest:
                ts_date = str(latest.get("ts", ""))[:10]
                ev = str(latest.get("event", "")).strip()
                dec = str(latest.get("decision", "")).strip()
                if ev:
                    status_lines.append(f"最近进展（{ts_date or '未知日期'}）：{ev}")
                if dec and dec != "无":
                    status_lines.append(f"最新决策：{dec}")
            else:
                status_lines.append("暂无时间线记录。")

            fact_lines: List[str] = []
            for rec in records[-15:]:
                ts_date = str(rec.get("ts", ""))[:10]
                ev = str(rec.get("event", "")).strip()
                dec = str(rec.get("decision", "")).strip()
                cog = str(rec.get("cognition", "")).strip()
                parts = [p for p in (ev, f"决策：{dec}" if dec and dec != "无" else "", f"认知：{cog}" if cog else "") if p]
                if parts:
                    prefix = f"[{ts_date}] " if ts_date else ""
                    fact_lines.append(f"- {prefix}{'；'.join(parts)}")

            sections = [
                f"# {name} · 项目状态卡",
                "### 当前状态\n" + "\n".join(status_lines),
                "### 关键事实\n" + ("\n".join(fact_lines) if fact_lines else "- 暂无记录"),
            ]
            if red_lines:
                sections.append("### 红线与禁忌\n" + red_lines)
            if tone:
                sections.append("### 情感基调\n" + tone)

            rebuilt = "\n\n".join(sections).rstrip() + "\n"
            self.write_card(ns_id, rebuilt)
            summary = extract_card_summary(rebuilt, max_len=100)
            self.update_namespace(ns_id, summary=summary)
            return rebuilt

    # ── 树叉层（topics/*.md） ─────────────────────────────────

    def list_topics(self, ns_id: str) -> List[Dict[str, Any]]:
        from .namespace_topics import list_topics as _list_topics

        with self._lock:
            return _list_topics(self._ns_dir(ns_id))

    def read_topic(self, ns_id: str, topic_name: str) -> str:
        from .namespace_topics import read_topic as _read_topic

        with self._lock:
            return _read_topic(self._ns_dir(ns_id), topic_name)

    def write_topic(self, ns_id: str, topic_name: str, content: str) -> str:
        from .namespace_topics import write_topic as _write_topic

        with self._lock:
            return _write_topic(
                self._ns_dir(ns_id),
                topic_name,
                content,
                writer=_atomic_write_text,
            )

    def delete_topic(self, ns_id: str, topic_name: str) -> bool:
        from .namespace_topics import delete_topic as _delete_topic

        with self._lock:
            return _delete_topic(self._ns_dir(ns_id), topic_name)

    def merge_topics(
        self,
        ns_id: str,
        source_topic: str,
        target_topic: str,
        merged_content: Optional[str] = None,
    ) -> str:
        from .namespace_topics import merge_topics as _merge_topics

        with self._lock:
            return _merge_topics(
                self._ns_dir(ns_id),
                source_topic,
                target_topic,
                merged_content=merged_content,
            )

    # ── 注入渲染 ──────────────────────────────────────────────

    def render_chat_context(
        self,
        session_ns: Optional[str] = None,
        workspace: Optional[str] = None,
        turn_ns: Optional[str] = None,
        global_max_chars: int = GLOBAL_MAX_CHARS,
        card_max_chars: int = CARD_MAX_CHARS,
        user_text: str = "",
    ) -> str:
        """拼装命名空间模式的记忆上下文。

        结构（与 legacy 路径互斥，永不注入 MEMORY.md）：
        1. ``## 全局记忆`` —— GLOBAL.md（关系层 + 跨项目习惯），常驻；
        2. ``## 当前项目档案`` —— 会话绑定项目的根节点 FACTS.md + 树叉目录 + 按当前话题摘取的 1~2 个相关树叉；
        3. ``## 本轮提及的项目档案`` —— 用户本轮消息命中了**另一个**项目时，
           临时带出该项目的卡（仅本轮参考，不改会话主绑定）。这是"中途
           聊到别的项目自动带记忆"的机制：不抢切、不劫持，随提随取。
        4. ``## 其他项目索引`` —— 每个项目一行（名字 + summary），
           模型看到索引即可主动 recall_memory / 问用户，细节不进上下文。
        """
        from .namespace_topics import (
            list_topics as _list_topics,
            render_topics_catalog,
            select_relevant_topics,
        )

        ns_entry = self.get_namespace(session_ns) if session_ns else None
        ns_id = ns_entry["id"] if (ns_entry and ns_entry.get("status") != "archived") else None
        if ns_id is None:
            ns_id = self.resolve_from_workspace(workspace)

        parts: List[str] = []

        global_text = self.read_global().strip()
        if global_text:
            parts.append("## 全局记忆\n" + _tail(global_text, global_max_chars))

        if ns_id:
            entry = self.get_namespace(ns_id) or {}
            card = self.read_card(ns_id).strip()
            if card and _has_substantive_content(card):
                parts.append(
                    f"## 当前项目档案 · {entry.get('name', ns_id)}\n" + _truncate_card(card, card_max_chars)
                )
            # 树叉层：常驻目录 + 按当前话题动态摘取 1~2 个相关树叉
            ns_dir = self._ns_dir(ns_id)
            topics = _list_topics(ns_dir)
            if topics:
                catalog = render_topics_catalog(topics)
                parts.append(
                    f"### 项目树叉目录（共 {len(topics)} 个专题，需要细节可调 project_archive read_topic 读取）\n"
                    + catalog
                )
                picked = select_relevant_topics(ns_dir, user_text)
                for pt in picked:
                    parts.append(
                        f"### 当前话题相关树叉 · {pt['name']}\n{pt['content']}"
                    )

        # 本轮临时提及的项目（≠ 会话主绑定）：带卡进上下文，标注临时性
        turn_entry = self.get_namespace(turn_ns) if turn_ns else None
        if (
            turn_ns
            and turn_ns != ns_id
            and turn_entry
            and turn_entry.get("status") != "archived"
        ):
            turn_card = self.read_card(turn_ns).strip()
            if turn_card and _has_substantive_content(turn_card):
                parts.append(
                    f"## 本轮提及的项目档案 · {turn_entry.get('name', turn_ns)}"
                    "（用户这轮消息提到，仅本轮参考；会话主项目未变）\n"
                    + _truncate_card(turn_card, card_max_chars)
                )
            turn_picked = select_relevant_topics(self._ns_dir(turn_ns), user_text, max_count=1)
            for pt in turn_picked:
                parts.append(
                    f"### 本轮提及树叉 · {turn_entry.get('name', turn_ns)} / {pt['name']}\n{pt['content']}"
                )

        index_lines = []
        for entry in self.list_namespaces(include_archived=False):
            if entry.get("id") == ns_id:
                continue
            summary = (entry.get("summary") or "").strip()
            suffix = f"—— {summary}" if summary else ""
            index_lines.append(f"- [{entry.get('name')}] {suffix}".rstrip())
        if index_lines:
            parts.append(
                "## 其他项目索引（仅目录，细节按需检索，勿主动联想）\n"
                + "\n".join(index_lines)
                + "\n（用户明确要求切换到某项目时，可调用 set_project_binding 工具切换本会话的项目记忆；"
                "跨项目参考用 recall_memory 检索即可，不必切换。）"
            )

        return "\n\n".join(parts)

    def stats(self) -> Dict[str, Any]:
        namespaces = self.list_namespaces()
        active_count = sum(1 for e in namespaces if e.get("status") != "archived")
        archived_count = len(namespaces) - active_count
        cards_info: Dict[str, Any] = {}
        total_card_chars = 0
        total_timeline_count = 0
        total_topic_count = 0
        for e in namespaces:
            nid = e["id"]
            c_len = len(self.read_card(nid))
            t_len = self.count_timeline(nid)
            topics_len = len(self.list_topics(nid))
            total_card_chars += c_len
            total_timeline_count += t_len
            total_topic_count += topics_len
            cards_info[nid] = {
                "chars": c_len,
                "timeline_count": t_len,
                "topic_count": topics_len,
                "name": e["name"],
                "status": e.get("status", "active"),
            }
        return {
            "active": self.is_active(),
            "count": len(namespaces),
            "active_count": active_count,
            "archived_count": archived_count,
            "global_chars": len(self.read_global()),
            "total_card_chars": total_card_chars,
            "total_timeline_count": total_timeline_count,
            "total_topic_count": total_topic_count,
            "cards": cards_info,
        }

    # ── 内部：注册表读写 ──────────────────────────────────────

    def _read_registry(self) -> Dict[str, Any]:
        if not self.registry_file.exists():
            return {"version": 1, "namespaces": []}
        try:
            data = json.loads(self.registry_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("namespaces"), list):
                return data
        except (json.JSONDecodeError, OSError):
            logger.warning("[NamespaceManager] REGISTRY.json 损坏，按空表处理")
        return {"version": 1, "namespaces": []}

    def _write_registry(self, data: Dict[str, Any]) -> None:
        _atomic_write_text(
            self.registry_file,
            json.dumps(data, ensure_ascii=False, indent=2),
        )


def normalize_refiner_scope(raw: Any) -> str:
    """refiner LLM 输出的 target_scope 归一化到四个合法值。"""
    value = str(raw or "").strip().lower()
    mapping = {
        "relationship": "relationship",
        "emotion": "relationship",
        "情感": "relationship",
        "project": "project",
        "project_fact": "project",
        "事实": "project",
        "user": "user",
        "user_insight": "user",
        "洞察": "user",
        "": "",
    }
    return mapping.get(value, "")


# 会话前 N 条消息内允许自动绑定（超出视为"会话已定型"，不再抢切）
AUTOBIND_MAX_MESSAGES = 12


def autobind_project(session: Any, manager: "NamespaceManager", text: str) -> Optional[str]:
    """会话开篇自动绑定项目（v5 · 读取端自动识别）。

    触发条件（全部满足才绑）：
    1. 命名空间模式已启用；
    2. 会话未绑定（project_namespace 为空）；
    3. 会话尚在开篇（message_count <= AUTOBIND_MAX_MESSAGES）；
    4. 用户消息命中某项目的 name/alias（确定性最长子串匹配，不调 LLM）。

    命中即持久化绑定（set_project_namespace），后续轮次不再重判；
    未命中/条件不符返回 None（全局视角 + 项目索引兜底）。
    误绑的修正出口：记忆浏览器内调整或对话调用 set_project_binding。
    """
    try:
        if not manager.is_active():
            return None
        if getattr(session, "project_namespace", None):
            return session.project_namespace
        if int(getattr(session, "message_count", 0) or 0) > AUTOBIND_MAX_MESSAGES:
            return None
        hit = manager.resolve_from_text(text or "")
        if not hit:
            return None
        session.set_project_namespace(hit)
        logger.info(
            "[NamespaceManager] autobind session=%s -> %s",
            getattr(session, "id", "?"), hit,
        )
        return hit
    except Exception:
        logger.debug("[NamespaceManager] autobind failed (non-fatal)", exc_info=True)
        return None


def _tail(text: str, max_chars: int) -> str:
    """注入截断：保留尾部（最新内容），截断处标注。"""
    if len(text) <= max_chars:
        return text
    cut = text[-max_chars:]
    nl = cut.find("\n")
    if nl > 0:
        cut = cut[nl + 1:]
    return f"（更早内容已截断）\n{cut}"


def _truncate_card(card: str, max_chars: int) -> str:
    """状态卡保头保尾裁剪：避免超长时把标题和 `### 当前状态` 切丢。"""
    if len(card) <= max_chars:
        return card
    marker = "\n\n（中间部分已省略）\n\n"
    usable = max(200, max_chars - len(marker))
    head_budget = max(120, int(usable * 0.4))
    tail_budget = max(80, usable - head_budget)

    head = card[:head_budget]
    last_nl = head.rfind("\n")
    if last_nl > head_budget // 2:
        head = head[:last_nl]

    tail = card[-tail_budget:]
    first_nl = tail.find("\n")
    if 0 < first_nl < len(tail) // 2:
        tail = tail[first_nl + 1:]

    return head.rstrip() + marker + tail.lstrip()


def _has_substantive_content(card: str) -> bool:
    """卡片是否含实际内容（剔除标题/HTML 注释/模板骨架后仍有正文）。
    新建项目的空模板卡不值得占注入预算。"""
    meaningful = []
    for line in card.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("<!--"):
            continue
        meaningful.append(stripped)
    return any(meaningful)


__all__ = [
    "NamespaceManager",
    "slugify_namespace_id",
    "normalize_refiner_scope",
    "autobind_project",
    "extract_card_summary",
    "CARD_MAX_CHARS",
    "GLOBAL_MAX_CHARS",
    "AUTOBIND_MAX_MESSAGES",
]
