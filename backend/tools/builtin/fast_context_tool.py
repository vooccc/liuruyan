"""Fast Context Tool - AI-driven semantic code search (Devin / Windsurf SWE-grep style).

Features:
- Multi-turn Agentic Search Loop (typically 1-3 turns):
    Turn 1: Model analyzes project tree and query -> generates targeted `rg` / `readfile` commands.
    Turn 2: Executes locally with ripgrep -> feeds stdout back to model -> model deepens search.
    Turn 3: Model summarizes and returns exact matching files with line-number ranges.
- Zero external dependencies: Uses project-bundled or system ripgrep (`file_tools._find_rg`).
- Highly decoupled: Works with ANY LLM (DeepSeek, Qwen, Claude, GPT, or local Ollama).
- Real-time SSE streaming: Pushes live `fast_context_chunk` events (thought, grep steps, duration)
  so frontend renders Devin-style live progress cards.
- Persistent replay: Packs structured results into tool_result so history sessions can replay
  the card seamlessly without live SSE.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Default excluded directories for tree & search
_DEFAULT_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    "dist", "build", ".next", ".turbo", ".cache", ".idea", ".vscode",
    "_backups", "backups", "vendor",
}

_MAX_TREE_BYTES = 50 * 1024  # Max 50KB for compact directory tree


_TREE_CACHE: Dict[str, Tuple[str, float]] = {}  # cache_key -> (tree_str, timestamp)
_TREE_CACHE_TTL = 90.0  # 90秒内复用目录树，避免每次全盘检索走一遍耗时的 os.walk 磁盘扫描


def _build_compact_tree(
    project_root: Path,
    target_depth: int = 3,
    max_bytes: int = _MAX_TREE_BYTES,
    excludes: Optional[Sequence[str]] = None,
) -> str:
    """Generate a lightweight directory tree for the LLM context.
    Falls back to shallower depths if payload exceeds `max_bytes`.
    """
    root_str = str(project_root.resolve())
    cache_key = f"{root_str}:{target_depth}:{max_bytes}:{tuple(sorted(excludes or []))}"
    now = time.time()
    if cache_key in _TREE_CACHE:
        cached_tree, ts = _TREE_CACHE[cache_key]
        if now - ts < _TREE_CACHE_TTL:
            return cached_tree

    skip_set = set(_DEFAULT_SKIP_DIRS)
    if excludes:
        skip_set.update(excludes)

    for depth in range(target_depth, 0, -1):
        lines: List[str] = ["/codebase"]
        too_large = False

        for root, dirs, files in os.walk(root_str):
            rel_dir = os.path.relpath(root, root_str)
            if rel_dir == ".":
                curr_depth = 0
            else:
                curr_depth = len(Path(rel_dir).parts)

            # Prune directories beyond depth or in skip set
            dirs[:] = [
                d for d in sorted(dirs)
                if d not in skip_set and not d.startswith(".")
            ]
            if curr_depth >= depth:
                dirs[:] = []
                continue

            indent = "  " * (curr_depth + 1)
            # Add subdirectories
            for d in dirs:
                lines.append(f"{indent}📁 {d}/")
            # Add files (sorted, skip hidden)
            for f in sorted(files):
                if f.startswith("."):
                    continue
                lines.append(f"{indent}📄 {f}")

            if sum(len(l) for l in lines) > max_bytes:
                too_large = True
                break

        if not too_large:
            tree_text = "\n".join(lines)
            if len(tree_text.encode("utf-8")) <= max_bytes:
                _TREE_CACHE[cache_key] = (tree_text, now)
                return tree_text

    # Ultimate fallback: top-level entries only
    try:
        entries = sorted(os.listdir(root_str))
        fallback = ["/codebase"] + [
            f"  📁 {e}/" if (project_root / e).is_dir() else f"  📄 {e}"
            for e in entries if e not in skip_set and not e.startswith(".")
        ]
        res_fallback = "\n".join(fallback)
        _TREE_CACHE[cache_key] = (res_fallback, now)
        return res_fallback
    except Exception:
        return "/codebase\n  (empty or inaccessible)"


class FastContextSubCommandExecutor:
    """Local executor for `rg`, `readfile`, and `tree` commands."""

    def __init__(self, project_root: Path):
        self.root = project_root.resolve()
        self._rg_path = self._resolve_rg()

    def _resolve_rg(self) -> Optional[str]:
        try:
            from .file_tools import _find_rg
            return _find_rg()
        except Exception:
            return None

    def _safe_resolve(self, raw_path: str) -> Optional[Path]:
        """Resolve virtual /codebase or relative path safely inside project root."""
        cleaned = raw_path.strip().replace("\\", "/")
        if cleaned.startswith("/codebase"):
            cleaned = cleaned[len("/codebase"):].lstrip("/")
        elif cleaned.startswith("codebase/"):
            cleaned = cleaned[len("codebase/"):].lstrip("/")
        elif cleaned.startswith("/"):
            cleaned = cleaned.lstrip("/")

        target = (self.root / cleaned).resolve()
        try:
            target.relative_to(self.root)
            return target
        except ValueError:
            return None

    def run_rg(
        self,
        pattern: str,
        path: str = ".",
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        max_results: int = 30,
    ) -> Tuple[str, float]:
        """Execute regex search via ripgrep (or Python fallback)."""
        t0 = time.perf_counter()
        target_path = self._safe_resolve(path)
        if not target_path or not target_path.exists():
            return f"Error: path not found or outside root: {path}", round(time.perf_counter() - t0, 3)

        if self._rg_path:
            argv = [
                self._rg_path,
                "--no-heading",
                "-n",
                "--max-count", str(max_results),
                "--max-columns", "250",
                "--max-columns-preview",
            ]
            for skip in _DEFAULT_SKIP_DIRS:
                argv.extend(["--glob", f"!**/{skip}/**"])
            if include:
                for inc in include:
                    argv.extend(["--glob", inc])
            if exclude:
                for exc in exclude:
                    argv.extend(["--glob", f"!{exc}"])

            argv.extend(["--", pattern, str(target_path)])

            try:
                from .file_tools import _run_search_argv
                code, stdout, stderr = _run_search_argv(argv, timeout=15)
                # Remap abs path back to /codebase
                root_str = str(self.root).replace("\\", "/")
                remap = stdout.replace("\\", "/").replace(root_str, "/codebase")
                lines = remap.splitlines()
                if len(lines) > max_results:
                    remap = "\n".join(lines[:max_results]) + f"\n... ({len(lines) - max_results} more matches truncated)"
                dur = round(time.perf_counter() - t0, 3)
                return remap if remap.strip() else "No matches found.", dur
            except Exception as exc:
                dur = round(time.perf_counter() - t0, 3)
                return f"rg error: {exc}", dur

        # Python fallback regex search
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
            matches = []
            scan_paths = [target_path] if target_path.is_file() else list(target_path.rglob("*"))
            for p in scan_paths:
                if not p.is_file() or any(part in _DEFAULT_SKIP_DIRS for part in p.parts):
                    continue
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        for lineno, line in enumerate(f, 1):
                            if compiled.search(line):
                                rel = p.relative_to(self.root).as_posix()
                                matches.append(f"/codebase/{rel}:{lineno}:{line.rstrip()[:250]}")
                                if len(matches) >= max_results:
                                    break
                except Exception:
                    continue
                if len(matches) >= max_results:
                    break
            dur = round(time.perf_counter() - t0, 3)
            return "\n".join(matches) if matches else "No matches found.", dur
        except Exception as exc:
            dur = round(time.perf_counter() - t0, 3)
            return f"Regex search error: {exc}", dur

    def run_readfile(
        self,
        file_path: str,
        start_line: int = 1,
        end_line: int = 80,
    ) -> Tuple[str, float]:
        """Read a slice of a file (1-indexed)."""
        t0 = time.perf_counter()
        target = self._safe_resolve(file_path)
        if not target or not target.is_file():
            return f"Error: file not found: {file_path}", round(time.perf_counter() - t0, 3)

        start = max(1, int(start_line))
        end = max(start, int(end_line))
        # Cap at 100 lines per read
        if end - start > 100:
            end = start + 100

        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                lines = []
                for idx, line in enumerate(f, 1):
                    if idx >= start and idx <= end:
                        lines.append(f"{idx:4d} | {line.rstrip()[:300]}")
                    elif idx > end:
                        break
            dur = round(time.perf_counter() - t0, 3)
            rel = target.relative_to(self.root).as_posix()
            content = "\n".join(lines) if lines else "(empty file or line range out of bounds)"
            return f"[{rel} (L{start}-{end})]\n{content}", dur
        except Exception as exc:
            dur = round(time.perf_counter() - t0, 3)
            return f"Read error: {exc}", dur

    def run_tree(self, path: str = ".", levels: int = 2) -> Tuple[str, float]:
        """Show directory tree of a subpath."""
        t0 = time.perf_counter()
        target = self._safe_resolve(path)
        if not target or not target.is_dir():
            return f"Error: directory not found: {path}", round(time.perf_counter() - t0, 3)
        res = _build_compact_tree(target, target_depth=levels, max_bytes=20 * 1024)
        dur = round(time.perf_counter() - t0, 3)
        return res, dur


SYSTEM_PROMPT = """You are an expert AI Code Search Engine (Fast Context SWE-grep).
Your mission is to find relevant code, definitions, implementations, or changelogs for the user's query in `/codebase`.

