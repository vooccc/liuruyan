"""
DEBUG flag 统一入口

把散落各处的 `print("[MIRROR-BARE] ...")` 这类调试 print 收口到一个开关。
一键打开/关闭，避免每次手动加 print 又手动删（容易漏）。

用法
----
1. 模块顶部 import：
       from backend.debug_flags import DEBUG

2. 打 print 前判断：
       if DEBUG.dispatch:
           print(f"[MIRROR-BARE] sid={sid}", flush=True)

3. 启动时设置环境变量打开：
       $env:LIURUYAN_DEBUG = "dispatch,sse,chat"   # PowerShell
       export LIURUYAN_DEBUG=dispatch,sse,chat       # bash

   特殊值：
       LIURUYAN_DEBUG=*    打开全部
       LIURUYAN_DEBUG=     关闭全部（默认）

4. 运行时切换：
       DEBUG.enable("dispatch")
       DEBUG.disable("dispatch")
       DEBUG.set_all(False)

可用通道
--------
- dispatch: route_stream / sse_manager 镜像
- sse: SSE 编码 / yield / 客户端连接
- chat: /chat 请求处理 / 双总线合流
- tool_loop: LLM 工具调用循环（json 修复、配对自愈）
- guardian: Guardian 拦截判定
- backup: 写前备份 / 审计 jsonl
- worker: legacy worker/debug 兼容开关
"""
from __future__ import annotations

import os
import threading
from typing import Iterable, Set


_ALL_CHANNELS = {
    "dispatch",
    "sse",
    "chat",
    "tool_loop",
    "guardian",
    "backup",
    "worker",
}


class _DebugFlags:
    """线程安全的 debug 通道开关。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled: Set[str] = set()
        self._load_from_env()

    def _load_from_env(self) -> None:
        raw = os.environ.get("LIURUYAN_DEBUG", "").strip()
        if not raw:
            return
        if raw == "*":
            self._enabled = set(_ALL_CHANNELS)
            return
        for token in raw.split(","):
            tok = token.strip().lower()
            if tok and tok in _ALL_CHANNELS:
                self._enabled.add(tok)

    # ---- 通道属性（提供 attribute-style 访问，性能开销极小）----
    @property
    def dispatch(self) -> bool:
        return "dispatch" in self._enabled

    @property
    def sse(self) -> bool:
        return "sse" in self._enabled

    @property
    def chat(self) -> bool:
        return "chat" in self._enabled

    @property
    def tool_loop(self) -> bool:
        return "tool_loop" in self._enabled

    @property
    def guardian(self) -> bool:
        return "guardian" in self._enabled

    @property
    def backup(self) -> bool:
        return "backup" in self._enabled

    @property
    def worker(self) -> bool:
        return "worker" in self._enabled

    # ---- 运行时控制 ----
    def enable(self, channel: str) -> None:
        with self._lock:
            if channel in _ALL_CHANNELS:
                self._enabled.add(channel)

    def disable(self, channel: str) -> None:
        with self._lock:
            self._enabled.discard(channel)

    def set_all(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = set(_ALL_CHANNELS) if enabled else set()

    def state(self) -> dict:
        return {ch: ch in self._enabled for ch in sorted(_ALL_CHANNELS)}

    def __repr__(self) -> str:
        on = sorted(self._enabled)
        return f"DebugFlags(enabled={on or '<none>'})"


DEBUG = _DebugFlags()


def trace(channel: str, message: str) -> None:
    """便捷函数：if DEBUG.<channel>: print('[CHANNEL] message')"""
    if channel in DEBUG._enabled:
        print(f"[{channel.upper()}] {message}", flush=True)
