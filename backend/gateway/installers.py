"""Gateway M5：一键 CLI 接入。

为支持的外部 CLI（claude-code / codex / ...）写它们的本地配置文件，
让 `claude` / `codex` 这些命令直接走 Gateway——免环境变量前缀。

核心纪律：
  1. **每次 install 都备份目标文件** → `<原路径>.lry-backup-<ts>`，不删旧备份
  2. **uninstall 还原最近一次备份**；备份不存在则只删除我们的字段（best-effort）
  3. **检测外部修改**：用 hash 记录我们写入后的内容，install/uninstall 前对比；
     若外部改过 install 时警告（备份后继续），uninstall 时拒绝（防误删用户改动）
  4. **dry-run preview**：UI 上"预览"按钮看 diff，不真改文件

数据文件：
  - 状态：~/.liuruyan/gateway_installers.json（installer_id → {target_path, last_install_at, written_hash, backup_paths}）

支持的 installer（M5 阶段）：
  - claude_code     ~/.claude/settings.json
  - codex           ~/.codex/config.toml + auth.json

  - VSCode 插件生态（由用户在图形界面中直接填入 Base URL 与 Key）
  - gemini-cli / opencode（schema 在快速变化，等稳定再加）
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import paths


# ─────────────────────── 公共数据类型 ───────────────────────


@dataclass
class InstallerInfo:
    """installer 元数据（前端展示用）。"""
    id: str
    name: str
    description: str
    protocol: str  # "openai_chat" | "anthropic_messages"
    target_path: str  # 平台展开后的真实路径


@dataclass
class StatusReport:
    """单个 installer 的当前状态。"""
    id: str
    target_path: str
    target_exists: bool
    state: str  # "not_installed" | "installed_pointing_to_us" | "installed_pointing_elsewhere" | "external_modified"
    detail: str  # 给用户看的一行说明
    last_install_at: Optional[str] = None
    backup_count: int = 0


@dataclass
class PreviewReport:
    """dry-run 报告：告诉用户 install/uninstall 会发生什么。"""
    id: str
    action: str  # "install" | "uninstall"
    target_path: str
    target_exists: bool
    will_create: bool  # 会从无到有创建
    will_modify: bool  # 会修改现有文件
    will_backup_to: Optional[str]
    external_modified: bool  # 文件被外部改过（warning 触发）
    diff_summary: str  # 多行字符串，给前端 <pre> 展示


@dataclass
class ActionResult:
    ok: bool
    message: str
    backup_path: Optional[str] = None
    target_path: Optional[str] = None


# ─────────────────────── 状态文件读写 ───────────────────────


_state_lock = threading.Lock()


def _load_state() -> Dict[str, Any]:
    p = paths.gateway_installer_state_path()
    if not p.exists():
        return {"installers": {}}
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {"installers": {}}
        data.setdefault("installers", {})
        return data
    except (OSError, ValueError):
        return {"installers": {}}


def _save_state(state: Dict[str, Any]) -> None:
    p = paths.gateway_installer_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _hash_file(p: Path) -> str:
    if not p.exists():
        return ""
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _strip_toml_sections_for_hash(text: str, prefixes: tuple[str, ...]) -> str:
    """从 toml 文本中删除匹配指定前缀的段（含段头到下一个 ``[...]`` 段头或 EOF）。

    用于 hash 归一化：把 codex 等 CLI **自己会动态写回**的段从 hash 输入中剔除，
    避免它们正常的内部更新（如 ``[tui.model_availability_nux]`` 计数变化、
    ``[projects.*]`` trust 写回）触发"目标文件被外部改动过"的 409 误报。

    匹配规则（与 :func:`_codex_capture_user_decision_sections` 对齐）：
    - 段头 ``[xxx]`` 完全等于 ``prefix``，或
    - 段头以 ``prefix`` 作为字面前缀开始（如 ``[projects.`` 匹配 ``[projects.'g:\\ai\\foo']``）

    保留：不匹配的段 + 顶层（首个 ``[...]`` 之前的部分）。
    """
    if not text or not prefixes:
        return text
    header_re = re.compile(r"^(\[[^\]\n]+\])\s*$", re.MULTILINE)
    matches = list(header_re.finditer(text))
    if not matches:
        return text
    # 顶层（首个段头之前）总保留
    kept: list[str] = [text[: matches[0].start()]]
    for i, m in enumerate(matches):
        header = m.group(1).strip()
        drop = any(
            header == prefix or header.startswith(prefix)
            for prefix in prefixes
        )
        if drop:
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        kept.append(text[start:end])
    return "".join(kept)


def _hash_target_for_drift(p: Path, spec: "_InstallerSpec") -> str:
    """计算用于"外部改动检测"的 hash：可能跳过 spec 声明的自管段。

    与原始 :func:`_hash_file` 的区别：若 ``spec.auto_managed_section_prefixes`` 不为空，
    则读文件 → 剥离这些段 → 对剩余文本算 sha256。否则等价 ``_hash_file``。

    设计目的：让 codex 等 CLI 启动后正常写回 ``[windows]`` / ``[projects.*]`` / 
    ``[tui.*]`` / ``[notice]`` 段时不被 hash 比对误判为"外部改动"。
    """
    if not p.exists():
        return ""
    if not spec.auto_managed_section_prefixes:
        return _hash_file(p)
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return _hash_file(p)
    normalized = _strip_toml_sections_for_hash(text, spec.auto_managed_section_prefixes)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _backup_file(target: Path) -> Path:
    """备份目标文件到 `<target>.lry-backup-<ts>`，返回备份路径。"""
    if not target.exists():
        raise FileNotFoundError(f"cannot backup: {target} does not exist")
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bk = target.with_name(f"{target.name}.lry-backup-{ts}")
    # 重名兜底
    suffix = 1
    while bk.exists():
        bk = target.with_name(f"{target.name}.lry-backup-{ts}-{suffix}")
        suffix += 1
    import shutil
    shutil.copy2(str(target), str(bk))
    return bk


# ─────────────────────── installer 注册 ───────────────────────


@dataclass
class _InstallerSpec:
    info: InstallerInfo
    """preview/install/uninstall 三件套；签名见下方各 installer。"""
    target_resolver: Callable[[], Path]
    """安装函数：(token, openai_base, anthropic_base, **extra) → 写入的内容字符串

    `extra` 是 installer-specific 配置，目前已知的：
      - claude_code: `default_models = {"haiku"/"sonnet"/"opus": "<provider:model>" | None}`
        让用户为 Claude Code 三个内置 alias 各选一个柳如烟 model（ccswitch 风格）。
      - codex: `default_model = "<alias_or_provider:model>"` 必填。
        写到 config.toml 顶层 `model = "..."`；不填 → render_install_content 抛 ValueError。
        不在意的 installer 加 `**_unused` 忽略即可。
    """
    render_install_content: Callable[..., str]
    """从已写入的内容反向判断"是不是我们写的"。返回 'us' / 'elsewhere' / 'unknown'"""
    classify_existing: Callable[[Optional[str]], str]
    """从已写入的内容剥除我们的字段（为 uninstall 用）。返回 None 表示"全删除文件"，否则返回新内容。"""
    render_uninstall_content: Callable[[str], Optional[str]]
    post_install_hook: Optional[Callable[..., None]] = None
    """install 主文件落盘后的副作用 hook（codex 用来写 ~/.codex/auth.json）。

    签名：``hook(token, openai_base, anthropic_base, **extra) -> None``。
    异常会向上传播到 install()，转换为 ActionResult(ok=False)。
    """
    post_uninstall_hook: Optional[Callable[[], None]] = None
    """uninstall 完成后清理 hook（codex 用来删 auth.json）。

    任何异常都被静默吞掉（uninstall 已经成功，副作用清理失败不应让用户看到错误）。
    """
    auto_managed_section_prefixes: tuple[str, ...] = ()
    """TOML 段前缀列表：这些段会被目标 CLI 自己启动后动态写回，hash 比对时忽略。

    用于让"外部改动检测"豁免 CLI 的正常自管行为。codex 例：

    - ``[windows]`` —— codex 写回 sandbox 决策
    - ``[projects.`` —— 目录 trust 写回
    - ``[tui.`` —— nux 计数 / TUI 内部状态
    - ``[notice]`` / ``[notice.`` —— 用户 dismiss 过的提示

    匹配语义：段头 ``[xxx]`` 完全等于前缀，或以前缀字面开头。详见
    :func:`_strip_toml_sections_for_hash`。不为空时 install/preview/uninstall 的
    drift 判断走 :func:`_hash_target_for_drift`（剥离段后算 hash），否则等价 full-file hash。
    """


_REGISTRY: Dict[str, _InstallerSpec] = {}


def register_installer(spec: _InstallerSpec) -> None:
    _REGISTRY[spec.info.id] = spec


def list_installers() -> List[InstallerInfo]:
    return [s.info for s in _REGISTRY.values()]


def get_spec(installer_id: str) -> _InstallerSpec:
    if installer_id not in _REGISTRY:
        raise KeyError(f"unknown installer '{installer_id}'")
    return _REGISTRY[installer_id]


# ─────────────────────── 状态查询 ───────────────────────


def get_status(installer_id: str) -> StatusReport:
    spec = get_spec(installer_id)
    target = spec.target_resolver()
    state = _load_state().get("installers", {}).get(installer_id) or {}
    last_install_at = state.get("last_install_at")
    backups = state.get("backup_paths") or []
    written_hash = state.get("written_hash") or ""

    if not target.exists():
        return StatusReport(
            id=installer_id,
            target_path=str(target),
            target_exists=False,
            state="not_installed",
            detail=f"目标文件 {target} 不存在",
            last_install_at=last_install_at,
            backup_count=len(backups),
        )

    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        return StatusReport(
            id=installer_id,
            target_path=str(target),
            target_exists=True,
            state="not_installed",
            detail=f"读不了：{exc}",
            last_install_at=last_install_at,
            backup_count=len(backups),
        )

    classify = spec.classify_existing(content)

    # 如果我们记过 hash，对比下 — 用于检测外部改动
    # 走 _hash_target_for_drift：spec 声明自管段（如 codex 的 [windows]/[projects.*]）时
    # 这些段被剥离后再算 hash，避免 CLI 启动后正常写回触发误报。
    if written_hash and last_install_at:
        actual_hash = _hash_target_for_drift(target, spec)
        if actual_hash != written_hash and classify == "us":
            # 内容仍含我们标记，但 hash 变了——既不是 elsewhere 也不完全是 us
            return StatusReport(
                id=installer_id,
                target_path=str(target),
                target_exists=True,
                state="external_modified",
                detail="文件被外部改动过（仍指向 Gateway，但内容已变）；uninstall 会拒绝自动还原",
                last_install_at=last_install_at,
                backup_count=len(backups),
            )

    if classify == "us":
        return StatusReport(
            id=installer_id,
            target_path=str(target),
            target_exists=True,
            state="installed_pointing_to_us",
            detail="已接入柳如烟 Gateway",
            last_install_at=last_install_at,
            backup_count=len(backups),
        )
    if classify == "elsewhere":
        return StatusReport(
            id=installer_id,
            target_path=str(target),
            target_exists=True,
            state="installed_pointing_elsewhere",
            detail="该 CLI 已配置但指向其他后端；install 会先备份再接管",
            last_install_at=last_install_at,
            backup_count=len(backups),
        )
    return StatusReport(
        id=installer_id,
        target_path=str(target),
        target_exists=True,
        state="not_installed",
        detail="文件存在但未配置 endpoint；install 会备份后写入",
        last_install_at=last_install_at,
        backup_count=len(backups),
    )


# ─────────────────────── preview / install / uninstall ───────────────────────


class InstallerExtraInvalid(ValueError):
    """installer 的 extra 配置非法（如 codex 缺 default_model）。

    路由层捕获此异常返 400 + ``code`` 字段，前端据此弹错误提示。
    与一般 ValueError 区分，便于精确捕获。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def preview_install(
    installer_id: str,
    *,
    token: str,
    openai_base_url: str,
    anthropic_base_url: str,
    extra: Optional[Dict[str, Any]] = None,
) -> PreviewReport:
    spec = get_spec(installer_id)
    target = spec.target_resolver()
    new_content = spec.render_install_content(
        token, openai_base_url, anthropic_base_url, **(extra or {}),
    )

    target_exists = target.exists()
    current = ""
    if target_exists:
        try:
            current = target.read_text(encoding="utf-8")
        except OSError:
            current = ""

    state = _load_state().get("installers", {}).get(installer_id) or {}
    written_hash = state.get("written_hash") or ""
    actual_hash = _hash_target_for_drift(target, spec) if target_exists else ""
    external_modified = bool(written_hash) and bool(actual_hash) and actual_hash != written_hash

    will_modify = target_exists and current != new_content
    will_create = not target_exists

    diff_summary = _make_diff_summary(current, new_content)

    return PreviewReport(
        id=installer_id,
        action="install",
        target_path=str(target),
        target_exists=target_exists,
        will_create=will_create,
        will_modify=will_modify,
        will_backup_to=str(target) + ".lry-backup-<ts>" if target_exists else None,
        external_modified=external_modified,
        diff_summary=diff_summary,
    )


def install(
    installer_id: str,
    *,
    token: str,
    openai_base_url: str,
    anthropic_base_url: str,
    force: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> ActionResult:
    spec = get_spec(installer_id)
    target = spec.target_resolver()
    new_content = spec.render_install_content(
        token, openai_base_url, anthropic_base_url, **(extra or {}),
    )

    state = _load_state()
    entry = state["installers"].setdefault(installer_id, {})
    written_hash = entry.get("written_hash") or ""

    backup_path: Optional[Path] = None
    target.parent.mkdir(parents=True, exist_ok=True)

    with _state_lock:
        if target.exists():
            actual_hash = _hash_target_for_drift(target, spec)
            external_modified = bool(written_hash) and actual_hash != written_hash
            if external_modified and not force:
                return ActionResult(
                    ok=False,
                    message="目标文件被外部改动过；如确认覆盖请重试时传 force=true",
                    target_path=str(target),
                )
            try:
                backup_path = _backup_file(target)
            except Exception as exc:
                return ActionResult(ok=False, message=f"备份失败：{exc}", target_path=str(target))

        try:
            target.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return ActionResult(ok=False, message=f"写入失败：{exc}", target_path=str(target))

        # written_hash 用 drift-aware 版本：与 preview/install/uninstall 的比对函数保持对称，
        # 这样 CLI 启动后正常写回自管段时下一次 drift 检查仍能匹配。
        new_hash = _hash_target_for_drift(target, spec)
        entry.update({
            "target_path": str(target),
            "last_install_at": _now_iso(),
            "written_hash": new_hash,
            "backup_paths": (entry.get("backup_paths") or []) + (
                [str(backup_path)] if backup_path else []
            ),
        })
        _save_state(state)

    # 副作用 hook（codex 用来写 ~/.codex/auth.json，让 codex 不依赖 env 也能用）。
    # 主文件已落盘成功，hook 失败时回滚不现实——返 ok=True 但 message 里附警告。
    hook_warning = ""
    if spec.post_install_hook is not None:
        try:
            spec.post_install_hook(
                token, openai_base_url, anthropic_base_url, **(extra or {}),
            )
        except Exception as exc:  # noqa: BLE001 — hook 失败不应阻塞主流程
            hook_warning = f"；post_install_hook 失败：{exc}"

    return ActionResult(
        ok=True,
        message=f"已写入 {target}"
        + (f"；原文件备份至 {backup_path}" if backup_path else "")
        + hook_warning,
        backup_path=str(backup_path) if backup_path else None,
        target_path=str(target),
    )


def uninstall(installer_id: str, *, force: bool = False) -> ActionResult:
    """优先还原最近一次备份；无备份时 best-effort 剥离我们的字段。

    若文件被外部改动过（hash 不匹配），需 force=true 才执行。
    """
    spec = get_spec(installer_id)
    target = spec.target_resolver()
    state = _load_state()
    entry = state["installers"].get(installer_id) or {}
    written_hash = entry.get("written_hash") or ""
    backups: List[str] = list(entry.get("backup_paths") or [])

    def _run_post_uninstall() -> None:
        """uninstall 成功后调一次清理 hook。异常吞掉。"""
        if spec.post_uninstall_hook is not None:
            try:
                spec.post_uninstall_hook()
            except Exception:  # noqa: BLE001 — 副作用清理失败不上报
                pass

    with _state_lock:
        if target.exists():
            actual_hash = _hash_target_for_drift(target, spec)
            if written_hash and actual_hash != written_hash and not force:
                return ActionResult(
                    ok=False,
                    message="目标文件被外部改动过；自动还原会丢失你的改动。如确认请传 force=true",
                    target_path=str(target),
                )

        # 优先：还原最近一次备份
        last_backup: Optional[Path] = None
        while backups:
            candidate = Path(backups[-1])
            if candidate.exists():
                last_backup = candidate
                break
            backups.pop()  # 备份文件丢了 → 跳过

        if last_backup is not None:
            try:
                import shutil
                shutil.copy2(str(last_backup), str(target))
            except OSError as exc:
                return ActionResult(ok=False, message=f"还原失败：{exc}", target_path=str(target))

            # 已使用的备份从列表移除
            backups = backups[:-1]
            entry["backup_paths"] = backups
            entry["written_hash"] = ""
            entry["last_install_at"] = None
            _save_state(state)
            _run_post_uninstall()
            return ActionResult(
                ok=True,
                message=f"已还原 {target} 自备份 {last_backup}",
                target_path=str(target),
                backup_path=str(last_backup),
            )

        # 退化：best-effort 剥离我们的字段
        if target.exists():
            try:
                content = target.read_text(encoding="utf-8")
            except OSError as exc:
                return ActionResult(ok=False, message=f"读不了：{exc}", target_path=str(target))
            stripped = spec.render_uninstall_content(content)
            try:
                if stripped is None:
                    target.unlink()
                    msg = f"已删除 {target}（best-effort，无可用备份）"
                else:
                    target.write_text(stripped, encoding="utf-8")
                    msg = f"已剥离 Gateway 字段 from {target}（best-effort，无可用备份）"
            except OSError as exc:
                return ActionResult(ok=False, message=f"剥离失败：{exc}", target_path=str(target))
            entry["written_hash"] = ""
            entry["last_install_at"] = None
            _save_state(state)
            _run_post_uninstall()
            return ActionResult(ok=True, message=msg, target_path=str(target))

        # 文件本来就不在
        entry["written_hash"] = ""
        entry["last_install_at"] = None
        _save_state(state)
        _run_post_uninstall()
        return ActionResult(ok=True, message=f"{target} 不存在；状态已重置", target_path=str(target))


# ─────────────────────── helpers ───────────────────────


def _make_diff_summary(old: str, new: str, max_lines: int = 40) -> str:
    """简易 diff 摘要：行级 +/- 标记，截断到 max_lines。"""
    import difflib
    if old == new:
        return "(无变化)"
    diff = list(difflib.unified_diff(
        old.splitlines(keepends=False),
        new.splitlines(keepends=False),
        fromfile="current",
        tofile="will-write",
        lineterm="",
    ))
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... (省略 {len(diff) - max_lines} 行)"]
    return "\n".join(diff)


# ════════════════════════════════════════════════════════════════════
# 具体 installer：claude_code / codex
# ════════════════════════════════════════════════════════════════════


# ─────────── Claude Code ───────────


_LRY_MARKER = "managed-by-liuruyan-gateway"


def _claude_target() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _is_invalid_anthropic_default_model(value: Any) -> bool:
    """判 ANTHROPIC_DEFAULT_*_MODEL* 字段值是否非法（不能直接发给 Anthropic / 柳如烟 gateway）。

    柳如烟 gateway 接受的 model 字段格式：
      - `<provider_id>:<vendor_model_id>` 命名空间
      - `<vendor_model_id>` 裸名（用 current_provider）
      - `<alias_name>`

    Claude Code 直连官方 Anthropic 时接受 `claude-sonnet-4-XXXXXXXX` 这种官方 model id。

    无论哪种合法形态都不会含 `[` `]` `{` `}` 空格 中文等字符。这里就用最简单的"含
    特殊字符 = 非法"启发式扫坏字段（典型坏值：`custom-xxx:claude-opus-4-7[1m]-thinking`）。

    回归 2026-05-23 用户报告："一键接入后配置文件又变成 [1m]-thinking 模型名"——根因
    是 install 时 _claude_render_install 保留了 base_existing 的所有 env 字段，连带把
    用户手填的非法值也保留下来。这里加一道清扫，render 时主动剥这些值。
    """
    if not isinstance(value, str):
        return True
    # 任何这些字符都不会出现在合法的 provider:model / Anthropic 官方 model id / alias name 里
    for ch in "[]{}() ":
        if ch in value:
            return True
    # 也不允许中文等非 ASCII（model id 都是 ASCII）
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return True
    return False


def _claude_render_install(
    token: str,
    _openai_base: str,
    anthropic_base: str,
    *,
    default_models: Optional[Dict[str, Any]] = None,
    mode: str = "gateway",
    provider_name: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    **_unused: Any,
) -> str:
    """Claude Code 配置：env 字段在所有 claude session 里被设为 ANTHROPIC_BASE_URL/AUTH_TOKEN。

    两种模式：

    - ``mode="gateway"``（默认）：指向柳如烟本地 Gateway（/gateway/anthropic，
      token = gateway token）。别名 / fallback / 协议互转 / 用量统计全保留。
    - ``mode="direct"``：直连 provider store 里某个 anthropic_messages 服务商。
      路由层把 provider 的 base_url（已剥末尾 /v1）/ api_key 分别作为
      anthropic_base / token 传入；provider 的 extra_headers 写成
      ANTHROPIC_CUSTOM_HEADERS（Claude Code 官方 env，格式 "k: v" 逗号分隔）。

    我们尽量保留用户已有的非 env 字段（先读，再 merge）。但因为 install 路径里我们已先备份，
    这里用的是新生成完整 settings.json：仅放 env + 一个 _comment 标记。

    用户原有的其他字段会丢吗？是的——但备份保留了，uninstall 一键还原。
    保守不丢字段需要更复杂的合并逻辑（涉及 schema 演进），M5 选简洁路径。

    清扫机制（2026-05-23 加）：保留 env 时主动剥掉 ANTHROPIC_DEFAULT_*_MODEL* 中明显
    非法的值（含 `[` `{` 空格 / 中文等字符），防止用户手填的 `claude-opus-4-7[1m]-thinking`
    类垃圾被反复保留。详见 _is_invalid_anthropic_default_model 的 docstring。

    模型分配（2026-05-23 加，ccswitch 风格）：
      `default_models = {"haiku": "...", "sonnet": "...", "opus": "..."}`
      网关模式值为合法 `<provider:model>` / alias 名；直连模式路由层已剥掉
      `<provider_id>:` 前缀（Claude Code 直连时必须是 vendor 裸 model id）。
      写到 env 的 ANTHROPIC_DEFAULT_HAIKU_MODEL / SONNET_MODEL / OPUS_MODEL 字段。
      值为 None / "" / 非法字符时，跳过该字段（让 Claude Code 用内置默认）。
    """
    target = _claude_target()
    base_existing: Dict[str, Any] = {}
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base_existing = data
        except (OSError, ValueError):
            pass

    # 不破坏用户已设的其他字段；只覆盖 env + _comment
    new_data = dict(base_existing)
    existing_env = new_data.get("env") if isinstance(new_data.get("env"), dict) else {}
    new_env: Dict[str, Any] = {}
    for k, v in existing_env.items():
        # 清扫：ANTHROPIC_DEFAULT_*_MODEL* 字段中含非法字符的值剥掉（2026-05-23 加）
        if k.startswith("ANTHROPIC_DEFAULT_") and "MODEL" in k:
            if _is_invalid_anthropic_default_model(v):
                continue  # 跳过 = 写出去的 settings.json 不含此字段
        new_env[k] = v
    new_env["ANTHROPIC_BASE_URL"] = anthropic_base
    new_env["ANTHROPIC_AUTH_TOKEN"] = token
    # 直连模式：provider 的 extra_headers 写成 Claude Code 官方支持的
    # ANTHROPIC_CUSTOM_HEADERS（"Name: Value" 逗号分隔）。
    # 网关模式：剥掉历史遗留值，避免直连切回网关后仍发旧 header。
    if mode == "direct" and extra_headers:
        cleaned_headers = {str(k): str(v) for k, v in extra_headers.items() if str(k).strip()}
        if cleaned_headers:
            new_env["ANTHROPIC_CUSTOM_HEADERS"] = ", ".join(
                f"{k}: {v}" for k, v in cleaned_headers.items()
            )
        else:
            new_env.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    else:
        new_env.pop("ANTHROPIC_CUSTOM_HEADERS", None)

    # 应用用户在前端面板里为 Claude Code 三个角色（haiku/sonnet/opus）选的柳如烟 model。
    # 合法值才写；空 / 非法 → 跳过该字段（Claude Code 用内置默认 model id，柳如烟需 alias 兜底）
    _ROLE_TO_ENV = {
        "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    }
    if default_models:
        for role, env_key in _ROLE_TO_ENV.items():
            val = default_models.get(role)
            if val and isinstance(val, str) and not _is_invalid_anthropic_default_model(val):
                new_env[env_key] = val
            # 用户改了配置：先把旧的 *_MODEL_NAME 历史字段也剥掉，避免老 Claude Code 同时读两组冲突
            new_env.pop(env_key + "_NAME", None)
        # 清除全局 ANTHROPIC_MODEL 覆盖，防止它硬编码抢占刚刚设置的角色模型（如 sonnet / fable）
        new_env.pop("ANTHROPIC_MODEL", None)

    new_data["env"] = new_env
    # 顶层 model 字段：claude code alias（如 "sonnet"），同样含 `[` 等字符即为非法（典型："sonnet[1m]"）
    top_model = new_data.get("model")
    if isinstance(top_model, str) and any(ch in top_model for ch in "[]{}() "):
        # 剥掉非法值，让 claude code 用内置默认（一般是 sonnet）
        new_data.pop("model", None)
    new_data["_comment"] = (
        f"{_LRY_MARKER}: 此文件由柳如烟 Gateway 一键接入写入（"
        + (f"直连 {provider_name}" if mode == "direct" and provider_name else "网关模式")
        + "）。uninstall 会还原最近备份。手工编辑前先在柳如烟里点'卸载'。"
    )
    return json.dumps(new_data, ensure_ascii=False, indent=2) + "\n"


def _claude_classify(content: Optional[str]) -> str:
    if not content:
        return "unknown"
    try:
        data = json.loads(content)
    except ValueError:
        return "unknown"
    if not isinstance(data, dict):
        return "unknown"
    comment = data.get("_comment") or ""
    if isinstance(comment, str) and _LRY_MARKER in comment:
        return "us"
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    if env.get("ANTHROPIC_BASE_URL") or env.get("ANTHROPIC_AUTH_TOKEN"):
        return "elsewhere"
    return "unknown"


def _claude_render_uninstall(content: str) -> Optional[str]:
    try:
        data = json.loads(content)
    except ValueError:
        return content
    if not isinstance(data, dict):
        return content
    if "_comment" in data and isinstance(data["_comment"], str) and _LRY_MARKER in data["_comment"]:
        del data["_comment"]
    env = data.get("env")
    if isinstance(env, dict):
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        # 直连模式写入的自定义 header 一并清掉
        env.pop("ANTHROPIC_CUSTOM_HEADERS", None)
        if not env:
            data.pop("env", None)
    if not data:
        return None
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


register_installer(_InstallerSpec(
    info=InstallerInfo(
        id="claude_code",
        name="Claude Code",
        description="Anthropic 官方编程 CLI；写 ~/.claude/settings.json。可选 haiku/sonnet/opus 角色模型；支持网关 / 直连服务商两种模式",
        protocol="anthropic_messages",
        target_path=str(_claude_target()),
    ),
    target_resolver=_claude_target,
    render_install_content=_claude_render_install,
    classify_existing=_claude_classify,
    render_uninstall_content=_claude_render_uninstall,
))


# ─────────── Codex CLI ───────────


def _codex_target() -> Path:
    return Path.home() / ".codex" / "config.toml"


_CODEX_PROVIDER_KEY = "liuruyan-gateway"
_CODEX_DIRECT_PROVIDER_KEY = "liuruyan-direct"
_CODEX_REASONING_DEFAULT = "high"


def _toml_escape(s: str) -> str:
    """TOML 基本字符串转义（反斜杠 + 双引号）。足够覆盖 URL / header 值场景。"""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _toml_inline_table(pairs: Dict[str, str]) -> str:
    """渲染 TOML inline table：{ "k1" = "v1", "k2" = "v2" }。"""
    items = ", ".join(f'"{_toml_escape(k)}" = "{_toml_escape(v)}"' for k, v in pairs.items())
    return f"{{{items}}}"

# 这些段名对应"codex 自己写回的用户决策"，安装时 capture & 保留：
# - [notice]   = codex 写的 hide_*_warning（dismiss 过的提示）
# - [windows]  = sandbox = "elevated"/"unelevated"（onboarding 选过的沙盒模式）
# - [projects.*] = trust_level = "trusted"（已信任的工作目录）
# - [tui.*]    = model_availability_nux 计数等 TUI 内部状态
# 对齐 ccswitch 的 capture-on-write 行为（services/config.rs:167-178）。
_CODEX_PRESERVED_SECTION_PREFIXES: tuple[str, ...] = (
    "[notice]",
    "[notice.",
    "[windows]",
    "[windows.",
    "[projects.",
    "[tui]",
    "[tui.",
)


def _codex_capture_user_decision_sections(target: Path) -> dict[str, str]:
    """从现有 ``~/.codex/config.toml`` 中提取用户决策类段的原文本。

    返回 ``{section_header: section_text}``，section_text 含 header 行 + body
    （到下一个 ``[...]`` 段头或 EOF），尾部已 rstrip 换行。

    设计目的：让柳如烟卸载/重装不再丢失用户首次 onboarding 选过的 sandbox 模式
    与目录 trust 决定——避免每次重装都必须重走 onboarding 触发"No active thread"
    红色 race（详见 ``codex-rs/tui/src/app/event_dispatch.rs:1189-1280``）。

    采用纯文本切片而非 ``tomllib`` parse + 手写 dumper：
    1. 不需要外部依赖（``tomllib`` 只读 / ``tomli_w`` 不在依赖里）
    2. 保留 codex 写盘的原始格式（含注释 / 空行 / 引号风格）
    3. 段名含特殊字符（如 ``[projects.'g:\\ai\\xxx']``）时不需自己处理转义
    """
    if not target.exists():
        return {}
    try:
        text = target.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    header_re = re.compile(r"^(\[[^\]\n]+\])\s*$", re.MULTILINE)
    matches = list(header_re.finditer(text))
    captured: dict[str, str] = {}
    for i, m in enumerate(matches):
        header = m.group(1).strip()
        if not any(
            header == prefix or header.startswith(prefix)
            for prefix in _CODEX_PRESERVED_SECTION_PREFIXES
        ):
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        captured[header] = text[start:end].rstrip("\n")
    return captured


def _codex_render_install(
    token: str,
    openai_base: str,
    _ant_base: str,
    *,
    default_model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    mode: str = "gateway",
    wire_api: Optional[str] = None,
    provider_name: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    **_unused: Any,
) -> str:
    """Codex CLI 配置（~/.codex/config.toml）。

    两种模式：

    - ``mode="gateway"``（默认）：指向柳如烟本地 Gateway（openai_base =
      http://localhost:<port>/gateway/v1，token = gateway token）。走 /responses
      翻译层，别名 / fallback / 用量统计全都保留。
    - ``mode="direct"``：直连 provider store 里某个 OpenAI 兼容服务商（如千刀）。
      路由层会把 provider 的 base_url / api_key 分别作为 openai_base / token
      传进来，本函数只负责渲染；wire_api 固定 ``responses``（codex 0.153+
      已移除 chat wire 支持，见 openai/codex#7782）。
      provider 的 extra_headers 写成 ``http_headers`` inline table。

    模板对齐 ccswitch ``generateThirdPartyConfig`` 风格（references/gateway-env
    src/config/codexProviderPresets.ts）：

    - ``model = "<alias 或 provider:model>"`` 顶层必填 —— codex 默认 ``gpt-5-codex``
      会让 ``resolve_chain`` 404 → TUI 报"No active thread is available"
      （direct 模式下 model 必须是 vendor 裸 model id，路由层负责剥 prefix）
    - ``disable_response_storage = true`` —— codex 0.30+ store=true 让 OpenAI 服务端
      存 conversation；柳如烟翻译层无状态，**必须关掉**否则 thread/start 失败
    - ``requires_openai_auth = true`` —— 强制走 OPENAI_API_KEY，**不**回退查
      ~/.codex/auth.json 的 ChatGPT 登录态（避免用错 token）
    - ``model_reasoning_effort`` —— 默认 high；codex 据此决定深度

    token 不写到 config.toml 里（明文落盘风险）；通过 ``post_install_hook`` 写到
    ~/.codex/auth.json，codex 启动时自动读取（direct 模式下即 provider 的 api_key）。
    """
    if not isinstance(default_model, str) or not default_model.strip():
        raise InstallerExtraInvalid(
            code="codex_default_model_required",
            message=(
                "codex installer 需要必填 `default_model`：填一个柳如烟里的 alias 名"
                "或 `<provider_id>:<vendor_model>` 命名空间格式（前端会列出可选项）。"
            ),
        )
    model = default_model.strip()
    effort = (reasoning_effort or _CODEX_REASONING_DEFAULT).strip().lower()
    if effort not in ("minimal", "low", "medium", "high"):
        effort = _CODEX_REASONING_DEFAULT

    direct = mode == "direct"
    provider_key = _CODEX_DIRECT_PROVIDER_KEY if direct else _CODEX_PROVIDER_KEY
    # wire_api：gateway 模式固定 responses（翻译层实现的就是 Responses 协议）；
    # direct 模式也固定 responses——codex 0.153+ 已移除 chat wire 支持
    # （openai/codex#7782），传 chat 会被 codex 拒载。保留参数仅为向后兼容旧请求。
    wire = "responses"
    if wire_api == "responses":
        wire = wire_api
    display_name = _toml_escape((provider_name or "").strip()) or (
        "Liuruyan Direct Provider" if direct else "Liuruyan Gateway"
    )
    headers_line = ""
    if direct and extra_headers:
        cleaned_headers = {str(k): str(v) for k, v in extra_headers.items() if str(k).strip()}
        if cleaned_headers:
            headers_line = f"http_headers = {_toml_inline_table(cleaned_headers)}\n"

    # 注意：**不要**写 ``env_key = "OPENAI_API_KEY"``。
    # 实测 codex 0.30+ 同时设 env_key + requires_openai_auth=true 会出现 auth 路径冲突，
    # 表现为 ``thread/start`` 失败 / TUI 报"No active thread is available"。
    # ccswitch 模板也不写 env_key，token 完全走 ~/.codex/auth.json（post_install_hook 落盘）。
    #
    # capture-on-install 策略（对齐 ccswitch services/config.rs:167-178）：
    # 安装前先读 ~/.codex/config.toml，把 [notice] / [windows] / [projects.*] / [tui.*]
    # 等"用户决策类"段原样保留——避免卸载/重装时丢失用户首次 onboarding 选过的
    # sandbox 模式与目录 trust，每次重装都不必再重走 onboarding 触发红色 race。
    #
    # 全新用户（capture 为空）走唯一默认值：
    # - [windows] sandbox = "elevated" → WindowsSandboxLevel::Elevated ≠ Disabled
    #   → should_prompt_windows_sandbox_nux_at_startup 短路 false → 跳过 sandbox onboarding
    #   → 不派发 EnableWindowsSandboxForAgentMode → 不抢跑 AppEvent::CodexOp
    #   → 永久消除首次目录 "No active thread is available." 红色
    #   （详见 codex-rs/tui/src/app/event_dispatch.rs:1189-1280 + lib.rs:1666-1668）
    #
    # 选 elevated 而非 unelevated 的理由（codex 自身的安全语义）：
    # - elevated   = "Set up default sandbox (requires Administrator permissions)"
    #                ← codex 自己叫这个 "default sandbox"，基于 Windows Sandbox 服务，完全隔离
    # - unelevated = "Use non-admin sandbox (higher risk if prompt injected)"
    #                ← codex 自己警告这个 "higher risk"，仅靠 RestrictedToken 降权，prompt 注入下脆弱
    # 启动期都不触发 NUX（windows_sandbox_prompts.rs:435-444 双重 Disabled 闸），但
    # elevated 在用户首次发消息时若沙盒服务未就绪会引导用户装服务（readiness=UpdateRequired
    # 不会塞 startup popup，只在真正执行 agent 命令时按需提示）——这是预期且更安全的体验。
    #
    # 注意：[notice] hide_*_warning 实测对红色 race **无效**（红色派发路径在 sandbox
    # onboarding handler 里，跟 world_writable / full_access 警告分支无关）。所以
    # 不主动注入默认 [notice]，仅在用户曾经 dismiss 过时由 capture 路径保留。
    captured = _codex_capture_user_decision_sections(_codex_target())
    windows_block = captured.pop(
        "[windows]",
        '[windows]\nsandbox = "elevated"',
    )
    # 其余 captured 段（[notice] / [projects.*] / [tui.*] / [notice.*] / [windows.*]）按原样追加。
    # 稳定排序便于 diff（按 header 字典序）。
    # 设计纪律：不替 codex 做决定——[notice] 若在备份里残留（历史柳如烟模板曾写过），
    # 我们 capture 时如实保留；要"干净"的话用户自己在 codex 里管理或手动改 config.toml。
    # 这符合 capture-on-install 的中性语义："用户决策原样回写，柳如烟不静态删任何段"。
    extra_blocks = "\n\n".join(
        captured[header] for header in sorted(captured)
    )
    extra_section_text = f"\n\n{extra_blocks}\n" if extra_blocks else "\n"
    mode_comment = (
        f'# 接入模式：直连（direct）—— codex 直接访问 {display_name}，不经柳如烟本地转发。\n'
        f'# 别名 / fallback / 用量统计等 Gateway 能力在此模式下不可用。\n'
        if direct else
        f'# 接入模式：网关（gateway）—— codex 经柳如烟本地 Gateway 转发，\n'
        f'# 支持别名 / fallback / 用量统计。\n'
    )
    return (
        f'# {_LRY_MARKER}: 此文件由柳如烟 Gateway 一键接入写入。\n'
        f'# uninstall 会还原最近备份。手工编辑前先在柳如烟里点"卸载"。\n'
        f'#\n'
        f'{mode_comment}'
        f'# token 不在此文件里——见 ~/.codex/auth.json（同样由 Gateway 写入）。\n'
        f'# disable_response_storage / requires_openai_auth 是 codex 0.30+ 接第三方 base 必备。\n'
        f'# 不要加 env_key —— 与 requires_openai_auth 冲突会让 thread/start 失败。\n'
        f'# [windows] / [projects.*] / [tui.*] / [notice] 段由 capture-on-install 保留，\n'
        f'# 删 [windows] 会让用户首次进新目录时看到 "No active thread is available." 红字。\n'
        f'\n'
        f'model_provider = "{provider_key}"\n'
        f'model = "{model}"\n'
        f'model_reasoning_effort = "{effort}"\n'
        f'disable_response_storage = true\n'
        f'\n'
        f'[model_providers.{provider_key}]\n'
        f'name = "{display_name}"\n'
        f'base_url = "{_toml_escape(openai_base)}"\n'
        f'wire_api = "{wire}"\n'
        f'requires_openai_auth = true\n'
        f'{headers_line}'
        f'\n'
        f'{windows_block}'
        f'{extra_section_text}'
    )


def _codex_auth_json_path() -> Path:
    return Path.home() / ".codex" / "auth.json"


def _codex_post_install_hook(
    token: str,
    _openai_base: str,
    _ant_base: str,
    **_unused: Any,
) -> None:
    """install 主流程后写 ~/.codex/auth.json。

    codex 0.30+ 在 ``requires_openai_auth = true`` 时优先读这里的 OPENAI_API_KEY，
    让用户随时启动 codex（不必经柳如烟的启动命令注入 env）都能用。

    原 auth.json 若存在先备份（codex login 过的用户会有 ChatGPT 登录态在这里）。
    """
    auth_path = _codex_auth_json_path()
    auth_path.parent.mkdir(parents=True, exist_ok=True)

    if auth_path.exists():
        try:
            _backup_file(auth_path)
        except OSError:
            # 备份失败不阻塞——auth.json 一般也就 token 本身
            pass

    payload = {"OPENAI_API_KEY": token}
    tmp = auth_path.with_suffix(auth_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, auth_path)


def _codex_post_uninstall_hook() -> None:
    """uninstall 后清掉 auth.json。用户重装 codex login 时不会被旧 token 干扰。"""
    auth_path = _codex_auth_json_path()
    if auth_path.exists():
        try:
            auth_path.unlink()
        except OSError:
            pass


def _codex_classify(content: Optional[str]) -> str:
    if not content:
        return "unknown"
    if _LRY_MARKER in content:
        return "us"
    if "model_provider" in content or "model_providers" in content or "base_url" in content:
        return "elsewhere"
    return "unknown"


def _codex_render_uninstall(content: str) -> Optional[str]:
    """Best-effort：删整个文件（无备份的兜底）。"""
    return None


register_installer(_InstallerSpec(
    info=InstallerInfo(
        id="codex",
        name="OpenAI Codex CLI",
        description="OpenAI 官方编程 CLI（codex 0.30+）；写 ~/.codex/config.toml + auth.json。需选默认 model；支持网关 / 直连服务商两种模式。",
        protocol="openai_chat",
        target_path=str(_codex_target()),
    ),
    target_resolver=_codex_target,
    render_install_content=_codex_render_install,
    classify_existing=_codex_classify,
    render_uninstall_content=_codex_render_uninstall,
    post_install_hook=_codex_post_install_hook,
    post_uninstall_hook=_codex_post_uninstall_hook,
    # codex 启动后会动态写回这些段（sandbox 决策 / 目录 trust / nux 计数 / dismiss
    # 状态），全部从 drift hash 中剔除——否则 codex 跑一会儿就会让 ~/.codex/config.toml
    # 的 hash 与 install 时记录的对不上，下次卸载报 "目标文件被外部改动过" 误警告。
    # 与 _codex_capture_user_decision_sections 的前缀清单保持一致。
    auto_managed_section_prefixes=_CODEX_PRESERVED_SECTION_PREFIXES,
))


__all__ = [
    "InstallerInfo",
    "StatusReport",
    "PreviewReport",
    "ActionResult",
    "list_installers",
    "get_status",
    "preview_install",
    "install",
    "uninstall",
]
