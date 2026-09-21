"""
outline_file 工具：快速看一个文件的结构（class/function 列表 + 行号）

为啥要这个：
- 大文件（>500 行）直接 read_file 看一眼概况太贵；outline 只占几百字 token
- 模型先看 outline → 决定 read_file 哪几个行段，避免反复 read 整个大文件
- 等价 IDE 的"大纲视图" / "Symbol Outline"

实现策略：
- Python: 走 ast 模块，准确（包括类内嵌函数、async def、装饰器）
- JS / TS: 用稳健的多行 regex（无 tree-sitter 依赖）
  - 覆盖：function decl, arrow assignment, class, interface, type alias, export *
  - 局限：嵌套类内成员只到 class 一级；箭头函数嵌套深处可能漏
- 其他语言（go / rust / java / cpp 等）: 通用 regex 兜底（class / function / def / fn）

输出格式：
{
  "path": str,
  "language": "python" | "javascript" | "typescript" | "generic",
  "symbols": [
    {"kind": "class"|"function"|"method"|"type"|"interface"|...,
     "name": str, "line_start": int, "line_end": int, "decorators": [str]?,
     "is_async": bool?, "parent": str?}
  ],
  "line_count": int,
  "truncated": bool
}
"""
from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 防 outline 自身炸：单文件超过这行数就拒（直接 read_file 也吃力）
_MAX_LINES = 50_000
# 单文件最多吐多少符号
_MAX_SYMBOLS = 500


# ============================================================================
# 语言识别
# ============================================================================

_EXT_TO_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".d.ts": "typescript",
}


def _detect_language(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".d.ts"):
        return "typescript"
    return _EXT_TO_LANG.get(path.suffix.lower(), "generic")


# ============================================================================
# Python: ast
# ============================================================================

def _outline_python(source: str) -> List[Dict[str, Any]]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        # 即使语法错误也尽量给些信息（指出错误位置）
        return [{
            "kind": "syntax_error",
            "name": str(exc.msg or "syntax error"),
            "line_start": exc.lineno or 1,
            "line_end": exc.lineno or 1,
        }]

    symbols: List[Dict[str, Any]] = []

    def _decorator_name(dec: ast.expr) -> str:
        # 支持 @foo, @foo.bar, @foo(...), @foo.bar(...)
        if isinstance(dec, ast.Call):
            return _decorator_name(dec.func) + "(...)"
        if isinstance(dec, ast.Attribute):
            return _decorator_name(dec.value) + "." + dec.attr
        if isinstance(dec, ast.Name):
            return dec.id
        return ast.unparse(dec) if hasattr(ast, "unparse") else "<expr>"

    def _walk(node: ast.AST, parent: Optional[str] = None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if parent else "function"
                symbols.append({
                    "kind": kind,
                    "name": child.name,
                    "line_start": child.lineno,
                    "line_end": getattr(child, "end_lineno", child.lineno),
                    "is_async": isinstance(child, ast.AsyncFunctionDef),
                    "decorators": [_decorator_name(d) for d in child.decorator_list],
                    "parent": parent,
                })
                # 函数内一般不再列嵌套函数（噪音），但如果是 class 内则不递归
                if not parent:
                    # 顶层函数：列其内部嵌套函数（一层）
                    for inner in ast.iter_child_nodes(child):
                        if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            symbols.append({
                                "kind": "function",
                                "name": inner.name,
                                "line_start": inner.lineno,
                                "line_end": getattr(inner, "end_lineno", inner.lineno),
                                "is_async": isinstance(inner, ast.AsyncFunctionDef),
                                "decorators": [_decorator_name(d) for d in inner.decorator_list],
                                "parent": child.name,
                            })
            elif isinstance(child, ast.ClassDef):
                bases = [
                    ast.unparse(b) if hasattr(ast, "unparse") else "<base>"
                    for b in child.bases
                ]
                symbols.append({
                    "kind": "class",
                    "name": child.name,
                    "line_start": child.lineno,
                    "line_end": getattr(child, "end_lineno", child.lineno),
                    "bases": bases,
                    "decorators": [_decorator_name(d) for d in child.decorator_list],
                    "parent": parent,
                })
                _walk(child, parent=child.name)

    _walk(tree)
    return symbols


# ============================================================================
# JS / TS: 多行 regex
# ============================================================================

# 注意：regex 故意保守，宁可漏几个不要乱标
_TS_PATTERNS: List[tuple[str, re.Pattern[str]]] = [
    # export class Foo / class Foo / abstract class Foo
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
    # interface Foo / export interface Foo
    ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
    # type Foo =
    ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*[=<]")),
    # enum Foo
    ("enum", re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)")),
    # function foo / async function foo / export function foo
    ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")),
    # const foo = (...) =>  /  const foo = async (...) =>  /  const foo: Type = (...) =>
    ("function", re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
    )),
    # React FC etc: const Foo: React.FC<...> = (...) => { ... }
    # ↑ 已被上一条覆盖
]

# class 体内的 method（粗略）
_TS_METHOD_PATTERN = re.compile(
    r"^\s*(?:public|private|protected|static|readonly|async|\*|override|\s)*\s*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*[:{]"
)


