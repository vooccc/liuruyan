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

本模块只做文件 IO 与解析，不调 LLM——LLM 分流在 MemoryRefinerV2，
存量迁移在 scripts/migrate_memory_namespaces.py。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 状态卡各节的 token 软上限（注入预算内截断，卡片本体不限）
CARD_MAX_CHARS = 6000
# 全局层注入上限。关系里程碑不是"越新越重要"——5 月的契约和今天的承诺同权，
# 尾部截断会把老里程碑切掉（迁移后 GLOBAL.md ≈ 8.5K chars 全量注入约 4K tokens，
# 仍在 8K 记忆预算内）。增长到超限前应做一次"关系层合并"压缩。
GLOBAL_MAX_CHARS = 20000
TIMELINE_TAIL_DEFAULT = 50

_SLUG_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def slugify_namespace_id(name: str) -> str:
    """项目名 → 文件系统安全的 ns_id。中文保留、空白/符号折叠为 '-'。"""
    slug = _SLUG_RE.sub("-", (name or "").strip()).strip("-").lower()
    return slug or "ns"


class NamespaceManager:
    """项目命名空间注册表 + 状态卡 + timeline 的文件后端。

    线程安全：REGISTRY.json 读写全程持锁（refiner 在后台线程跑，
    主线程同时可能建项目/绑定会话）。
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
        if not self.is_active():
            with self._lock:
                self._write_registry({"version": 1, "namespaces": []})

    # ── 注册表 CRUD ───────────────────────────────────────────

    def list_namespaces(self) -> List[Dict[str, Any]]:
        with self._lock:
            data = self._read_registry()
        return [dict(entry) for entry in data.get("namespaces", [])]

    def get_namespace(self, ns_id: str) -> Optional[Dict[str, Any]]:
        for entry in self.list_namespaces():
            if entry.get("id") == ns_id:
                return dict(entry)
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
                card.write_text(
                    f"# {name} · 项目状态卡\n\n"
                    "<!-- 状态卡：新进展覆写更新，不要追加流水账。"
                    "结构建议：当前状态 / 关键事实 / 红线与禁忌 / 情感基调 -->\n",
                    encoding="utf-8",
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

    # ── 别名/工作区解析（refiner 路由 + 会话锚定共用） ─────────

    def resolve_from_text(self, text: str) -> Optional[str]:
        """从一段记忆/对话文本里匹配项目：canonical name + aliases 子串命中。

        命中多个时返回注册表顺序里最靠前的（先建的项目优先）。
        """
        if not text:
            return None
        for entry in self.list_namespaces():
            if entry.get("status") == "archived":
                continue
            candidates = [entry.get("name", "")] + list(entry.get("aliases") or [])
            for token in candidates:
                token = (token or "").strip()
                if len(token) >= 2 and token in text:
                    return entry.get("id")
        return None

    def resolve_from_workspace(self, workspace: Optional[str]) -> Optional[str]:
        """工作区目录 → 命名空间（编码项目自动锚定用）。前缀匹配，长路径优先。"""
        if not workspace:
            return None
        try:
            target = Path(workspace).expanduser().resolve()
        except Exception:
            return None
        best: Optional[tuple[int, str]] = None
        for entry in self.list_namespaces():
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

    # ── 全局层（关系记忆） ────────────────────────────────────

    def read_global(self) -> str:
        if self.global_file.exists():
            return self.global_file.read_text(encoding="utf-8")
        return ""

    def write_global(self, content: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.global_file.write_text(content, encoding="utf-8")

    def append_global_entry(self, entry_text: str, date: Optional[str] = None) -> None:
        """追加一条全局层记忆（关系里程碑 / 跨项目认知）。"""
        existing = self.read_global().rstrip()
        block = f"### [{date or datetime.now().strftime('%Y-%m-%d')}] {entry_text.strip()}"
        self.write_global((existing + "\n\n" + block).lstrip() + "\n")

    # ── 项目状态卡 + timeline ─────────────────────────────────

    def _ns_dir(self, ns_id: str) -> Path:
        return self.dir / ns_id

    def read_card(self, ns_id: str) -> str:
        card = self._ns_dir(ns_id) / "FACTS.md"
        if card.exists():
            return card.read_text(encoding="utf-8")
        return ""

    def write_card(self, ns_id: str, content: str) -> None:
        ns_dir = self._ns_dir(ns_id)
        ns_dir.mkdir(parents=True, exist_ok=True)
        (ns_dir / "FACTS.md").write_text(content, encoding="utf-8")

    def append_card_line(self, ns_id: str, line: str) -> None:
        """LLM 卡片合并失败时的兜底：原文追加到卡底，保证信息不丢。"""
        existing = self.read_card(ns_id).rstrip()
        self.write_card(ns_id, existing + "\n\n- " + line.strip() + "\n")

    def append_timeline(self, ns_id: str, entry: Dict[str, Any]) -> None:
        """timeline 追加一条事件（事实之源，永不覆写）。"""
        record = {
            "ts": _now_iso(),
            **entry,
        }
        ns_dir = self._ns_dir(ns_id)
        ns_dir.mkdir(parents=True, exist_ok=True)
        with open(ns_dir / "timeline.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_timeline(self, ns_id: str, limit: int = TIMELINE_TAIL_DEFAULT) -> List[Dict[str, Any]]:
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
        return records[-limit:]

    # ── 注入渲染 ──────────────────────────────────────────────

    def render_chat_context(
        self,
        session_ns: Optional[str] = None,
        workspace: Optional[str] = None,
        turn_ns: Optional[str] = None,
        global_max_chars: int = GLOBAL_MAX_CHARS,
        card_max_chars: int = CARD_MAX_CHARS,
    ) -> str:
        """拼装命名空间模式的记忆上下文。

        结构（与 legacy 路径互斥，永不注入 MEMORY.md）：
        1. ``## 全局记忆`` —— GLOBAL.md（关系层 + 跨项目习惯），常驻；
        2. ``## 当前项目档案`` —— 会话绑定项目的状态卡（工作区锚定兜底）；
        3. ``## 本轮提及的项目档案`` —— 用户本轮消息命中了**另一个**项目时，
           临时带出该项目的卡（仅本轮参考，不改会话主绑定）。这是"中途
           聊到别的项目自动带记忆"的机制：不抢切、不劫持，随提随取。
        4. ``## 其他项目索引`` —— 每个项目一行（名字 + summary），
           模型看到索引即可主动 recall_memory / 问用户，细节不进上下文。
        """
        ns_id = session_ns if session_ns and self.get_namespace(session_ns) else None
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
                    f"## 当前项目档案 · {entry.get('name', ns_id)}\n" + _tail(card, card_max_chars)
                )

        # 本轮临时提及的项目（≠ 会话主绑定）：带卡进上下文，标注临时性
        if turn_ns and turn_ns != ns_id and self.get_namespace(turn_ns):
            turn_entry = self.get_namespace(turn_ns) or {}
            turn_card = self.read_card(turn_ns).strip()
            if turn_card and _has_substantive_content(turn_card):
                parts.append(
                    f"## 本轮提及的项目档案 · {turn_entry.get('name', turn_ns)}"
                    "（用户这轮消息提到，仅本轮参考；会话主项目未变）\n"
                    + _tail(turn_card, card_max_chars)
                )

        index_lines = []
        for entry in self.list_namespaces():
            if entry.get("status") == "archived" or entry.get("id") == ns_id:
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
        return {
            "active": self.is_active(),
            "count": len(namespaces),
            "global_chars": len(self.read_global()),
            "cards": {
                e["id"]: {"chars": len(self.read_card(e["id"])), "name": e["name"]}
                for e in namespaces
            },
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
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.registry_file)


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
AUTOBIND_MAX_MESSAGES = 4


def autobind_project(session: Any, manager: "NamespaceManager", text: str) -> Optional[str]:
    """会话开篇自动绑定项目（v5 · 读取端自动识别）。

    触发条件（全部满足才绑）：
    1. 命名空间模式已启用；
    2. 会话未绑定（project_namespace 为空）；
    3. 会话尚在开篇（message_count <= AUTOBIND_MAX_MESSAGES）；
    4. 用户消息命中某项目的 name/alias（确定性子串匹配，不调 LLM）。

    命中即持久化绑定（set_project_namespace），后续轮次不再重判；
    未命中/条件不符返回 None（全局视角 + 项目索引兜底）。
    误绑的修正出口：输入栏项目选择器手动改。

    session 需要 duck-type 提供：project_namespace / message_count /
    set_project_namespace()。绑定失败静默返回 None，绝不阻塞主链路。
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
    "CARD_MAX_CHARS",
    "GLOBAL_MAX_CHARS",
]
