"""角色 / 全局记忆 导入导出。

功能：
- export_agents(agent_ids, dest):  打 ZIP，每个 agent 包含 sessions / memory / 设定 / 头像
- export_memory(dest):             打 ZIP，全局 memory/* 数据（social_graph、game_mechanics、日记）
- import_archive(zip_path):        自动识别包类型 + 合并

合并语义（仅当 zip 里 agent ID 在本机已存在）：
- 角色设定文件（GENOME/IDENTITY/PERSONALITY/SOUL/MEMORY.md）：以本机为准（活体在用）
- memory/<日期>.md 日记：同名跳过、新文件补入
- sessions/<sid>/：同 sid 跳过、新 sid 补入
- 头像 / meta.json：以本机为准
- 跨机器创建的角色 ID 必然不同 → 直接拷贝整目录

包结构（zip 内）：
  manifest.json                      — 标识包类型 + 版本
  agents/<agent_id>/...               — 仅 agent 包
  memory/...                          — 仅 memory 包

ZIP 安全：
- 路径穿越（zip slip）：所有 entry 解压前校验 normpath 不出 base 目录
- 大小上限：ARCHIVE_MAX_BYTES = 200MB（防 zip bomb）
"""
from __future__ import annotations

import io
import json
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from . import paths

ARCHIVE_VERSION = 1
ARCHIVE_MAX_BYTES = 200 * 1024 * 1024  # 200 MB
ARCHIVE_KIND_AGENTS = "agents"
ARCHIVE_KIND_MEMORY = "memory"

# 角色设定文件（合并时以本机为准，不被 zip 覆盖）
AGENT_PROTECTED_FILES = {
    "GENOME.md", "IDENTITY.md", "PERSONALITY.md",
    "REFLECTIONS.md", "SOUL.md", "MEMORY.md",
    "meta.json", "avatar.png", "custom_avatar.png",
}


@dataclass
class ImportReport:
    """导入合并报告。"""
    kind: str = ""                                                  # agents / memory
    new_agents: List[str] = field(default_factory=list)             # 全新拷入的 agent_id
    merged_agents: List[str] = field(default_factory=list)          # 同 ID 合并的
    skipped_files: List[str] = field(default_factory=list)          # 已存在跳过（含 protected files）
    added_files: List[str] = field(default_factory=list)            # 合并新增的文件
    skipped_sessions: List[str] = field(default_factory=list)       # 同 sid 跳过的
    added_sessions: List[str] = field(default_factory=list)         # 新增 session
    errors: List[str] = field(default_factory=list)                 # 容错 error 不抛

    def to_dict(self) -> Dict:
        return {
            "kind": self.kind,
            "new_agents": self.new_agents,
            "merged_agents": self.merged_agents,
            "skipped_files_count": len(self.skipped_files),
            "added_files_count": len(self.added_files),
            "skipped_sessions": self.skipped_sessions,
            "added_sessions": self.added_sessions,
            "errors": self.errors,
        }


# ──────────────────────────── 导出 ────────────────────────────


def _build_manifest(kind: str, payload: Dict) -> Dict:
    return {
        "format": "liuruyan-archive",
        "version": ARCHIVE_VERSION,
        "kind": kind,
        "exported_at": datetime.now().isoformat(),
        **payload,
    }


def _write_dir_to_zip(zf: zipfile.ZipFile, src: Path, arc_prefix: str) -> int:
    """递归把 src 目录写入 zip 的 arc_prefix 下，返回写入的文件数。"""
    if not src.exists() or not src.is_dir():
        return 0
    count = 0
    for item in src.rglob("*"):
        if not item.is_file():
            continue
        # arcname 用正斜杠（zip 标准），即使 Windows 也是
        rel = item.relative_to(src).as_posix()
        zf.write(item, arcname=f"{arc_prefix}/{rel}")
        count += 1
    return count


def export_agents(agent_ids: List[str], dest: Path) -> Dict:
    """把指定 agent_ids 打成 ZIP 写到 dest。

    Returns:
        {agents: [...], total_files: int, archive_path: str}
    """
    agents_root = paths.agents_dir()
    exported: List[str] = []
    skipped: List[str] = []
    total_files = 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for agent_id in agent_ids:
            agent_dir = agents_root / agent_id
            if not agent_dir.exists() or not agent_dir.is_dir():
                skipped.append(agent_id)
                continue
            n = _write_dir_to_zip(zf, agent_dir, f"agents/{agent_id}")
            total_files += n
            exported.append(agent_id)

        manifest = _build_manifest(ARCHIVE_KIND_AGENTS, {
            "agents": exported,
            "skipped": skipped,
        })
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return {
        "agents": exported,
        "skipped": skipped,
        "total_files": total_files,
        "archive_path": str(dest),
    }


