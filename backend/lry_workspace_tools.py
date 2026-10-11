"""`.lry/` 工作区写入与检索工具

5 个工具（设计文档 docs/lry_workspace_design.md §7.4）：

- `add_project_rule(topic, content, severity, ...)` → `.lry/rules/<topic>.md`
- `update_project_notes(content, append?)` → `.lry/PROJECT_NOTES.md`
- `save_project_memory(topic, content, kind, ...)` → `.lry/memory/<kind>_<topic>_<date>.md`
- `add_project_workflow(name, description, steps, ...)` → `.lry/workflows/<name>.md`
- `recall_project_memory(query, top_k)` → 检索 `.lry/memory/*.md`

约定：

- 写入位置都基于 `lry_workspace.resolve_workspace_root()`
- 文件名 slug 强校验：禁止 `/` / `\` / `..` / 开头点（防越界）
- 失败统一返 JSON {"error": "..."}
- 成功返 JSON {"success": True, "path": ..., "action": "created"|"updated"}

并发安全：纯文件 I/O；同名覆写靠 overwrite 标志；多 task 并发改同文件不加锁
（设计假定单机单用户，柳如烟自己用，并发概率极低）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .lry_workspace import (
    ARCHIVE_SUBDIR,
    LRY_DIR_NAME,
    MEMORY_SUBDIR,
    NOTES_FILE,
    NOTES_MAX_CHARS,
    PRIVATE_SUBDIR,
    RULES_SUBDIR,
    WORKFLOWS_SUBDIR,
    ensure_lry_gitignore,
    lry_archive_dir,
    lry_private_memory_dir,
    regenerate_index_file,
    resolve_workspace_root,
)


def _maybe_regenerate_index(root: Optional[Path]) -> None:
    """写入 tool 成功后的钩子。失败静默——INDEX.md 不是核心资产。"""
    if root is None:
        return
    try:
        regenerate_index_file(root)
    except Exception:
        pass


# === 常量 ===

_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")
_SEVERITY_VALID = {"critical", "warning", "info"}
_MEMORY_KIND_VALID = {"handover", "insight", "adr", "note"}

# 单条 rule / workflow 软上限（整篇内容字符数）
_RULE_MAX_CHARS = 5000
_WORKFLOW_MAX_CHARS = 8000
_MEMORY_MAX_CHARS = 12000


# === 共用工具 ===

def _err(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _ok(**fields) -> str:
    return json.dumps({"success": True, **fields}, ensure_ascii=False)


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _validate_slug(value: str, *, label: str) -> Optional[str]:
    """校验文件名 slug。返回错误描述，或 None 表示 OK。"""
    if not isinstance(value, str) or not value.strip():
        return f"{label} must be a non-empty string"
    v = value.strip()
    # 砍掉用户可能传的 .md 后缀
    if v.endswith(".md"):
        v = v[:-3]
    if not _SLUG_PATTERN.match(v):
        return (
            f"{label} must be lowercase kebab-case (a-z, 0-9, '-', '_'), "
            f"length 1-64; got: {value!r}"
        )
    return None


def _normalize_slug(value: str) -> str:
    """slug 已校验过；做最后归一化（去 .md 后缀）。"""
    v = value.strip()
    if v.endswith(".md"):
        v = v[:-3]
    return v


def _resolve_subpath(subdir: str, *parts: str) -> Optional[Path]:
    """解析 `<wd>/.lry/<subdir>/<parts...>` 绝对路径。"""
    root = resolve_workspace_root()
    if root is None:
        return None
    return root / LRY_DIR_NAME / subdir / Path(*parts)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _audit_lry_write(
    *,
    tool_name: str,
    target: Path,
    agent_id: Optional[str],
    session_id: Optional[str],
    action: str,
) -> None:
    """轻量审计：写一行到 data_dir/lry_writes.jsonl。

    跟 record_backend_write 同思路（jsonl 一行一事件）但独立文件——
    `.lry/` 不在 _PROTECTED_SOURCE_DIRS 内，复用现有 backend_writes.jsonl
    会污染语义。失败静默吞——审计不能阻断主流程。
    """
    try:
        from . import paths

        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "agent_id": agent_id,
            "session_id": session_id,
            "tool_name": tool_name,
            "path": str(target),
            "action": action,
        }
        jsonl_path = paths.data_dir() / "lry_writes.jsonl"
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# === add_project_rule ===

def add_project_rule(
    topic: str,
    content: str,
    severity: str = "warning",
    overwrite: bool = False,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """新建或覆盖 `.lry/rules/<topic>.md`。

    自动加 YAML frontmatter：severity / added / by / topic。
    """
    err = _validate_slug(topic, label="topic")
    if err:
        return _err(err)
    if not isinstance(content, str) or not content.strip():
        return _err("content must be a non-empty string")
    if len(content) > _RULE_MAX_CHARS:
        return _err(
            f"content too long ({len(content)} chars > {_RULE_MAX_CHARS}). "
            f"Rules should be short and sharp; consider splitting."
        )
    severity = (severity or "warning").lower().strip()
    if severity not in _SEVERITY_VALID:
        return _err(
            f"severity must be one of {sorted(_SEVERITY_VALID)}; got: {severity!r}"
        )

    slug = _normalize_slug(topic)
    target = _resolve_subpath(RULES_SUBDIR, f"{slug}.md")
    if target is None:
        return _err("workspace root unresolved (no working_directory and no fallback)")

    action = "updated" if target.exists() else "created"
    if action == "updated" and not overwrite:
        return _err(
            f"rule '{slug}' already exists at {target}. "
            f"Pass overwrite=true to replace."
        )

    frontmatter = (
        "---\n"
        f"severity: {severity}\n"
        f"added: {_today_iso()}\n"
        f"by: {agent_id or 'unknown'}\n"
        f"topic: {slug}\n"
        "---\n\n"
    )
    body = content.strip() + "\n"

    _ensure_parent(target)
    target.write_text(frontmatter + body, encoding="utf-8")
    _audit_lry_write(
        tool_name="add_project_rule",
        target=target,
        agent_id=agent_id,
        session_id=session_id,
        action=action,
    )
    _maybe_regenerate_index(resolve_workspace_root())

    return _ok(
        action=action,
        path=str(target),
        slug=slug,
        severity=severity,
        bytes_written=target.stat().st_size,
    )


# === update_project_notes ===

def update_project_notes(
    content: str,
    append: bool = False,
    auto_archive: bool = True,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """覆写或追加 `.lry/PROJECT_NOTES.md`。

    auto_archive=True（默认）：
    - 写入后总长度 > NOTES_MAX_CHARS → 把**旧 NOTES** 整篇归档到
      `.lry/memory/note_notes-archive_<date>.md`，新 NOTES 只保留本次 content
    - 这是"NOTES 满了归档"的核心机制，保证每轮注入的 NOTES 都在 cap 以内

    auto_archive=False：
    - 超 cap → 报错（保持向后兼容；但默认行为已切到自动归档）
    """
    if not isinstance(content, str) or not content.strip():
        return _err("content must be a non-empty string")

    root = resolve_workspace_root()
    if root is None:
        return _err("workspace root unresolved (no working_directory and no fallback)")
    target = root / LRY_DIR_NAME / NOTES_FILE

    body = content.strip() + "\n"
    final_text: str
    archived_to: Optional[str] = None

    existing_text = ""
    if target.exists():
        try:
            existing_text = target.read_text(encoding="utf-8-sig")
        except OSError:
            existing_text = ""

    if append and existing_text:
        final_text = existing_text.rstrip() + "\n\n" + body
        action = "appended"
    else:
        final_text = body
        action = "updated" if existing_text else "created"

    # 容量检查 + 归档
    if len(final_text) > NOTES_MAX_CHARS:
        if not auto_archive:
            return _err(
                f"final notes content {len(final_text)} chars > {NOTES_MAX_CHARS} cap. "
                f"Archive older sections to .lry/memory/ first, "
                f"or call with auto_archive=true."
            )
        # 自动归档：把旧 NOTES 整篇存到 memory，新 NOTES 只留本次 content
        if existing_text.strip():
            mem_dir = root / LRY_DIR_NAME / MEMORY_SUBDIR
            mem_dir.mkdir(parents=True, exist_ok=True)
            today = _today_iso()
            archive_base = f"note_notes-archive_{today}"
            archive_path = mem_dir / f"{archive_base}.md"
            serial = 2
            while archive_path.exists():
                archive_path = mem_dir / f"{archive_base}_{serial:03d}.md"
                serial += 1
            archive_frontmatter = (
                "---\n"
                "kind: note\n"
                "topic: notes-archive\n"
                f"added: {today}\n"
                f"by: {agent_id or 'unknown'}\n"
                "auto_archived_from: PROJECT_NOTES.md\n"
                "---\n\n"
                "# 自动归档：NOTES 旧内容\n\n"
                f"超出 {NOTES_MAX_CHARS} 字符上限时自动归档。\n\n"
                "---\n\n"
            )
            archive_path.write_text(
                archive_frontmatter + existing_text.strip() + "\n",
                encoding="utf-8",
            )
            archived_to = str(archive_path)
            _audit_lry_write(
                tool_name="update_project_notes_archive",
                target=archive_path,
                agent_id=agent_id,
                session_id=session_id,
                action="created",
            )
        # 新 NOTES 只保留本次 content
        final_text = body
        action = "auto_archived"

    _ensure_parent(target)
    target.write_text(final_text, encoding="utf-8")
    _audit_lry_write(
        tool_name="update_project_notes",
        target=target,
        agent_id=agent_id,
        session_id=session_id,
        action=action,
    )
    _maybe_regenerate_index(resolve_workspace_root())

    response_fields = {
        "action": action,
        "path": str(target),
        "bytes_written": target.stat().st_size,
    }
    if archived_to:
        response_fields["archived_to"] = archived_to
    return _ok(**response_fields)


# === save_project_memory ===

def save_project_memory(
    topic: str,
    content: str,
    kind: str = "handover",
    private: bool = False,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """新建 `.lry/memory/<kind>_<topic>_<YYYY-MM-DD>.md`。

    同一天同 topic 同 kind → 自动追加序号 _002 / _003 避免覆盖。

    `private=True` 时改写 `.lry/private/memory/<同名>.md`，并首次写入时
    通过 `ensure_lry_gitignore` 保证 `.lry/.gitignore` 含 `private/`，防止
    敏感条目被误 commit。frontmatter 多出 `private: true` 字段让 LLM 识别。
    """
    err = _validate_slug(topic, label="topic")
    if err:
        return _err(err)
    if not isinstance(content, str) or not content.strip():
        return _err("content must be a non-empty string")
    if len(content) > _MEMORY_MAX_CHARS:
        return _err(
            f"content too long ({len(content)} chars > {_MEMORY_MAX_CHARS}). "
            f"Split into multiple memory entries."
        )
    kind = (kind or "handover").lower().strip()
    if kind not in _MEMORY_KIND_VALID:
        return _err(
            f"kind must be one of {sorted(_MEMORY_KIND_VALID)}; got: {kind!r}"
        )

    slug = _normalize_slug(topic)
    today = _today_iso()
    base = f"{kind}_{slug}_{today}"

    root = resolve_workspace_root()
    if root is None:
        return _err("workspace root unresolved")

    # private=True 前先保证 .gitignore，public 路径不需要（memory/ 默认入 git）
    if private:
        ensure_lry_gitignore(root)
        mem_dir = lry_private_memory_dir(root)
    else:
        mem_dir = root / LRY_DIR_NAME / MEMORY_SUBDIR
    mem_dir.mkdir(parents=True, exist_ok=True)

    # 同名 → 追加序号防覆盖
    target = mem_dir / f"{base}.md"
    serial = 2
    while target.exists():
        target = mem_dir / f"{base}_{serial:03d}.md"
        serial += 1

    frontmatter_lines = [
        "---",
        f"kind: {kind}",
        f"topic: {slug}",
        f"added: {today}",
        f"by: {agent_id or 'unknown'}",
    ]
    if private:
        frontmatter_lines.append("private: true")
    frontmatter_lines.extend(["---", ""])
    frontmatter = "\n".join(frontmatter_lines) + "\n"
    body = content.strip() + "\n"
    target.write_text(frontmatter + body, encoding="utf-8")

    _audit_lry_write(
        tool_name="save_project_memory",
        target=target,
        agent_id=agent_id,
        session_id=session_id,
        action="created",
    )
    _maybe_regenerate_index(root)

    return _ok(
        action="created",
        path=str(target),
        slug=slug,
        kind=kind,
        private=bool(private),
        bytes_written=target.stat().st_size,
    )


# === add_project_workflow ===

def add_project_workflow(
    name: str,
    description: str,
    steps: str,
    overwrite: bool = False,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """新建或覆盖 `.lry/workflows/<name>.md`。

    沿用柳如烟自身 `workflows/` 的 frontmatter + 标题 + 步骤格式。
    """
    err = _validate_slug(name, label="name")
    if err:
        return _err(err)
    if not isinstance(description, str) or not description.strip():
        return _err("description must be a non-empty string")
    if not isinstance(steps, str) or not steps.strip():
        return _err("steps must be a non-empty string")
    total_chars = len(description) + len(steps)
    if total_chars > _WORKFLOW_MAX_CHARS:
        return _err(
            f"workflow content too long ({total_chars} chars > {_WORKFLOW_MAX_CHARS}). "
            f"Split into multiple workflows or move detail to memory/."
        )

    slug = _normalize_slug(name)
    target = _resolve_subpath(WORKFLOWS_SUBDIR, f"{slug}.md")
    if target is None:
        return _err("workspace root unresolved")

    action = "updated" if target.exists() else "created"
    if action == "updated" and not overwrite:
        return _err(
            f"workflow '{slug}' already exists. Pass overwrite=true to replace."
        )

    frontmatter = (
        "---\n"
        f"description: {description.strip()}\n"
        f"added: {_today_iso()}\n"
        f"by: {agent_id or 'unknown'}\n"
        "---\n\n"
    )
    body = f"# {slug}\n\n{steps.strip()}\n"

    _ensure_parent(target)
    target.write_text(frontmatter + body, encoding="utf-8")
    _audit_lry_write(
        tool_name="add_project_workflow",
        target=target,
        agent_id=agent_id,
        session_id=session_id,
        action=action,
    )
    _maybe_regenerate_index(resolve_workspace_root())

    return _ok(
        action=action,
        path=str(target),
        slug=slug,
        bytes_written=target.stat().st_size,
    )


# === get_project_workflow ===

def get_project_workflow(name: str) -> str:
    """读取 `.lry/workflows/<name>.md` 完整内容。

    workflow 索引（name + description）已经在 system prompt 静态段；
    本工具用于按需取完整步骤。
    """
    err = _validate_slug(name, label="name")
    if err:
        return _err(err)

    slug = _normalize_slug(name)
    target = _resolve_subpath(WORKFLOWS_SUBDIR, f"{slug}.md")
    if target is None:
        return _err("workspace root unresolved")
    if not target.is_file():
        return _err(f"workflow '{slug}' not found at {target}")

    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return _err(f"failed to read workflow: {exc}")

    return _ok(
        name=slug,
        path=str(target),
        content=text,
    )


# === list_project_rules ===

def list_project_rules() -> str:
    """列出 `.lry/rules/*.md` 的元数据（不含完整内容）。

    用于 agent 自查"我现在还有哪些 rule"——比如要 overwrite / 删除前先 list。
    完整内容已经在 system prompt 静态段，无需再用工具拉。
    """
    root = resolve_workspace_root()
    if root is None:
        return _err("workspace root unresolved")
    rules_dir = root / LRY_DIR_NAME / RULES_SUBDIR
    if not rules_dir.is_dir():
        return _ok(items=[], total=0, message=".lry/rules/ does not exist yet")

    items: list[dict] = []
    for path in sorted(rules_dir.glob("*.md"), key=lambda p: p.name):
        if not path.is_file():
            continue
        # 简单解析 frontmatter severity / topic
        text_head = ""
        try:
            with path.open("r", encoding="utf-8-sig") as fh:
                # 只读前 2 KB 抽 frontmatter 够用
                text_head = fh.read(2048)
        except OSError:
            continue
        severity = "unknown"
        for line in text_head.splitlines()[:30]:
            stripped = line.strip()
            if stripped.lower().startswith("severity:"):
                severity = stripped.split(":", 1)[1].strip()
                break
        items.append(
            {
                "slug": path.stem,
                "file": path.name,
                "path": str(path),
                "severity": severity,
                "size_bytes": path.stat().st_size,
            }
        )
    return _ok(items=items, total=len(items))


# === archive_lry_file ===

_ARCHIVE_KINDS = {"rule", "workflow", "memory"}


def _resolve_archive_source(
    root: Path, kind: str, name: str
) -> tuple[Optional[Path], bool]:
    """解析 archive 的源文件绝对路径 + 是否属于 private 领域。

    rule / workflow：名字若没后缀自动补 .md，源目录是 `.lry/rules|workflows/`
    memory：name 必须含 `.md`（文件名结构 `kind_topic_YYYY-MM-DD.md`）；先查公开目
      录，找不到再查 `.lry/private/memory/`。返回时通过第二个返回值标明是否 private。

    任何越界 / 不存在 → (None, False)。
    """
    if kind == "rule":
        slug = _normalize_slug(name)
        source = root / LRY_DIR_NAME / RULES_SUBDIR / f"{slug}.md"
        return (source, False) if source.is_file() else (None, False)
    if kind == "workflow":
        slug = _normalize_slug(name)
        source = root / LRY_DIR_NAME / WORKFLOWS_SUBDIR / f"{slug}.md"
        return (source, False) if source.is_file() else (None, False)
    if kind == "memory":
        filename = name if name.endswith(".md") else f"{name}.md"
        public_path = root / LRY_DIR_NAME / MEMORY_SUBDIR / filename
        if public_path.is_file():
            return public_path, False
        private_path = lry_private_memory_dir(root) / filename
        if private_path.is_file():
            return private_path, True
        return None, False
    return None, False


def archive_lry_file(
    kind: str,
    name: str,
    dry_run: bool = False,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """把某条 rule / workflow / memory 归档到 `.lry/archive/<kind>s/`。

    - `kind`：{"rule", "workflow", "memory"} 之一
    - `name`：rule 的 topic slug / workflow 的 name slug / memory 的 filename
      （可含 `.md`；内部会补齐）
    - `dry_run=True`：只返回预览（"要动的源文件路径 + 目标路径"），不实际移动
    - 归档成功后会触发 `regenerate_index_file` 刷新 INDEX.md（因为 counts 变了）

    私密 memory 归档到 `.lry/archive/private/memory/`，保留原隔离层。
    `.lry/archive/` 被 `.gitignore` 覆盖，归档默认不入 git（噪音大，git log 已有历史）。
    """
    kind = (kind or "").strip().lower()
    if kind not in _ARCHIVE_KINDS:
        return _err(
            f"kind must be one of {sorted(_ARCHIVE_KINDS)}; got: {kind!r}"
        )
    if not isinstance(name, str) or not name.strip():
        return _err("name must be a non-empty string")

    # rule / workflow 的 name 做 slug 校验；memory filename 用较宽松的文件名校验
    if kind in ("rule", "workflow"):
        err = _validate_slug(name, label="name")
        if err:
            return _err(err)
    else:
        # memory 文件名形如 "kind_topic_YYYY-MM-DD.md"，含 `_` / `-` / 可能含 `.md`
        # 只拦 `/` `\` `..` 等明显越界字符
        if "/" in name or "\\" in name or ".." in name:
            return _err(f"invalid memory filename: {name!r}")

    root = resolve_workspace_root()
    if root is None:
        return _err("workspace root unresolved")

    source, is_private_memory = _resolve_archive_source(root, kind, name)
    if source is None:
        return _err(
            f"{kind} not found: {name!r}. Nothing to archive."
        )

    dest_dir = lry_archive_dir(root, kind, private=is_private_memory)
    # 目标文件名：源同名，若已存在（旧 archive），在 stem 末尾追加 .archived_<timestamp>
    dest_name = source.name
    dest = dest_dir / dest_name
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = dest_dir / f"{source.stem}.archived_{stamp}{source.suffix}"

    response_fields = {
        "kind": kind,
        "name": name,
        "source": str(source),
        "dest": str(dest),
        "private": is_private_memory,
    }

    if dry_run:
        response_fields["action"] = "preview"
        return _ok(**response_fields)

    # 执行归档：mkdir + rename。rename 失败可能因为跨卷——兜底改成 read/write/unlink
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            source.replace(dest)  # 原子 rename 优先
        except OSError:
            # 跨卷 fallback
            content = source.read_bytes()
            dest.write_bytes(content)
            source.unlink()
    except OSError as exc:
        return _err(f"archive failed: {exc}")

    _audit_lry_write(
        tool_name="archive_lry_file",
        target=dest,
        agent_id=agent_id,
        session_id=session_id,
        action="archived",
    )
    _maybe_regenerate_index(root)

    response_fields["action"] = "archived"
    return _ok(**response_fields)


# === restore_lry_file ===

# 从 "collide.archived_20260514T004125Z.md" 还原回 "collide.md" 的后缀识别正则。
# 跟 `archive_lry_file` 里用的 strftime("%Y%m%dT%H%M%SZ") 格式对齐。
_ARCHIVED_SUFFIX_RE = re.compile(r"\.archived_\d{8}T\d{6}Z(\.[^.]+)?$")


def _strip_archived_suffix(filename: str) -> str:
    """把 archive 里的文件名还原成原始名字。

    - `collide.archived_20260514T004125Z.md` → `collide.md`
    - `legacy-rule.md`（无 archive 后缀） → `legacy-rule.md`（原样）

    `_ARCHIVED_SUFFIX_RE` 捕获 stem 末尾的 `.archived_<ts>` 段并连带原后缀还原。
    """
    # 尝试匹配 stem 末尾的 archive 后缀：`stem.archived_<ts><ext>`
    # 把 `.archived_<ts>` 整段拿掉，扩展名（.md 等）保留
    match = _ARCHIVED_SUFFIX_RE.search(filename)
    if match:
        ext = match.group(1) or ""
        prefix = filename[: match.start()]
        return f"{prefix}{ext}"
    return filename


def _resolve_restore_source(
    root: Path, kind: str, name: str
) -> tuple[Optional[Path], bool]:
    """解析 restore 的源文件（在 archive/ 下）。

    返回 (archive_path, is_private_memory_archive)。找不到返 (None, False)。
    """
    if kind in ("rule", "workflow"):
        subdir = RULES_SUBDIR if kind == "rule" else WORKFLOWS_SUBDIR
        # name 可传 slug 也可传 filename；统一补 .md
        if not name.endswith(".md"):
            # 尝试作为 slug 解析（允许 .archived_<ts> 后缀先保留）
            if not _ARCHIVED_SUFFIX_RE.search(name):
                name = f"{name}.md"
        archive_path = root / LRY_DIR_NAME / ARCHIVE_SUBDIR / subdir / name
        return (archive_path, False) if archive_path.is_file() else (None, False)
    if kind == "memory":
        filename = name if name.endswith(".md") else f"{name}.md"
        pub = (
            root
            / LRY_DIR_NAME
            / ARCHIVE_SUBDIR
            / MEMORY_SUBDIR
            / filename
        )
        if pub.is_file():
            return pub, False
        priv = (
            root
            / LRY_DIR_NAME
            / ARCHIVE_SUBDIR
            / PRIVATE_SUBDIR
            / MEMORY_SUBDIR
            / filename
        )
        if priv.is_file():
            return priv, True
        return None, False
    return None, False


def _restore_destination(
    root: Path, kind: str, archive_filename: str, *, private: bool
) -> Path:
    """计算 restore 的目标路径。

    `.archived_<ts>` 后缀会被剥掉恢复原名。
    """
    original = _strip_archived_suffix(archive_filename)
    if kind == "rule":
        return root / LRY_DIR_NAME / RULES_SUBDIR / original
    if kind == "workflow":
        return root / LRY_DIR_NAME / WORKFLOWS_SUBDIR / original
    if kind == "memory":
        if private:
            return lry_private_memory_dir(root) / original
        return root / LRY_DIR_NAME / MEMORY_SUBDIR / original
    raise ValueError(f"unsupported kind: {kind!r}")


def restore_lry_file(
    kind: str,
    name: str,
    dry_run: bool = False,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """把 `.lry/archive/...` 下的文件还原到原位置（`archive_lry_file` 的逆操作）。

    - `kind`：{"rule", "workflow", "memory"} 之一
    - `name`：archive 里的文件名（rule/workflow 也接受 slug；memory 要全名）。
      如果带 `.archived_<ts>` 后缀（同名冲突时的备份名），还原时自动剥掉后缀恢复原名。
    - `dry_run=True`：只返 `{source, dest}` preview，不实际移动
    - **目标位置已存在同名 → 返 error**（不默认覆盖；用户要先 archive 当前版或改 name）

    Private memory 归档还原回 `private/memory/`，public 还原回 `memory/`，隔离保留。
    成功后触发 INDEX.md 刷新。
    """
    kind = (kind or "").strip().lower()
    if kind not in _ARCHIVE_KINDS:
        return _err(
            f"kind must be one of {sorted(_ARCHIVE_KINDS)}; got: {kind!r}"
        )
    if not isinstance(name, str) or not name.strip():
        return _err("name must be a non-empty string")

    # memory filename 字符校验（rule/workflow slug 在后面按需兼容）
    if "/" in name or "\\" in name or ".." in name:
        return _err(f"invalid name: {name!r}")

    root = resolve_workspace_root()
    if root is None:
        return _err("workspace root unresolved")

    source, is_private = _resolve_restore_source(root, kind, name)
    if source is None:
        return _err(
            f"{kind} archive not found: {name!r}. Nothing to restore."
        )

    dest = _restore_destination(root, kind, source.name, private=is_private)
    response_fields = {
        "kind": kind,
        "name": name,
        "source": str(source),
        "dest": str(dest),
        "private": is_private,
    }

    if dest.exists():
        return _err(
            f"destination already exists: {dest.name}. "
            f"Archive or rename the current version before restoring."
        )

    if dry_run:
        response_fields["action"] = "preview"
        return _ok(**response_fields)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            source.replace(dest)
        except OSError:
            content = source.read_bytes()
            dest.write_bytes(content)
            source.unlink()
    except OSError as exc:
        return _err(f"restore failed: {exc}")

    _audit_lry_write(
        tool_name="restore_lry_file",
        target=dest,
        agent_id=agent_id,
        session_id=session_id,
        action="restored",
    )
    _maybe_regenerate_index(root)

    response_fields["action"] = "restored"
    return _ok(**response_fields)


# === recall_project_memory ===

# 中文字符范围（简体 + 常用标点外），用于判断要不要跑 bigram 切分。
# 判断式：char in CJK → 需要字符级 bigram；否则按空格切 word token。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _tokenize_query(query: str) -> tuple[list[str], list[str]]:
    """把 query 切成两路 token：word_tokens（英文/数字按空白切）+ bigrams（中文 2-gram）。

    举例：
      "登录bug"     → words=["bug"]（英文段）        bigrams=["登录"]
      "登录问题"    → words=[]                        bigrams=["登录","录问","问题"]
      "oauth callback" → words=["oauth","callback"]  bigrams=[]
      "OAuth 登录 bug" → words=["oauth","bug"]       bigrams=["登录"]

    返回 (lowercase words, bigrams)。空串时返 ([], [])。
    """
    if not query:
        return [], []

    lower = query.lower()
    words: list[str] = [w for w in re.split(r"[^\w\u4e00-\u9fff]+", lower) if w]
    # 过滤纯数字太短、单字符英文等无意义 token
    words = [w for w in words if len(w) >= 2 or (len(w) == 1 and _CJK_RE.match(w))]
    # 英文 / 数字类 word 不做 bigram；中文段内连续 CJK 字符做 2-gram
    #   这样"登录bug"的"登录"部分能形成 bigram，"bug"进 words。
    bigrams: list[str] = []
    # 抽出所有 CJK 连续段
    for run in re.findall(r"[\u4e00-\u9fff]+", lower):
        if len(run) == 1:
            # 单字 CJK 走 unigram（放 words 里），避免丢召回
            if run not in words:
                words.append(run)
            continue
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            bigrams.append(bg)

    # 去重保序
    words = list(dict.fromkeys(words))
    bigrams = list(dict.fromkeys(bigrams))
    # 中文 bigram 已经自己成 unit，不要再把包含 bigram 的长 word 加进 words 重复计分
    # （当前 words 里主要是英文段 + 单 CJK，不会重复，留个注释警戒）
    return words, bigrams


def _score_memory(
    text_lc: str,
    query_words: list[str],
    query_bigrams: list[str],
    *,
    frontmatter_lc: str = "",
    body_first_lines_lc: str = "",
) -> int:
    """改进打分：
    - word token 命中 body 3 分 / 命中 frontmatter 5 分 / 命中首行 8 分
    - CJK bigram 命中 body 2 分 / frontmatter 4 分 / 首行 6 分（bigram 密度大，单次 2 分防爆）
    - **全命中 bonus**：所有 query token（words + bigrams）都至少出现一次 → +10 分（主要用于区分全命中 vs 部分命中）

    0 = 不匹配，>0 越大越相关。没有 query → 返 1（caller 走 mtime 倒序）。
    """
    total_tokens = len(query_words) + len(query_bigrams)
    if total_tokens == 0:
        return 1  # 无 query → 统一分；上层用 mtime 排

    score = 0
    hit_count = 0

    for tok in query_words:
        body_hit = text_lc.count(tok)
        fm_hit = frontmatter_lc.count(tok) if frontmatter_lc else 0
        title_hit = body_first_lines_lc.count(tok) if body_first_lines_lc else 0
        # hit_count：该 token 在 body / frontmatter / title 任一位置至少命中一次
        # 就算 "这个文件覆盖了这个查询维度"。这样 frontmatter-only 命中也能触发 bonus。
        if body_hit or fm_hit or title_hit:
            hit_count += 1
        score += body_hit * 3 + fm_hit * 5 + title_hit * 8

    for bg in query_bigrams:
        body_hit = text_lc.count(bg)
        fm_hit = frontmatter_lc.count(bg) if frontmatter_lc else 0
        title_hit = body_first_lines_lc.count(bg) if body_first_lines_lc else 0
        if body_hit or fm_hit or title_hit:
            hit_count += 1
        score += body_hit * 2 + fm_hit * 4 + title_hit * 6

    # 全命中 bonus：hit_count == total_tokens 说明这条文件同时涵盖所有查询维度
    if score > 0 and hit_count >= total_tokens:
        score += 10

    return score


def _extract_frontmatter_and_title(text: str) -> tuple[str, str]:
    """切出 frontmatter 块 + 首 5 行正文（用于 title 匹配加权）。

    返回 (frontmatter_content, first_5_lines_after_fm)。两者皆去换行后 lowercase。
    没有 frontmatter 时 frontmatter_content = ""；没有正文首行时 title = ""。
    """
    fm = ""
    body_start = 0
    if text.startswith("---\n"):
        # 找 frontmatter 结束
        end_idx = text.find("\n---\n", 4)
        if end_idx > 0:
            fm = text[4:end_idx]
            body_start = end_idx + len("\n---\n")
    body_lines = text[body_start:].splitlines()[:5]
    title = " ".join(body_lines)
    return fm.lower(), title.lower()


def recall_project_memory(
    query: str = "",
    top_k: int = 5,
    kind: Optional[str] = None,
    include_private: bool = False,
) -> str:
    """检索 `.lry/memory/*.md`。

    - query 空 → 按 mtime 倒序返最近 top_k 条
    - query 非空 → 按子串命中打分排序
    - kind 过滤：handover / insight / adr / note，只搜对应前缀的文件
    - `include_private=True` 时**额外**扫 `.lry/private/memory/*.md`，命中的
      条目带 `private: true` 字段，LLM 需要自知不在回复里泄露敏感原文

    返回 JSON：{ items: [{file, score, mtime, snippet, private?}], total }
    """
    if not isinstance(top_k, int) or top_k <= 0:
        top_k = 5
    top_k = min(top_k, 20)  # 硬上限防爆

    if kind is not None:
        kind = kind.lower().strip()
        if kind not in _MEMORY_KIND_VALID:
            return _err(
                f"kind must be one of {sorted(_MEMORY_KIND_VALID)}; got: {kind!r}"
            )

    root = resolve_workspace_root()
    if root is None:
        return _err("workspace root unresolved")

    # 扫描目录列表：公开目录始终扫；include_private 时追加私密目录
    scan_dirs: list[tuple[Path, bool]] = []
    public_dir = root / LRY_DIR_NAME / MEMORY_SUBDIR
    if public_dir.is_dir():
        scan_dirs.append((public_dir, False))
    if include_private:
        priv_dir = lry_private_memory_dir(root)
        if priv_dir.is_dir():
            scan_dirs.append((priv_dir, True))

    if not scan_dirs:
        return _ok(items=[], total=0, message=".lry/memory/ does not exist yet")

    # Phase 9：新 tokenizer。英文/数字按空白切；中文段按 2-gram；短单字和纯标点扔掉。
    query_words, query_bigrams = _tokenize_query(query or "")

    candidates: list[dict] = []
    for scan_dir, is_private in scan_dirs:
        for path in scan_dir.glob("*.md"):
            if not path.is_file():
                continue
            # kind 过滤：文件名以 kind_ 开头
            if kind and not path.name.startswith(f"{kind}_"):
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            text_lc = text.lower()
            fm_lc, title_lc = _extract_frontmatter_and_title(text)
            score = _score_memory(
                text_lc,
                query_words,
                query_bigrams,
                frontmatter_lc=fm_lc,
                body_first_lines_lc=title_lc,
            )
            if score <= 0:
                continue
            # 抽 snippet：先试命中最强的那个 token（优先 words，回退 bigrams）
            snippet = ""
            for tok in query_words + query_bigrams:
                idx = text_lc.find(tok)
                if idx >= 0:
                    start = max(0, idx - 80)
                    end = min(len(text), idx + 200)
                    snippet = text[start:end].replace("\n", " ").strip()
                    break
            if not snippet:
                # 跳过 frontmatter 后取前 200 字
                after_fm = re.sub(r"^---\n.*?\n---\n*", "", text, count=1, flags=re.DOTALL)
                snippet = after_fm[:200].replace("\n", " ").strip()

            entry = {
                "file": path.name,
                "path": str(path),
                "score": score,
                "mtime": path.stat().st_mtime,
                "snippet": snippet,
            }
            if is_private:
                entry["private"] = True
            candidates.append(entry)

    # 排序：score 降序、mtime 降序
    candidates.sort(key=lambda x: (-x["score"], -x["mtime"]))
    items = candidates[:top_k]

    return _ok(items=items, total=len(candidates))


# ============================================================
# tool registry handlers (受 _drop_context 同款保护，避免 context kwargs 漏给业务函数)
# ============================================================

_CONTEXT_KEYS = {"session_id", "llm_client", "trace_id", "agent_id"}


def _drop_ctx(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items() if k not in _CONTEXT_KEYS}


def _handle_add_project_rule(**kwargs) -> str:
    args = _drop_ctx(kwargs)
    return add_project_rule(
        topic=args.get("topic", ""),
        content=args.get("content", ""),
        severity=args.get("severity", "warning"),
        overwrite=bool(args.get("overwrite", False)),
        agent_id=kwargs.get("agent_id"),
        session_id=kwargs.get("session_id"),
    )


def _handle_update_project_notes(**kwargs) -> str:
    args = _drop_ctx(kwargs)
    return update_project_notes(
        content=args.get("content", ""),
        append=bool(args.get("append", False)),
        auto_archive=bool(args.get("auto_archive", True)),
        agent_id=kwargs.get("agent_id"),
        session_id=kwargs.get("session_id"),
    )


def _handle_save_project_memory(**kwargs) -> str:
    args = _drop_ctx(kwargs)
    return save_project_memory(
        topic=args.get("topic", ""),
        content=args.get("content", ""),
        kind=args.get("kind", "handover"),
        private=bool(args.get("private", False)),
        agent_id=kwargs.get("agent_id"),
        session_id=kwargs.get("session_id"),
    )


def _handle_add_project_workflow(**kwargs) -> str:
    args = _drop_ctx(kwargs)
    return add_project_workflow(
        name=args.get("name", ""),
        description=args.get("description", ""),
        steps=args.get("steps", ""),
        overwrite=bool(args.get("overwrite", False)),
        agent_id=kwargs.get("agent_id"),
        session_id=kwargs.get("session_id"),
    )


def _handle_recall_project_memory(**kwargs) -> str:
    args = _drop_ctx(kwargs)
    return recall_project_memory(
        query=args.get("query", ""),
        top_k=int(args.get("top_k", 5) or 5),
        kind=args.get("kind"),
        include_private=bool(args.get("include_private", False)),
    )


def _handle_get_project_workflow(**kwargs) -> str:
    args = _drop_ctx(kwargs)
    return get_project_workflow(name=args.get("name", ""))


def _handle_list_project_rules(**kwargs) -> str:  # noqa: ARG001
    return list_project_rules()


def _handle_archive_lry_file(**kwargs) -> str:
    args = _drop_ctx(kwargs)
    return archive_lry_file(
        kind=args.get("kind", ""),
        name=args.get("name", ""),
        dry_run=bool(args.get("dry_run", False)),
        agent_id=kwargs.get("agent_id"),
        session_id=kwargs.get("session_id"),
    )


def _handle_restore_lry_file(**kwargs) -> str:
    args = _drop_ctx(kwargs)
    return restore_lry_file(
        kind=args.get("kind", ""),
        name=args.get("name", ""),
        dry_run=bool(args.get("dry_run", False)),
        agent_id=kwargs.get("agent_id"),
        session_id=kwargs.get("session_id"),
    )


__all__ = [
    "add_project_rule",
    "update_project_notes",
    "save_project_memory",
    "add_project_workflow",
    "recall_project_memory",
    "get_project_workflow",
    "list_project_rules",
    "_handle_add_project_rule",
    "_handle_update_project_notes",
    "_handle_save_project_memory",
    "_handle_add_project_workflow",
    "_handle_recall_project_memory",
    "_handle_get_project_workflow",
    "_handle_list_project_rules",
    "archive_lry_file",
    "_handle_archive_lry_file",
    "restore_lry_file",
    "_handle_restore_lry_file",
]
