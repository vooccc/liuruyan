"""Skill Pack 管理器 — 兼容 Anthropic Claude Code Plugin 协议的插件管理。

核心能力：
- install_pack：从 GitHub / git URL / git-subdir 克隆插件到 `{DATA}/skills/<pack>/`
- update_pack：git pull --ff-only，同时刷新 Python 子 venv 依赖
- uninstall_pack：删目录
- list_packs：扫 skills/ 列出所有已安装 pack
- pack_status：单 pack 详情（版本、venv 健康度）

设计取舍：
- 每个 pack 一个独立 venv（`<pack>/.venv/`），不污染柳如烟主 venv，卸载干净
- 跟着 GitHub 走，不维护私有 mirror；用户离线时已有 git clone 仍可用
- subprocess 调 git / pip，不引入新 Python 依赖
- skill name 用 `__` 分隔，避免冲突现有 39 个单层 skill

参考：`_docs/skill_marketplace_research.md` § 4-5
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths


# ────────────────────────────────────────────────────────────────────────────
# 元数据 / 常量
# ────────────────────────────────────────────────────────────────────────────

# 写在每个 pack 根目录的柳如烟内部元信息（区别于上游 `.claude-plugin/plugin.json`）
LIURUYAN_META_FILE = ".liuruyan-pack.json"

# 安装时拒绝的危险目录名（防 path traversal / 覆盖系统目录）
_FORBIDDEN_NAMES = {"", ".", "..", "__pycache__", ".git", ".venv", "node_modules"}

# pack name 合法字符（紧贴 npm package name 子集 + 横线/下划线）
_VALID_PACK_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


# ────────────────────────────────────────────────────────────────────────────
# 异常类型
# ────────────────────────────────────────────────────────────────────────────

class SkillPackError(Exception):
    """所有 pack 管理操作的统一异常类型。"""


# ────────────────────────────────────────────────────────────────────────────
# 辅助：路径 / 命令运行
# ────────────────────────────────────────────────────────────────────────────

def _packs_root() -> Path:
    """所有 pack 安装的根目录 = 现有 skills_dir。"""
    return paths.skills_dir()


def pack_dir(pack_name: str) -> Path:
    """给一个 pack name 返回它在 skills/ 下的目录路径（不保证存在）。"""
    if not _VALID_PACK_NAME.match(pack_name):
        raise SkillPackError(
            f"非法 pack 名：{pack_name!r}。允许 [a-z0-9._-]，长度 1-64。"
        )
    if pack_name in _FORBIDDEN_NAMES:
        raise SkillPackError(f"禁用名：{pack_name!r}")
    return _packs_root() / pack_name


def _run(
    cmd: List[str],
    cwd: Optional[Path] = None,
    timeout: int = 600,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """执行命令，返回 (returncode, stdout, stderr)。

    - timeout 默认 10 分钟（git clone / pip install 大仓库可能慢）
    - 失败不抛异常，由调用方决定怎么处理
    - stdout/stderr 都解码为 utf-8（替换非法字节）
    """
    try:
        run_kwargs = {
            "cwd": str(cwd) if cwd else None,
            "capture_output": True,
            "timeout": timeout,
            "env": env,
        }
        if os.name == "nt":
            run_kwargs["creationflags"] = 0x08000000
        proc = subprocess.run(cmd, **run_kwargs)
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"command timed out after {timeout}s"
    except FileNotFoundError as exc:
        return -1, "", f"command not found: {exc}"


def _git_available() -> bool:
    rc, _, _ = _run(["git", "--version"], timeout=10)
    return rc == 0


# ── Windows 友好的目录处理 ─────────────────────────────────────────────────────
# git clone 下来的 `.git/objects/pack/*.idx` 在 Windows 上是只读的，
# shutil.rmtree 默认会 WinError 5。同时 Windows Defender / 索引服务偏爱
# 在 git 完成后临时锁住 .git/，让 os.rename 颙不住。
# 这两个问题加起来是本项目装过一次后走上变 Windows-flavor 的必经之路。

def _force_rmtree(path: Path, retries: int = 3, sleep: float = 0.3) -> None:
    """重试 + 去只读位 双保险的 rmtree。

    历史坑：之前 onerror 回调内 `func(p)` 失败被 `except Exception: pass` 吞了，
    导致 shutil.rmtree 以为都删成了不抛异常，`last_exc` 永远是 None，
    函数全部 retry 走完后付出偶尔还剩文件但静默返回“成功”。现在改为：
    onerror 内部将异常记入 nonlocal `last_exc`；retry 结束后只要目录还在就 raise。
    """
    if not Path(path).exists():
        return

    last_exc: Optional[BaseException] = None

    def _on_rm_error(func, p, exc_info):
        nonlocal last_exc
        try:
            os.chmod(p, stat.S_IWRITE)
        except Exception:
            pass
        try:
            func(p)
        except Exception as exc:  # noqa: BLE001
            # 记住原始异常，不要吞；外层 retry 后统一拋
            last_exc = exc

    for _ in range(retries):
        try:
            shutil.rmtree(path, onerror=_on_rm_error)
            if not Path(path).exists():
                return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(sleep)
    # 仍有资源反锁 → 报错出去让上层处理
    if Path(path).exists():
        raise last_exc or RuntimeError(f"_force_rmtree 清理失败，目录还存在: {path}")


def _atomic_move_dir(src: Path, dst: Path, retries: int = 3, sleep: float = 0.4) -> None:
    """尽量原子地把目录 src 移到 dst。

    Windows 上 os.rename 被 Defender / antivirus / git 后台 hook 临时锁住是常态，
    所以 retry 几次；实在不行才退到 copytree + rmtree（代价是多一次 IO，但能走通）。
    """
    src, dst = Path(src), Path(dst)
    last_exc: Optional[BaseException] = None
    for _ in range(retries):
        try:
            os.rename(src, dst)
            return
        except (PermissionError, OSError) as exc:
            last_exc = exc
            time.sleep(sleep)
    # rename 走不通，copytree 兑一次
    if dst.exists():
        _force_rmtree(dst)
    shutil.copytree(
        src,
        dst,
        symlinks=False,
        # ignore_dangling_symlinks 避免 Windows + git lfs 里面中头符链问题
        ignore_dangling_symlinks=True,
    )
    _force_rmtree(src)
    # copytree 后验证存在
    if not dst.exists():
        raise last_exc or RuntimeError(f"move {src} -> {dst} 失败")


# ────────────────────────────────────────────────────────────────────────────
# Source 解析（兼容 Claude Code Plugin source schema）
# ────────────────────────────────────────────────────────────────────────────

def _normalize_source(raw: Any) -> Dict[str, Any]:
    """把多种 source 输入归一成 dict 形态。

    支持：
    - "owner/repo"             → {source:'github', repo:'owner/repo'}
    - "https://github.com/..." → {source:'url', url:...}
    - {source:'github', ...}   → 原样
    - {source:'url', ...}      → 原样
    - {source:'git-subdir', url, path, ...} → 原样

    抛 SkillPackError 表示输入无法识别。
    """
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise SkillPackError("source 为空字符串")
        # github 简写
        if re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", s):
            return {"source": "github", "repo": s}
        # 完整 URL（http/https/git@）
        if s.startswith(("http://", "https://", "git@", "git://")):
            return {"source": "url", "url": s}
        raise SkillPackError(f"无法识别的 source 字符串：{s!r}")

    if isinstance(raw, dict):
        kind = raw.get("source") or raw.get("type")
        if kind == "github":
            repo = raw.get("repo")
            if not repo or "/" not in repo:
                raise SkillPackError("github source 缺少 repo='owner/name'")
            return {
                "source": "github",
                "repo": repo,
                "ref": raw.get("ref"),
                "path": raw.get("path"),  # 可选 subdir，扩展用
            }
        if kind in ("url", "git"):
            url = raw.get("url")
            if not url:
                raise SkillPackError("url source 缺少 url")
            return {
                "source": "url",
                "url": url,
                "ref": raw.get("ref"),
                "path": raw.get("path"),
            }
        if kind == "git-subdir":
            url = raw.get("url")
            sub = raw.get("path")
            if not url or not sub:
                raise SkillPackError("git-subdir source 缺少 url 或 path")
            return {
                "source": "git-subdir",
                "url": url,
                "path": sub,
                "ref": raw.get("ref"),
            }
        raise SkillPackError(f"未知的 source 类型：{kind!r}")

    raise SkillPackError(f"source 必须是字符串或 dict，收到 {type(raw).__name__}")


def _source_to_clone_url(src: Dict[str, Any]) -> str:
    """把 normalized source 转为 git clone 用的 URL。"""
    kind = src["source"]
    if kind == "github":
        return f"https://github.com/{src['repo']}.git"
    if kind in ("url", "git-subdir"):
        return src["url"]
    raise SkillPackError(f"无法转 clone URL：{kind}")


# ────────────────────────────────────────────────────────────────────────────
# Plugin manifest 探测
# ────────────────────────────────────────────────────────────────────────────

def _find_plugin_manifest(root: Path, max_depth: int = 2) -> Optional[Path]:
    """在 root 下找 `.claude-plugin/plugin.json`，深度有限。

    搜索顺序：
    1. root/.claude-plugin/plugin.json
    2. root/<single-subdir>/.claude-plugin/plugin.json   (max_depth=2 时)

    超过深度仍未找到 → None，由调用方判断。

    设计原因：webnovel-writer 等 plugin 在仓库根目录套了一层同名子目录
    （`webnovel-writer-master/webnovel-writer/.claude-plugin/`），需要兜底。
    但深度过大会把性能搞垮（一个 monorepo 可能套 5 层），所以默认 2。
    """
    candidate = root / ".claude-plugin" / "plugin.json"
    if candidate.exists():
        return candidate
    if max_depth <= 1:
        return None
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        if sub.name.startswith(".") or sub.name in {"node_modules", "__pycache__"}:
            continue
        nested = _find_plugin_manifest(sub, max_depth=max_depth - 1)
        if nested is not None:
            return nested
    return None


def _read_plugin_manifest(manifest_path: Path) -> Dict[str, Any]:
    """读 plugin.json，失败抛 SkillPackError。"""
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise SkillPackError(f"plugin.json 解析失败 ({manifest_path}): {exc}")


# ────────────────────────────────────────────────────────────────────────────
# Python 子 venv 管理
# ────────────────────────────────────────────────────────────────────────────

def _venv_python(pack_root: Path) -> Path:
    """返回 pack 子 venv 的 python 解释器路径（不保证存在）。"""
    if os.name == "nt":
        return pack_root / ".venv" / "Scripts" / "python.exe"
    return pack_root / ".venv" / "bin" / "python"


def _create_venv(pack_root: Path) -> Tuple[bool, str]:
    """在 pack 根目录创建 .venv/。已存在则跳过。

    历史坑：Windows 上某些 Python 安装（特别是从源码编译或迁移过的）跱
    `python -m venv` 后不会 bootstrap pip，导致 `<venv>/Scripts/python.exe -m pip` 报
    `No module named pip`。需要面补一句 `python -m ensurepip --upgrade`。
    """
    venv_dir = pack_root / ".venv"
    if venv_dir.exists():
        return True, "已存在"
    rc, out, err = _run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        timeout=300,
    )
    if rc != 0:
        return False, f"venv 创建失败: {err.strip() or out.strip()}"

    # bootstrap pip：防 Windows 某些 Python 安装里 venv 不自动带 pip
    py = _venv_python(pack_root)
    if not py.exists():
        return False, f"venv 创建后 python 不存在: {py}"
    rc2, out2, err2 = _run(
        [str(py), "-m", "ensurepip", "--upgrade"],
        timeout=120,
    )
    if rc2 != 0:
        return False, (
            f"venv 已建但 ensurepip 失败，依赖装不了: "
            f"{err2.strip() or out2.strip()}"
        )
    return True, "创建成功 (含 pip bootstrap)"


def _pip_install_requirements(
    pack_root: Path,
    plugin_root: Path,
) -> Tuple[bool, str]:
    """如果 plugin_root（或 pack_root）下有 requirements.txt，用子 venv 装。

    搜索顺序：
    - plugin_root/requirements.txt
    - plugin_root/scripts/requirements.txt    （webnovel-writer 实际位置）
    - pack_root/requirements.txt              （wrapper 仓库根）

    没找到任何 requirements.txt → 直接成功（不是错误）。
    """
    candidates = [
        plugin_root / "requirements.txt",
        plugin_root / "scripts" / "requirements.txt",
        pack_root / "requirements.txt",
    ]
    req_files = [p for p in candidates if p.exists()]
    if not req_files:
        return True, "无 requirements.txt，跳过"

    py = _venv_python(pack_root)
    if not py.exists():
        return False, f"venv python 不存在: {py}"

    # 先升级 pip / setuptools（避免老 pip 装新版本包失败）
    _run(
        [str(py), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
        timeout=300,
    )

    msgs: List[str] = []
    for req in req_files:
        rc, out, err = _run(
            [str(py), "-m", "pip", "install", "-r", str(req)],
            timeout=900,  # 大依赖可能装很久
        )
        if rc != 0:
            return False, f"pip install -r {req.name} 失败: {err.strip()[:500]}"
        msgs.append(f"✓ {req.name} 已装")
    return True, "; ".join(msgs)


# ────────────────────────────────────────────────────────────────────────────
# 元数据：.liuruyan-pack.json
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class PackMeta:
    """柳如烟内部 pack 元数据（写在 `<pack>/.liuruyan-pack.json`）。"""
    pack_name: str
    source: Dict[str, Any]                 # normalized source
    plugin_root_relative: str = "."        # plugin.json 所在子路径（相对 pack_dir）
    installed_at: float = field(default_factory=time.time)
    installed_version: Optional[str] = None
    last_updated_at: Optional[float] = None
    has_venv: bool = False
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pack_name": self.pack_name,
            "source": self.source,
            "plugin_root_relative": self.plugin_root_relative,
            "installed_at": self.installed_at,
            "installed_version": self.installed_version,
            "last_updated_at": self.last_updated_at,
            "has_venv": self.has_venv,
            "notes": self.notes,
        }


def _read_meta(pack_root: Path) -> Optional[PackMeta]:
    f = pack_root / LIURUYAN_META_FILE
    if not f.exists():
        return None
    try:
        raw = json.loads(f.read_text(encoding="utf-8-sig"))
        return PackMeta(
            pack_name=raw["pack_name"],
            source=raw.get("source", {}),
            plugin_root_relative=raw.get("plugin_root_relative", "."),
            installed_at=float(raw.get("installed_at", 0)),
            installed_version=raw.get("installed_version"),
            last_updated_at=raw.get("last_updated_at"),
            has_venv=bool(raw.get("has_venv", False)),
            notes=raw.get("notes", ""),
        )
    except Exception:
        return None


def _write_meta(pack_root: Path, meta: PackMeta) -> None:
    f = pack_root / LIURUYAN_META_FILE
    f.write_text(
        json.dumps(meta.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ────────────────────────────────────────────────────────────────────────────
# 关键操作：install / update / uninstall / list / status
# ────────────────────────────────────────────────────────────────────────────

def install_pack(
    source: Any,
    pack_name: Optional[str] = None,
    create_venv: bool = True,
    install_deps: bool = True,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """安装一个 plugin pack。

    Args:
        source: github 简写 / 完整 URL / 结构化 source dict
        pack_name: 安装目录名。默认从 plugin.json 的 name 字段读
        create_venv: 是否创建子 venv（默认 True）
        install_deps: 是否 pip install requirements.txt（默认 True；create_venv=False 时强制 False）
        overwrite: 目标目录已存在时是否强制覆盖（默认 False，已存在则报错）

    Returns:
        {success, pack_name, pack_dir, plugin_root, plugin_name, plugin_version,
         skills_count, venv_created, deps_installed, messages: [...]}

    抛 SkillPackError 表示安装彻底失败（比如克隆 404 / 找不到 plugin.json）。
    """
    if not _git_available():
        raise SkillPackError("git 未安装或不在 PATH。请先安装 git。")

    norm = _normalize_source(source)
    clone_url = _source_to_clone_url(norm)
    messages: List[str] = []

    # 第 1 步：先 clone 到临时目录，确认 plugin.json 后再决定最终落脚点和名字
    # （这样能从 manifest.name 自动推 pack_name，用户不用手动指定）
    tmp_root = _packs_root() / f".tmp_install_{int(time.time())}"
    if tmp_root.exists():
        _force_rmtree(tmp_root)

    clone_args = ["git", "clone", "--depth", "1"]
    if norm.get("ref"):
        clone_args += ["--branch", str(norm["ref"])]
    clone_args += [clone_url, str(tmp_root)]

    rc, out, err = _run(clone_args, timeout=600)
    if rc != 0:
        _force_rmtree(tmp_root)
        raise SkillPackError(f"git clone 失败: {err.strip() or out.strip()}")
    messages.append(f"✓ clone {clone_url}")

    try:
        # 第 2 步：找 plugin.json
        manifest_path = _find_plugin_manifest(tmp_root)
        if manifest_path is None:
            raise SkillPackError(
                f"在 {tmp_root} 内未找到 .claude-plugin/plugin.json，"
                "可能不是 Claude Code Plugin。"
            )
        manifest = _read_plugin_manifest(manifest_path)
        plugin_name = manifest.get("name") or "unnamed-plugin"
        plugin_version = manifest.get("version") or "0.0.0"
        messages.append(f"✓ 识别 plugin: {plugin_name}@{plugin_version}")

        # 第 3 步：决定 pack_name + 最终目录
        final_name = pack_name or plugin_name
        try:
            target = pack_dir(final_name)
        except SkillPackError as exc:
            raise SkillPackError(f"pack_name 不合法 ({final_name}): {exc}")

        if target.exists():
            if not overwrite:
                raise SkillPackError(
                    f"目标目录已存在: {target}。"
                    "传 overwrite=True 强制覆盖，或先 uninstall。"
                )
            _force_rmtree(target)
            messages.append(f"✓ 覆盖原有 {target.name}")

        # 第 4 步：把 tmp 移到 final
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_move_dir(tmp_root, target)
        tmp_root = None  # 表示已经搬走，finally 不要再删

        # 移动后 manifest_path 指向的是已失效的 tmp 路径，必须从 target 重新定位
        manifest_path_new = _find_plugin_manifest(target)
        if manifest_path_new is None:
            raise SkillPackError("移动后丢失了 plugin.json，安装失败")
        plugin_root_in_target = manifest_path_new.parent.parent
        rel = plugin_root_in_target.relative_to(target).as_posix()
        plugin_root_relative = rel if rel else "."
        messages.append(f"✓ plugin_root: {plugin_root_relative}")

        # 第 5 步：venv + 依赖
        venv_ok = False
        deps_ok = False
        if create_venv:
            ok, msg = _create_venv(target)
            messages.append(f"venv: {msg}")
            venv_ok = ok
            if ok and install_deps:
                ok2, msg2 = _pip_install_requirements(target, plugin_root_in_target)
                messages.append(f"deps: {msg2}")
                deps_ok = ok2

        # 第 6 步：写元数据
        meta = PackMeta(
            pack_name=final_name,
            source=norm,
            plugin_root_relative=plugin_root_relative,
            installed_version=plugin_version,
            has_venv=venv_ok,
        )
        _write_meta(target, meta)
        messages.append(f"✓ 元数据已写入 {LIURUYAN_META_FILE}")

        # 第 7 步：统计 skills 数（如果 plugin_root/skills/ 存在）
        skills_count = 0
        skills_subdir = plugin_root_in_target / "skills"
        if skills_subdir.exists():
            for sub in skills_subdir.iterdir():
                if sub.is_dir() and (sub / "SKILL.md").exists():
                    skills_count += 1
        messages.append(f"✓ 发现 {skills_count} 个 skill")

        return {
            "success": True,
            "pack_name": final_name,
            "pack_dir": str(target),
            "plugin_root": str(plugin_root_in_target),
            "plugin_root_relative": plugin_root_relative,
            "plugin_name": plugin_name,
            "plugin_version": plugin_version,
            "skills_count": skills_count,
            "venv_created": venv_ok,
            "deps_installed": deps_ok,
            "messages": messages,
        }
    except Exception:
        # 失败时清理已经搬到 target 的内容，避免半成品被 SkillLoader 加载
        if tmp_root is not None and tmp_root.exists():
            try:
                _force_rmtree(tmp_root)
            except Exception:
                pass
        raise


def update_pack(
    pack_name: str,
    install_deps: bool = True,
) -> Dict[str, Any]:
    """升级一个已安装的 pack：git pull --ff-only + 刷新依赖。

    Returns:
        {success, pack_name, before_version, after_version, changed,
         deps_installed, messages: [...]}
    """
    target = pack_dir(pack_name)
    if not target.exists():
        raise SkillPackError(f"pack 未安装: {pack_name}")
    if not (target / ".git").exists():
        raise SkillPackError(
            f"pack {pack_name} 不是 git 仓库（可能是手工拷贝），无法自动升级"
        )
    if not _git_available():
        raise SkillPackError("git 未安装或不在 PATH")

    messages: List[str] = []
    meta = _read_meta(target)
    before_version = meta.installed_version if meta else None

    rc, out, err = _run(
        ["git", "pull", "--ff-only"],
        cwd=target,
        timeout=600,
    )
    if rc != 0:
        raise SkillPackError(f"git pull 失败: {err.strip() or out.strip()}")
    messages.append(f"✓ git pull: {out.strip().splitlines()[-1] if out.strip() else 'OK'}")

    # 重新读 manifest 拿新版本号
    manifest_path = _find_plugin_manifest(target)
    after_version: Optional[str] = None
    if manifest_path is not None:
        try:
            mani = _read_plugin_manifest(manifest_path)
            after_version = mani.get("version")
        except Exception:
            pass

    # 依赖刷新
    deps_ok = False
    if install_deps:
        plugin_root = manifest_path.parent.parent if manifest_path else target
        ok, msg = _pip_install_requirements(target, plugin_root)
        deps_ok = ok
        messages.append(f"deps: {msg}")

    # 写回元数据
    if meta:
        meta.installed_version = after_version
        meta.last_updated_at = time.time()
        _write_meta(target, meta)

    return {
        "success": True,
        "pack_name": pack_name,
        "before_version": before_version,
        "after_version": after_version,
        "changed": before_version != after_version,
        "deps_installed": deps_ok,
        "messages": messages,
    }


def uninstall_pack(pack_name: str) -> Dict[str, Any]:
    """卸载：删整个 pack 目录。"""
    target = pack_dir(pack_name)
    if not target.exists():
        return {"success": False, "pack_name": pack_name, "reason": "未安装"}
    # 安全检查：必须含 .claude-plugin/ 或 .liuruyan-pack.json，避免误删用户单层 skill
    has_meta = (target / LIURUYAN_META_FILE).exists()
    has_plugin = (target / ".claude-plugin" / "plugin.json").exists()
    if not (has_meta or has_plugin):
        raise SkillPackError(
            f"目录 {target} 看起来不是柳如烟管理的 plugin pack，拒绝删除"
        )
    _force_rmtree(target)
    return {"success": True, "pack_name": pack_name, "removed": str(target)}


def list_packs() -> List[Dict[str, Any]]:
    """列出所有已安装的 pack。"""
    root = _packs_root()
    if not root.exists():
        return []
    out: List[Dict[str, Any]] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        meta = _read_meta(d)
        manifest_path = _find_plugin_manifest(d)
        if meta is None and manifest_path is None:
            continue  # 不是 pack（可能是单层 skill 目录）
        info: Dict[str, Any] = {
            "pack_name": d.name,
            "pack_dir": str(d),
            "managed_by_liuruyan": meta is not None,
        }
        if meta:
            info.update({
                "installed_version": meta.installed_version,
                "installed_at": meta.installed_at,
                "last_updated_at": meta.last_updated_at,
                "source": meta.source,
                "has_venv": meta.has_venv,
            })
        if manifest_path:
            try:
                mani = _read_plugin_manifest(manifest_path)
                info["plugin_name"] = mani.get("name")
                info["plugin_version"] = mani.get("version")
                info["plugin_description"] = mani.get("description")
            except Exception:
                pass
        out.append(info)
    return out


def pack_status(pack_name: str) -> Dict[str, Any]:
    """单 pack 详情。"""
    target = pack_dir(pack_name)
    if not target.exists():
        raise SkillPackError(f"pack 未安装: {pack_name}")
    meta = _read_meta(target)
    manifest_path = _find_plugin_manifest(target)
    manifest: Dict[str, Any] = {}
    if manifest_path:
        try:
            manifest = _read_plugin_manifest(manifest_path)
        except Exception:
            pass

    skills: List[Dict[str, str]] = []
    if manifest_path:
        skills_dir = manifest_path.parent.parent / "skills"
        if skills_dir.exists():
            for sub in skills_dir.iterdir():
                skill_md = sub / "SKILL.md"
                if sub.is_dir() and skill_md.exists():
                    skills.append({
                        "name": sub.name,
                        "fully_qualified": f"{pack_name}__{sub.name}",
                        "path": str(skill_md),
                    })

    py = _venv_python(target)
    return {
        "pack_name": pack_name,
        "pack_dir": str(target),
        "is_git_repo": (target / ".git").exists(),
        "venv_python": str(py) if py.exists() else None,
        "manifest": manifest,
        "meta": meta.to_dict() if meta else None,
        "skills": skills,
    }


__all__ = [
    "SkillPackError",
    "PackMeta",
    "LIURUYAN_META_FILE",
    "pack_dir",
    "install_pack",
    "update_pack",
    "uninstall_pack",
    "list_packs",
    "pack_status",
]