# 全局 memory/* 里"系统记忆"白名单。
# 老版本 memory/ 还会有：
#   - 2026-04-*.md：早期全局日记（已迁移到 agents/<id>/memory/）
#   - sessions/ + sessions.backup-*：早期 session 存储（已迁移到 agents/<id>/sessions/）
# 这些是历史遗留 / 一次性迁移产物，不属于"系统记忆"，不导出。
MEMORY_SYSTEM_DIRS = ("social_graph", "game_mechanics")


def export_memory(dest: Path) -> Dict:
    """把全局 memory/ 下的"系统文件夹"打成 ZIP 写到 dest。

    严格只导 MEMORY_SYSTEM_DIRS 列出的目录（社交关系图 + 游戏化机制），
    跳过早期日记 / sessions 迁移残留 / 其他历史遗留文件。

    Returns:
        {dirs, total_files, archive_path}
    """
    memory_root = paths.memory_dir()
    dest.parent.mkdir(parents=True, exist_ok=True)
    exported_dirs: List[str] = []
    total_files = 0
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dirname in MEMORY_SYSTEM_DIRS:
            sub = memory_root / dirname
            if not sub.exists() or not sub.is_dir():
                continue
            n = _write_dir_to_zip(zf, sub, f"memory/{dirname}")
            if n > 0:
                exported_dirs.append(dirname)
                total_files += n

        manifest = _build_manifest(ARCHIVE_KIND_MEMORY, {
            "dirs": exported_dirs,
            "total_files": total_files,
        })
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return {
        "dirs": exported_dirs,
        "total_files": total_files,
        "archive_path": str(dest),
    }


# ──────────────────────────── 导入 ────────────────────────────


def _safe_extract_path(base: Path, member_name: str) -> Optional[Path]:
    """校验 zip member 路径不出 base 目录。

    防 zip slip：member_name 可能是 '../foo' / 'agents/../../etc/passwd' 等。
    用 normpath + commonpath 双重校验，外溢就返回 None（调用方跳过）。
    """
    # zip 标准用 / 分隔；统一转 OS 分隔后再 normpath
    target = (base / member_name).resolve()
    base_resolved = base.resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError:
        return None
    return target


def _read_manifest(zf: zipfile.ZipFile) -> Optional[Dict]:
    """从 zip 顶层读 manifest.json，无效返回 None。"""
    try:
        with zf.open("manifest.json") as f:
            data = json.loads(f.read().decode("utf-8"))
            if not isinstance(data, dict):
                return None
            if data.get("format") != "liuruyan-archive":
                return None
            return data
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _check_archive_safety(zip_path: Path) -> Optional[str]:
    """导入前的安全检查。返回 None 表示安全；返回字符串是 error 描述。"""
    if not zip_path.exists():
        return f"archive not found: {zip_path}"
    if zip_path.stat().st_size > ARCHIVE_MAX_BYTES:
        return f"archive too large (>{ARCHIVE_MAX_BYTES // (1024 * 1024)} MB)"
    return None


def import_archive(zip_path: Path) -> ImportReport:
    """导入 zip。自动识别 manifest.kind 并按规则合并。"""
    report = ImportReport()
    err = _check_archive_safety(zip_path)
    if err:
        report.errors.append(err)
        return report

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            manifest = _read_manifest(zf)
            if manifest is None:
                report.errors.append("invalid or missing manifest.json")
                return report

            kind = manifest.get("kind", "")
            report.kind = kind
            if kind == ARCHIVE_KIND_AGENTS:
                _import_agents(zf, report)
            elif kind == ARCHIVE_KIND_MEMORY:
                _import_memory(zf, report)
            else:
                report.errors.append(f"unknown archive kind: {kind!r}")
    except zipfile.BadZipFile as e:
        report.errors.append(f"bad zip file: {e}")
    return report


def _list_agent_ids_in_zip(zf: zipfile.ZipFile) -> List[str]:
    """从 zip 名单里提取所有顶层 agents/<id>/ 段。"""
    seen: List[str] = []
    for name in zf.namelist():
        # name 形如 'agents/agent_xxx/sessions/yyy/transcript.jsonl'
        parts = name.split("/")
        if len(parts) >= 2 and parts[0] == "agents" and parts[1]:
            if parts[1] not in seen:
                seen.append(parts[1])
    return seen


