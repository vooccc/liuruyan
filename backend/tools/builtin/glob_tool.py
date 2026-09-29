"""
Glob Tool - 文件模式匹配工具（Cascade `find_by_name` 对齐版）

特性：
- Glob pattern：`**/*.py`、`src/**/*.tsx`、`test_*.py`、`config.*` 等
- `path`：搜索起点（绝对路径直接锚到该处；相对路径拼到 project_root）
- `excludes`：glob 排除列表，例如 `["**/vendor/**", "**/dist/**"]`
- `extensions`：扩展名白名单，例如 `["ts", "tsx"]`（无 `.` 前缀也接受）
- `max_depth`：相对 `path` 起点的最大递归层数（None = 无限）
- `file_type`：`"file"` / `"directory"` / `"any"`（取代旧 `include_dirs` 布尔）
- `full_path`：True 时 pattern 匹配完整相对路径，否则只匹配 basename
- 引擎选择：优先 ripgrep `rg --files`（尊重 .gitignore，跳隐藏目录，并行），
  无 rg 时回落到 `os.walk` 显式跳常见垃圾目录。
"""

import fnmatch
import glob as _stdlib_glob
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Sequence


# fallback 路径显式排除的目录（rg 默认就跳）
_DEFAULT_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    "dist", "build", ".next", ".turbo", ".cache",
}


def _normalize_extensions(extensions: Optional[Sequence[str]]) -> Optional[set]:
    if not extensions:
        return None
    out = set()
    for ext in extensions:
        if not isinstance(ext, str) or not ext.strip():
            continue
        e = ext.strip().lower()
        if not e.startswith("."):
            e = "." + e
        out.add(e)
    return out or None


def _match_extension(rel_path: str, exts: Optional[set]) -> bool:
    if not exts:
        return True
    return Path(rel_path).suffix.lower() in exts


def _match_pattern(rel_path: str, pattern: Optional[str], full_path: bool) -> bool:
    """pattern=None → 任意；否则按 full_path / basename 匹配 fnmatch。

    若 pattern 含 `/`（多层 glob，例如 `src/**/*.tsx`），自动按完整路径匹配，
    避免 `src/**/*.tsx` 退化成"任意 .tsx 文件"。
    """
    if not pattern:
        return True
    rel_norm = rel_path.replace("\\", "/")
    # `**` → `*` （fnmatch 中 `*` 默认跨 `/`，配合手动收敛足够近似 glob 语义）
    pat_norm = pattern.replace("\\", "/").replace("**/", "*/").replace("/**", "/*")
    # 含 `/` 的 pattern 强制按 full_path 匹配
    if full_path or "/" in pattern:
        # 头尾再退化：`*/foo` 形态使 fnmatch `*` 跨多层
        loose = pat_norm.replace("*/", "*").replace("/*", "*")
        return (
            fnmatch.fnmatch(rel_norm, pat_norm)
            or fnmatch.fnmatch(rel_norm, loose)
            or fnmatch.fnmatch(rel_norm, pattern.replace("\\", "/"))
        )
    return fnmatch.fnmatch(os.path.basename(rel_norm), pat_norm)


def _match_excludes(rel_path: str, excludes: Sequence[str]) -> bool:
    """返 True 表示该路径**被排除**。"""
    for ex in excludes:
        if not ex:
            continue
        ex_norm = ex.replace("**/", "*").replace("/**", "*")
        if fnmatch.fnmatch(rel_path, ex) or fnmatch.fnmatch(rel_path, ex_norm):
            return True
    return False


def _which_rg() -> Optional[str]:
    return shutil.which("rg")


