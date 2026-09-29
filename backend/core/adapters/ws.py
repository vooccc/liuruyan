"""WebSocket Adapter：新架构传输层（Phase 1）

设计：
- 一个 FastAPI WS 连接 = 一个客户端，可同时订阅多个 channel（多路复用）
- 协议（前后端约定）：

  客户端 → 服务端：
    {"action": "subscribe",   "channel": "session:xxx", "last_event_id": 12}
    {"action": "unsubscribe", "channel": "session:xxx"}
    {"action": "ping"}

  服务端 → 客户端：
    {"action": "subscribed",   "channel": "session:xxx"}
    {"action": "unsubscribed", "channel": "session:xxx"}
    {"action": "event",        "channel": "session:xxx", "event": {...}}
    {"action": "pong",         "ts": 1234.5}
    {"action": "error",        "message": "..."}

- 心跳：服务端每 60s 主动发 ping（带 ts），客户端必须回 pong；超 90s 无心跳关闭连接
- 断线续传：客户端重连后用 last_event_id 重新 subscribe，自动从 ring buffer 回放

线程模型：每个 WS 连接独占一个 task；订阅多个 channel 时为每个订阅起一个 reader task，
全部聚合到 send queue 串行写出。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from backend.core.adapters.base import (
    DeliveryTarget,
    OutboundAdapter,
)
from backend.core.event_bus import EventBus, get_default_bus
from backend.core.inbox_message import BusEvent

logger = logging.getLogger(__name__)


# ─────── 协议常量 ─────────

CLIENT_HEARTBEAT_TIMEOUT_SEC = 90.0       # 客户端 90s 无心跳 → 断
SERVER_HEARTBEAT_INTERVAL_SEC = 60.0      # 服务端每 60s 主动发一次 ping
SEND_QUEUE_MAXSIZE = 1024


# ─────── 类型别名（避免硬依赖 FastAPI / Starlette 类型）─────────

# 一个最小 WebSocket 协议：accept / send_json / receive_text / close
# FastAPI/Starlette WebSocket 自动满足。
WSObj = Any  # noqa: UP


# ─────── OutboundAdapter 实现 ─────────

class WebSocketOutboundAdapter(OutboundAdapter):
    """把 BusEvent 投递到 WS 客户端

    target.platform 必须是 "web"，target.target_id 是 WSConnection 的 client_id。
    target.extra["sender"] 是 WSConnection.send 协程引用。
    """

    PLATFORM = "web"

    def supports_platform(self, platform: str) -> bool:
        return platform == self.PLATFORM

    async def deliver(self, event: BusEvent, target: DeliveryTarget) -> None:
        if not self.supports_platform(target.platform):
            raise ValueError(f"Unsupported platform: {target.platform}")
        sender: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = (
            target.extra.get("sender") if target.extra else None
        )
        if sender is None:
            raise ValueError("DeliveryTarget.extra['sender'] is required")
        payload = {
            "action": "event",
            "channel": target.channel,
            "event": event.to_dict(),
        }
        await sender(payload)


# ─────── WS 连接处理（main.py 直接调）─────────

class WSConnection:
    """单个 WebSocket 连接的处理上下文

    使用方式（在 FastAPI 端点中）：

        @app.websocket("/ws")
        async def ws_ep(ws: WebSocket):
            await ws.accept()
            conn = WSConnection(ws)
            await conn.run()
    """

    _next_client_id: int = 1

    def __init__(self, ws: WSObj, *, bus: Optional[EventBus] = None) -> None:
        self.ws = ws
        self.bus = bus or get_default_bus()

        WSConnection._next_client_id += 1
        self.client_id: str = f"ws-{WSConnection._next_client_id}-{int(time.time() % 1e6)}"

        self._send_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(
            maxsize=SEND_QUEUE_MAXSIZE
        )
        self._sub_tasks: Dict[str, asyncio.Task] = {}
        self._closed = False
        self._last_client_msg_at = time.time()

    # ─────── 公共入口 ─────────

    async def run(self) -> None:
        """主循环：起 reader / writer / heartbeat 三个 task，任一退出即整体收尾"""
        reader = asyncio.create_task(self._reader_loop(), name=f"{self.client_id}:read")
        writer = asyncio.create_task(self._writer_loop(), name=f"{self.client_id}:write")
        heartbeat = asyncio.create_task(self._heartbeat_loop(), name=f"{self.client_id}:hb")
        tasks = [reader, writer, heartbeat]

        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in pending:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        finally:
            await self._cleanup()

    # ─────── 子任务 ─────────

    async def _reader_loop(self) -> None:
        """从客户端读消息，分发到 subscribe/unsubscribe/ping"""
        try:
            while not self._closed:
                raw = await self.ws.receive_text()
                self._last_client_msg_at = time.time()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await self._enqueue({"action": "error", "message": "invalid json"})
                    continue
                await self._handle_client_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # 客户端断开 / WebSocketDisconnect 等，正常退出
            logger.debug("WS[%s]: reader exited", self.client_id, exc_info=True)

    async def _writer_loop(self) -> None:
        try:
            while not self._closed:
                payload = await self._send_queue.get()
                if payload is None:
                    return
                try:
                    await self.ws.send_json(payload)
                except Exception:  # noqa: BLE001
                    logger.debug("WS[%s]: send_json failed", self.client_id, exc_info=True)
                    return
        except asyncio.CancelledError:
            raise

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(SERVER_HEARTBEAT_INTERVAL_SEC)
                # 客户端 90s 无任何消息（含 pong）→ 视为僵尸
                if (time.time() - self._last_client_msg_at) > CLIENT_HEARTBEAT_TIMEOUT_SEC:
                    logger.info(
                        "WS[%s]: client heartbeat timeout, closing",
                        self.client_id,
                    )
                    return
                await self._enqueue({"action": "ping", "ts": time.time()})
        except asyncio.CancelledError:
            raise

    # ─────── 协议处理 ─────────

    async def _handle_client_message(self, msg: Dict[str, Any]) -> None:
        action = msg.get("action")
        if action == "subscribe":
            channel = msg.get("channel")
            if not isinstance(channel, str) or not channel:
                await self._enqueue(
                    {"action": "error", "message": "subscribe: channel required"}
                )
                return
            last_event_id = msg.get("last_event_id")
            await self._subscribe(channel, last_event_id)

        elif action == "unsubscribe":
            channel = msg.get("channel")
            if isinstance(channel, str) and channel:
                await self._unsubscribe(channel)

        elif action == "ping":
            await self._enqueue({"action": "pong", "ts": time.time()})

        elif action == "pong":
            # 客户端响应服务端 ping，已在 _last_client_msg_at 记录
            pass

        else:
            await self._enqueue(
                {"action": "error", "message": f"unknown action: {action!r}"}
            )

    async def _subscribe(
        self,
        channel: str,
        last_event_id: Any = None,
    ) -> None:
        if channel in self._sub_tasks:
            await self._enqueue(
                {"action": "subscribed", "channel": channel, "note": "already"}
            )
            return

        leid: Optional[int] = None
        if isinstance(last_event_id, (int, float)):
            leid = int(last_event_id)

        async def _pump() -> None:
            try:
                async for ev in self.bus.subscribe(channel, last_event_id=leid):
                    await self._enqueue(
                        {
                            "action": "event",
                            "channel": channel,
                            "event": ev.to_dict(),
                        }
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception(
                    "WS[%s]: subscriber on channel=%s crashed",
                    self.client_id,
                    channel,
                )

        task = asyncio.create_task(
            _pump(),
            name=f"{self.client_id}:sub:{channel}",
        )
        self._sub_tasks[channel] = task
        await self._enqueue({"action": "subscribed", "channel": channel})

    async def _unsubscribe(self, channel: str) -> None:
        task = self._sub_tasks.pop(channel, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await self._enqueue({"action": "unsubscribed", "channel": channel})

    # ─────── 工具 ─────────

    async def _enqueue(self, payload: Dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            self._send_queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning(
                "WS[%s]: send queue full, dropping payload action=%s",
                self.client_id,
                payload.get("action"),
            )

    async def _cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        for channel, task in list(self._sub_tasks.items()):
            task.cancel()
        for channel, task in list(self._sub_tasks.items()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._sub_tasks.clear()
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass

    # ─────── 用作 OutboundAdapter 的 sender ─────────

    async def send(self, payload: Dict[str, Any]) -> None:
        """供 WebSocketOutboundAdapter.deliver 使用的 sender 协程"""
        await self._enqueue(payload)


# ─────── FastAPI 集成 helper ─────────

async def serve_websocket(ws: WSObj, *, bus: Optional[EventBus] = None) -> None:
    """FastAPI WebSocket 端点的最小实现：

        from fastapi import WebSocket
        from backend.core.adapters.ws import serve_websocket

        @app.websocket("/ws")
        async def ws_ep(ws: WebSocket):
            await ws.accept()
            await serve_websocket(ws)
    """
    conn = WSConnection(ws, bus=bus)
    await conn.run()
