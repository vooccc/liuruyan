"""IDE tool 反向调桥（C-2）。

设计目标：
让 backend Python tool handler 能"调用"前端 runtime.ide.xxx —— 即 VSCode 插件的
bridge HTTP server 提供的 IDE 能力（open_file / insert_snippet / get_diagnostics）。

为啥需要桥：
backend 进程跟插件的 bridge HTTP server 是两个进程，backend 拿不到 csrf 和
bridgePort（这俩只在前端 webview iframe URL query 里）。所以 backend 只能间接
"请前端代我执行"。

调用流程：
    1. tool handler 调 await get_ide_tool_bridge().request(session_id, "open_file", args)
    2. bridge 生成 call_id，把 asyncio.Future 注册到 _pending_calls
    3. bridge 通过 sse_manager.send_event 推一个 'ide_tool_request' 事件给前端
       （chat /chat 流的 pump_sidecar 已订阅 sse_manager，自动透传）
    4. handler await Future，30s timeout
    5. 前端 SSE consumer 收到 ide_tool_request → 调 runtime.ide.xxx →
       POST /v2/ide_tool_result {call_id, ok, result, error}
    6. POST handler 调 bridge.receive_result → set Future
    7. handler 拿到结果继续执行

并发安全：
全局 _pending_calls 是同一 event loop 里的 dict，不需要锁（GIL + 单 loop 保证原子）。
跨 session 并发调 ide tool 没问题，每次都生成独立 call_id + Future。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass
class _PendingCall:
    call_id: str
    session_id: str
    tool: str
    future: "asyncio.Future[Dict[str, Any]]"
    created_at: float


class IdeToolBridge:
    """IDE 反向调桥 —— 全局单例，按 call_id 索引等待槽。"""

    def __init__(self) -> None:
        self._pending: Dict[str, _PendingCall] = {}

    async def request(
        self,
        session_id: str,
        tool: str,
        args: Dict[str, Any],
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        """让前端代调 runtime.ide.<tool>(args)，等结果返回。

        Returns:
            {"ok": bool, "result": Any, "error": Optional[str]}
            - ok=True 时 result 是前端 runtime.ide.<tool> 的返回值（已解构成 plain dict）
            - ok=False 时 error 是错误描述

        Raises:
            asyncio.TimeoutError: 30s 内前端没回填（VSCode webview 没开/没加载/没收到事件）

        前端 SSE consumer 必须订阅 'ide_tool_request' 类型，否则永远等不到回填。
        """
        call_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: "asyncio.Future[Dict[str, Any]]" = loop.create_future()
        self._pending[call_id] = _PendingCall(
            call_id=call_id,
            session_id=session_id,
            tool=tool,
            future=fut,
            created_at=time.time(),
        )

        # 推 sse_manager；chat /chat 流的 pump_sidecar 自动透传给前端。
        # 使用 system_notice + kind='ide_tool_request' 路径会被 sseEventAdapter 误映射为
        # worker 状态。直接用独立 type 'ide_tool_request' 让前端业务层 explicitly handle。
        try:
            from backend.sse_manager import get_sse_manager
            await get_sse_manager().send_event(
                session_id,
                {
                    "type": "ide_tool_request",
                    "call_id": call_id,
                    "tool": tool,
                    "args": args,
                },
            )
        except Exception as exc:
            # 推送失败必须清理 future，否则永远阻塞
            self._pending.pop(call_id, None)
            if not fut.done():
                fut.cancel()
            raise RuntimeError(f"failed to dispatch ide_tool_request: {exc}") from exc

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning(
                "[ide_tool_bridge] timeout: session=%s tool=%s call_id=%s (no frontend response in %.1fs)",
                session_id, tool, call_id, timeout,
            )
            raise
        finally:
            # 无论成功/超时/异常，清理 _pending
            self._pending.pop(call_id, None)

    def receive_result(
        self,
        call_id: str,
        *,
        ok: bool,
        result: Optional[Any] = None,
        error: Optional[str] = None,
    ) -> bool:
        """前端 POST /v2/ide_tool_result 回填时调用。

        Returns:
            True: 找到对应 future 并 set 成功
            False: call_id 不存在或 future 已完成（超时 / 重复回填）
        """
        pending = self._pending.get(call_id)
        if pending is None:
            return False
        if pending.future.done():
            return False
        pending.future.set_result({
            "ok": bool(ok),
            "result": result,
            "error": error,
        })
        return True

    # ---- 观测辅助 ----
    def stats(self) -> Dict[str, Any]:
        return {
            "pending_count": len(self._pending),
            "pending_calls": [
                {
                    "call_id": p.call_id,
                    "session_id": p.session_id,
                    "tool": p.tool,
                    "age_seconds": time.time() - p.created_at,
                }
                for p in self._pending.values()
            ],
        }


_bridge: Optional[IdeToolBridge] = None


def get_ide_tool_bridge() -> IdeToolBridge:
    global _bridge
    if _bridge is None:
        _bridge = IdeToolBridge()
    return _bridge


# ---- 给 tool handler 用的便捷封装：把 bridge.request 的 result 包装成 LLM 可读字符串 ----

async def call_ide_tool(
    session_id: Optional[str],
    tool: str,
    args: Dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Awaitable[Dict[str, Any]]:
    """tool handler 调它，统一处理 session_id 缺失 / 超时 / 错误，返回 dict 给上层格式化。

    Returns:
        统一形态:
        - 成功: {"ok": True, "result": <前端 runtime.ide.xxx 返回值>}
        - 失败: {"ok": False, "error": "..."}
    """
    if not session_id:
        return {"ok": False, "error": "ide tool requires a session context (session_id missing)"}
    try:
        return await get_ide_tool_bridge().request(session_id, tool, args, timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": (
                f"IDE tool '{tool}' timed out after {timeout:.0f}s. "
                "可能原因: VSCode 插件 webview 没打开 / 不在 vscode runtime / 前端 SSE 消费者没启动。"
            ),
        }
    except Exception as exc:
        logger.exception("[ide_tool_bridge] request failed: tool=%s session=%s", tool, session_id)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
