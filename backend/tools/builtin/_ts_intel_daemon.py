"""TS-intel daemon subprocess manager — long-lived node process for ts-intel.cjs.

设计取舍：
- 单 daemon 进程跑 `ts-intel.cjs --daemon`，stdin/stdout 行式 JSON-RPC
- asyncio.Lock 强制串行请求，简单 + 正确（pipeline async 易出错且 TS LS 内部
  也是同步的，串行不亏）
- daemon 进程自己看 idle timer (env TS_INTEL_IDLE_MS，默认 120s) 自杀，Python 端
  不需要管生命周期
- 失败回退：spawn 失败 / write 失败 / 子进程已死 → 客户端把 daemon 标记为
  unhealthy，下一次调用 spawn 新的；连续失败 N 次进入 cooldown，走 spawn-per-call

此模块只暴露 `TSIntelDaemon` 类，由 TypeScriptLSPClient 使用。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional


_READY_TIMEOUT_S = 15.0
_REQUEST_TIMEOUT_S = 60.0
_HEALTH_FAIL_THRESHOLD = 2     # 连续失败 N 次进入 cooldown
_HEALTH_COOLDOWN_S = 30.0      # cooldown 期间所有调用走 fallback


class TSIntelDaemon:
    """One long-lived node subprocess running ts-intel.cjs --daemon.

    Methods are async-safe via an internal lock. The daemon may die at any
    time (idle timeout / crash) — `request` transparently respawns on first
    use after death. Repeated spawn/communication failures put the daemon
    into a cooldown window during which `request` returns None to signal
    the caller should use spawn-per-call fallback.
    """

    def __init__(self, script_path: Path, cwd: Path, node_exe: str = "node"):
        self._script_path = script_path
        self._cwd = cwd
        self._node_exe = node_exe
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._req_id = 0
        self._consecutive_failures = 0
        self._cooldown_until: float = 0.0
        self._stderr_drain_task: Optional[asyncio.Task] = None

    @property
    def in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    async def _spawn(self) -> bool:
        """Spawn daemon and await its 'ready' line. Return True on success."""
        try:
            exec_kwargs: Dict[str, Any] = {
                "stdin": asyncio.subprocess.PIPE,
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "cwd": str(self._cwd),
            }
            if os.name == "nt":
                exec_kwargs["creationflags"] = 0x08000000
            self._proc = await asyncio.create_subprocess_exec(
                self._node_exe,
                str(self._script_path),
                "--daemon",
                **exec_kwargs,
            )
        except FileNotFoundError:
            self._record_failure()
            return False
        except Exception:
            self._record_failure()
            return False

        # Drain stderr in background to avoid pipe blocking on node warnings.
        async def _drain():
            try:
                assert self._proc and self._proc.stderr
                while True:
                    line = await self._proc.stderr.readline()
                    if not line:
                        break
                    # discard quietly; uncomment for debug:
                    # print(f"[ts-intel-daemon stderr] {line.decode(errors='replace').rstrip()}")
            except Exception:
                pass

        self._stderr_drain_task = asyncio.create_task(_drain())

        # Await ready line
        try:
            line_b = await asyncio.wait_for(
                self._proc.stdout.readline(), timeout=_READY_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            await self._terminate()
            self._record_failure()
            return False

        if not line_b:
            self._record_failure()
            return False

        try:
            msg = json.loads(line_b.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            await self._terminate()
            self._record_failure()
            return False

        if not msg.get("ready"):
            await self._terminate()
            self._record_failure()
            return False

        return True

    async def _ensure_alive(self) -> bool:
        if self._proc and self._proc.returncode is None:
            return True
        return await self._spawn()

    async def _terminate(self) -> None:
        if self._proc:
            try:
                if self._proc.returncode is None:
                    self._proc.terminate()
                    try:
                        await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        self._proc.kill()
            except ProcessLookupError:
                pass
            except Exception:
                pass
        self._proc = None
        if self._stderr_drain_task:
            self._stderr_drain_task.cancel()
            self._stderr_drain_task = None

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _HEALTH_FAIL_THRESHOLD:
            self._cooldown_until = time.monotonic() + _HEALTH_COOLDOWN_S

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._cooldown_until = 0.0

    async def request(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Send one request, return parsed response dict.

        Returns None if daemon is in cooldown or unrecoverable — caller should
        fall back to spawn-per-call.
        """
        if self.in_cooldown:
            return None

        async with self._lock:
            if not await self._ensure_alive():
                return None

            self._req_id += 1
            payload = dict(payload)
            payload["id"] = str(self._req_id)
            line = json.dumps(payload, ensure_ascii=False) + "\n"

            assert self._proc and self._proc.stdin and self._proc.stdout
            try:
                self._proc.stdin.write(line.encode("utf-8"))
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                await self._terminate()
                self._record_failure()
                return None

            try:
                resp_b = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=_REQUEST_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                await self._terminate()
                self._record_failure()
                return None

            if not resp_b:
                # daemon exited mid-request
                await self._terminate()
                self._record_failure()
                return None

            try:
                resp = json.loads(resp_b.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                self._record_failure()
                return None

            self._record_success()
            return resp

    async def shutdown(self) -> None:
        """Signal daemon to exit (stdin EOF)."""
        async with self._lock:
            if self._proc and self._proc.stdin:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
            await self._terminate()
