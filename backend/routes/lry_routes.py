"""`.lry/` 工作区只读 API

供前端面板展示柳如烟在当前工作区积累的：
rules / workflows / memory / NOTES / CODE_MAP。

设计文档：`_docs/lry_workspace_design.md` §10 P4-1（前端面板）

## 路由清单（全部 readonly）

- `GET /api/lry/status`                 → 工作区总览 + 各项 count
- `GET /api/lry/rules`                  → 所有 rules 元数据列表
- `GET /api/lry/rules/{slug}`           → 单个 rule 完整内容
- `GET /api/lry/workflows`              → 所有 workflows 元数据列表
- `GET /api/lry/workflows/{name}`       → 单个 workflow 完整内容
- `GET /api/lry/notes`                  → PROJECT_NOTES.md 全文
- `GET /api/lry/codemap`                → CODE_MAP.md 全文
- `GET /api/lry/memory?include_private` → memory 文件元数据列表（默认公开；显式 flag 时合并 private）
- `GET /api/lry/memory/{filename}?private` → 单个 memory 完整内容（private=true 时从 .lry/private/memory/ 读）

## 安全

- slug / filename 严格按 `[a-zA-Z0-9_\\-.]` 校验，禁止 `/` `\\` `..`
- 路径越界检查：解析后必须仍在 `.lry/<subdir>/` 内
- 文件不存在 → 404
- `.lry/` 整个不存在 → status 返 exists=False，其他端点返 404
- Private 内容：API 默认不返回；必须显式 `?include_private=true` / `?private=true`。
  HTTP 层仅在 localhost 暴露，作为"防误列出"防线，不防主动 attacker。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from backend.lry_workspace import (
    CODE_MAP_FILE,
    LRY_DIR_NAME,
    MEMORY_SUBDIR,
    NOTES_FILE,
    PRIVATE_SUBDIR,
    RULES_SUBDIR,
    WORKFLOWS_SUBDIR,
    lry_private_memory_dir,
    resolve_workspace_root,
)


router = APIRouter(prefix="/api/lry", tags=["lry"])


# === 共用工具 ===

# slug 严格白名单：禁 `/` `\` `..` 等越界字符
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-.]+$")


def _safe_subpath(root: Path, subdir: str, name: str, allow_md_suffix: bool = True) -> Path:
    """在 root/subdir/ 下安全解析 name 子路径。

    name 必须符合白名单字符集；解析后 resolved 路径必须仍在 subdir 内。
    任何越界 → HTTPException(400)。
    """
    if not name or not _SAFE_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"invalid name: {name!r}")

    base_dir = (root / LRY_DIR_NAME / subdir).resolve()
    if not base_dir.is_dir():
        raise HTTPException(
            status_code=404, detail=f".lry/{subdir}/ does not exist"
        )

    candidate = name if (not allow_md_suffix or name.endswith(".md")) else f"{name}.md"
    target = (base_dir / candidate).resolve()
    # 路径越界检查
    try:
        target.relative_to(base_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes base directory")

    return target


def _read_file_or_404(path: Path) -> str:
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"not found: {path.name}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"read failed: {exc}")


def _file_meta(path: Path) -> Dict[str, Any]:
    """通用文件元数据。

    注意：故意**不**返回 `name` 字段，避免覆盖 caller 自己设的 `name`/`slug`/`filename`。
    需要文件名时 caller 自行用 `path.name` / `path.stem`。
    """
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "mtime_iso": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


def _parse_frontmatter(text: str, *, max_lines: int = 30) -> Dict[str, str]:
    """简易 YAML frontmatter 解析（只支持 `key: value` 形式）。"""
    out: Dict[str, str] = {}
    if not text.startswith("---"):
        return out
    lines = text.splitlines()
    if len(lines) < 2:
        return out
    in_fm = False
    for i, line in enumerate(lines[: max_lines + 2]):
        if i == 0 and line.strip() == "---":
            in_fm = True
            continue
        if in_fm and line.strip() == "---":
            break
        if in_fm and ":" in line:
            key, _, value = line.partition(":")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            out[key.strip()] = value
    return out


# === GET /api/lry/status ===

@router.get("/status")
async def get_lry_status() -> Dict[str, Any]:
    """返回 .lry/ 工作区总览。"""
    root = resolve_workspace_root()
    if root is None:
        return {
            "exists": False,
            "workspace_root": None,
            "message": "workspace root unresolved",
        }

    lry = root / LRY_DIR_NAME
    if not lry.is_dir():
        return {
            "exists": False,
            "workspace_root": str(root),
        }

    rules_count = sum(1 for _ in (lry / RULES_SUBDIR).glob("*.md")) if (lry / RULES_SUBDIR).is_dir() else 0
    workflows_count = sum(1 for _ in (lry / WORKFLOWS_SUBDIR).glob("*.md")) if (lry / WORKFLOWS_SUBDIR).is_dir() else 0
    memory_count = sum(1 for _ in (lry / MEMORY_SUBDIR).glob("*.md")) if (lry / MEMORY_SUBDIR).is_dir() else 0
    # private 目录只报 count，不列内容；用来告诉前端 "有 N 条私密记忆" 做 UI 标记
    priv_dir = lry / PRIVATE_SUBDIR / MEMORY_SUBDIR
    private_memory_count = sum(1 for _ in priv_dir.glob("*.md")) if priv_dir.is_dir() else 0

    return {
        "exists": True,
        "workspace_root": str(root),
        "lry_root": str(lry),
        "counts": {
            "rules": rules_count,
            "workflows": workflows_count,
            "memory": memory_count,
            "private_memory": private_memory_count,
        },
        "has_notes": (lry / NOTES_FILE).is_file(),
        "has_codemap": (lry / CODE_MAP_FILE).is_file(),
        "has_readme": (lry / "README.md").is_file(),
        "has_gitignore": (lry / ".gitignore").is_file(),
    }


# === GET /api/lry/rules ===

@router.get("/rules")
async def list_rules() -> Dict[str, Any]:
    """列所有 rules 元数据（不含完整内容）。"""
    root = resolve_workspace_root()
    if root is None:
        return {"items": [], "total": 0}
    rules_dir = root / LRY_DIR_NAME / RULES_SUBDIR
    if not rules_dir.is_dir():
        return {"items": [], "total": 0}

    items: List[Dict[str, Any]] = []
    for path in sorted(rules_dir.glob("*.md"), key=lambda p: p.name):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        fm = _parse_frontmatter(text)
        meta = _file_meta(path)
        items.append(
            {
                "slug": path.stem,
                **meta,
                "severity": fm.get("severity", "unknown"),
                "topic": fm.get("topic", path.stem),
                "added": fm.get("added"),
                "by": fm.get("by"),
            }
        )
    return {"items": items, "total": len(items)}


@router.get("/rules/{slug}")
async def get_rule(slug: str) -> Dict[str, Any]:
    root = resolve_workspace_root()
    if root is None:
        raise HTTPException(status_code=404, detail="workspace root unresolved")
    target = _safe_subpath(root, RULES_SUBDIR, slug, allow_md_suffix=True)
    text = _read_file_or_404(target)
    return {
        "slug": slug.removesuffix(".md"),
        "path": str(target),
        "content": text,
        **_file_meta(target),
    }


# === GET /api/lry/workflows ===

@router.get("/workflows")
async def list_workflows() -> Dict[str, Any]:
    root = resolve_workspace_root()
    if root is None:
        return {"items": [], "total": 0}
    wf_dir = root / LRY_DIR_NAME / WORKFLOWS_SUBDIR
    if not wf_dir.is_dir():
        return {"items": [], "total": 0}

    items: List[Dict[str, Any]] = []
    for path in sorted(wf_dir.glob("*.md"), key=lambda p: p.name):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        fm = _parse_frontmatter(text)
        meta = _file_meta(path)
        items.append(
            {
                "name": path.stem,
                **meta,
                "description": fm.get("description"),
                "added": fm.get("added"),
                "by": fm.get("by"),
            }
        )
    return {"items": items, "total": len(items)}


@router.get("/workflows/{name}")
async def get_workflow(name: str) -> Dict[str, Any]:
    root = resolve_workspace_root()
    if root is None:
        raise HTTPException(status_code=404, detail="workspace root unresolved")
    target = _safe_subpath(root, WORKFLOWS_SUBDIR, name, allow_md_suffix=True)
    text = _read_file_or_404(target)
    return {
        "name": name.removesuffix(".md"),
        "path": str(target),
        "content": text,
        **_file_meta(target),
    }


# === GET /api/lry/notes ===

@router.get("/notes")
async def get_notes() -> Dict[str, Any]:
    root = resolve_workspace_root()
    if root is None:
        raise HTTPException(status_code=404, detail="workspace root unresolved")
    target = root / LRY_DIR_NAME / NOTES_FILE
    if not target.is_file():
        return {"exists": False, "content": "", "path": str(target)}
    text = _read_file_or_404(target)
    return {
        "exists": True,
        "path": str(target),
        "content": text,
        **_file_meta(target),
    }


# === GET /api/lry/codemap ===

@router.get("/codemap")
async def get_codemap() -> Dict[str, Any]:
    root = resolve_workspace_root()
    if root is None:
        raise HTTPException(status_code=404, detail="workspace root unresolved")
    target = root / LRY_DIR_NAME / CODE_MAP_FILE
    if not target.is_file():
        return {"exists": False, "content": "", "path": str(target)}
    text = _read_file_or_404(target)
    return {
        "exists": True,
        "path": str(target),
        "content": text,
        **_file_meta(target),
    }


# === GET /api/lry/memory ===


def _collect_memory_items(mem_dir: Path, *, is_private: bool) -> List[Dict[str, Any]]:
    """公共逻辑：扫一个 memory 目录，返回带 frontmatter 的 item 列表。

    mtime 倒序由 caller 统一 sort（合并公开 + 私密时需要）。
    """
    out: List[Dict[str, Any]] = []
    if not mem_dir.is_dir():
        return out
    for path in mem_dir.glob("*.md"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        fm = _parse_frontmatter(text)
        meta = _file_meta(path)
        entry: Dict[str, Any] = {
            "filename": path.name,
            **meta,
            "kind": fm.get("kind"),
            "topic": fm.get("topic"),
            "added": fm.get("added"),
            "by": fm.get("by"),
        }
        if is_private:
            entry["private"] = True
        out.append(entry)
    return out


@router.get("/memory")
async def list_memory(
    include_private: bool = Query(
        False,
        description="If true, merge .lry/private/memory/ items with `private: true` flag.",
    ),
) -> Dict[str, Any]:
    """列 memory 元数据。默认只列 `.lry/memory/`；`?include_private=true` 合并私密目录。

    返回按**文件名 desc** 排序（因为文件名里带 YYYY-MM-DD，字典序 desc ≈ 时间 desc）；
    private 条目带 `private: true` 字段，前端用以加标签。
    """
    root = resolve_workspace_root()
    if root is None:
        return {"items": [], "total": 0}

    items = _collect_memory_items(root / LRY_DIR_NAME / MEMORY_SUBDIR, is_private=False)
    if include_private:
        items.extend(_collect_memory_items(lry_private_memory_dir(root), is_private=True))

    # 排序：文件名 desc（kind_topic_YYYY-MM-DD.md 里日期部分在末尾，字典序 desc ≈ 时间 desc）。
    # 不用 mtime——Windows NTFS 连续 write 的 mtime 精度在测试里会丢次序，
    # 文件名 desc 又免费给前端"相同日期的按 kind/topic 稳定排序"。
    items.sort(key=lambda x: x.get("filename", ""), reverse=True)
    return {"items": items, "total": len(items)}


@router.get("/memory/{filename}")
async def get_memory_file(
    filename: str,
    private: bool = Query(
        False,
        description="If true, read from .lry/private/memory/ instead of the public memory dir.",
    ),
) -> Dict[str, Any]:
    """读单个 memory 文件。

    `?private=true` 时从 `.lry/private/memory/{filename}` 读，否则走公开目录。
    filename 自身仍走严格白名单，不能含路径分隔符，防 traversal。
    """
    root = resolve_workspace_root()
    if root is None:
        raise HTTPException(status_code=404, detail="workspace root unresolved")
    # filename 必须含 .md 后缀（memory 文件名带 kind_topic_date 结构）
    if not filename.endswith(".md"):
        filename = f"{filename}.md"

    if private:
        # 私密路径走独立解析：lry/private/memory/ 下做白名单 + 越界检查
        if not _SAFE_NAME_RE.match(filename):
            raise HTTPException(status_code=400, detail=f"invalid filename: {filename!r}")
        base_dir = lry_private_memory_dir(root).resolve()
        if not base_dir.is_dir():
            raise HTTPException(status_code=404, detail=".lry/private/memory/ does not exist")
        target = (base_dir / filename).resolve()
        try:
            target.relative_to(base_dir)
        except ValueError:
            raise HTTPException(status_code=400, detail="path escapes base directory")
    else:
        target = _safe_subpath(root, MEMORY_SUBDIR, filename, allow_md_suffix=True)

    text = _read_file_or_404(target)
    fm = _parse_frontmatter(text)
    response: Dict[str, Any] = {
        "filename": filename,
        "path": str(target),
        "content": text,
        "frontmatter": fm,
        **_file_meta(target),
    }
    if private:
        response["private"] = True
    return response


__all__ = ["router"]
