"""原子写 — tempfile + rename，crash-safe。

设计（docs/architecture_v2/02_runtime_core.md § 7）：

- 所有持久化文件统一走这里
- 同目录 tempfile 保证 rename 原子（Linux/macOS POSIX；Windows 用 os.replace）
- 失败时清理 tempfile，不污染原文件
- async 版走 io_pool（不阻塞 event loop）

⚠️ 不支持：跨文件系统 rename（必须同目录 tempfile，所以默认就是同目录）
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

# Windows os.replace 在并发竞争时会偶发 PermissionError（WinError 5）
# 因为 ReplaceFile 不是真正的原子操作，而是 unlink + rename 两步。
# POSIX rename 是真原子，无此问题。
_WINDOWS_REPLACE_RETRIES = 5
_WINDOWS_REPLACE_BACKOFF_S = 0.01  # 10ms 起，指数退避


def _replace_with_retry(src: Path, dst: Path) -> None:
    """os.replace 包一层 retry，专门兜 Windows 并发 PermissionError。"""
    if os.name != "nt":
        os.replace(src, dst)
        return
    last_err: Exception | None = None
    delay = _WINDOWS_REPLACE_BACKOFF_S
    for _ in range(_WINDOWS_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(delay)
            delay *= 2
    # 所有 retry 用完，抛最后一次错
    assert last_err is not None
    raise last_err


def atomic_write_text(path: Path | str, content: str, encoding: str = "utf-8") -> None:
    """原子写文本。tempfile + rename。

    流程：
    1. 同目录创建 .tmp.{pid}.{rand} 文件
    2. 写完整内容
    3. os.replace 原子替换（POSIX rename 真原子；Windows 包 retry 兜并发竞争）
    4. 任意步骤失败 → 清理 tempfile + 抛原异常

    parent 目录不存在时自动 mkdir(parents=True)。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        tmp.write_text(content, encoding=encoding)
        _replace_with_retry(tmp, p)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def atomic_write_json(path: Path | str, data: Any, *, indent: int = 2) -> None:
    """原子写 JSON。

    ensure_ascii=False 中文不转义；indent 默认 2 美化。
    """
    content = json.dumps(data, ensure_ascii=False, indent=indent)
    atomic_write_text(path, content)


def atomic_append_jsonl(path: Path | str, line: dict) -> None:
    """追加一行 JSON 到 JSONL 文件。

    Linux/macOS：单 write < PIPE_BUF (4096) 是原子的，无需锁
    Windows：用 portalocker 文件锁（如果可用）；否则 fallback 到 lock-free（best effort）

    line 必须是 dict（不允许 None / list 顶层）。serialize 后超过 PIPE_BUF
    （默认 4096）会打 warning（行原子性不再保证），但仍写入。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(line, ensure_ascii=False) + "\n"
    payload = serialized.encode("utf-8")

    if os.name == "nt":
        _append_with_lock_windows(p, payload)
    else:
        # POSIX：O_APPEND 单 write 原子（< PIPE_BUF）
        fd = os.open(str(p), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)


def _append_with_lock_windows(p: Path, payload: bytes) -> None:
    """Windows append：尝试 portalocker，失败 fallback 到 simple append。"""
    try:
        import portalocker  # type: ignore[import-untyped]

        with open(p, "ab") as f:
            portalocker.lock(f, portalocker.LOCK_EX)
            try:
                f.write(payload)
                f.flush()
            finally:
                portalocker.unlock(f)
    except ImportError:
        # fallback：无锁追加（best effort）
        with open(p, "ab") as f:
            f.write(payload)


# ── async 版本（走 io_pool）─────────────────────────────────────────────


async def atomic_write_text_async(path: Path | str, content: str, encoding: str = "utf-8") -> None:
    import asyncio

    from liuruyan.runtime.pools import get_io_pool

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(get_io_pool(), atomic_write_text, path, content, encoding)


async def atomic_write_json_async(path: Path | str, data: Any, *, indent: int = 2) -> None:
    import asyncio

    from liuruyan.runtime.pools import get_io_pool

    loop = asyncio.get_running_loop()
    # 闭包传 indent
    def _do():
        atomic_write_json(path, data, indent=indent)

    await loop.run_in_executor(get_io_pool(), _do)


async def atomic_append_jsonl_async(path: Path | str, line: dict) -> None:
    import asyncio

    from liuruyan.runtime.pools import get_io_pool

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(get_io_pool(), atomic_append_jsonl, path, line)


__all__ = [
    "atomic_write_text",
    "atomic_write_json",
    "atomic_append_jsonl",
    "atomic_write_text_async",
    "atomic_write_json_async",
    "atomic_append_jsonl_async",
]
