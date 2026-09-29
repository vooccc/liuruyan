"""Pinned Scope Item 数据模型 + 解析。

灵感参考 Windsurf/Codeium 协议（见 _research/codeium 反编译产物）的 ContextScopeItem 设计：
用户钉住的"上下文片段"不是裸文本，而是一组**有类型的引用**，每次拼 prompt 时按类型重新解析。
好处是文件改了下次自动跟新；token 预算超限时按类型截断而不是整体丢。

起步只做 5 种类型：file / directory / code_context / rule / code_map。
其它（slack/jira/browser_page/...）等真用得到再扩。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ====== 常量 ======

SCOPE_ITEM_TYPES: Tuple[str, ...] = (
    "file",
    "directory",
    "code_context",
    "rule",
    "code_map",
)

# 单个 scope item 解析后注入 prompt 的最大字符数。
# 文件 8k 字符 ≈ 2k tokens 上限；超过截断并打 [truncated] 标记。
DEFAULT_MAX_CHARS_PER_ITEM = 8000

# 整个 pinned scope（所有 items 合并）的最大字符数；超出按 items 顺序逐个 drop。
DEFAULT_MAX_TOTAL_CHARS = 32000

# 目录列表预览最多展示多少个 entry（pin 后注入 prompt 的一级清单上限）。
# 注：单 item 还有 DEFAULT_MAX_CHARS_PER_ITEM=8000 字符兜底，200 行 × ~30 字节 ≈ 6k chars 仍在内。
DIRECTORY_PREVIEW_MAX_ENTRIES = 200


# ====== 数据模型 ======

@dataclass
class ScopeItem:
    """单条 pinned scope。

    Fields:
        type: 见 `SCOPE_ITEM_TYPES`。
        path: 主标识。file/directory/rule/code_map 是相对/绝对路径；code_context 是 symbol 名。
        label: 前端显示用的短名；缺省取 path 的 basename。
        line_range: file 类型可选，限定渲染行范围 (start, end)，1-indexed inclusive。
        entries_count: directory 类型可选，一级 entries 数（不递归）。前端用于显示
            "_research · 41 项" + 大目录警告。计算开销 = 一次 iterdir，零感知。
            参考 Codeium PathScopeItem.num_files 字段（他们靠后台 indexer 递归算，
            柳如烟无 indexer 故只统计一级）。
    """

    type: str
    path: str
    label: Optional[str] = None
    line_range: Optional[Tuple[int, int]] = None
    entries_count: Optional[int] = None

    def __post_init__(self) -> None:
        if self.type not in SCOPE_ITEM_TYPES:
            raise ValueError(f"unknown scope item type: {self.type!r}")
        if not isinstance(self.path, str):
            raise ValueError("scope item path must be string")
        # code_map 允许空 path（resolver 会兜底找 _docs/CODE_MAP.md）；其它类型必须非空
        if self.type != "code_map" and not self.path.strip():
            raise ValueError("scope item path must be non-empty string")

    def display_label(self) -> str:
        if self.label:
            return self.label
        # 尽量还原 basename；symbol 类型直接用 path
        if self.type == "code_context":
            return self.path
        try:
            return Path(self.path).name or self.path
        except Exception:
            return self.path

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"type": self.type, "path": self.path}
        if self.label:
            out["label"] = self.label
        if self.line_range:
            out["line_range"] = [int(self.line_range[0]), int(self.line_range[1])]
        if self.entries_count is not None:
            out["entries_count"] = int(self.entries_count)
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScopeItem":
        if not isinstance(data, dict):
            raise TypeError("scope item dict expected")
        line_range_raw = data.get("line_range")
        line_range: Optional[Tuple[int, int]] = None
        if isinstance(line_range_raw, (list, tuple)) and len(line_range_raw) == 2:
            try:
                start = int(line_range_raw[0])
                end = int(line_range_raw[1])
                if start > 0 and end >= start:
                    line_range = (start, end)
            except (TypeError, ValueError):
                line_range = None
        entries_count_raw = data.get("entries_count")
        entries_count: Optional[int] = None
        if entries_count_raw is not None:
            try:
                ec = int(entries_count_raw)
                if ec >= 0:
                    entries_count = ec
            except (TypeError, ValueError):
                entries_count = None
        return cls(
            type=str(data.get("type", "")),
            path=str(data.get("path", "")),
            label=(str(data["label"]) if data.get("label") else None),
            line_range=line_range,
            entries_count=entries_count,
        )


# ====== 解析为 prompt 文本 ======

def _read_text_safely(path: Path) -> Optional[str]:
    if not path.exists() or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    return f"{cut}\n\n[... truncated, original {len(text)} chars]"


def _resolve_path_under_root(rel_or_abs: str, project_root: Path) -> Path:
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    return (project_root / rel_or_abs).resolve()


def _slice_lines(text: str, line_range: Tuple[int, int]) -> str:
    lines = text.splitlines()
    start, end = line_range
    start_idx = max(start - 1, 0)
    end_idx = min(end, len(lines))
    if start_idx >= end_idx:
        return ""
    return "\n".join(lines[start_idx:end_idx])


def _resolve_file(item: ScopeItem, project_root: Path, max_chars: int) -> str:
    abs_path = _resolve_path_under_root(item.path, project_root)
    body = _read_text_safely(abs_path)
    if body is None:
        return f"[scope:file] {item.path} — (file not found or unreadable)"
    if item.line_range:
        body = _slice_lines(body, item.line_range)
        header = f"[scope:file] {item.path}:{item.line_range[0]}-{item.line_range[1]}"
    else:
        header = f"[scope:file] {item.path}"
    return f"{header}\n```\n{_truncate(body, max_chars)}\n```"


def _resolve_directory(item: ScopeItem, project_root: Path, max_chars: int) -> str:
    abs_path = _resolve_path_under_root(item.path, project_root)
    if not abs_path.exists() or not abs_path.is_dir():
        return f"[scope:directory] {item.path} — (directory not found)"
    try:
        entries = sorted(abs_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return f"[scope:directory] {item.path} — (read failed: {exc})"
    lines: List[str] = []
    for child in entries[:DIRECTORY_PREVIEW_MAX_ENTRIES]:
        suffix = "/" if child.is_dir() else ""
        lines.append(f"- {child.name}{suffix}")
    overflow = len(entries) - DIRECTORY_PREVIEW_MAX_ENTRIES
    if overflow > 0:
        lines.append(f"- ... ({overflow} more entries)")
    body = "\n".join(lines) if lines else "(empty)"
    return f"[scope:directory] {item.path}\n{_truncate(body, max_chars)}"


def _resolve_code_context(item: ScopeItem, project_root: Path, max_chars: int) -> str:
    """code_context 起步用占位实现：把 symbol 名告诉模型，让它自己用 jedi_lsp 工具查。

    后续可以集成 backend.tools.jedi_lsp.find_symbol_definition 直接展开。
    """
    return (
        f"[scope:code_context] {item.path} "
        f"— (use `jedi_lsp` / `glob_search` to inspect the actual definition)"
    )


def _rule_search_paths(project_root: Path) -> List[Path]:
    candidates = [
        project_root / ".lry" / "rules",
        project_root / "rules",
    ]
    try:
        from . import paths as paths_mod  # type: ignore

        candidates.append(paths_mod.data_dir() / "rules")
    except Exception:
        pass
    return candidates


def _resolve_rule(item: ScopeItem, project_root: Path, max_chars: int) -> str:
    rule_id = item.path.strip()
    # 直接路径优先
    direct = _resolve_path_under_root(rule_id, project_root)
    if direct.exists() and direct.is_file():
        body = _read_text_safely(direct) or ""
        return f"[scope:rule] {rule_id}\n```\n{_truncate(body, max_chars)}\n```"
    # 在已知规则目录下找同名（含/不含 .md）
    for base in _rule_search_paths(project_root):
        for candidate in (base / rule_id, base / f"{rule_id}.md"):
            if candidate.exists() and candidate.is_file():
                body = _read_text_safely(candidate) or ""
                rel = candidate.relative_to(project_root) if str(candidate).startswith(str(project_root)) else candidate
                return f"[scope:rule] {rel}\n```\n{_truncate(body, max_chars)}\n```"
    return f"[scope:rule] {rule_id} — (rule file not found)"


def _resolve_code_map(item: ScopeItem, project_root: Path, max_chars: int) -> str:
    # 默认在 _docs/CODE_MAP.md，路径可被覆盖
    candidate_paths: List[Path] = []
    p = item.path.strip()
    if p:
        candidate_paths.append(_resolve_path_under_root(p, project_root))
    candidate_paths.extend([
        project_root / "_docs" / "CODE_MAP.md",
        project_root / "CODE_MAP.md",
    ])
    seen = set()
    for cand in candidate_paths:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        body = _read_text_safely(cand)
        if body is not None:
            try:
                label = cand.relative_to(project_root)
            except ValueError:
                label = cand
            return f"[scope:code_map] {label}\n```\n{_truncate(body, max_chars)}\n```"
    return f"[scope:code_map] {item.path or '_docs/CODE_MAP.md'} — (not found)"


_RESOLVERS = {
    "file": _resolve_file,
    "directory": _resolve_directory,
    "code_context": _resolve_code_context,
    "rule": _resolve_rule,
    "code_map": _resolve_code_map,
}


def resolve_to_text(
    item: ScopeItem,
    *,
    project_root: Optional[Path] = None,
    max_chars: int = DEFAULT_MAX_CHARS_PER_ITEM,
) -> str:
    """把单个 scope item 解析为 prompt 注入用的文本块。失败永远返回占位，不抛异常。"""
    if project_root is None:
        try:
            from . import paths as paths_mod  # type: ignore

            project_root = paths_mod.legacy_project_root()
        except Exception:
            project_root = Path.cwd()
    resolver = _RESOLVERS.get(item.type)
    if resolver is None:
        return f"[scope:{item.type}] {item.path} — (unsupported type)"
    try:
        return resolver(item, project_root, max_chars)
    except Exception as exc:  # pragma: no cover — 防御性兜底
        return f"[scope:{item.type}] {item.path} — (resolve failed: {exc})"


def render_pinned_scope(
    items: List[ScopeItem],
    *,
    project_root: Optional[Path] = None,
    max_chars_per_item: int = DEFAULT_MAX_CHARS_PER_ITEM,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
) -> str:
    """把整个 pinned scope 列表渲染为一段注入用文本。

    超出 max_total_chars 时按列表顺序截断（前面的优先保留），
    并在末尾追加 [Pinned scope: N items dropped due to budget]。
    空列表返回空串。
    """
    if not items:
        return ""

    rendered: List[str] = []
    used = 0
    dropped = 0
    for item in items:
        block = resolve_to_text(item, project_root=project_root, max_chars=max_chars_per_item)
        block_size = len(block) + 2  # 留双换行开销
        if used + block_size > max_total_chars and rendered:
            dropped += 1
            continue
        rendered.append(block)
        used += block_size

    if dropped:
        rendered.append(f"[Pinned scope: {dropped} item(s) dropped due to budget]")

    return "\n\n".join(rendered)


# ====== 序列化辅助 ======

def parse_scope_items(raw: Any) -> List[ScopeItem]:
    """从持久化或 API 入参的 list[dict] 安全解析为 ScopeItem 列表，跳过非法条目。"""
    if not isinstance(raw, list):
        return []
    out: List[ScopeItem] = []
    for entry in raw:
        try:
            out.append(ScopeItem.from_dict(entry))
        except Exception:
            continue
    return out


def dump_scope_items(items: List[ScopeItem]) -> List[Dict[str, Any]]:
    return [item.to_dict() for item in items]
