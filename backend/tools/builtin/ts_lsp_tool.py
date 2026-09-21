"""TypeScript / TSX / JS / JSX code intelligence backend.

Sister file to `jedi_lsp_tool.py`. Same method surface so they're interchangeable
at the `LSPTool` dispatch layer.

Implementation: dispatch each op to `frontend/scripts/ts-intel.cjs`. By default
runs a long-lived daemon (subprocess held alive across calls, idle-shutdown at
2 min) for ~15-30x speedup over spawn-per-call. Falls back to one-shot if the
daemon repeatedly fails to spawn or communicate.

Disable daemon by setting env `LIURUYAN_TS_INTEL_NO_DAEMON=1`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from ._ts_intel_daemon import TSIntelDaemon


# -----------------------------------------------------------------------------
# Path resolution
# -----------------------------------------------------------------------------
def _project_root() -> Path:
    # backend/tools/builtin/ts_lsp_tool.py → 3 levels up
    return Path(__file__).resolve().parents[3]


def _frontend_dir() -> Optional[Path]:
    for cand in (_project_root() / "frontend",):
        if (cand / "tsconfig.json").exists() and (cand / "node_modules" / "typescript").exists():
            return cand
    return None


def _ts_intel_script() -> Optional[Path]:
    fe = _frontend_dir()
    if not fe:
        return None
    s = fe / "scripts" / "ts-intel.cjs"
    return s if s.exists() else None


SUPPORTED_EXTS = (".ts", ".tsx", ".js", ".jsx", ".cts", ".mts", ".cjs", ".mjs")


# -----------------------------------------------------------------------------
# Backend
# -----------------------------------------------------------------------------
class TypeScriptLSPClient:
    """TS/JS code intelligence via TypeScript LanguageService (Node subprocess)."""

    name = "typescript"
    extensions = SUPPORTED_EXTS

    def __init__(self, project_path: Optional[str] = None, timeout: float = 30.0):
        self.project_path = project_path
        self.timeout = timeout
        # daemon 是 lazy 构建的——直到第一次调用 _run 才 spawn 子进程
        self._daemon: Optional[TSIntelDaemon] = None
        self._daemon_disabled = bool(os.environ.get("LIURUYAN_TS_INTEL_NO_DAEMON"))

    def is_available(self) -> Optional[str]:
        """Return None if available, otherwise an error string."""
        if _frontend_dir() is None:
            return "frontend/ with typescript not found"
        if _ts_intel_script() is None:
            return "frontend/scripts/ts-intel.cjs missing"
        return None

    def _get_daemon(self) -> Optional[TSIntelDaemon]:
        if self._daemon_disabled:
            return None
        if self._daemon is None:
            script = _ts_intel_script()
            cwd = _frontend_dir()
            if script is None or cwd is None:
                return None
            self._daemon = TSIntelDaemon(script_path=script, cwd=cwd)
        return self._daemon

    async def _run(self, op: str, **kw) -> Dict[str, Any]:
        err = self.is_available()
        if err:
            return {"success": False, "error": err}

        payload: Dict[str, Any] = {"op": op}
        for k, v in kw.items():
            if v is not None:
                payload[k] = v

        # --- Path 1: daemon (preferred) -----------------------------------
        daemon = self._get_daemon()
        if daemon is not None and not daemon.in_cooldown:
            resp = await daemon.request(payload)
            if resp is not None:
                # strip the protocol "id" before returning to caller
                resp.pop("id", None)
                return resp
            # daemon failed for this call → fall through to one-shot below.
            # The daemon's own consecutive-failure counter will trip the
            # cooldown if it keeps failing.

        # --- Path 2: one-shot fallback ------------------------------------
        return await self._run_oneshot(payload)

    async def _run_oneshot(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Legacy spawn-per-call path. Used when daemon disabled / cooling down."""
        script = _ts_intel_script()
        node_exe = "node"
        try:
            exec_kwargs: Dict[str, Any] = {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "cwd": str(_frontend_dir()),
            }
            if os.name == "nt":
                exec_kwargs["creationflags"] = 0x08000000
            proc = await asyncio.create_subprocess_exec(
                node_exe,
                str(script),
                json.dumps(payload),
                **exec_kwargs,
            )
        except FileNotFoundError:
            return {"success": False, "error": "node executable not found in PATH"}
        except Exception as e:
            return {"success": False, "error": f"failed to spawn node: {e}"}

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return {"success": False, "error": f"ts-intel timed out after {self.timeout}s"}

        stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()

        if not stdout:
            return {"success": False, "error": f"ts-intel produced no stdout. stderr: {stderr[:500]}"}

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            return {
                "success": False,
                "error": f"ts-intel returned non-JSON: {e}. stdout[:500]={stdout[:500]} stderr[:300]={stderr[:300]}",
            }
        return data

    def cleanup(self) -> None:
        """Best-effort sync shutdown — closes stdin; daemon's own idle timer kills it shortly after."""
        if self._daemon is not None:
            proc = self._daemon._proc  # type: ignore[attr-defined]
            if proc is not None and proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            self._daemon = None

    # --- API matching JediLSPClient surface ---

    async def go_to_definition(self, file_path: str, line: int, character: int) -> Dict[str, Any]:
        return await self._run("goToDefinition", file=file_path, line=line, character=character)

    async def find_references(self, file_path: str, line: int, character: int) -> Dict[str, Any]:
        return await self._run("findReferences", file=file_path, line=line, character=character)

    async def hover(self, file_path: str, line: int, character: int) -> Dict[str, Any]:
        return await self._run("hover", file=file_path, line=line, character=character)

    async def document_symbols(self, file_path: str) -> Dict[str, Any]:
        return await self._run("documentSymbol", file=file_path)

    async def workspace_symbol(self, query: str, root: Optional[str] = None) -> Dict[str, Any]:
        # `root` is honored only as tsconfig override; for standard layout we
        # always use frontend/tsconfig.json. Pass through if explicitly given.
        kw = {"query": query or ""}
        if root:
            # Treat root as a tsconfig path if it looks like one
            if root.endswith(".json"):
                kw["tsconfig"] = root
        return await self._run("workspaceSymbol", **kw)

    async def incoming_calls(self, file_path: str, line: int, character: int) -> Dict[str, Any]:
        return await self._run("incomingCalls", file=file_path, line=line, character=character)

    async def outgoing_calls(self, file_path: str, line: int, character: int) -> Dict[str, Any]:
        return await self._run("outgoingCalls", file=file_path, line=line, character=character)

    async def rename(self, file_path: str, line: int, character: int, new_name: str) -> Dict[str, Any]:
        return await self._run("rename", file=file_path, line=line, character=character, newName=new_name)

    async def go_to_implementation(self, file_path: str, line: int, character: int) -> Dict[str, Any]:
        return await self._run("goToImplementation", file=file_path, line=line, character=character)

    async def diagnostics(self, file_path: str) -> Dict[str, Any]:
        return await self._run("diagnostics", file=file_path)
