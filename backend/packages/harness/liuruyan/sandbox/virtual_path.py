"""v2 · D-2 · 虚拟路径透明翻译。

agent prompt 里看到的路径**永远稳定**：

  /mnt/user-data/workspace   ← {base}/users/{uid}/agents/{aid}/threads/{tid}/workspace
  /mnt/user-data/uploads     ← .../uploads
  /mnt/user-data/outputs     ← .../outputs
  /mnt/agent-data            ← {base}/users/{uid}/agents/{aid}/data
  /mnt/skills                ← {base}/skills（全局共享）

设计原则（docs/architecture_v2/02_runtime_core.md § 六）：

1. **工具入口最外层替换**——工具内部代码看到的永远是物理路径。
2. **替换在 Path.resolve() 后强制 is_relative_to** — 防穿越攻击
   （`/mnt/user-data/workspace/../../../etc/passwd` → resolve 后跳出 base → reject）。
3. **session_id 缺失时 user-data/* 不可解析** — 抛 VirtualPathError；
   /mnt/agent-data 和 /mnt/skills 不需要 session_id。
4. **不读全局状态** — base 由调用方传，user/agent/session 默认从 ContextVar
   读但显式参数可覆盖（跨 thread 边界手动捕获场景）。
5. **harness 不 import backend.*** — base 通过 env LIURUYAN_DATA_DIR 兜底。

D-5 让工具入口（builtin tools）调用 `replace_virtual_paths_in_args` /
`replace_virtual_paths_in_command`；prompt 拼装层（mw 02 ThreadData /
mw 03 UserMemoryInject）调 `to_virtual` 把物理路径反向展示成虚拟。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..runtime.context import (
    get_effective_agent_id,
    get_effective_user_id,
    get_session_id,
)


# ── 常量 ────────────────────────────────────────────────────────────────


DEFAULT_DATA_DIR_ENV = "LIURUYAN_DATA_DIR"

# 工具参数中"整体路径"字段——值若以 /mnt/ 开头按虚拟路径整体替换。
# 跟 v1 Guardian.PATH_PARAMS 对齐。
DEFAULT_PATH_PARAMS: frozenset[str] = frozenset(
    {
        "file_path",
        "path",
        "directory",
        "source",
        "destination",
        "notebook_path",
    }
)

# 工具参数中"shell 命令"字段——值是命令字符串，做子串替换。
DEFAULT_COMMAND_PARAMS: frozenset[str] = frozenset({"command"})


# 虚拟挂载点 → (kind, sub_segments)
# kind = "thread"  → 需要 user/agent/session
# kind = "agent"   → 需要 user/agent
# kind = "global"  → 全局共享
_VIRTUAL_MOUNTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("/mnt/user-data/workspace", "thread", ("workspace",)),
    ("/mnt/user-data/uploads",   "thread", ("uploads",)),
    ("/mnt/user-data/outputs",   "thread", ("outputs",)),
    ("/mnt/agent-data",          "agent",  ()),
    ("/mnt/skills",              "global", ()),
)

# 命令字符串扫描时按"最长前缀优先"——避免 /mnt/user-data/uploads 命中 /mnt/user-data/。
# 排序：长 → 短。
_VIRTUAL_PREFIXES_LONGEST_FIRST: tuple[str, ...] = tuple(
    sorted({m[0] for m in _VIRTUAL_MOUNTS}, key=len, reverse=True)
)


# ── 数据类 / 异常 ───────────────────────────────────────────────────────


class VirtualPathError(ValueError):
    """虚拟路径解析失败：缺 session_id / 路径穿越 / 未知挂载点。"""


@dataclass(frozen=True, slots=True)
class VirtualPathContext:
    """虚拟路径解析所需的最小上下文。"""

    user_id: str
    agent_id: str
    session_id: str | None
    base: Path


# ── public API ──────────────────────────────────────────────────────────


def get_default_base() -> Path:
    """LIURUYAN_DATA_DIR env → cwd 兜底。

    返绝对路径（resolve 一次）。harness 内部禁用 backend.paths，所以走 env。
    """
    raw = os.environ.get(DEFAULT_DATA_DIR_ENV) or "."
    try:
        return Path(raw).resolve()
    except Exception:
        return Path(raw)


def from_context(
    *,
    user_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    base: Path | None = None,
) -> VirtualPathContext:
    """从 ContextVar 取默认值；显式参数优先。

    user_id / agent_id 永不 None（getter 内部 default 兜底）。
    session_id 默认从 ContextVar 读，可能 None。
    """
    return VirtualPathContext(
        user_id=user_id if user_id is not None else get_effective_user_id(),
        agent_id=agent_id if agent_id is not None else get_effective_agent_id(),
        session_id=session_id if session_id is not None else get_session_id(),
        base=base if base is not None else get_default_base(),
    )


def from_virtual(virtual: str, vpc: VirtualPathContext) -> Path:
    """虚拟 → 物理；最长前缀匹配 + 路径穿越检查。

    `virtual` 不以已知 /mnt/ 前缀开头 → 原样返（可能是绝对/相对物理路径）。
    """
    if not isinstance(virtual, str) or not virtual:
        return Path(virtual) if isinstance(virtual, str) else Path()
    norm = virtual.replace("\\", "/")
    matched = _match_mount(norm)
    if matched is None:
        # 不是虚拟路径，原样返（让调用方按物理路径处理）
        return Path(virtual)

    prefix, kind, sub = matched
    rest = norm[len(prefix):].lstrip("/")
    physical_base = _physical_root(vpc, kind, sub)
    physical = (physical_base / rest) if rest else physical_base

    # 路径穿越防御：resolve 后必须 is_relative_to(physical_base)。
    # 注意：resolve 会展开 ../，是关键。strict=False 让目标不存在时也能解析。
    try:
        resolved = physical.resolve(strict=False)
        root_resolved = physical_base.resolve(strict=False)
    except OSError:
        # 系统级路径错误（如非法字符）按穿越处理。
        raise VirtualPathError(
            f"虚拟路径解析失败：{virtual!r}（OS 拒绝 resolve）"
        )
    if not _is_relative_to(resolved, root_resolved):
        raise VirtualPathError(
            f"虚拟路径穿越检测：{virtual!r} resolve 后跳出 {prefix} 物理根 {root_resolved}"
        )
    return resolved


def to_virtual(
    physical: Path | str,
    vpc: VirtualPathContext,
) -> str | None:
    """物理 → 虚拟；命中已知挂载点返虚拟路径，否则 None。

    用途：prompt 拼装层 / 工具结果回显层把物理路径转回虚拟，agent 看到的永远稳定。
    """
    if not physical:
        return None
    p = Path(physical)
    try:
        p_resolved = p.resolve(strict=False)
    except OSError:
        return None

    # 按 thread / agent / global 三级回查
    candidates: list[tuple[str, Path]] = []
    if vpc.session_id:
        for vp, kind, sub in _VIRTUAL_MOUNTS:
            if kind == "thread":
                root = _physical_root(vpc, kind, sub)
                candidates.append((vp, root))
    for vp, kind, sub in _VIRTUAL_MOUNTS:
        if kind == "agent":
            candidates.append((vp, _physical_root(vpc, kind, sub)))
    for vp, kind, sub in _VIRTUAL_MOUNTS:
        if kind == "global":
            candidates.append((vp, _physical_root(vpc, kind, sub)))

    # 长前缀优先（_physical_root 长度未必单调，按 root 字符串长度倒排）
    candidates.sort(key=lambda x: len(str(x[1])), reverse=True)

    for vp, root in candidates:
        try:
            root_resolved = root.resolve(strict=False)
        except OSError:
            continue
        if not _is_relative_to(p_resolved, root_resolved):
            continue
        rel = p_resolved.relative_to(root_resolved)
        rel_posix = rel.as_posix()
        if not rel_posix or rel_posix == ".":
            return vp
        return f"{vp}/{rel_posix}"
    return None


def replace_virtual_paths_in_args(
    args: Mapping[str, Any],
    vpc: VirtualPathContext,
    *,
    path_params: Iterable[str] = DEFAULT_PATH_PARAMS,
    command_params: Iterable[str] = DEFAULT_COMMAND_PARAMS,
) -> dict[str, Any]:
    """工具入口最外层调；返回新 dict。

    - path_params 中的字段：值是 str 且以 /mnt/ 开头 → 整体替换为物理 str。
    - command_params 中的字段：值是 str → 子串扫描所有 /mnt/* 替换。
    - 其它字段：原样保留。
    """
    if not isinstance(args, Mapping):
        return dict(args) if args is not None else {}
    path_set = frozenset(path_params)
    cmd_set = frozenset(command_params)
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and k in path_set and v.startswith("/mnt/"):
            try:
                out[k] = str(from_virtual(v, vpc))
            except VirtualPathError:
                # 解析失败：保留原值让上层 guardrail 拦截
                out[k] = v
        elif isinstance(v, str) and k in cmd_set:
            out[k] = replace_virtual_paths_in_command(v, vpc)
        else:
            out[k] = v
    return out


_TOKEN_RE = re.compile(r"/mnt/[^\s'\"`<>|;&]+")


def replace_virtual_paths_in_command(command: str, vpc: VirtualPathContext) -> str:
    """命令字符串中所有 /mnt/<known>... token 替换为物理路径。

    扫描以 `/mnt/` 开头到下一个 shell 分隔字符（空白/引号/管道/重定向）为止的 token。
    每个 token 单独 from_virtual；前缀未命中的 token 原样保留。
    """
    if not isinstance(command, str) or not command:
        return command

    def _sub(m: re.Match) -> str:
        token = m.group(0)
        norm = token.replace("\\", "/")
        # 必须是已知前缀开头才替换；非已知 /mnt/ 前缀（比如 /mnt/foo）原样保留
        if not any(
            norm == p or norm.startswith(p + "/")
            for p in _VIRTUAL_PREFIXES_LONGEST_FIRST
        ):
            return token
        try:
            return str(from_virtual(token, vpc))
        except VirtualPathError:
            return token

    return _TOKEN_RE.sub(_sub, command)


# ── 内部 ────────────────────────────────────────────────────────────────


def _match_mount(virtual: str) -> tuple[str, str, tuple[str, ...]] | None:
    """最长前缀匹配。返回 (prefix, kind, sub) 或 None。"""
    # _VIRTUAL_MOUNTS 已经按定义顺序排好；这里再保险按 prefix 长度倒排。
    sorted_mounts = sorted(_VIRTUAL_MOUNTS, key=lambda m: len(m[0]), reverse=True)
    for prefix, kind, sub in sorted_mounts:
        if virtual == prefix or virtual.startswith(prefix + "/"):
            return prefix, kind, sub
    return None


def _physical_root(
    vpc: VirtualPathContext,
    kind: str,
    sub: tuple[str, ...],
) -> Path:
    """根据 kind 计算物理根目录。

    thread → {base}/users/{uid}/agents/{aid}/threads/{tid}/{sub*}
    agent  → {base}/users/{uid}/agents/{aid}/data/{sub*}
    global → {base}/skills/{sub*}
    """
    if kind == "thread":
        if not vpc.session_id:
            raise VirtualPathError(
                "/mnt/user-data/* 需要 session_id；当前 ContextVar 未设。"
            )
        root = (
            vpc.base
            / "users"
            / vpc.user_id
            / "agents"
            / vpc.agent_id
            / "threads"
            / vpc.session_id
        )
        for s in sub:
            root = root / s
        return root
    if kind == "agent":
        return (
            vpc.base
            / "users"
            / vpc.user_id
            / "agents"
            / vpc.agent_id
            / "data"
        )
    if kind == "global":
        return vpc.base / "skills"
    raise VirtualPathError(f"未知挂载点 kind: {kind!r}")


def _is_relative_to(child: Path, parent: Path) -> bool:
    """py3.9 之前 Path 没有 is_relative_to；这里手写。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


__all__ = [
    "DEFAULT_COMMAND_PARAMS",
    "DEFAULT_DATA_DIR_ENV",
    "DEFAULT_PATH_PARAMS",
    "VirtualPathContext",
    "VirtualPathError",
    "from_context",
    "from_virtual",
    "get_default_base",
    "replace_virtual_paths_in_args",
    "replace_virtual_paths_in_command",
    "to_virtual",
]
