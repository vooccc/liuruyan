"""写前自动备份 + 审计日志——给"自进化"配的护网。

两个 hook 点：
  写前 backup_before_write(): cp 目标文件到 data_dir/backups/<UTC>/<rel>
  写后 record_backend_write(): 落一行 jsonl 到 data_dir/backend_writes.jsonl

角色写错了能从备份恢复；想知道"谁改了什么"翻 jsonl。

不备份的场景：
- 目标文件不存在（新建文件无需备份）
- 目标不是普通文件（目录/符号链接）
- 路径参数不存在（工具不带 path）

调用方在 backend/tools/builtin/clean_registry.py 的 execute() 里：
    if name in WRITE_TOOLS:
        backup_path = backup_before_write(name, arguments, agent_id)
    ... _dispatch ...
    if name in WRITE_TOOLS:
        record_backend_write(name, arguments, agent_id, session_id, backup_path, success=True)
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set

from backend import paths

logger = logging.getLogger(__name__)


# 触发备份的工具名 → 目标路径的参数键。
# 键名必须是工具 input_schema 里的 canonical name（registry.execute 入口已把
# file_path / target_file 等别名归一化过）。这里写错了不会报错，只会静默不备份：
# 2026-07-28 前 edit 写的是 "file_path"、multi_edit 整个没进表，两个工具的写前
# 备份因此一直是空转。改这张表时对着 registrations/files.py 的 schema 核一遍。
_WRITE_TOOL_PATH_KEYS: Dict[str, str] = {
    "write_file": "path",
    "edit": "path",
    "multi_edit": "path",
    "notebook_edit": "notebook_path",
}

# 唯一真源：registry_core 的写工具判定、read-before-edit gate 都该引用这里，
# 各自硬编码 set 必然漂。
WRITE_TOOLS: Set[str] = set(_WRITE_TOOL_PATH_KEYS.keys())

# 备份/审计的"敏感源码"作用域——只对这些目录下的写改启用护网。
# 角色自己的 agents/<aid>/* 是日常状态写（SOUL/REFLECTIONS 等），备份没意义、还吃磁盘；
# memory/sessions/* 是会话数据；frontend 改了能编译看见，重要性次之。
# backend 才是"自进化"真风险所在——改了直接影响下次启动行为。
_PROTECTED_SOURCE_DIRS: tuple = ("backend",)


def backup_before_write(
    tool_name: str,
    arguments: Dict[str, Any],
    agent_id: Optional[str] = None,
) -> Optional[Path]:
    """如果目标文件已存在，cp 到 backups/<UTC>/<rel>，返回备份路径；否则返 None。

    任何异常吞掉只 log——备份失败不该阻断原始写入操作。
    范围限制：只对 _PROTECTED_SOURCE_DIRS 命中的路径做备份。
    """
    try:
        target = _extract_target_path(tool_name, arguments)
        if not target:
            return None
        src = Path(target)
        try:
            src = src.resolve()
        except OSError:
            pass
        if not src.exists() or not src.is_file():
            return None
        if not _is_protected_source(src):
            return None  # 不在 backend/ 等敏感源码目录，不备份

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        backup_root = paths.backend_backups_dir()

        rel_subpath = _compute_backup_subpath(src)
        backup_path = backup_root / timestamp / rel_subpath
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, backup_path)

        logger.debug(
            f"[BACKUP] {agent_id or '?'} → {tool_name}({src}) → {backup_path}"
        )
        return backup_path
    except Exception as exc:
        logger.warning(f"backup_before_write failed: {exc}")
        return None


def rotate_audit_log_if_needed(max_bytes: int = 10 * 1024 * 1024) -> Optional[Path]:
    """jsonl 审计日志超阈值时归档（rename 成 .archived_<UTC>）。

    Lifespan 启动时调一次即可——只增不减的 jsonl 长期会膨胀，归档把它定期切片。
    归档文件保留在 data_dir 不主动删，由用户判断是否清理（防误删历史）。
    """
    try:
        path = paths.backend_writes_jsonl_path()
    except Exception:
        return None
    if not path.exists():
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= max_bytes:
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    archived = path.parent / f"{path.name}.archived_{timestamp}"
    try:
        path.rename(archived)
        logger.debug(f"[AUDIT] rotated {path} → {archived} ({size} bytes)")
        return archived
    except OSError as exc:
        logger.warning(f"rotate_audit_log_if_needed rename failed: {exc}")
        return None


def cleanup_old_backups(retention_days: int = 7) -> int:
    """删除超过 retention_days 的备份子目录。返回清理数量。

    在启动 lifespan 调一次即可（不需要后台 thread——下次启动会再清）。
    """
    if retention_days <= 0:
        return 0
    try:
        backup_root = paths.backend_backups_dir()
    except Exception:
        return 0
    if not backup_root.exists():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for child in backup_root.iterdir():
        if not child.is_dir():
            continue
        try:
            # 时间戳目录名：YYYYMMDDTHHMMSS_microseconds
            stamp = child.name.split("_")[0]
            ts = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts < cutoff:
            try:
                shutil.rmtree(child)
                removed += 1
            except OSError:
                pass
    return removed


def record_backend_write(
    tool_name: str,
    arguments: Dict[str, Any],
    agent_id: Optional[str],
    session_id: Optional[str],
    backup_path: Optional[Path],
    success: bool = True,
) -> Optional[Path]:
    """写后审计——落一行 jsonl 到 data_dir/backend_writes.jsonl。

    字段：ts / agent_id / session_id / tool_name / path / chars_before / chars_after /
    lines_before / lines_after / created / success / backup_path

    范围限制：只记 _PROTECTED_SOURCE_DIRS 命中的路径。
    任何异常吞掉只 log——审计失败不该阻断主流程。
    """
    try:
        target = _extract_target_path(tool_name, arguments)
        if not target:
            return None
        if not _is_protected_source(Path(target).resolve() if Path(target).exists() else Path(target)):
            return None

        chars_before, lines_before = _measure(backup_path) if backup_path else (0, 0)
        chars_after, lines_after = _measure(Path(target))
        created = backup_path is None  # 没旧版 = 新建

        record: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "agent_id": agent_id,
            "session_id": session_id,
            "tool_name": tool_name,
            "path": str(Path(target)),
            "chars_before": chars_before,
            "chars_after": chars_after,
            "lines_before": lines_before,
            "lines_after": lines_after,
            "created": created,
            "success": success,
            "backup_path": str(backup_path) if backup_path else None,
        }

        jsonl_path = paths.backend_writes_jsonl_path()
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return jsonl_path
    except Exception as exc:
        logger.warning(f"record_backend_write failed: {exc}")
        return None


async def emit_backend_modified_sse(
    tool_name: str,
    arguments: Dict[str, Any],
    agent_id: Optional[str],
    session_id: Optional[str],
    backup_path: Optional[Path],
) -> None:
    """前端 toast 通知——SSE 事件 backend_modified。

    自包含：自己 measure 文件大小/行数，不依赖 record_backend_write 的返回值。
    没有 session_id 就跳过（如 lifespan / 后台 runtime 间接写）。
    任何异常吞掉 log——不阻断主流程。
    """
    if not session_id:
        return
    try:
        target = _extract_target_path(tool_name, arguments)
        if not target:
            return
        target_path = Path(target).resolve() if Path(target).exists() else Path(target)
        if not _is_protected_source(target_path):
            return
        chars_after, lines_after = _measure(Path(target))
        chars_before, lines_before = _measure(backup_path) if backup_path else (0, 0)
        from backend.sse_manager import get_sse_manager
        path_obj = Path(target)
        await get_sse_manager().send_event(
            session_id,
            {
                "type": "backend_modified",
                "agent_id": agent_id,
                "tool_name": tool_name,
                "path": str(path_obj),
                "filename": path_obj.name,
                "chars_before": chars_before,
                "chars_after": chars_after,
                "lines_before": lines_before,
                "lines_after": lines_after,
                "created": backup_path is None,
                "backup_path": str(backup_path) if backup_path else None,
            },
            event_type="backend_modified",
        )
    except Exception as exc:
        logger.warning(f"emit_backend_modified_sse failed: {exc}")


# ===== internal =====

def _is_protected_source(path: Path) -> bool:
    """是否在 backend/ 等敏感源码目录下（绝对路径或项目内相对路径都算）。

    判定方式：
    - 项目根 / 受保护目录前缀（resolve 后判 startswith）
    - 路径里出现 /<受保护目录>/ 片段（兜底，应对相对路径）
    """
    try:
        project_root = paths.legacy_project_root().resolve()
    except Exception:
        project_root = None

    abs_path_unix = str(path).replace("\\", "/")
    for proto_dir in _PROTECTED_SOURCE_DIRS:
        # 1. 项目根下命中：<project_root>/backend/...
        if project_root:
            try:
                rooted = project_root / proto_dir
                # path 已 resolve 时这步是字符串前缀比较
                rel = path.resolve().relative_to(rooted) if path.exists() else None
                if rel is not None:
                    return True
            except (ValueError, OSError):
                pass
        # 2. 兜底：路径里有 /<proto_dir>/ 片段（处理未 resolve 的相对路径）
        sentinel = f"/{proto_dir}/"
        if sentinel in abs_path_unix:
            return True
        if abs_path_unix.endswith(f"/{proto_dir}"):
            return True
    return False


def _measure(path: Optional[Path]) -> tuple[int, int]:
    """读文件返回 (字符数, 行数)。读不到返 (0, 0)。"""
    if not path or not path.exists() or not path.is_file():
        return 0, 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0
    if not text:
        return 0, 0
    chars = len(text)
    lines = text.count("\n")
    if not text.endswith("\n"):
        lines += 1
    return chars, lines


def _extract_target_path(tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
    key = _WRITE_TOOL_PATH_KEYS.get(tool_name)
    if not key:
        return None
    val = arguments.get(key)
    if isinstance(val, str) and val:
        return val
    return None


def _compute_backup_subpath(src: Path) -> Path:
    """把绝对路径转成可放入 backups/<ts>/ 下的相对路径。

    优先：项目根的相对路径（保持目录结构可读）
    兜底：盘符 + 分隔符 flatten 成单层文件名（跨盘/容器情况）
    """
    try:
        project_root = paths.legacy_project_root().resolve()
        return src.relative_to(project_root)
    except (ValueError, OSError):
        flat = str(src).replace(":", "_").replace("\\", "_").replace("/", "_")
        return Path(flat)
