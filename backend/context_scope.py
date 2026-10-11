"""ScopeItem 上下文作用域检索实现。

为前端 `@` mention 下拉提供候选：用户输入 `@xxx`，后端返回项目里
名字含 `xxx` 的 file / directory / rule / code_map / code_context（symbol）。

设计要点：
- 单次遍历项目根 → 累积所有候选 → 内存里按类型/分数过滤排序，避免每类型一次 walk
- ignore-list 跳过常见噪音目录（.git / node_modules / __pycache__ / .venv 等）
- 路径严格归一在 project_root 下（防越界）
- substring 匹配 + 简单评分（前缀 > 连续匹配 > 任意位置），保证 top-k 体验合理
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from .scope_item import SCOPE_ITEM_TYPES, ScopeItem


# ====== 常量 ======

# 硬过滤：任何场景（walk / 顶级目录列表）都跳。系统/构建/IDE 噪音目录。
DEFAULT_IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components",
    ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt",
    ".idea", ".vscode", ".lry", ".cursor", ".windsurf",
    "coverage", ".cache", ".turbo",
    "target",  # rust/cargo 编译产物，体积大、噪音多
})

# 仅 walk 时跳子树（性能保护），避免深度遍历撑爆 MAX_WALK_FILES。
WALK_HEAVY_DIRS = frozenset({
    "_research",
    "references",
    "_archive",
})


# Files 子页"recent"推荐用的源代码/文档后缀白名单。
# 不影响有 query 时的 substring 检索（用户搜 session.json 仍能找到），
# 仅在空 query 推荐场景过滤掉自动生成 / 业务数据 / 二进制中间件。
SOURCE_FILE_SUFFIXES = frozenset({
    ".py", ".pyi",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".rs", ".go", ".java", ".kt", ".scala",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".cs",
    ".rb", ".php", ".swift", ".m", ".mm",
    ".html", ".css", ".scss", ".sass", ".less", ".vue", ".svelte",
    ".md", ".mdc", ".rst", ".txt",
    ".toml", ".yaml", ".yml", ".ini", ".cfg",
    ".sh", ".ps1", ".bat",
    ".sql", ".graphql", ".proto",
})

# 二进制后缀直接跳过（虽然 file 类型理论可以钉，但 mention 检索一般找不到它）
BINARY_SUFFIXES = frozenset({
    ".pyc", ".so", ".dll", ".exe", ".bin", ".dat",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".tiff",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".ttf", ".otf", ".woff", ".woff2",
    ".lock",  # package-lock 太大没意义
})

# 单次 walk 上限（防止极大仓库爆性能）
MAX_WALK_FILES = 30000

# 默认返回 top-k
DEFAULT_LIMIT = 20

# code_context 用 grep 启发式：def / class / function / interface / type 后跟 symbol
# 只在一些代码后缀里搜索，不无差别全跑
SYMBOL_FILE_SUFFIXES = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs",
})

# 在文件里搜符号定义的正则模板（query 已 re.escape）
SYMBOL_PATTERNS = (
    r"\bdef\s+{q}\b",
    r"\bclass\s+{q}\b",
    r"\bfunction\s+{q}\b",
    r"\b(?:export\s+)?(?:const|let|var)\s+{q}\b",
    r"\binterface\s+{q}\b",
    r"\btype\s+{q}\s*=",
)

# code_context 搜索时最多扫多少个候选文件（防超时）
MAX_SYMBOL_SCAN_FILES = 500


# ====== Walk + 评分 ======

def _is_ignored_dir(name: str) -> bool:
    """硬过滤：顶级目录列表 / 任何场景都该跳的。`.` 前缀 + DEFAULT_IGNORE_DIRS。"""
    return name in DEFAULT_IGNORE_DIRS or name.startswith(".")


def _is_walk_skipped_dir(name: str) -> bool:
    """walk 阶段要跳子树的：硬过滤集 + WALK_HEAVY_DIRS（列表里能看见但不进 walk）。"""
    return _is_ignored_dir(name) or name in WALK_HEAVY_DIRS


# 单次 iterdir 计数上限（防御极端目录如 node_modules 子目录撑爆 mention 响应）。
# 命中上限时返回该值，前端可视为"≥ N"，触发大目录警告。
DIR_ENTRIES_COUNT_CAP = 2000


def _count_dir_entries(abs_path: Path) -> Optional[int]:
    """统计目录一级 entries 数（不递归）。失败静默返回 None。

    上限 DIR_ENTRIES_COUNT_CAP，超出停止计数返回 cap 值（前端理解为"≥ cap"）。
    用 os.scandir 比 iterdir 更快——不读 stat，只 readdir entry name。
    """
    try:
        count = 0
        with os.scandir(abs_path) as it:
            for _ in it:
                count += 1
                if count >= DIR_ENTRIES_COUNT_CAP:
                    return DIR_ENTRIES_COUNT_CAP
        return count
    except OSError:
        return None


def _walk_project(project_root: Path) -> Tuple[List[Tuple[Path, str]], List[Tuple[Path, str]]]:
    """单次遍历项目根。返回 (files, directories)，每项 (abs_path, rel_path_posix)。

    跳过 ignore-list 子树和二进制文件。命中 MAX_WALK_FILES 截断（保护性能）。
    """
    files: List[Tuple[Path, str]] = []
    dirs: List[Tuple[Path, str]] = []
    root_str = str(project_root)
    file_count = 0

    for dirpath, dirnames, filenames in os.walk(project_root):
        # in-place 过滤：os.walk 会按修改后的 dirnames 决定下钻
        # 用 _is_walk_skipped_dir 而非 _is_ignored_dir：把 WALK_HEAVY_DIRS 排除子树，
        # 它们的目录条目仍可由 _list_top_level_directories 通过硬过滤集放行
        dirnames[:] = [d for d in dirnames if not _is_walk_skipped_dir(d)]

        cur = Path(dirpath)
        try:
            rel_dir = cur.relative_to(project_root)
        except ValueError:
            continue
        rel_dir_str = rel_dir.as_posix() if str(rel_dir) != "." else ""

        # 记录目录（跳过项目根本身）
        if rel_dir_str:
            dirs.append((cur, rel_dir_str))

        for fname in filenames:
            suffix = Path(fname).suffix.lower()
            if suffix in BINARY_SUFFIXES:
                continue
            abs_path = cur / fname
            rel_path = f"{rel_dir_str}/{fname}" if rel_dir_str else fname
            files.append((abs_path, rel_path))
            file_count += 1
            if file_count >= MAX_WALK_FILES:
                return files, dirs

    return files, dirs


def _score(query_lower: str, candidate_lower: str, basename_lower: str) -> Optional[int]:
    """空 query 视作通配匹配 → 0 分。否则 substring 匹配 + 启发式排序。

    分越高越靠前：
    - basename 前缀命中 → 1000
    - basename 任意位置命中 → 500 + (10 - min(idx, 10))
    - 完整路径命中 → 100 + (20 - min(path_idx, 20))
    - 不命中 → None
    """
    if not query_lower:
        return 0
    if basename_lower.startswith(query_lower):
        return 1000
    base_idx = basename_lower.find(query_lower)
    if base_idx >= 0:
        return 500 + max(0, 10 - base_idx)
    path_idx = candidate_lower.find(query_lower)
    if path_idx >= 0:
        return 100 + max(0, 20 - path_idx)
    return None


def _make_label(rel_path: str) -> str:
    return rel_path.rsplit("/", 1)[-1]


# ====== 各类型检索 ======

def _match_files(
    files: Sequence[Tuple[Path, str]],
    query_lower: str,
    limit: int,
) -> List[Tuple[int, ScopeItem]]:
    # 加 depth penalty：同 basename 命中时浅路径优先（backend/main.py > docs/archive/.../main.py）。
    # 实现方式：score 减去 depth * 5（深度每多一层扣 5 分，不破坏 basename-vs-path-substring 大档位排序）。
    out: List[Tuple[int, ScopeItem]] = []
    for _, rel in files:
        rel_lower = rel.lower()
        base_lower = rel_lower.rsplit("/", 1)[-1]
        score = _score(query_lower, rel_lower, base_lower)
        if score is None:
            continue
        depth = rel.count("/")
        out.append((score - depth * 5, ScopeItem(type="file", path=rel, label=_make_label(rel))))
    out.sort(key=lambda x: (-x[0], x[1].path))
    return out[:limit]


def _match_directories(
    dirs: Sequence[Tuple[Path, str]],
    query_lower: str,
    limit: int,
) -> List[Tuple[int, ScopeItem]]:
    # 先收集分数 + abs_path（不算 entries_count，避免对所有命中目录都做 scandir）
    scored: List[Tuple[int, Path, str]] = []
    for abs_path, rel in dirs:
        rel_lower = rel.lower()
        base_lower = rel_lower.rsplit("/", 1)[-1]
        score = _score(query_lower, rel_lower, base_lower)
        if score is None:
            continue
        depth = rel.count("/")
        scored.append((score - depth * 5, abs_path, rel))
    scored.sort(key=lambda x: (-x[0], x[2]))
    # 仅对 top-limit 的目录算 entries_count，最多 limit 次 scandir
    out: List[Tuple[int, ScopeItem]] = []
    for adjusted_score, abs_path, rel in scored[:limit]:
        out.append((
            adjusted_score,
            ScopeItem(
                type="directory",
                path=rel,
                label=_make_label(rel),
                entries_count=_count_dir_entries(abs_path),
            ),
        ))
    return out


def _list_recent_files(
    files: Sequence[Tuple[Path, str]],
    limit: int,
) -> List[Tuple[int, ScopeItem]]:
    """空 query 进入 Files 子页时的兜底推荐：按 mtime 倒序取最近修改的 N 个。

    柳如烟没接 IDE 拿不到 "最近打开" 信号，用 mtime 近似——大部分时候
    mtime 最新的文件就是用户当前在改的。stat 调用每文件一次（SSD 上 6k
    文件约几百 ms），仅在用户主动进 Files 子页时触发，不阻塞 categories 页。
    """
    items: List[Tuple[float, str, Path]] = []
    for abs_path, rel in files:
        # 仅推荐源代码/文档类（避免 council/rooms/.../session.json 这种业务数据霸榜）
        if Path(rel).suffix.lower() not in SOURCE_FILE_SUFFIXES:
            continue
        try:
            st = abs_path.stat()
        except OSError:
            continue
        # 跳过太大的文件（避免推荐 100MB 的 log）
        if st.st_size > 2_000_000:
            continue
        items.append((st.st_mtime, rel, abs_path))
    items.sort(key=lambda x: -x[0])
    out: List[Tuple[int, ScopeItem]] = []
    # 给一个递减的 score 让多次合并时不抢占有 query 命中的位置
    for idx, (_, rel, _abs) in enumerate(items[:limit]):
        out.append((50 - idx, ScopeItem(type="file", path=rel, label=_make_label(rel))))
    return out


def _list_top_level_directories(project_root: Path, limit: int) -> List[Tuple[int, ScopeItem]]:
    """空 query 推荐：直接返回项目根一级子目录（不走 walk，便宜很多）。

    跳过 ignore-list 里的目录（.git / node_modules / __pycache__ 等）。
    """
    out: List[Tuple[int, ScopeItem]] = []
    try:
        entries = sorted(project_root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out
    for entry in entries:
        if not entry.is_dir():
            continue
        name = entry.name
        if _is_ignored_dir(name):
            continue
        out.append((
            50,
            ScopeItem(
                type="directory",
                path=name,
                label=name,
                entries_count=_count_dir_entries(entry),
            ),
        ))
        if len(out) >= limit:
            break
    return out


def _match_rules(
    project_root: Path,
    query_lower: str,
    limit: int,
) -> List[Tuple[int, ScopeItem]]:
    out: List[Tuple[int, ScopeItem]] = []
    candidates: List[Path] = []
    for rule_dir in (
        project_root / ".lry" / "rules",
        project_root / "rules",
        project_root / ".lry" / "rules",
        project_root / ".cursor" / "rules",
    ):
        if rule_dir.exists() and rule_dir.is_dir():
            try:
                for entry in rule_dir.iterdir():
                    if entry.is_file() and entry.suffix.lower() in (".md", ".mdc", ".txt"):
                        candidates.append(entry)
            except OSError:
                continue
    for path in candidates:
        rule_id = path.stem
        score = _score(query_lower, rule_id.lower(), rule_id.lower())
        if score is None:
            # 也允许在子串里命中（比如 "agent" 命中 "coding-agent"）
            full = path.name.lower()
            score = _score(query_lower, full, full)
        if score is None:
            continue
        out.append((score, ScopeItem(type="rule", path=rule_id, label=rule_id)))
    out.sort(key=lambda x: (-x[0], x[1].path))
    return out[:limit]


def _match_code_maps(
    project_root: Path,
    query_lower: str,
    limit: int,
) -> List[Tuple[int, ScopeItem]]:
    """code_map 候选：docs/CODE_MAP.md 之类的项目地图文档。"""
    out: List[Tuple[int, ScopeItem]] = []
    for cand in (
        project_root / "CODE_MAP.md",
        project_root / "docs" / "CODE_MAP.md",
        project_root / "_docs" / "CODE_MAP.md",
        project_root / "code_map.md",
        project_root / "docs" / "code_map.md",
        project_root / "_docs" / "code_map.md",
    ):
        if not cand.exists():
            continue
        try:
            rel = cand.relative_to(project_root).as_posix()
        except ValueError:
            continue
        rel_lower = rel.lower()
        score = _score(query_lower, rel_lower, rel_lower.rsplit("/", 1)[-1])
        # code_map 允许空 query（用户想钉项目地图就给）
        if score is None and query_lower:
            continue
        out.append((score or 0, ScopeItem(type="code_map", path=rel, label=_make_label(rel))))
    out.sort(key=lambda x: (-x[0], x[1].path))
    return out[:limit]


def _match_code_context(
    files: Sequence[Tuple[Path, str]],
    query: str,
    limit: int,
) -> List[Tuple[int, ScopeItem]]:
    """对代码文件 grep 寻找 def/class/function 等 symbol 定义。

    起步实现：扫描有限数量文件（MAX_SYMBOL_SCAN_FILES）。
    准确度足够 mention 体验，重场景再换 jedi/treesitter。
    """
    if not query.strip():
        return []
    q_re = re.escape(query)
    pattern = re.compile("|".join(p.format(q=q_re) for p in SYMBOL_PATTERNS))

    out: List[Tuple[int, ScopeItem]] = []
    seen_symbols: set = set()
    scanned = 0
    for abs_path, rel in files:
        if scanned >= MAX_SYMBOL_SCAN_FILES:
            break
        if Path(rel).suffix.lower() not in SYMBOL_FILE_SUFFIXES:
            continue
        scanned += 1
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if pattern.search(text) is None:
            continue
        # 找到了——再粗略提取首个 symbol 名（其实就是 query 本身，因为正则锚定它）
        symbol = query.strip()
        key = (symbol, rel)
        if key in seen_symbols:
            continue
        seen_symbols.add(key)
        out.append((100, ScopeItem(
            type="code_context",
            path=symbol,
            label=f"{symbol}  ({rel})",
        )))
        if len(out) >= limit:
            break
    return out[:limit]


# ====== 公共入口 ======

def match_scope_items(
    query: str,
    *,
    project_root: Path,
    types: Optional[Iterable[str]] = None,
    limit: int = DEFAULT_LIMIT,
) -> List[ScopeItem]:
    """检索匹配 query 的 scope items。

    Args:
        query: 用户输入；空串只对 code_map / 排序无意义类型返回兜底。
        project_root: 项目根（绝对路径）。所有候选必须在它下面。
        types: 限制只返回这些类型；None = 全部。
        limit: 单次响应总上限（按混合分数取 top-k）。

    Returns: 排序后的 ScopeItem 列表（混合各类型，分数降序）。
    """
    if types is None:
        wanted = set(SCOPE_ITEM_TYPES)
    else:
        wanted = {t for t in types if t in SCOPE_ITEM_TYPES}
    if not wanted:
        return []

    query_lower = query.strip().lower()

    # 何时需要 walk 整棵项目树：
    # - 有 query 且需要查 file / directory / code_context（substring 全匹配需全量）
    # - 空 query 但用户主动进 Files 子页（wanted == {'file'}）→ 用 mtime 倒序拿 recent files
    # 空 query + 默认 5 类型时不 walk，由 listdir 一级的 top-level dirs + rules/code_map 兜底。
    needs_walk = (
        bool(query_lower) and bool(wanted & {"file", "directory", "code_context"})
    ) or (not query_lower and wanted == {"file"})
    files: List[Tuple[Path, str]] = []
    dirs: List[Tuple[Path, str]] = []
    if needs_walk:
        files, dirs = _walk_project(project_root)

    pool: List[Tuple[int, ScopeItem]] = []
    per_type_limit = max(limit, 5)

    if "file" in wanted:
        if query_lower:
            pool.extend(_match_files(files, query_lower, per_type_limit))
        elif wanted == {"file"}:
            # 单独进 Files 子页空 query → 推荐最近修改的 N 个文件
            pool.extend(_list_recent_files(files, per_type_limit))
    if "directory" in wanted:
        if query_lower:
            pool.extend(_match_directories(dirs, query_lower, per_type_limit))
        else:
            # 空 query 推荐：项目根一级目录（不走 walk）
            pool.extend(_list_top_level_directories(project_root, per_type_limit))
    if "rule" in wanted:
        pool.extend(_match_rules(project_root, query_lower, per_type_limit))
    if "code_map" in wanted:
        pool.extend(_match_code_maps(project_root, query_lower, per_type_limit))
    if "code_context" in wanted and query_lower:
        pool.extend(_match_code_context(files, query, per_type_limit))

    # 全局重排：分数降序、同分按 path
    pool.sort(key=lambda x: (-x[0], x[1].path))

    # 去重（同 type+path 视为同一条）
    seen: set = set()
    out: List[ScopeItem] = []
    for _score_val, item in pool:
        key = (item.type, item.path)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out
