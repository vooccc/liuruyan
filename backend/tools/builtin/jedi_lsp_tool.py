"""LSP Tool - Python code intelligence using Jedi.

Provides IDE-level code intelligence for Python files:
- Go to definition
- Find references
- Hover information
- Document symbols
- Rename symbols
"""

import ast
import os
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

try:
    import jedi
    JEDI_AVAILABLE = True
except ImportError:
    JEDI_AVAILABLE = False


# Directories skipped during workspace-wide scans.
# Keep this conservative: only well-known noise dirs.
_WORKSPACE_SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", "env", ".env",
    "build", "dist", ".tox", ".eggs",
    "references",  # 第三方参考代码，避免噪音
}

# Workspace scan caps to avoid OOM on huge trees.
_WORKSPACE_MAX_FILES = 5000
_WORKSPACE_MAX_RESULTS = 200


def _iter_python_files(root: Path):
    """Walk root, yielding .py file paths while skipping noise dirs."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # in-place prune
        dirnames[:] = [d for d in dirnames if d not in _WORKSPACE_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn
                count += 1
                if count >= _WORKSPACE_MAX_FILES:
                    return


def _parse_ast_safe(file_path: Path) -> Optional[ast.AST]:
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return ast.parse(f.read(), filename=str(file_path))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return None


def _find_enclosing_def(tree: ast.AST, target_line: int) -> Optional[ast.AST]:
    """Find the innermost FunctionDef/AsyncFunctionDef/ClassDef containing target_line.

    target_line is 1-based.
    """
    candidate: Optional[ast.AST] = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", None) or start
        if start <= target_line <= end:
            # innermost wins: prefer node with the largest start line (deepest)
            if candidate is None or node.lineno > candidate.lineno:
                candidate = node
    return candidate


def _node_kind(node: ast.AST) -> str:
    if isinstance(node, ast.AsyncFunctionDef):
        return "async function"
    if isinstance(node, ast.FunctionDef):
        return "function"
    if isinstance(node, ast.ClassDef):
        return "class"
    return "module"


def _call_callee_name(call: ast.Call) -> Optional[Tuple[str, int, int]]:
    """Extract (name, line, col) of the callee in a Call node.

    Returns None for calls we can't statically name (e.g. (foo or bar)()).
    Line is 1-based; col is 0-based (Jedi convention internally).
    """
    func = call.func
    # Walk to the rightmost identifier we can resolve at call site.
    if isinstance(func, ast.Name):
        return (func.id, func.lineno, func.col_offset)
    if isinstance(func, ast.Attribute):
        # foo.bar.baz() → resolve at "baz" position
        return (func.attr, func.lineno, func.end_col_offset - len(func.attr) if func.end_col_offset else func.col_offset)
    return None


def _subseq_match(query: str, candidate: str) -> bool:
    """Fuzzy subsequence match: every char of query appears in candidate in order."""
    if not query:
        return True
    qi = 0
    cl = candidate.lower()
    ql = query.lower()
    for c in cl:
        if c == ql[qi]:
            qi += 1
            if qi == len(ql):
                return True
    return False


def _rank_match(query: str, name: str) -> int:
    """Lower is better. 0=exact, 1=prefix, 2=contains, 3=subseq."""
    if not query:
        return 4
    nl = name.lower()
    ql = query.lower()
    if nl == ql:
        return 0
    if nl.startswith(ql):
        return 1
    if ql in nl:
        return 2
    return 3


class JediLSPClient:
    """Python code intelligence using Jedi library."""

    name = "python"
    extensions = (".py",)

    def __init__(self):
        if not JEDI_AVAILABLE:
            raise RuntimeError("Jedi library not installed. Run: pip install jedi")

        self.project_path = None
        # P1: 缓存 jedi.Project 避免每次调用重建——Project 里有 import resolution
        # cache，重建等于丢 cache。lazy 初始化是因为 project_path 在 __init__
        # 之后才被 LSPTool 赋值。
        self._jedi_project: Optional['jedi.Project'] = None
        self._cached_project_path: Optional[str] = None

    def _get_project(self) -> Optional['jedi.Project']:
        """Lazily build & cache jedi.Project for self.project_path."""
        if not self.project_path:
            return None
        # 项目路径变了重建（运行期一般不变，但保险）
        if self._jedi_project is None or self._cached_project_path != self.project_path:
            self._jedi_project = jedi.Project(path=self.project_path)
            self._cached_project_path = self.project_path
        return self._jedi_project

    def _read_file(self, file_path: str) -> str:
        """Read file content."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            raise RuntimeError(f"Failed to read file {file_path}: {e}")

    def _create_script(self, file_path: str, line: int, column: int) -> 'jedi.Script':
        """Create Jedi Script object (reuses cached jedi.Project)."""
        code = self._read_file(file_path)
        # Jedi uses 1-based line numbers, 0-based columns
        return jedi.Script(
            code=code,
            path=file_path,
            project=self._get_project(),
        )

    async def go_to_definition(
        self,
        file_path: str,
        line: int,
        character: int
    ) -> Dict[str, Any]:
        """Find definition of symbol at position.

        Args:
            file_path: Absolute path to file
            line: Line number (1-based)
            character: Character offset (1-based)

        Returns:
            dict with 'success', 'locations' (list of {file, line, character, description})
        """
        try:
            script = self._create_script(file_path, line, character)

            # Jedi uses 1-based line, 0-based column
            definitions = script.goto(line, character - 1)

            if not definitions:
                return {
                    "success": True,
                    "locations": [],
                    "message": "No definition found"
                }

            locations = []
            for d in definitions:
                if d.module_path:
                    locations.append({
                        "file": str(d.module_path),
                        "line": d.line,
                        "character": d.column + 1,  # Convert to 1-based
                        "description": d.description,
                        "full_name": d.full_name
                    })

            return {
                "success": True,
                "locations": locations
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to find definition: {str(e)}"
            }

    async def find_references(
        self,
        file_path: str,
        line: int,
        character: int
    ) -> Dict[str, Any]:
        """Find all references to symbol at position.

        Args:
            file_path: Absolute path to file
            line: Line number (1-based)
            character: Character offset (1-based)

        Returns:
            dict with 'success', 'references' (list of {file, line, character})
        """
        try:
            script = self._create_script(file_path, line, character)

            # Jedi uses 1-based line, 0-based column
            references = script.get_references(line, character - 1)

            if not references:
                return {
                    "success": True,
                    "references": [],
                    "message": "No references found"
                }

            refs = []
            for ref in references:
                if ref.module_path:
                    refs.append({
                        "file": str(ref.module_path),
                        "line": ref.line,
                        "character": ref.column + 1,  # Convert to 1-based
                        "description": ref.description
                    })

            return {
                "success": True,
                "references": refs,
                "count": len(refs)
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to find references: {str(e)}"
            }

    async def hover(
        self,
        file_path: str,
        line: int,
        character: int
    ) -> Dict[str, Any]:
        """Get hover information for symbol at position.

        Args:
            file_path: Absolute path to file
            line: Line number (1-based)
            character: Character offset (1-based)

        Returns:
            dict with 'success', 'hover_text' (signature and docstring)
        """
        try:
            script = self._create_script(file_path, line, character)

            # Jedi uses 1-based line, 0-based column
            # Get help (docstring)
            help_results = script.help(line, character - 1)

            if not help_results:
                return {
                    "success": True,
                    "hover_text": "",
                    "message": "No information available"
                }

            hover_parts = []
            for h in help_results:
                # Add signature
                if hasattr(h, 'signatures') and h.signatures:
                    for sig in h.signatures:
                        hover_parts.append(f"```python\n{sig.to_string()}\n```")

                # Add full name
                if h.full_name:
                    hover_parts.append(f"**{h.full_name}**")

                # Add docstring
                if h.docstring():
                    hover_parts.append(h.docstring())

            return {
                "success": True,
                "hover_text": "\n\n".join(hover_parts)
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to get hover info: {str(e)}"
            }

    async def document_symbols(self, file_path: str) -> Dict[str, Any]:
        """Get all symbols in document.

        Args:
            file_path: Absolute path to file

        Returns:
            dict with 'success', 'symbols' (list of {name, kind, line, character})
        """
        try:
            code = self._read_file(file_path)
            script = jedi.Script(code=code, path=file_path, project=self._get_project())

            # Get all names in the file
            names = script.get_names(all_scopes=True, definitions=True, references=False)

            symbols = []
            for name in names:
                # Determine symbol kind
                kind = "unknown"
                if name.type == "class":
                    kind = "class"
                elif name.type == "function":
                    kind = "function"
                elif name.type == "module":
                    kind = "module"
                elif name.type == "param":
                    kind = "parameter"
                elif name.type == "statement":
                    kind = "variable"

                symbols.append({
                    "name": name.name,
                    "kind": kind,
                    "line": name.line,
                    "character": name.column + 1,  # Convert to 1-based
                    "full_name": name.full_name
                })

            return {
                "success": True,
                "symbols": symbols,
                "count": len(symbols)
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to get document symbols: {str(e)}"
            }

    async def workspace_symbol(self, query: str, root: Optional[str] = None) -> Dict[str, Any]:
        """Search symbols (classes/functions/methods) across the workspace by name.

        Args:
            query: Symbol name fragment. Empty/None lists everything (capped).
            root: Workspace root. Falls back to self.project_path or cwd.

        Returns:
            dict with 'success', 'symbols' (list of {name, kind, file, line, container, snippet}).
        """
        try:
            root_path = Path(root or self.project_path or os.getcwd()).resolve()
            if not root_path.is_dir():
                return {"success": False, "error": f"workspace root not a directory: {root_path}"}

            q = (query or "").strip()
            results: List[Dict[str, Any]] = []

            for py_file in _iter_python_files(root_path):
                tree = _parse_ast_safe(py_file)
                if tree is None:
                    continue

                # Track class stack for "container" attribution
                # We do a structured traversal instead of ast.walk to know parent class.
                def visit(node, container: Optional[str]):
                    for child in ast.iter_child_nodes(node):
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            name = child.name
                            if not q or _subseq_match(q, name):
                                results.append({
                                    "name": name,
                                    "kind": _node_kind(child),
                                    "file": str(py_file),
                                    "line": child.lineno,
                                    "character": child.col_offset + 1,
                                    "container": container,
                                    "_rank": _rank_match(q, name),
                                })
                            new_container = name if isinstance(child, ast.ClassDef) else container
                            visit(child, new_container)

                visit(tree, None)

                if len(results) >= _WORKSPACE_MAX_RESULTS * 4:
                    # gather plenty, then sort/cap below
                    break

            # Sort by rank (better matches first), then by file/line for stability
            results.sort(key=lambda r: (r["_rank"], r["file"], r["line"]))
            for r in results:
                r.pop("_rank", None)

            truncated = len(results) > _WORKSPACE_MAX_RESULTS
            results = results[:_WORKSPACE_MAX_RESULTS]

            return {
                "success": True,
                "symbols": results,
                "count": len(results),
                "truncated": truncated,
                "root": str(root_path),
            }

        except Exception as e:
            return {"success": False, "error": f"workspace_symbol failed: {e}"}

    async def outgoing_calls(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> Dict[str, Any]:
        """List all calls made from inside the function at the given position.

        Args:
            file_path: Absolute path to file.
            line: Line inside the target function (1-based).
            character: Character (1-based) — only used to disambiguate; can be 1.

        Returns:
            dict with 'success', 'caller' (the enclosing function info),
            'calls' (list of {name, line, character, target_file, target_line, target_kind, snippet}).
        """
        try:
            tree = _parse_ast_safe(Path(file_path))
            if tree is None:
                return {"success": False, "error": f"failed to parse: {file_path}"}

            enclosing = _find_enclosing_def(tree, line)
            if enclosing is None or not isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return {
                    "success": True,
                    "caller": None,
                    "calls": [],
                    "message": "no enclosing function at given line",
                }

            script = self._create_script(file_path, line, character)
            calls: List[Dict[str, Any]] = []
            seen: set = set()

            for node in ast.walk(enclosing):
                if not isinstance(node, ast.Call):
                    continue
                info = _call_callee_name(node)
                if info is None:
                    continue
                name, call_line, call_col = info

                # Resolve via Jedi (col is 0-based for Jedi)
                target_file = None
                target_line = None
                target_kind = None
                try:
                    defs = script.goto(call_line, max(0, call_col), follow_imports=True, follow_builtin_imports=False)
                    for d in defs:
                        if d.module_path and d.line:
                            target_file = str(d.module_path)
                            target_line = d.line
                            target_kind = d.type or "unknown"
                            break
                except Exception:
                    pass

                key = (name, call_line, call_col, target_file, target_line)
                if key in seen:
                    continue
                seen.add(key)

                calls.append({
                    "name": name,
                    "line": call_line,
                    "character": call_col + 1,
                    "target_file": target_file,
                    "target_line": target_line,
                    "target_kind": target_kind,
                })

            calls.sort(key=lambda c: (c["line"], c["character"]))

            return {
                "success": True,
                "caller": {
                    "name": enclosing.name,
                    "kind": _node_kind(enclosing),
                    "file": file_path,
                    "line": enclosing.lineno,
                },
                "calls": calls,
                "count": len(calls),
            }

        except Exception as e:
            return {"success": False, "error": f"outgoing_calls failed: {e}"}

    async def incoming_calls(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> Dict[str, Any]:
        """Find all functions that call the symbol at the given position.

        Args:
            file_path: Absolute path to file.
            line: Line of target symbol (1-based).
            character: Character offset (1-based).

        Returns:
            dict with 'success', 'target' (the called symbol info),
            'callers' (list of {caller_name, caller_kind, caller_file, caller_line, call_site_line, call_site_character}).
        """
        try:
            script = self._create_script(file_path, line, character)

            # Resolve target name for reporting
            target_name = None
            try:
                defs = script.goto(line, character - 1, follow_imports=False)
                if defs:
                    target_name = defs[0].name
            except Exception:
                pass

            references = script.get_references(line, character - 1, include_builtins=False)
            if not references:
                return {
                    "success": True,
                    "target": {"name": target_name, "file": file_path, "line": line},
                    "callers": [],
                    "count": 0,
                }

            # Group references by caller (enclosing function), de-dup
            ast_cache: Dict[str, Optional[ast.AST]] = {}
            callers_map: Dict[Tuple[str, int, str], Dict[str, Any]] = {}

            for ref in references:
                if not ref.module_path:
                    continue
                ref_file = str(ref.module_path)
                # Skip the definition site itself
                try:
                    if ref.is_definition():
                        continue
                except Exception:
                    pass

                if ref_file not in ast_cache:
                    ast_cache[ref_file] = _parse_ast_safe(Path(ref_file))
                tree = ast_cache[ref_file]
                if tree is None:
                    continue

                enc = _find_enclosing_def(tree, ref.line)
                if enc is None:
                    caller_name = "<module>"
                    caller_kind = "module"
                    caller_line = 1
                else:
                    caller_name = enc.name
                    caller_kind = _node_kind(enc)
                    caller_line = enc.lineno

                key = (ref_file, caller_line, caller_name)
                entry = callers_map.get(key)
                if entry is None:
                    entry = {
                        "caller_name": caller_name,
                        "caller_kind": caller_kind,
                        "caller_file": ref_file,
                        "caller_line": caller_line,
                        "call_sites": [],
                    }
                    callers_map[key] = entry
                entry["call_sites"].append({
                    "line": ref.line,
                    "character": ref.column + 1,
                })

            callers = sorted(
                callers_map.values(),
                key=lambda c: (c["caller_file"], c["caller_line"]),
            )

            return {
                "success": True,
                "target": {"name": target_name, "file": file_path, "line": line},
                "callers": callers,
                "count": len(callers),
            }

        except Exception as e:
            return {"success": False, "error": f"incoming_calls failed: {e}"}

    async def diagnostics(self, file_path: str) -> Dict[str, Any]:
        """Return syntactic diagnostics for a Python file (jedi.get_syntax_errors).

        仅香法层错误（SyntaxError）。语义层错误（undefined name / type 错 / unused
        import 等）需要接 pyright/pyflakes/ruff——本轮不依赖外部进程，所以仅
        语法。Returned items: 1-based positions, severity='error'.
        """
        try:
            code = self._read_file(file_path)
            script = jedi.Script(code=code, path=file_path, project=self._get_project())
            errors = script.get_syntax_errors() or []
            items: List[Dict[str, Any]] = []
            for e in errors:
                items.append({
                    "line": getattr(e, "line", 0),
                    "character": getattr(e, "column", 0) + 1,
                    "end_line": getattr(e, "until_line", None) or getattr(e, "line", 0),
                    "end_character": (getattr(e, "until_column", None) or 0) + 1,
                    "severity": "error",
                    "message": e.get_message() if hasattr(e, "get_message") else str(e),
                    "source": "jedi",
                })
            return {
                "success": True,
                "diagnostics": items,
                "count": len(items),
                "file": file_path,
                "note": "Syntax-level only. For semantic diagnostics (undefined names, type errors), run pyright or mypy via execute_command.",
            }
        except Exception as e:
            return {"success": False, "error": f"diagnostics failed: {e}"}

    async def go_to_implementation(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> Dict[str, Any]:
        """goToImplementation is a TS/Java concept (find concrete impls of an interface).

        Python doesn't have interfaces; we honestly report unsupported and tell the
        model to use goToDefinition instead. Returning success=True + empty list +
        explanatory message keeps the schema consistent.
        """
        return {
            "success": True,
            "locations": [],
            "message": (
                "goToImplementation is TypeScript-specific (concrete impls of an "
                "interface/abstract method). Python has no interface concept; use "
                "goToDefinition or findReferences for class subclasses instead."
            ),
        }

    async def rename(
        self,
        file_path: str,
        line: int,
        character: int,
        new_name: str
    ) -> Dict[str, Any]:
        """Get all locations that need to be renamed.

        Note: This returns the locations, actual renaming should be done by the caller.

        Args:
            file_path: Absolute path to file
            line: Line number (1-based)
            character: Character offset (1-based)
            new_name: New name for the symbol

        Returns:
            dict with 'success', 'changes' (dict of file -> list of {line, character, old_name})
        """
        try:
            script = self._create_script(file_path, line, character)

            # Get all references (including definition)
            references = script.get_references(line, character - 1, include_builtins=False)

            if not references:
                return {
                    "success": True,
                    "changes": {},
                    "message": "No references found to rename"
                }

            # Group by file
            changes_by_file = {}
            for ref in references:
                if ref.module_path:
                    file_key = str(ref.module_path)
                    if file_key not in changes_by_file:
                        changes_by_file[file_key] = []

                    changes_by_file[file_key].append({
                        "line": ref.line,
                        "character": ref.column + 1,  # Convert to 1-based
                        "old_name": ref.name,
                        "new_name": new_name
                    })

            # Sort changes by line (reverse order for safe replacement)
            for file_key in changes_by_file:
                changes_by_file[file_key].sort(key=lambda x: x["line"], reverse=True)

            total_changes = sum(len(changes) for changes in changes_by_file.values())

            return {
                "success": True,
                "changes": changes_by_file,
                "total_changes": total_changes,
                "files_affected": len(changes_by_file)
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to prepare rename: {str(e)}"
            }


class LSPTool:
    """LSP façade — dispatches operations to language-specific backends.

    Each backend exposes the same async method surface (go_to_definition,
    find_references, hover, document_symbols, workspace_symbol,
    incoming_calls, outgoing_calls, rename). To add a new language (e.g.
    Java), implement that surface on a new client class and register it in
    `__init__`. Nothing in `execute` or the registered tool schema needs to
    change.
    """

    def __init__(self, project_path: Optional[str] = None):
        self.project_path = project_path

        # Register backends. Order matters only for workspaceSymbol fan-out
        # display; lookup is keyed by extension.
        self._backends_by_ext: Dict[str, Any] = {}
        self._all_backends: List[Any] = []

        # Python (Jedi) — always available if jedi imported successfully
        if JEDI_AVAILABLE:
            jedi_client = JediLSPClient()
            jedi_client.project_path = project_path
            self._register_backend(jedi_client, [".py"])

        # TypeScript / JS — optional, requires frontend/ + node
        try:
            from .ts_lsp_tool import TypeScriptLSPClient, SUPPORTED_EXTS
            ts_client = TypeScriptLSPClient(project_path=project_path)
            self._register_backend(ts_client, list(SUPPORTED_EXTS))
        except ImportError:
            pass

    def _register_backend(self, backend: Any, extensions: List[str]) -> None:
        for ext in extensions:
            self._backends_by_ext[ext.lower()] = backend
        if backend not in self._all_backends:
            self._all_backends.append(backend)

    def _backend_for_file(self, file_path: str) -> Optional[Any]:
        ext = Path(file_path).suffix.lower()
        return self._backends_by_ext.get(ext)

    @staticmethod
    def _format(result: Dict[str, Any]) -> dict:
        """Wrap a client result dict into the {success, output, error} contract."""
        import json
        if result.get("success"):
            return {
                "success": True,
                "output": json.dumps(result, indent=2, ensure_ascii=False),
                "error": "",
            }
        return {
            "success": False,
            "output": "",
            "error": result.get("error", "Unknown error"),
        }

    async def execute(
        self,
        operation: str,
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        character: Optional[int] = None,
        **kwargs
    ) -> dict:
        """Execute LSP operation.

        Args:
            operation: One of 'goToDefinition', 'findReferences', 'hover',
                      'documentSymbol', 'rename', 'workspaceSymbol',
                      'incomingCalls', 'outgoingCalls'
            file_path: Absolute or relative path to file (not required for workspaceSymbol)
            line: Line number (1-based) — required for position-based ops
            character: Character offset (1-based) — required for position-based ops
            **kwargs: Additional arguments:
                - new_name (str): for rename
                - query (str): for workspaceSymbol
                - root (str, optional): for workspaceSymbol

        Returns:
            dict with operation result
        """
        position_required_ops = {
            "goToDefinition", "findReferences", "hover", "rename",
            "incomingCalls", "outgoingCalls", "goToImplementation",
        }
        file_required_ops = position_required_ops | {"documentSymbol", "diagnostics"}

        # ---------------------------------------------------------------
        # workspaceSymbol: fan out across all backends, merge & rank
        # ---------------------------------------------------------------
        if operation == "workspaceSymbol":
            query = kwargs.get("query", "") or ""
            root = kwargs.get("root")
            if not root:
                try:
                    from .file_tools import _current_working_dir
                    wd = _current_working_dir()
                    if wd:
                        root = str(wd)
                except Exception:
                    pass
            language = kwargs.get("language")  # optional filter: "python" | "typescript" | ...

            backends_to_use = self._all_backends
            if language:
                backends_to_use = [b for b in self._all_backends if getattr(b, "name", "") == language]
                if not backends_to_use:
                    return {"success": False, "output": "",
                            "error": f"no backend named '{language}'. "
                                     f"available: {[getattr(b, 'name', '?') for b in self._all_backends]}"}

            merged: List[Dict[str, Any]] = []
            errors: List[str] = []
            for backend in backends_to_use:
                try:
                    sub = await backend.workspace_symbol(query=query, root=root)
                except Exception as e:
                    errors.append(f"{getattr(backend, 'name', '?')}: {e}")
                    continue
                if not sub.get("success"):
                    errors.append(f"{getattr(backend, 'name', '?')}: {sub.get('error', 'unknown')}")
                    continue
                lang_tag = getattr(backend, "name", "?")
                for sym in sub.get("symbols") or []:
                    sym["language"] = lang_tag
                    merged.append(sym)

            return self._format({
                "success": True,
                "symbols": merged,
                "count": len(merged),
                "languages": [getattr(b, "name", "?") for b in backends_to_use],
                "partial_errors": errors or None,
            })

        # ---------------------------------------------------------------
        # File-scoped ops: validate file + route to backend by extension
        # ---------------------------------------------------------------
        if operation not in file_required_ops:
            return {"success": False, "output": "",
                    "error": f"Unknown operation: {operation}"}

        if not file_path:
            return {"success": False, "output": "",
                    "error": f"{operation} requires 'file_path'"}
        try:
            from .file_tools import _resolve_read_path
            resolved_p = _resolve_read_path(file_path)
            abs_path = str(resolved_p.resolve())
        except Exception:
            abs_path = os.path.abspath(os.path.expanduser(file_path))
        if not os.path.isfile(abs_path):
            return {"success": False, "output": "",
                    "error": f"File not found: {file_path}"}

        backend = self._backend_for_file(abs_path)
        if backend is None:
            supported = sorted(self._backends_by_ext.keys())
            return {"success": False, "output": "",
                    "error": f"No backend for extension {Path(abs_path).suffix}. "
                             f"Supported: {supported}"}

        if operation in position_required_ops:
            if line is None or character is None:
                return {"success": False, "output": "",
                        "error": f"{operation} requires 'line' and 'character'"}

        # ---------------------------------------------------------------
        # Dispatch
        # ---------------------------------------------------------------
        try:
            if operation == "goToDefinition":
                result = await backend.go_to_definition(abs_path, line, character)
            elif operation == "findReferences":
                result = await backend.find_references(abs_path, line, character)
            elif operation == "hover":
                result = await backend.hover(abs_path, line, character)
            elif operation == "documentSymbol":
                result = await backend.document_symbols(abs_path)
            elif operation == "rename":
                new_name = kwargs.get("new_name")
                if not new_name:
                    return {
                        "success": False,
                        "output": "",
                        "error": "rename operation requires 'new_name' parameter"
                    }
                result = await backend.rename(abs_path, line, character, new_name)
            elif operation == "incomingCalls":
                result = await backend.incoming_calls(abs_path, line, character)
            elif operation == "outgoingCalls":
                result = await backend.outgoing_calls(abs_path, line, character)
            elif operation == "goToImplementation":
                result = await backend.go_to_implementation(abs_path, line, character)
            elif operation == "diagnostics":
                result = await backend.diagnostics(abs_path)
            else:
                return {
                    "success": False,
                    "output": "",
                    "error": f"Unknown operation: {operation}"
                }

            # Format result
            if result.get("success"):
                import json
                return {
                    "success": True,
                    "output": json.dumps(result, indent=2, ensure_ascii=False),
                    "error": ""
                }
            else:
                return {
                    "success": False,
                    "output": "",
                    "error": result.get("error", "Unknown error")
                }

        except Exception as e:
            return {
                "success": False,
                "output": "",
                "error": f"LSP operation failed: {str(e)}"
            }

    def cleanup(self):
        """Clean up backend resources (no-op for Jedi/TS subprocess backends)."""
        for backend in self._all_backends:
            cleanup = getattr(backend, "cleanup", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception:
                    pass