def _outline_ts_js(source: str) -> List[Dict[str, Any]]:
    symbols: List[Dict[str, Any]] = []
    lines = source.splitlines()

    # 跟踪 class 体（基于花括号深度做简单状态机）
    class_stack: List[Dict[str, Any]] = []  # [{"name", "depth"}]
    brace_depth = 0
    # 行内字符串 / 注释清理：粗暴去掉以避免假阳，足够 outline 用
    cleaned_lines: List[str] = []
    in_block_comment = False
    for raw in lines:
        line = raw
        if in_block_comment:
            end = line.find("*/")
            if end >= 0:
                line = line[end + 2:]
                in_block_comment = False
            else:
                cleaned_lines.append("")
                continue
        # 去掉单行 // 注释
        line = re.sub(r"//.*$", "", line)
        # 去掉 /* ... */ 单行块
        line = re.sub(r"/\*.*?\*/", "", line)
        # 检测未闭合块注释
        bs = line.find("/*")
        if bs >= 0 and "*/" not in line[bs:]:
            in_block_comment = True
            line = line[:bs]
        cleaned_lines.append(line)

    for idx, line in enumerate(cleaned_lines, start=1):
        # 先尝试匹配各类符号（顶层）
        if not class_stack:
            for kind, pat in _TS_PATTERNS:
                m = pat.match(line)
                if m:
                    sym: Dict[str, Any] = {
                        "kind": kind,
                        "name": m.group(1),
                        "line_start": idx,
                        "line_end": idx,  # 用 brace 计算补
                    }
                    if kind == "class":
                        class_stack.append({
                            "name": m.group(1),
                            "depth": brace_depth,
                            "sym_index": len(symbols),
                        })
                    symbols.append(sym)
                    break
        else:
            # class 体内：识别 method
            m = _TS_METHOD_PATTERN.match(line)
            if m and m.group(1) not in {"if", "for", "while", "switch", "return", "catch", "function"}:
                # 粗略过滤关键字假阳
                symbols.append({
                    "kind": "method",
                    "name": m.group(1),
                    "line_start": idx,
                    "line_end": idx,
                    "parent": class_stack[-1]["name"],
                })

        # 维护花括号深度
        for ch in line:
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                # class 出栈
                if class_stack and brace_depth <= class_stack[-1]["depth"]:
                    cls = class_stack.pop()
                    symbols[cls["sym_index"]]["line_end"] = idx

    # 兜底：填 line_end（对没闭合的）
    for sym in symbols:
        if sym["line_end"] < sym["line_start"]:
            sym["line_end"] = sym["line_start"]

    return symbols


# ============================================================================
# generic: 通用 regex 兜底
# ============================================================================

_GENERIC_PATTERNS: List[tuple[str, re.Pattern[str]]] = [
    ("function", re.compile(r"^\s*(?:public|private|protected|static)?\s*func\s+([A-Za-z_][\w]*)")),  # go
    ("function", re.compile(r"^\s*(?:pub\s+)?fn\s+([A-Za-z_][\w]*)")),  # rust
    ("class", re.compile(r"^\s*(?:public\s+|abstract\s+)*class\s+([A-Za-z_][\w]*)")),  # java/c#
    ("function", re.compile(r"^\s*def\s+([A-Za-z_][\w]*)")),  # ruby/python-ish兜底
]


def _outline_generic(source: str) -> List[Dict[str, Any]]:
    symbols: List[Dict[str, Any]] = []
    for idx, line in enumerate(source.splitlines(), start=1):
        for kind, pat in _GENERIC_PATTERNS:
            m = pat.match(line)
            if m:
                symbols.append({
                    "kind": kind,
                    "name": m.group(1),
                    "line_start": idx,
                    "line_end": idx,
                })
                break
    return symbols


# ============================================================================
# 主入口
# ============================================================================

def outline_file_tool(path: str, language: Optional[str] = None) -> str:
    if not path:
        return json.dumps({"error": "path is required"})
    p = Path(path)
    if not p.exists():
        return json.dumps({"error": f"file not found: {path}"})
    if not p.is_file():
        return json.dumps({"error": f"not a regular file: {path}"})

    try:
        source = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return json.dumps({"error": f"read failed: {exc}"})

    line_count = source.count("\n") + 1
    if line_count > _MAX_LINES:
        return json.dumps({
            "error": f"file too large for outline ({line_count} lines > {_MAX_LINES} limit)",
        })

    lang = (language or _detect_language(p)).lower()
    try:
        if lang == "python":
            symbols = _outline_python(source)
        elif lang in ("javascript", "typescript"):
            symbols = _outline_ts_js(source)
        else:
            symbols = _outline_generic(source)
            lang = "generic"
    except Exception as exc:
        logger.exception("outline failed")
        return json.dumps({"error": f"outline failed: {exc}"})

    truncated = False
    if len(symbols) > _MAX_SYMBOLS:
        symbols = symbols[:_MAX_SYMBOLS]
        truncated = True

    return json.dumps({
        "path": str(p),
        "language": lang,
        "line_count": line_count,
        "symbols": symbols,
        "symbol_count": len(symbols),
        "truncated": truncated,
    }, ensure_ascii=False)


def _handle_outline_file(**kwargs):
    """支持 paths 数组（新规范）和 path 单值（向后兼容）。

    单文件返回 {path, language, line_count, symbols, ...}；
    多文件返回 {outlines: [<single-file result>, ...]}。
    """
    paths = kwargs.get("paths")
    language = kwargs.get("language")

    if paths is None:
        single = kwargs.get("path")
        if single:
            paths = [single]
    if isinstance(paths, str):
        paths = [paths]
    if not paths or not isinstance(paths, list):
        return json.dumps({"error": "paths is required (array of file paths)"})

    if len(paths) == 1:
        # 单文件路径：保持原有响应形态（向后兼容 165 次历史调用）
        return outline_file_tool(path=paths[0], language=language)

    # 多文件：包成 {outlines: [...]}
    outlines = []
    for p in paths:
        raw = outline_file_tool(path=p, language=language)
        try:
            outlines.append(json.loads(raw))
        except Exception:
            outlines.append({"path": p, "error": "outline parse failed"})
    return json.dumps({"outlines": outlines}, ensure_ascii=False)