class GlobTool:
    """文件模式匹配工具"""

    def __init__(self, project_root: str = None):
        """初始化 GlobTool

        Args:
            project_root: 项目根目录，默认为当前工作目录
        """
        self.project_root = Path(project_root) if project_root else Path.cwd()

    def search(
        self,
        pattern: str,
        recursive: bool = True,
        include_dirs: bool = False,
        max_results: int = 1000,
        # === 新增（对齐 Cascade find_by_name） ===
        path: Optional[str] = None,
        excludes: Optional[Sequence[str]] = None,
        extensions: Optional[Sequence[str]] = None,
        max_depth: Optional[int] = None,
        file_type: str = "any",
        full_path: bool = False,
    ) -> Dict[str, Any]:
        """搜索匹配模式的文件 / 目录

        Args:
            pattern: Glob 模式（如 "**/*.py" / "src/**/*.tsx" / "config.*"）
            recursive: 递归搜索（默认 True）
            include_dirs: 旧参数；如果 True 且 file_type='any'，等价 file_type='any'（保兼容）
            max_results: 最大返回数（默认 1000）
            path: 搜索起点。绝对路径直接用；相对路径拼到 project_root。None = project_root
            excludes: glob 排除列表，例如 ["**/vendor/**", "**/dist/**"]
            extensions: 扩展名白名单（如 ["ts", "tsx"]，无 `.` 也行）
            max_depth: 相对 path 起点的最大递归层数（None=无限；0=只看 path 自身一层）
            file_type: "file" / "directory" / "any"。覆盖 include_dirs
            full_path: True=pattern 匹配相对路径全串；False=只匹配 basename

        Returns:
            {pattern, matches: [...], count, truncated, engine}
        """
        try:
            # 起点解析：path 优先，缺省用 project_root
            if path:
                p = Path(path).expanduser()
                root = p if p.is_absolute() else (self.project_root / p)
            else:
                root = self.project_root
            root = root.resolve()

            if not root.exists():
                return {
                    "pattern": pattern,
                    "error": f"Search path not found: {root}",
                    "matches": [],
                    "count": 0,
                    "truncated": False,
                }

            # file_type 归一（兼容 include_dirs）
            ft = (file_type or "any").lower()
            if ft not in ("file", "directory", "any"):
                ft = "any"
            # 旧 include_dirs=True 且未显式指定 file_type → 仍然只放行文件 + 目录混
            # 旧 include_dirs=False 且未显式指定 file_type → 只放行文件
            if file_type == "any" and not include_dirs:
                ft = "file"

            exts = _normalize_extensions(extensions)
            excludes_list = list(excludes or [])
            engine = "stdlib"

            # 候选路径列表（相对 root 的相对路径）
            candidates: List[str] = []

            # 优先 ripgrep：尊重 .gitignore + 跳隐藏目录 + 并行遍历
            # 注意 rg --files 只列文件，directory 模式必须用 fallback
            use_rg = _which_rg() is not None and ft != "directory" and recursive

            if use_rg:
                engine = "rg"
                argv = ["rg", "--files", "--hidden", "--no-ignore-vcs"] if False else ["rg", "--files"]
                # 上面留作备忘：默认我们要 .gitignore 感知（rg 默认就感知），
                # 不传 --hidden（避免扫 .git/）
                if max_depth is not None and isinstance(max_depth, int) and max_depth >= 0:
                    argv.extend(["--max-depth", str(max_depth + 1)])
                argv.append(str(root))
                try:
                    run_kwargs = {
                        "shell": False,
                        "capture_output": True,
                        "text": True,
                        "encoding": "utf-8",
                        "errors": "replace",
                        "timeout": 60,
                    }
                    if os.name == "nt":
                        run_kwargs["creationflags"] = 0x08000000
                    proc = subprocess.run(argv, **run_kwargs)
                    if proc.returncode in (0, 1):  # 1 = no match
                        for line in (proc.stdout or "").split("\n"):
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rel = os.path.relpath(line, root)
                            except ValueError:
                                rel = line
                            rel = rel.replace("\\", "/")
                            candidates.append(rel)
                    else:
                        # rg 失败 → 走 fallback
                        use_rg = False
                except Exception:
                    use_rg = False

            if not use_rg:
                engine = "walk"
                # 纯 Python 遍历，跳常见垃圾目录
                root_str = str(root)
                root_depth = root_str.count(os.sep)
                walk_iter = os.walk(root_str) if recursive else [next(os.walk(root_str), (root_str, [], []))]
                for cur, dirs, files in walk_iter:
                    # 跳隐藏 + 默认垃圾目录
                    dirs[:] = [d for d in dirs if d not in _DEFAULT_EXCLUDE_DIRS and not d.startswith(".")]
                    # max_depth 控制（相对 root）
                    if max_depth is not None and isinstance(max_depth, int) and max_depth >= 0:
                        cur_depth = cur.count(os.sep) - root_depth
                        if cur_depth >= max_depth:
                            dirs[:] = []

                    if ft in ("directory", "any"):
                        for d in dirs:
                            full = os.path.join(cur, d)
                            try:
                                rel = os.path.relpath(full, root_str).replace("\\", "/")
                            except ValueError:
                                rel = full
                            candidates.append(rel + "/")  # 末尾 / 标记目录

                    if ft in ("file", "any"):
                        for fname in files:
                            full = os.path.join(cur, fname)
                            try:
                                rel = os.path.relpath(full, root_str).replace("\\", "/")
                            except ValueError:
                                rel = full
                            candidates.append(rel)

            # 过滤 + 截断
            matches: List[str] = []
            for rel in candidates:
                is_dir = rel.endswith("/")
                rel_clean = rel.rstrip("/")
                # file_type 过滤
                if ft == "file" and is_dir:
                    continue
                if ft == "directory" and not is_dir:
                    continue
                # extensions（仅作用于 file）
                if not is_dir and not _match_extension(rel_clean, exts):
                    continue
                # excludes
                if excludes_list and _match_excludes(rel_clean, excludes_list):
                    continue
                # pattern
                if not _match_pattern(rel_clean, pattern, full_path):
                    continue
                matches.append(rel_clean)
                if len(matches) >= max_results:
                    break

            matches.sort()
            return {
                "pattern": pattern,
                "matches": matches,
                "count": len(matches),
                "truncated": len(matches) >= max_results,
                "engine": engine,
                "root": str(root),
            }

        except Exception as e:
            return {
                "pattern": pattern,
                "error": str(e),
                "matches": [],
                "count": 0,
                "truncated": False,
            }

    def find_by_extension(self, extension: str, directory: str = ".",
                          max_results: int = 1000) -> Dict[str, Any]:
        """按文件扩展名查找文件

        Args:
            extension: 文件扩展名，例如 "py", ".tsx", "js"
            directory: 搜索目录（相对于项目根目录）
            max_results: 最大返回结果数

        Returns:
            搜索结果字典
        """
        # 规范化扩展名
        if not extension.startswith('.'):
            extension = '.' + extension

        # 构建 glob 模式
        pattern = f"{directory}/**/*{extension}"

        return self.search(pattern, recursive=True, max_results=max_results)

    def find_by_name(self, name_pattern: str, directory: str = ".",
                     max_results: int = 1000) -> Dict[str, Any]:
        """按文件名模式查找文件

        Args:
            name_pattern: 文件名模式，例如 "test_*", "*config*"
            directory: 搜索目录（相对于项目根目录）
            max_results: 最大返回结果数

        Returns:
            搜索结果字典
        """
        # 构建 glob 模式
        pattern = f"{directory}/**/{name_pattern}"

        return self.search(pattern, recursive=True, max_results=max_results)


# 全局实例
_glob_tool = None


def get_glob_tool(project_root: str = None) -> GlobTool:
    """获取 GlobTool 单例"""
    global _glob_tool
    if _glob_tool is None:
        _glob_tool = GlobTool(project_root)
    return _glob_tool
