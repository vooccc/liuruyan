"""BackgroundCommandManager —— 后台命令执行 + 增量状态查询的统一管理器。

为什么要这个：
- 长命令（pytest / npm install / build）阻塞式执行时，模型只能等结果一次性返回，
  期间用户什么都看不到，体感断裂；同时也可能撞 timeout。
- 采用 Cascade 同款方案：execute_command 加 blocking=False 时立刻返回 command_id，
  模型用 command_status 拉增量输出 / 等待完成 / 主动 kill。
- 这套机制对 bash 和 PowerShell 通用，所以抽到一个独立 manager 单例。

关键约束：
- asyncio 友好：用 asyncio.create_subprocess_exec，stdout 异步读到 ring buffer
- 缓冲上限 1MB（防长命令爆内存），过载时丢前端保后端（用户最关心末尾）
- TTL 清理：1800s 没被查询的已结束命令自动 GC，防内存泄漏
- 进程组 kill：Unix 用 setsid + killpg，Windows 用 terminate / kill
- 单例线程安全：通过 asyncio.Lock 保护字典写
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

_IS_WINDOWS = platform.system() == "Windows"

# 单条命令累计输出上限（bytes）。超出时旧字节被丢——LLM 关心末尾尾巴
_OUTPUT_BUFFER_BYTES = 1_048_576  # 1 MB

# 已结束命令保留多久（秒），超时自动从字典 GC
_DEAD_COMMAND_TTL = 1800  # 30 min

# 单次 get_status(output_chars=N) 默认返回末尾多少字符
_DEFAULT_OUTPUT_CHARS = 4000


# stream_callback 类型：kind 是 'stdout' 或 'stderr'；text 是本次增量文本
StreamCallback = Callable[[str, str], Awaitable[None]]


@dataclass
class _CommandRecord:
    """单条后台命令的元数据 + 状态。"""

    command_id: str
    command: str
    workdir: str
    started_at: float
    proc: Optional[asyncio.subprocess.Process] = None
    # bytes ring buffer——存原始字节，end 阶段再 decode（避免半字符截断 + 节省内存）
    stdout_buf: Deque[int] = field(default_factory=lambda: deque(maxlen=_OUTPUT_BUFFER_BYTES))
    stderr_buf: Deque[int] = field(default_factory=lambda: deque(maxlen=_OUTPUT_BUFFER_BYTES))
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0
    exit_code: Optional[int] = None
    finished_at: Optional[float] = None
    last_polled_at: float = field(default_factory=time.time)
    reader_task_out: Optional[asyncio.Task] = None
    reader_task_err: Optional[asyncio.Task] = None
    waiter_task: Optional[asyncio.Task] = None
    status_event: asyncio.Event = field(default_factory=asyncio.Event)
    killed: bool = False
    error: Optional[str] = None  # 启动 / 读流过程中的非命令错误
    stream_callback: Optional[StreamCallback] = None

    def status_str(self) -> str:
        """对外暴露的简化状态标签。"""
        if self.error:
            return "error"
        if self.killed and self.exit_code is None:
            return "killed"
        if self.exit_code is not None:
            return "done"
        return "running"

    @staticmethod
    def _tail_text(buf: Deque[int], max_chars: int) -> str:
        if max_chars <= 0:
            return ""
        full = bytes(buf).decode("utf-8", errors="replace")
        if len(full) <= max_chars:
            return full
        return full[-max_chars:]

    def stdout_text(self, max_chars: int) -> str:
        return self._tail_text(self.stdout_buf, max_chars)

    def stderr_text(self, max_chars: int) -> str:
        return self._tail_text(self.stderr_buf, max_chars)

    # 向后兼容：output_text 返 stdout+stderr 合并（与原 "output" 语义一致）
    def output_text(self, max_chars: int) -> str:
        out = self.stdout_text(max_chars)
        err = self.stderr_text(max_chars)
        if not err:
            return out
        if not out:
            return err
        # 合并后再取末尾 max_chars，保证不超岸
        combined = out + "\n" + err
        if len(combined) > max_chars:
            combined = combined[-max_chars:]
        return combined


class BackgroundCommandManager:
    """全局单例：管理所有后台 shell / powershell 进程。"""

    def __init__(self):
        self._records: Dict[str, _CommandRecord] = {}
        self._lock = asyncio.Lock()

    def new_command_id(self) -> str:
        return f"cmd-{uuid.uuid4().hex[:12]}"

    async def start(
        self,
        argv: List[str],
        command_text: str,
        workdir: str,
        env: Optional[Dict[str, str]] = None,
        stream_callback: Optional[StreamCallback] = None,
        command_id: Optional[str] = None,
    ) -> str:
        """启动后台进程，返回 command_id。立即可用，不阻塞等命令完成。

        argv: 直接传 create_subprocess_exec 的 args（已经包含 shell 启动器和脚本）
        command_text: 仅作元数据展示，让用户/模型查 status 时知道这是哪条命令
        stream_callback: 可选 async callable(kind, text)——读到增量输出时变打 SSE
        command_id: 可选预生成 id，供调用方让 SSE chunk 与返回 command_id 稳定一致
        """
        cid = command_id or self.new_command_id()
        rec = _CommandRecord(
            command_id=cid,
            command=command_text,
            workdir=workdir,
            started_at=time.time(),
            stream_callback=stream_callback,
        )
        try:
            # 进程组隔离：Unix 用 start_new_session（=preexec_fn setsid），方便 kill 整组
            # Windows 用 CREATE_NEW_PROCESS_GROUP，便于发 CTRL_BREAK
            kwargs: Dict[str, Any] = {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,  # 不再 merge——独立 buffer
                "stdin": asyncio.subprocess.DEVNULL,
                "cwd": workdir,
                "env": env,
            }
            if _IS_WINDOWS:
                # CREATE_NO_WINDOW = 0x08000000 | CREATE_NEW_PROCESS_GROUP = 0x00000200
                kwargs["creationflags"] = 0x08000000 | 0x00000200
            else:
                kwargs["start_new_session"] = True

            rec.proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        except Exception as e:
            rec.error = f"failed to start: {type(e).__name__}: {e}"
            rec.exit_code = -1
            rec.finished_at = time.time()
            rec.status_event.set()
            async with self._lock:
                self._records[cid] = rec
            return cid

        async with self._lock:
            self._records[cid] = rec

        # 异步读 stdout / stderr 各自累积进 buffer + 可选推送 SSE
        rec.reader_task_out = asyncio.create_task(self._reader_loop(rec, "stdout"))
        rec.reader_task_err = asyncio.create_task(self._reader_loop(rec, "stderr"))
        # 异步等进程结束，更新 exit_code
        rec.waiter_task = asyncio.create_task(self._waiter(rec))

        return cid

    async def _reader_loop(self, rec: _CommandRecord, kind: str) -> None:
        """从子进程 stdout / stderr 持续读取字节追加到对应 ring buffer。"""
        if rec.proc is None:
            return
        stream = rec.proc.stdout if kind == "stdout" else rec.proc.stderr
        if stream is None:
            return
        buf = rec.stdout_buf if kind == "stdout" else rec.stderr_buf
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                # 追加到 deque：超过 maxlen 旧 bytes 自动丢，统计丢失量
                room = _OUTPUT_BUFFER_BYTES - len(buf)
                if len(chunk) > room:
                    if kind == "stdout":
                        rec.stdout_dropped_bytes += len(chunk) - room
                    else:
                        rec.stderr_dropped_bytes += len(chunk) - room
                buf.extend(chunk)
                # 增量推 SSE（有 stream_callback 时）
                if rec.stream_callback is not None:
                    try:
                        text = chunk.decode("utf-8", errors="replace")
                        await rec.stream_callback(kind, text)
                    except Exception:
                        pass
        except Exception as e:
            rec.error = f"reader error ({kind}): {type(e).__name__}: {e}"

    async def _waiter(self, rec: _CommandRecord) -> None:
        """等进程结束，记录 exit_code，置 status_event。"""
        if rec.proc is None:
            return
        try:
            exit_code = await rec.proc.wait()
            # 等 stdout/stderr reader 都读完再标完成（避免 done 后用户拿不到末尾输出）
            tasks = [t for t in (rec.reader_task_out, rec.reader_task_err) if t is not None]
            if tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True), timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    pass
            rec.exit_code = exit_code
            rec.finished_at = time.time()
            if rec.stream_callback is not None and hasattr(rec.stream_callback, "emit_done"):
                try:
                    await rec.stream_callback.emit_done(  # type: ignore[attr-defined]
                        exit_code,
                        exit_code == 0,
                    )
                except Exception:
                    pass
        except Exception as e:
            rec.error = f"wait error: {type(e).__name__}: {e}"
            rec.exit_code = -1
            rec.finished_at = time.time()
        finally:
            rec.status_event.set()

    async def get_status(
        self,
        command_id: str,
        wait_seconds: float = 0.0,
        output_chars: int = _DEFAULT_OUTPUT_CHARS,
        kill: bool = False,
    ) -> Dict[str, Any]:
        """查命令状态 / 增量输出 / 杀命令。

        wait_seconds > 0 时，最多等命令在该时间内完成，期间 await event。命令早结束就早返回。
        kill=True 立即杀命令，再返回当前状态（一次调用兼任 kill + 拉输出）。
        """
        async with self._lock:
            rec = self._records.get(command_id)
        if rec is None:
            return {
                "success": False,
                "command_id": command_id,
                "error": f"command_id not found (or already cleaned up): {command_id}",
            }

        # 顺手 GC 太老的死命令（每次调用 piggyback 一次）
        await self._gc_dead_locked()

        rec.last_polled_at = time.time()

        if kill:
            await self._kill_record(rec)

        if wait_seconds > 0 and rec.exit_code is None:
            try:
                await asyncio.wait_for(rec.status_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass

        elapsed = (rec.finished_at or time.time()) - rec.started_at
        stdout_text = rec.stdout_text(output_chars)
        stderr_text = rec.stderr_text(output_chars)
        total_dropped = rec.stdout_dropped_bytes + rec.stderr_dropped_bytes
        return {
            "success": rec.exit_code == 0 if rec.exit_code is not None else True,
            "command_id": rec.command_id,
            "status": rec.status_str(),
            "command": rec.command,
            "exit_code": rec.exit_code,
            "elapsed_sec": round(elapsed, 2),
            # 向后兼容：output 返合并后的拼接
            "output": rec.output_text(output_chars),
            "stdout": stdout_text,
            "stderr": stderr_text,
            "output_total_bytes": (
                len(rec.stdout_buf) + len(rec.stderr_buf) + total_dropped
            ),
            "output_truncated": total_dropped > 0,
            "output_dropped_bytes": total_dropped,
            "error": rec.error,
        }

    async def _kill_record(self, rec: _CommandRecord) -> None:
        if rec.proc is None or rec.exit_code is not None:
            return
        rec.killed = True
        try:
            if _IS_WINDOWS:
                # proc.terminate() 只杀启动器（如 bash.exe 自己）；它的子进程
                # （bash 内部启动的 sleep / pytest / npm）会继续运行 → kill 不生效。
                # 用 taskkill /T /F /PID 杀整个进程树。/T = tree, /F = force.
                # 同步调用即可——taskkill 自身极快（<100ms），不必 asyncio。
                import subprocess as _sp
                try:
                    _sp.run(
                        ["taskkill", "/T", "/F", "/PID", str(rec.proc.pid)],
                        capture_output=True, timeout=3,
                    )
                except (FileNotFoundError, _sp.TimeoutExpired):
                    # taskkill 不可用（极罕见）→ 兜底走 terminate
                    rec.proc.terminate()
            else:
                # 杀整个进程组（命令本身可能 fork 子进程）
                try:
                    pgid = os.getpgid(rec.proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    rec.proc.terminate()
        except Exception as e:
            rec.error = f"kill error: {type(e).__name__}: {e}"

    async def _gc_dead_locked(self) -> None:
        """清理 TTL 过期的已结束命令（轻量，每次 get_status 顺手）。"""
        now = time.time()
        async with self._lock:
            stale = [
                cid for cid, rec in self._records.items()
                if rec.exit_code is not None
                and (now - rec.last_polled_at) > _DEAD_COMMAND_TTL
            ]
            for cid in stale:
                self._records.pop(cid, None)

    async def shutdown(self) -> None:
        """主进程退出时调用：杀掉所有还在跑的命令，释放资源。"""
        async with self._lock:
            recs = list(self._records.values())
        for rec in recs:
            if rec.exit_code is None:
                await self._kill_record(rec)
                # 给点时间让 waiter 收尾
                if rec.waiter_task is not None:
                    try:
                        await asyncio.wait_for(rec.waiter_task, timeout=2.0)
                    except (asyncio.TimeoutError, Exception):
                        pass


# 全局单例
_manager: Optional[BackgroundCommandManager] = None


def get_command_manager() -> BackgroundCommandManager:
    global _manager
    if _manager is None:
        _manager = BackgroundCommandManager()
    return _manager