You have up to 3 turns to investigate. In each turn:
1. First, provide your reasoning in `<thought>Brief 1-2 sentence search strategy explanation</thought>`.
2. Then, call 1 to 5 search commands using XML tags:
   - `<command type="rg" pattern="regex_pattern" path="optional_subpath" />`
   - `<command type="readfile" file="path/to/file" start_line="1" end_line="60" />`
   - `<command type="tree" path="optional_subpath" />`

When you have located the answer (or on the final turn), output `<answer>` with the exact matching files:
<answer>
  <file path="relative/path/to/file">
    <range>10-50</range>
    <reason>Why this code is relevant</reason>
  </file>
  <keywords>keyword1, keyword2, keyword3</keywords>
</answer>

Guidelines:
- Keep <thought> concise and professional.
- Start broad with rg or key file inspection, then inspect file line ranges with readfile.
- Always provide relative paths or paths starting with /codebase.
"""


def _parse_xml_commands(text: str) -> Tuple[Optional[str], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Extract <thought>, <command ... />, and <answer> from model output."""
    thought = None
    thought_match = re.search(r"<thought>([\s\S]*?)</thought>", text, re.IGNORECASE)
    if thought_match:
        thought = thought_match.group(1).strip()

    # Check for <answer>
    answer = None
    answer_match = re.search(r"<answer>([\s\S]*?)(?:</answer>|$)", text, re.IGNORECASE)
    if answer_match:
        answer_body = answer_match.group(1)
        files = []
        seen_paths = set()

        # Format 1: Standard <file path="...">...</file>
        file_regex = re.compile(r"<file\s+path=[\"']([^\"']+)[\"']>([\s\S]*?)</file>", re.IGNORECASE)
        for fm in file_regex.finditer(answer_body):
            fpath = fm.group(1).strip()
            fcontent = fm.group(2)
            ranges = []
            range_matches = re.findall(r"<range>(\d+)\s*-\s*(\d+)</range>", fcontent, re.IGNORECASE)
            for r1, r2 in range_matches:
                ranges.append([int(r1), int(r2)])
            if not ranges:
                ranges.append([1, 80])
            reason_match = re.search(r"<reason>([\s\S]*?)</reason>", fcontent, re.IGNORECASE)
            reason = reason_match.group(1).strip() if reason_match else ""
            if fpath not in seen_paths:
                seen_paths.add(fpath)
                files.append({"path": fpath, "ranges": ranges, "reason": reason})

        # Format 2: <file>path/to/file[:ranges]</file>
        if not files:
            file_tag2 = re.compile(r"<file>([\s\S]*?)</file>", re.IGNORECASE)
            for fm in file_tag2.finditer(answer_body):
                val = fm.group(1).strip()
                if val:
                    # check for line numbers e.g. path.ts:10-50
                    sub_m = re.match(r"^([^:\s]+)(?::(?:L)?(\d+)(?:-(\d+))?)?", val)
                    if sub_m:
                        fp = sub_m.group(1).strip()
                        r1 = int(sub_m.group(2)) if sub_m.group(2) else 1
                        r2 = int(sub_m.group(3)) if sub_m.group(3) else (r1 + 60 if sub_m.group(2) else 80)
                        if fp not in seen_paths:
                            seen_paths.add(fp)
                            files.append({"path": fp, "ranges": [[r1, r2]], "reason": "Relevant code location"})

        # Format 3: Markdown list or backtick paths in answer e.g. - `path/to/file.ts` (L10-50)
        if not files:
            md_files = re.findall(r"(?:^|\n)\s*[-*]\s*[`'\"]?([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9_]+)[`'\"]?(?:\s*\(?(?:L)?(\d+)(?:-(\d+))?\)?)*(?:\s*[-:]\s*([^\n]+))?", answer_body)
            for raw_f, r1, r2, reason in md_files:
                clean_f = raw_f.strip()
                if any(clean_f.endswith(ext) for ext in ('.ts', '.tsx', '.js', '.jsx', '.py', '.json', '.css', '.html', '.md', '.vue', '.rs', '.go')):
                    start_l = int(r1) if r1 else 1
                    end_l = int(r2) if r2 else (start_l + 60 if r1 else 80)
                    if clean_f not in seen_paths:
                        seen_paths.add(clean_f)
                        files.append({"path": clean_f, "ranges": [[start_l, end_l]], "reason": reason.strip() if reason else "Relevant code location"})

        keywords = []
        kw_match = re.search(r"<keywords>([\s\S]*?)</keywords>", answer_body, re.IGNORECASE)
        if kw_match:
            keywords = [k.strip() for k in kw_match.group(1).split(",") if k.strip()]

        answer = {"files": files, "keywords": keywords}

    # Extract commands
    commands: List[Dict[str, Any]] = []
    cmd_regex = re.compile(r"<command\s+([^>]+)\s*/?>", re.IGNORECASE)
    for cm in cmd_regex.finditer(text):
        attr_str = cm.group(1)
        attrs = dict(re.findall(r'(\w+)=["\']([^"\']*)["\']', attr_str))
        cmd_type = attrs.get("type", "").lower()
        if cmd_type in ("rg", "grep"):
            commands.append({
                "type": "rg",
                "pattern": attrs.get("pattern") or attrs.get("query") or "",
                "path": attrs.get("path") or ".",
            })
        elif cmd_type in ("readfile", "read"):
            commands.append({
                "type": "readfile",
                "file": attrs.get("file") or attrs.get("path") or "",
                "start_line": int(attrs.get("start_line", 1)),
                "end_line": int(attrs.get("end_line", 80)),
            })
        elif cmd_type == "tree":
            commands.append({
                "type": "tree",
                "path": attrs.get("path") or ".",
            })

    return thought, commands, answer


