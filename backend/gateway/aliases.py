"""Gateway 别名映射 + fallback 候选链（M3）。

格式：~/.liuruyan/gateway_aliases.json
{
    "aliases": {
        "<alias_name>": {
            "candidates": ["<provider_id>:<vendor_model>", ...],
            "description": "<可选>",
            "created_at": "...", "updated_at": "..."
        }
    }
}

设计：_docs/GATEWAY_M3.md
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from .. import paths


# alias 名字限制：字母数字 + . _ -，1~64 字符（不含冒号 / 斜杠，避免与命名空间格式冲突）
_ALIAS_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
# candidate 必须是 `provider_id:vendor_model` 格式
_CANDIDATE_PATTERN = re.compile(r"^[^:\s]+:[^\s]+$")


@dataclass
class AliasEntry:
    candidates: List[str] = field(default_factory=list)
    description: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "AliasEntry":
        if not isinstance(d, dict):
            return cls()
        return cls(
            candidates=[str(c) for c in (d.get("candidates") or []) if isinstance(c, str)],
            description=str(d.get("description") or ""),
            created_at=str(d.get("created_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
        )


class AliasValidationError(ValueError):
    """alias 数据非法时抛。`code` 给前端 / API 用。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_alias_name(name: str) -> None:
    if not isinstance(name, str) or not _ALIAS_NAME_PATTERN.match(name):
        raise AliasValidationError(
            "invalid_alias_name",
            f"alias name must match {_ALIAS_NAME_PATTERN.pattern} (no ':' or '/')",
        )


def validate_candidates(candidates: List[str]) -> None:
    if not isinstance(candidates, list) or not candidates:
        raise AliasValidationError(
            "candidates_required", "alias must have at least one candidate"
        )
    for c in candidates:
        if not isinstance(c, str) or not _CANDIDATE_PATTERN.match(c):
            raise AliasValidationError(
                "invalid_candidate",
                f"candidate '{c}' must be '<provider_id>:<vendor_model>' format",
            )


# ─────────────────────────── 文件 IO + mtime cache ───────────────────────────

_cache_lock = threading.Lock()
_cached_signature: Optional[Tuple[float, int]] = None
_cached_payload: Optional[Dict[str, AliasEntry]] = None


def _file_signature() -> Tuple[float, int]:
    p = paths.gateway_aliases_path()
    if not p.exists():
        return (0.0, 0)
    st = p.stat()
    return (st.st_mtime, st.st_size)


def _load_from_disk() -> Dict[str, AliasEntry]:
    p = paths.gateway_aliases_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[GATEWAY-ALIAS-WARN] failed to load %s: %s", p, exc)
        return {}
    aliases_raw = raw.get("aliases") or {}
    out: Dict[str, AliasEntry] = {}
    if isinstance(aliases_raw, dict):
        for name, val in aliases_raw.items():
            if isinstance(name, str) and _ALIAS_NAME_PATTERN.match(name):
                out[name] = AliasEntry.from_dict(val)
    return out


def load_aliases() -> Dict[str, AliasEntry]:
    """读全部 alias。每次返新 dict（深拷贝）。"""
    return copy.deepcopy(_load_from_disk())


def load_aliases_cached() -> Dict[str, AliasEntry]:
    """高频读路径用：mtime/size 任一变化才重读。"""
    global _cached_signature, _cached_payload
    sig = _file_signature()
    with _cache_lock:
        if _cached_signature == sig and _cached_payload is not None:
            return copy.deepcopy(_cached_payload)
    payload = _load_from_disk()
    with _cache_lock:
        _cached_signature = sig
        _cached_payload = copy.deepcopy(payload)
    return copy.deepcopy(payload)


def invalidate_cache() -> None:
    global _cached_signature, _cached_payload
    with _cache_lock:
        _cached_signature = None
        _cached_payload = None


def _save_to_disk(aliases: Dict[str, AliasEntry]) -> None:
    p = paths.gateway_aliases_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"aliases": {name: entry.to_dict() for name, entry in aliases.items()}}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    invalidate_cache()


# ─────────────────────────── 公开 CRUD ───────────────────────────


def get_alias(name: str) -> Optional[AliasEntry]:
    return load_aliases().get(name)


def list_aliases() -> Dict[str, AliasEntry]:
    return load_aliases()


def upsert_alias(name: str, candidates: List[str], description: str = "") -> AliasEntry:
    """创建或整体替换一条 alias。校验失败 raise AliasValidationError。"""
    validate_alias_name(name)
    validate_candidates(candidates)
    aliases = load_aliases()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    existing = aliases.get(name)
    entry = AliasEntry(
        candidates=list(candidates),
        description=description or "",
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )
    aliases[name] = entry
    _save_to_disk(aliases)
    return entry


def delete_alias(name: str) -> bool:
    aliases = load_aliases()
    if name not in aliases:
        return False
    del aliases[name]
    _save_to_disk(aliases)
    return True


def split_candidate(candidate: str) -> Tuple[str, str]:
    """`provider_id:vendor_model` → (provider_id, vendor_model)。"""
    if ":" not in candidate:
        raise AliasValidationError("invalid_candidate", f"candidate '{candidate}' missing ':'")
    pid, vm = candidate.split(":", 1)
    return pid, vm


__all__ = [
    "AliasEntry",
    "AliasValidationError",
    "validate_alias_name",
    "validate_candidates",
    "load_aliases",
    "load_aliases_cached",
    "invalidate_cache",
    "get_alias",
    "list_aliases",
    "upsert_alias",
    "delete_alias",
    "split_candidate",
]