def _import_agents(zf: zipfile.ZipFile, report: ImportReport) -> None:
    """合并语义见模块 docstring。"""
    agents_root = paths.agents_dir()
    agent_ids = _list_agent_ids_in_zip(zf)

    for aid in agent_ids:
        local_dir = agents_root / aid
        is_new = not local_dir.exists()

        # 收集 zip 里这个 agent 的所有 entry
        agent_entries = [
            n for n in zf.namelist()
            if n.startswith(f"agents/{aid}/") and not n.endswith("/")
        ]

        if is_new:
            # 全新角色 → 直接拷贝
            for entry in agent_entries:
                target = _safe_extract_path(agents_root, entry[len("agents/"):])
                if target is None:
                    report.errors.append(f"unsafe path in zip skipped: {entry}")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            report.new_agents.append(aid)
            continue

        # 同 ID → 合并
        report.merged_agents.append(aid)

        # 关键：session 级判断要"整体快照"，不能 per-entry 看 exists()。
        # 因为同一个 sid 在 zip 里通常有多个 entry（session.json + transcript.jsonl + emotion.json），
        # 第一个 entry 写完后 sid 目录就存在了，第二个 entry 再看 exists() 会误判为"已存在"，
        # 导致同 sid 后续文件被吞。这里在 loop 前一次性算出"导入开始时哪些 sid 已存在"。
        sessions_dir = local_dir / "sessions"
        existing_sids: set[str] = set()
        if sessions_dir.exists():
            existing_sids = {p.name for p in sessions_dir.iterdir() if p.is_dir()}

        for entry in agent_entries:
            rel_in_agent = entry[len(f"agents/{aid}/"):]  # e.g. "sessions/sid/transcript.jsonl"
            parts = rel_in_agent.split("/")

            # session 级合并：本机已存在的 sid 整段跳过，新 sid 整段补入
            if parts[0] == "sessions" and len(parts) >= 2:
                sid = parts[1]
                if sid in existing_sids:
                    if sid not in report.skipped_sessions:
                        report.skipped_sessions.append(sid)
                    continue
                target = _safe_extract_path(agents_root, entry[len("agents/"):])
                if target is None:
                    report.errors.append(f"unsafe path skipped: {entry}")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if sid not in report.added_sessions:
                    report.added_sessions.append(sid)
                continue

            # 受保护的角色设定文件：以本机为准
            if rel_in_agent in AGENT_PROTECTED_FILES:
                report.skipped_files.append(f"{aid}/{rel_in_agent}")
                continue

            # 其他文件（memory/<日期>.md 等）：同名跳过、新文件补入
            target = _safe_extract_path(agents_root, entry[len("agents/"):])
            if target is None:
                report.errors.append(f"unsafe path skipped: {entry}")
                continue
            if target.exists():
                report.skipped_files.append(f"{aid}/{rel_in_agent}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(entry) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            report.added_files.append(f"{aid}/{rel_in_agent}")


def _import_memory(zf: zipfile.ZipFile, report: ImportReport) -> None:
    """全局记忆：同名文件跳过，新文件补入。

    注：social_graph / game_mechanics 等 JSON 数据按文件级合并已能覆盖大部分场景；
    如未来需要 JSON 内部 merge（关系图节点 union 等）再单独扩展。
    """
    memory_root = paths.memory_dir()
    for entry in zf.namelist():
        if not entry.startswith("memory/") or entry.endswith("/"):
            continue
        rel = entry[len("memory/"):]
        target = _safe_extract_path(memory_root, rel)
        if target is None:
            report.errors.append(f"unsafe path skipped: {entry}")
            continue
        if target.exists():
            report.skipped_files.append(f"memory/{rel}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(entry) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        report.added_files.append(f"memory/{rel}")


# ──────────────────────────── 工具函数（供 API 用） ────────────────────────────


def list_agents_summary() -> List[Dict]:
    """枚举本机所有 agent 的 id + name + session_count + last_active。

    供前端勾选 UI 使用。失败时静默跳过单个 agent，不阻断列表。
    """
    out: List[Dict] = []
    agents_root = paths.agents_dir()
    if not agents_root.exists():
        return out
    for child in sorted(agents_root.iterdir()):
        if not child.is_dir():
            continue
        agent_id = child.name
        if agent_id == "templates":  # 模板目录跳过
            continue
        meta_path = child / "meta.json"
        name = agent_id
        last_active: Optional[str] = None
        try:
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(meta, dict):
                    name = meta.get("name") or agent_id
                    last_active = meta.get("last_active_at") or meta.get("updated_at")
        except (json.JSONDecodeError, OSError):
            pass

        sessions_count = 0
        sessions_dir = child / "sessions"
        if sessions_dir.exists():
            sessions_count = sum(1 for s in sessions_dir.iterdir() if s.is_dir())

        out.append({
            "agent_id": agent_id,
            "name": name,
            "session_count": sessions_count,
            "last_active": last_active,
        })
    return out