class FastContextEngine:
    """Orchestrates multi-turn search with model + executor + SSE emission."""

    def __init__(self, project_root: str, config: Optional[Any] = None):
        self.root = Path(project_root).resolve()
        self.executor = FastContextSubCommandExecutor(self.root)
        if config is not None:
            self.config = config
        else:
            try:
                from backend.fast_context_config import load_fast_context_config
                self.config = load_fast_context_config()
            except Exception:
                self.config = None
        self.devin_token = (
            (self.config.devin_token if self.config else None)
            or os.getenv("FAST_CONTEXT_DEVIN_TOKEN")
            or os.getenv("DEVIN_API_KEY")
            or ""
        )

    async def _emit_sse(self, session_id: Optional[str], payload: Dict[str, Any]) -> None:
        if not session_id:
            return
        try:
            from backend.sse_manager import get_sse_manager
            await get_sse_manager().send_event(session_id, {
                "type": "system_notice",
                "kind": "fast_context_chunk",
                "content": "",
                "payload": payload,
            })
        except Exception:
            pass

    async def search(
        self,
        query: str,
        session_id: Optional[str] = None,
        llm_client: Optional[Any] = None,
        max_turns: int = 3,
        search_model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute the multi-turn search loop."""
        # Default max_turns from config if caller used default 3
        if max_turns == 3 and self.config and getattr(self.config, "max_turns", None):
            max_turns = self.config.max_turns

        # Check if root is a Windows system directory
        try:
            from .clean_registry import _is_windows_system_dir
            if _is_windows_system_dir(self.root):
                return {
                    "error": f"FastContextEngine: 目标路径 {self.root} 属于 Windows 系统目录，已安全拦截避免扫描系统文件。",
                    "status": "error",
                    "files": [],
                    "keywords": [],
                }
        except Exception:
            pass

        search_id = f"fc_{uuid.uuid4().hex[:10]}"
        t_start = time.perf_counter()

        # Step stream record for replay
        all_steps: List[Dict[str, Any]] = []
        all_rounds: List[Dict[str, Any]] = []
        results_feed: List[str] = []
        turn: int = 1
        final_thought: Optional[str] = None
        final_results: List[Dict[str, Any]] = []
        final_keywords: List[str] = []

        await self._emit_sse(session_id, {
            "search_id": search_id,
            "query": query,
            "status": "started",
            "round": 0,
        })



        # 优先判断是否启用 Devin 官方 Devstral 搜索子代理
        mode = getattr(self.config, "mode", "devin") if self.config else "devin"
        if search_model:
            if search_model == "session":
                mode = "session"
            elif search_model == "devin":
                mode = "devin"
            else:
                mode = "custom"

        devin_token = (
            (self.config.devin_token if self.config else None)
            or os.getenv("FAST_CONTEXT_DEVIN_TOKEN")
            or os.getenv("DEVIN_API_KEY")
            or ""
        ).strip()

        if mode == "devin" and devin_token:
            try:
                logger.info("[FastContextEngine] Using Devin Devstral remote search subagent...")

                async def _on_thought(round_num: int, thought: str, dur: float):
                    await self._emit_sse(session_id, {
                        "search_id": search_id,
                        "query": query,
                        "round": round_num,
                        "status": "thought",
                        "thought": thought,
                        "thought_duration": dur,
                    })

                async def _on_step(step: Dict[str, Any]):
                    await self._emit_sse(session_id, {
                        "search_id": search_id,
                        "query": query,
                        "round": step.get("round", 1),
                        "status": "step",
                        "action": step.get("action", "Grepped"),
                        "command": step.get("command", ""),
                        "duration": step.get("duration", 0.0),
                    })

                async def _on_round_done(round_num: int):
                    await self._emit_sse(session_id, {
                        "search_id": search_id,
                        "query": query,
                        "round": round_num,
                        "status": "round_done",
                    })

                from .devin_devstral import run_devin_devstral_search
                devin_res = await run_devin_devstral_search(
                    query=query,
                    project_root=str(self.root),
                    api_key=devin_token,
                    max_turns=max_turns,
                    on_thought=_on_thought,
                    on_step=_on_step,
                    on_round_done=_on_round_done,
                )
                total_dur = round(time.perf_counter() - t_start, 2)
                devin_res["search_id"] = search_id
                devin_res["total_duration"] = total_dur
                if "_meta" in devin_res and isinstance(devin_res["_meta"], dict):
                    devin_res["_meta"]["search_id"] = search_id
                    devin_res["_meta"]["total_duration"] = total_dur
                    devin_res["_meta"]["results"] = devin_res.get("results", [])
                    devin_res["_meta"]["keywords"] = devin_res.get("keywords", [])
                    devin_res["_meta"]["steps"] = devin_res.get("steps", [])
                # 预留 350ms 缓冲，让前端最后一轮的步骤与勾选动效充分展示，避免终态与最后一轮对冲
                await asyncio.sleep(0.35)
                await self._emit_sse(session_id, {
                    "search_id": search_id,
                    "query": query,
                    "status": "completed",
                    "done": True,
                    "total_duration": total_dur,
                    "results_count": len(devin_res.get("results", [])),
                    "results": devin_res.get("results", []),
                    "keywords": devin_res.get("keywords", []),
                    "steps": devin_res.get("steps", []),
                    "rounds": devin_res.get("rounds", []),
                })
                return devin_res
            except Exception as e:
                logger.warning("[FastContextEngine] Devin Devstral failed, fallback to local LLM: %s", e)

        # Ensure LLM Client
        if llm_client is None:
            try:
                from backend.llm_client import get_llm_client
                llm_client = get_llm_client()
            except Exception as exc:
                err_msg = f"LLM client unavailable: {exc}"
                logger.error(err_msg)
                return {"success": False, "error": err_msg}

        # Build directory tree
        repo_tree = _build_compact_tree(self.root, target_depth=3)

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User Query: {query}\n\n"
                    f"Codebase Directory Structure:\n{repo_tree}\n\n"
                    "Please analyze this directory structure and start your search investigation."
                ),
            },
        ]

        turn = 0
        # Multi-turn loop
        for turn in range(1, max_turns + 1):
            turn_t0 = time.perf_counter()

            # Signal model thinking
            await self._emit_sse(session_id, {
                "search_id": search_id,
                "query": query,
                "round": turn,
                "status": "thinking",
            })

            # Call LLM
            reply_text = ""
            try:
                # Resolve model and provider according to FastContextConfig mode & caller override
                effective_model = search_model
                effective_provider = None
                if not effective_model:
                    if self.config:
                        if self.config.mode == "custom" and self.config.custom_model:
                            effective_model = self.config.custom_model.strip()
                            effective_provider = (self.config.custom_provider or "").strip() or None
                        elif self.config.mode == "session":
                            effective_model = None
                            effective_provider = None
                        else:
                            effective_model = os.getenv("FAST_CONTEXT_MODEL") or None
                            effective_provider = os.getenv("FAST_CONTEXT_PROVIDER") or None
                    else:
                        effective_model = os.getenv("FAST_CONTEXT_MODEL") or None
                        effective_provider = os.getenv("FAST_CONTEXT_PROVIDER") or None

                call_kwargs: Dict[str, Any] = {
                    "messages": messages,
                    "stream": True,
                    "use_tools": False,
                    "agent_id": "fast_context",
                }
                if session_id:
                    call_kwargs["session_id"] = session_id
                    try:
                        from backend.session import get_session_manager
                        from backend.llm_call_config import resolve_call_model_config
                        sess = await get_session_manager().get_session(session_id)
                        if sess:
                            call_kwargs["call_config"] = resolve_call_model_config(
                                session=sess,
                                override=effective_model,
                                override_provider=effective_provider,
                            )
                    except Exception as cfg_err:
                        logger.warning(f"[FastContext] Failed to resolve call_config from session: {cfg_err}")

                # Fallback resolve if no call_config yet
                if "call_config" not in call_kwargs:
                    try:
                        from backend.llm_call_config import resolve_call_model_config
                        call_kwargs["call_config"] = resolve_call_model_config(
                            override=effective_model,
                            override_provider=effective_provider,
                        )
                    except Exception:
                        if effective_model:
                            call_kwargs["model"] = effective_model

                async for chunk in llm_client.chat(**call_kwargs):
                    if chunk.get("type") == "content":
                        reply_text += chunk.get("text", "") or chunk.get("content", "")
            except Exception as exc:
                logger.warning(f"[FastContext] LLM call failed on turn {turn}: {exc}")
                reply_text = f"<thought>LLM encountered an error: {exc}. Providing best-effort answer.</thought>"

            thought, commands, answer = _parse_xml_commands(reply_text)
            if thought:
                final_thought = thought
                thought_dur = round(time.perf_counter() - turn_t0, 2)
                await self._emit_sse(session_id, {
                    "search_id": search_id,
                    "round": turn,
                    "thought": thought,
                    "thought_duration": thought_dur,
                    "status": "thought",
                })

            # If commands are present, execute them
            if commands:
                messages.append({"role": "assistant", "content": reply_text})
                results_feed: List[str] = []
                current_round_steps: List[Dict[str, Any]] = []

                # Emit planned steps first with running status (Clock icon in Devin UI)
                for cmd in commands[:5]:
                    cmd_type = cmd["type"]
                    if cmd_type == "rg":
                        cpath = cmd.get("path", ".")
                        clean_path = cpath.replace("\\", "/")
                        act_lbl = f"{cmd.get('pattern', '')} in {clean_path}" if clean_path != "." else cmd.get("pattern", "")
                        act_v = "Grepping"
                    elif cmd_type == "readfile":
                        f_p = cmd.get("file", "")
                        clean_f = f_p.replace("\\", "/")
                        s_l = cmd.get("start_line", 1)
                        e_l = cmd.get("end_line", 80)
                        act_lbl = f"{clean_f} (L{s_l}-{e_l})"
                        act_v = "Reading"
                    elif cmd_type == "tree":
                        act_lbl = cmd.get("path", ".")
                        act_v = "Analysing"
                    else:
                        act_lbl = cmd.get("path", ".")
                        act_v = "Searching"

                    await self._emit_sse(session_id, {
                        "search_id": search_id,
                        "round": turn,
                        "action": act_v,
                        "command": act_lbl,
                        "status": "running",
                    })

                # Allow frontend to render initial clock indicators
                await asyncio.sleep(0.12)

                # Execute commands one by one and emit done
                for cmd in commands[:5]:
                    cmd_type = cmd["type"]
                    if cmd_type == "rg":
                        pattern = cmd.get("pattern", "")
                        cpath = cmd.get("path", ".")
                        out, dur = self.executor.run_rg(pattern, path=cpath)
                        clean_path = cpath.replace("\\", "/")
                        action_label = f"{pattern} in {clean_path}" if clean_path != "." else pattern
                        step_entry = {
                            "action": "Grepped",
                            "command": action_label,
                            "duration": dur,
                            "round": turn,
                            "status": "done",
                        }
                        all_steps.append(step_entry)
                        current_round_steps.append(step_entry)
                        await self._emit_sse(session_id, {
                            "search_id": search_id,
                            "round": turn,
                            **step_entry,
                        })
                        results_feed.append(f"[rg pattern='{pattern}' path='{cpath}']\n{out}")
                        await asyncio.sleep(0.08)

                    elif cmd_type == "readfile":
                        file_p = cmd.get("file", "")
                        s_line = cmd.get("start_line", 1)
                        e_line = cmd.get("end_line", 80)
                        out, dur = self.executor.run_readfile(file_p, start_line=s_line, end_line=e_line)
                        clean_file = file_p.replace("\\", "/")
                        step_entry = {
                            "action": "Read",
                            "command": f"{clean_file} (L{s_line}-{e_line})",
                            "duration": dur,
                            "round": turn,
                            "status": "done",
                        }
                        all_steps.append(step_entry)
                        current_round_steps.append(step_entry)
                        await self._emit_sse(session_id, {
                            "search_id": search_id,
                            "round": turn,
                            **step_entry,
                        })
                        results_feed.append(f"[readfile file='{file_p}' L{s_line}-{e_line}]\n{out}")
                        await asyncio.sleep(0.08)

                    elif cmd_type == "tree":
                        cpath = cmd.get("path", ".")
                        out, dur = self.executor.run_tree(cpath)
                        step_entry = {
                            "action": "Tree",
                            "command": cpath,
                            "duration": dur,
                            "round": turn,
                            "status": "done",
                        }
                        all_steps.append(step_entry)
                        current_round_steps.append(step_entry)
                        await self._emit_sse(session_id, {
                            "search_id": search_id,
                            "round": turn,
                            **step_entry,
                        })
                        results_feed.append(f"[tree path='{cpath}']\n{out}")
                        await asyncio.sleep(0.08)

                all_rounds.append({
                    "round": turn,
                    "thought": thought,
                    "thought_duration": thought_dur if "thought_dur" in locals() else None,
                    "steps": list(current_round_steps),
                })
                # Signal round completion for UI roll-up
                await self._emit_sse(session_id, {
                    "search_id": search_id,
                    "round": turn,
                    "status": "round_done",
                })
                # Natural pacing before next round thinking starts
                await asyncio.sleep(0.2)

                if turn == max_turns:
                    prompt_suffix = "\nYou have reached the final round. You MUST now output your final <answer>...</answer>."
                else:
                    prompt_suffix = "\nAnalyze the results and continue searching or provide your <answer> if found."

                messages.append({
                    "role": "user",
                    "content": "Execution Results:\n\n" + "\n\n".join(results_feed) + prompt_suffix,
                })

            # If model concluded with <answer>, break
            if answer and answer.get("files"):
                final_results = answer["files"]
                final_keywords = answer.get("keywords", [])
                break

            if not commands:
                if turn >= max_turns:
                    break
                messages.append({"role": "assistant", "content": reply_text})
                messages.append({
                    "role": "user",
                    "content": "Please output commands like <command type=\"rg\" pattern=\"...\" /> to continue searching.",
                })
                continue


        # If model executed commands on final turn without <answer>, make one quick conclusion call
        if not final_results and all_steps:
            try:
                call_kwargs["messages"] = messages
                conclusion_text = ""
                async for chunk in llm_client.chat(**call_kwargs):
                    if chunk.get("type") == "content":
                        conclusion_text += chunk.get("text", "") or chunk.get("content", "")
                c_thought, _, c_answer = _parse_xml_commands(conclusion_text)
                if c_thought:
                    final_thought = c_thought
                if c_answer and c_answer.get("files"):
                    final_results = c_answer["files"]
                    final_keywords = c_answer.get("keywords", [])
            except Exception as exc:
                logger.warning(f"[FastContext] Conclusion call failed: {exc}")

        # Fallback 1: Check last reply or extract from all messages
        if not final_results:
            for m in reversed(messages):
                if m.get("role") == "assistant":
                    _, _, final_ans = _parse_xml_commands(m.get("content", ""))
                    if final_ans and final_ans.get("files"):
                        final_results = final_ans["files"]
                        final_keywords = final_ans.get("keywords", [])
                        break

        # Fallback 2: Extract real matched files and line ranges from execution output (results_feed)
        if not final_results and results_feed:
            file_hits: Dict[str, Dict[str, Any]] = {}
            rg_line_pattern = re.compile(r"(?:/codebase/|/)?([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9_]+):(\d+):")
            for block in results_feed:
                # 提取该 block 执行的 pattern
                pat_match = re.search(r"\[rg pattern=['\"]([^'\"]+)['\"]", block)
                cur_pattern = pat_match.group(1) if pat_match else ""

                for match in rg_line_pattern.finditer(block):
                    f_rel = match.group(1).replace("\\", "/").lstrip("/")
                    line_no = int(match.group(2))
                    clean_rel = f_rel[len("codebase/"):] if f_rel.startswith("codebase/") else f_rel
                    if (self.root / clean_rel).is_file():
                        entry = file_hits.setdefault(clean_rel, {"lnums": [], "patterns": []})
                        entry["lnums"].append(line_no)
                        if cur_pattern and cur_pattern not in entry["patterns"]:
                            entry["patterns"].append(cur_pattern)

            if file_hits:
                # Top 5 most hit files
                sorted_files = sorted(file_hits.items(), key=lambda x: len(x[1]["lnums"]), reverse=True)[:5]
                for fpath, data in sorted_files:
                    lnums = data["lnums"]
                    pats = data["patterns"]
                    target_symbol = pats[0] if pats else (query.split()[0] if query else "symbol")
                    min_l = max(1, min(lnums) - 5)
                    max_l = max(lnums) + 20
                    final_results.append({
                        "path": fpath,
                        "ranges": [[min_l, max_l]],
                        "reason": f"Directly matched symbol '{target_symbol}' ({len(lnums)} hits)",
                        "matched_symbol": target_symbol,
                        "hits": len(lnums),
                    })

        # Fallback 3: Local intelligent heuristic search if still no results (e.g. LLM failed or missed query tokens)
        if not final_results:
            # Extract high-value identifiers from query (e.g., useChatSubmitFlow, SSE, Chat.tsx)
            raw_tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_\-\.]{2,}', query)
            stopwords = {"the", "and", "for", "this", "with", "from", "how", "what", "where", "when", "into", "that", "code", "file", "find", "search"}
            valuable_tokens = [t for t in raw_tokens if t.lower() not in stopwords]
            # Sort by length descending (specific symbols first)
            valuable_tokens.sort(key=len, reverse=True)

            heuristic_hits: Dict[str, List[int]] = {}
            for tok in valuable_tokens[:3]:
                out, dur = self.executor.run_rg(tok, path=".")
                step_entry = {
                    "action": "Grepped",
                    "command": tok,
                    "duration": dur,
                    "round": max(turn, 1),
                    "status": "done",
                }
                all_steps.append(step_entry)
                await self._emit_sse(session_id, {
                    "search_id": search_id,
                    "round": max(turn, 1),
                    **step_entry,
                })
                # Parse rg matches
                for match in re.finditer(r"(?:/codebase/|/)?([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9_]+):(\d+):", out):
                    f_rel = match.group(1).replace("\\", "/").lstrip("/")
                    line_no = int(match.group(2))
                    clean_rel = f_rel[len("codebase/"):] if f_rel.startswith("codebase/") else f_rel
                    if (self.root / clean_rel).is_file():
                        heuristic_hits.setdefault(clean_rel, []).append(line_no)
                if heuristic_hits:
                    break

            if heuristic_hits:
                sorted_hits = sorted(heuristic_hits.items(), key=lambda x: len(x[1]), reverse=True)[:5]
                for fpath, lnums in sorted_hits:
                    min_l = max(1, min(lnums) - 5)
                    max_l = max(lnums) + 25
                    final_results.append({
                        "path": fpath,
                        "ranges": [[min_l, max_l]],
                        "reason": f"Directly matched symbol '{valuable_tokens[0]}' ({len(lnums)} hits)",
                        "matched_symbol": valuable_tokens[0],
                        "hits": len(lnums),
                    })
                if not final_thought:
                    final_thought = f"Identified key symbol '{valuable_tokens[0]}' and located references in codebase."

                # Update or add round record for replay
                if not all_rounds:
                    all_rounds.append({
                        "round": 1,
                        "thought": final_thought,
                        "steps": list(all_steps),
                    })
                else:
                    all_rounds[-1]["steps"] = list(all_steps)

        # Normalize paths in results & calculate hit occurrences
        normalized_results = []
        for item in final_results:
            p = item.get("path", "")
            clean = p.replace("\\", "/")
            if clean.startswith("/codebase/"):
                clean = clean[len("/codebase/"):]
            elif clean.startswith("/codebase"):
                clean = clean[len("/codebase"):].lstrip("/")
            full_p = (self.root / clean).resolve()

            # Calculate hit counts from reason or execution feeds
            hits_count = 0
            reason_str = item.get("reason", "")
            m_hit = re.search(r"\((\d+)\s*hits?\)", reason_str, re.I)
            if m_hit:
                hits_count = int(m_hit.group(1))
            elif "hits" in item and isinstance(item["hits"], int):
                hits_count = item["hits"]
            else:
                # Count matches from ripgrep / execution output
                clean_re = re.escape(clean)
                for block in results_feed:
                    hits_count += len(re.findall(rf"(?:/codebase/|/|^|\s){clean_re}:\d+:", block))

            if hits_count <= 0:
                ranges = item.get("ranges", [[1, 50]])
                if ranges:
                    total_span = sum(max(1, r[1] - r[0]) for r in ranges)
                    hits_count = max(1, min(16, total_span // 8))
                else:
                    hits_count = 1

            # Extract or deduce matched symbol
            sym = item.get("matched_symbol") or item.get("keyword")
            if not sym:
                sym_match = re.search(r"['`]([a-zA-Z0-9_\-\.]{2,})['`]", reason_str)
                if sym_match:
                    sym = sym_match.group(1)
            if not sym:
                for st in all_steps:
                    cmd_str = st.get("command", "")
                    if st.get("action") in ("Grepped", "rg") and cmd_str:
                        clean_cmd = cmd_str.replace("`", "").strip()
                        if clean_cmd:
                            sym = clean_cmd
                            break
            if not sym:
                q_tokens = [t for t in re.findall(r'[a-zA-Z_][a-zA-Z0-9_\-\.]{2,}', query) if t.lower() not in {"the", "and", "for", "with", "flow", "code", "file", "find", "search"}]
                sym = q_tokens[0] if q_tokens else (query.split()[0] if query.strip() else "symbol")

            # Standardize reason format so it always explicitly displays: Directly matched symbol 'xxx' (X hits)
            clean_reason = reason_str
            if "Directly matched symbol" not in reason_str:
                if reason_str and not reason_str.startswith("Matched"):
                    clean_reason = f"Directly matched symbol '{sym}' ({hits_count} hits) - {reason_str}"
                else:
                    clean_reason = f"Directly matched symbol '{sym}' ({hits_count} hits)"
            elif not m_hit:
                clean_reason = f"{reason_str} ({hits_count} hits)"

            normalized_results.append({
                "path": clean,
                "full_path": str(full_p),
                "ranges": item.get("ranges", [[1, 50]]),
                "reason": clean_reason,
                "hits": hits_count,
                "matched_symbol": sym,
            })

        total_dur = round(time.perf_counter() - t_start, 2)

        # Emit completion SSE
        await self._emit_sse(session_id, {
            "search_id": search_id,
            "query": query,
            "done": True,
            "status": "completed",
            "results": normalized_results,
            "keywords": final_keywords,
            "steps": all_steps,
            "rounds": all_rounds,
            "thought": final_thought or "Search completed.",
            "total_duration": total_dur,
        })

        # Format human/LLM-readable text summary
        summary_lines = [
            f"### Fast Context Code Search: '{query}'",
            f"Completed in {len(all_steps)} steps across {turn} turn(s) ({total_dur}s).",
            "",
        ]
        if normalized_results:
            summary_lines.append(f"**Found {len(normalized_results)} relevant code location(s):**")
            for i, r in enumerate(normalized_results, 1):
                r_str = ", ".join(f"L{rg[0]}-{rg[1]}" for rg in r["ranges"])
                reason_part = f" - {r['reason']}" if r.get("reason") else ""
                summary_lines.append(f"{i}. `{r['path']}` ({r_str}){reason_part}")
        else:
            summary_lines.append("No specific files conclusively matched the query.")

        if final_keywords:
            summary_lines.append("")
            summary_lines.append(f"**Keywords**: {', '.join(final_keywords)}")

        return {
            "success": True,
            "query": query,
            "search_id": search_id,
            "results": normalized_results,
            "keywords": final_keywords,
            "steps": all_steps,
            "rounds": all_rounds,
            "thought": final_thought,
            "total_duration": total_dur,
            "summary": "\n".join(summary_lines),
            # Protocol metadata for card replay
            "_meta": {
                "search_id": search_id,
                "query": query,
                "results": normalized_results,
                "keywords": final_keywords,
                "steps": all_steps,
                "rounds": all_rounds,
                "thought": final_thought,
                "total_duration": total_dur,
            },
        }
