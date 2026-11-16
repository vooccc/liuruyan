"""DashScope 实时语音识别 provider（官方 WebSocket 协议，2026-11 核实版）。

协议（help.aliyun.com/zh/model-studio/paraformer-* -events）：
- 端点  wss://dashscope.aliyuncs.com/api-ws/v1/inference（或业务空间专属域名）
- 鉴权  握手头 Authorization: bearer <api_key>
- 上行  run-task(JSON) → task-started → 二进制音频帧（单声道）→ finish-task(JSON)
- 下行  task-started / result-generated / task-finished / task-failed
       result-generated: payload.output.sentence{text, sentence_end, heartbeat}
       句级语义：sentence_end=false 为中间结果（同句后续会修订），
                true 为该句最终结果，下一 event 开始新句。

累计全文 = 已结束句子依序拼接；finished 事件携带。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import List, Optional

import websockets

from ..config import VoiceStreamConfig
from .base import ProviderEvent, ProviderEventCallback, RealtimeProvider

_CONNECT_TIMEOUT = 10.0
# finish-task 后等 task-finished 的兜底上限（正常几秒内返回）
_DRAIN_TIMEOUT = 20.0


class DashScopeRealtimeProvider(RealtimeProvider):
    """一个实例 = 一次识别任务。不可复用。"""

    def __init__(self, cfg: VoiceStreamConfig, api_key: str) -> None:
        self._cfg = cfg
        self._api_key = api_key
        self._task_id = str(uuid.uuid4())
        self._ws: Optional[websockets.asyncio.client.ClientConnection] = None
        self._on_event: Optional[ProviderEventCallback] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._sentences: List[str] = []   # 已结束的句子
        self._closed = False
        self._finished = False            # 已收到 task-finished / task-failed

    # ── 生命周期 ──────────────────────────────────────────────

    async def start(self, on_event: ProviderEventCallback) -> None:
        self._on_event = on_event
        headers = {"Authorization": f"bearer {self._api_key}"}
        try:
            self._ws = await asyncio.wait_for(
                websockets.asyncio.client.connect(
                    self._cfg.ws_url,
                    additional_headers=headers,
                    max_size=None,
                    open_timeout=_CONNECT_TIMEOUT,
                ),
                _CONNECT_TIMEOUT,
            )
        except Exception as e:
            raise RuntimeError(f"dashscope ws connect failed: {e}") from e

        await self._ws.send(json.dumps(self._build_run_task()))
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def feed(self, frame: bytes) -> None:
        if self._ws is None or self._closed:
            return
        await self._ws.send(frame)

    async def finish(self) -> None:
        if self._ws is None or self._closed or self._finished:
            return
        await self._ws.send(json.dumps({
            "header": {
                "action": "finish-task",
                "task_id": self._task_id,
                "streaming": "duplex",
            },
            "payload": {"input": {}},
        }))
        # 给 task-finished / 尾部 result-generated 一点到达时间
        if self._recv_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._recv_task), _DRAIN_TIMEOUT
                )
            except asyncio.TimeoutError:
                self._recv_task.cancel()
            except Exception:
                pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ── 内部 ──────────────────────────────────────────────

    def _build_run_task(self) -> dict:
        params: dict = {
            "format": "pcm",
            "sample_rate": self._cfg.sample_rate,
        }
        if self._cfg.heartbeat:
            params["heartbeat"] = True
        if self._cfg.language_hints:
            params["language_hints"] = list(self._cfg.language_hints)
        return {
            "header": {
                "action": "run-task",
                "task_id": self._task_id,
                "streaming": "duplex",
            },
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self._cfg.model,
                "parameters": params,
                "input": {},
            },
        }

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    continue  # 该协议不下发二进制，防御性忽略
                try:
                    event = json.loads(message)
                except Exception:
                    continue
                await self._handle_event(event)
                if self._finished:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._finished and not self._closed:
                await self._emit(ProviderEvent.failed("connection_error", str(e)))
        finally:
            # 正常/异常都确保连接释放
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _handle_event(self, event: dict) -> None:
        header = event.get("header") or {}
        etype = header.get("event") or header.get("event_type") or ""
        if etype == "task-started":
            await self._emit(ProviderEvent.started())
        elif etype == "result-generated":
            sentence = ((event.get("payload") or {}).get("output") or {}).get("sentence") or {}
            if sentence.get("heartbeat"):
                return
            text = (sentence.get("text") or "").strip()
            if not text:
                return
            if sentence.get("sentence_end") or sentence.get("is_end"):
                self._sentences.append(text)
                await self._emit(ProviderEvent.final(text))
            else:
                await self._emit(ProviderEvent.partial(text))
        elif etype == "task-finished":
            self._finished = True
            await self._emit(ProviderEvent.finished(self._cumulative()))
        elif etype == "task-failed":
            self._finished = True
            await self._emit(
                ProviderEvent.failed(
                    str(header.get("error_code") or "task_failed"),
                    str(header.get("error_message") or "dashscope task failed"),
                )
            )

    def _cumulative(self) -> str:
        # 句子自带标点（punctuation_prediction_enabled 默认 true），直接拼接
        return "".join(self._sentences)

    async def _emit(self, ev: dict) -> None:
        if self._on_event is not None:
            await self._on_event(ev)
