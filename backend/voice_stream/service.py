"""VoiceStreamSession：浏览器 WebSocket ↔ realtime provider 的中继。

浏览器侧协议（JSON / 二进制混传）：
- 连接后第一帧必须是 {"type": "start"}（文本）。校验配置可用性，建 provider。
- provider started 前到达的二进制帧先缓存，started 后 flush（避免竞态丢音）。
- 录音中持续发二进制 PCM 帧（16k 单声道 little-endian）。
- {"type": "finish"} → provider.finish()，等待 finished/failed 后回传并断开。
- 浏览器随时断开 = 取消（provider 直接 close，不 finish）。

服务端 → 浏览器事件（JSON）：
    {"type": "started"}                       可发音频
    {"type": "partial", "text"}               当前句中间结果（实时预览用）
    {"type": "final", "text"}                 一句最终结果（原稿，立即拼进输入框）
    {"type": "polished", "index", "text"}     第 index 句的成稿（后台加工完成，
                                              前端无感替换原稿；可能多次，以最后一次为准）
    {"type": "finished", "text"}              任务完成，text=累计全文
    {"type": "failed", "code", "message"}      失败（含 disabled / no key / 上游错误）
"""
from __future__ import annotations

import asyncio
import time
from typing import List, Optional

from fastapi import WebSocket, WebSocketDisconnect

from .config import VoiceStreamConfig, load_voice_stream_config, resolve_api_key
from .polish import apply_rules, local_filler_cleanup, polish_available, polish_via_llm
from .providers.base import ProviderEvent, RealtimeProvider
from .providers.dashscope import DashScopeRealtimeProvider
from .providers.local import LocalSenseVoiceProvider, resolve_local_model_paths
from .providers.mock import MockRealtimeProvider

# task-started 前缓存音频的硬上限（防恶意灌内存；600s * 32KB/s ≈ 19MB，给一半）
_MAX_BUFFERED_BYTES = 10 * 1024 * 1024
# finish 后等 provider 终态的兜底（provider 内部另有 drain timeout）
_FINISH_WAIT_TIMEOUT = 30.0
# finish 后等成稿队列排空的上限（LLM 超时 6s + 规则瞬时，10s 够）
_POLISH_DRAIN_TIMEOUT = 10.0


def build_provider(cfg: VoiceStreamConfig) -> RealtimeProvider:
    """provider 工厂：mock（测试）/ local（免 key 本地）/ 其余走 DashScope 协议。"""
    if cfg.provider_id == "mock":
        return MockRealtimeProvider(cfg)
    if cfg.provider_id == "local":
        return LocalSenseVoiceProvider(cfg)
    key = resolve_api_key(cfg.provider_id)
    if not key:
        raise RuntimeError(
            f"voice_stream provider '{cfg.provider_id}' has no api_key"
        )
    return DashScopeRealtimeProvider(cfg, key)


class VoiceStreamSession:
    """一次浏览器 WS 连接的完整处理。一个实例只 run() 一次。"""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._cfg = load_voice_stream_config()
        self._provider: Optional[RealtimeProvider] = None
        self._buffer: List[bytes] = []
        self._buffered_bytes = 0
        self._started = False
        self._session_started_at = 0.0
        # 成稿队列：final 原稿立即上屏，后台加工完成后发 polished 让前端替换
        self._polish_tasks: List[asyncio.Task] = []
        # 会话内句子序号（polished.index 与之对应）
        self._sentence_seq = 0

    async def run(self) -> None:
        await self._ws.accept()
        try:
            await self._handshake_and_stream()
        except WebSocketDisconnect:
            pass
        finally:
            await self._teardown()

    # ── 主循环 ──────────────────────────────────────────────

    async def _handshake_and_stream(self) -> None:
        # 1) 等 start 帧（用 receive() 而非 receive_text()：二进制首帧不炸、走 bad_request）
        first = await self._ws.receive()
        if first["type"] == "websocket.disconnect":
            return
        msg = _parse_json(first.get("text") or "")
        if not msg or msg.get("type") != "start":
            await self._ws.send_json(
                ProviderEvent.failed("bad_request", "first message must be {type:start}")
            )
            return

        # 2) 配置闸门（前端虽会先查 /config，这里服务端再拦一道）
        if not self._cfg.enabled:
            await self._ws.send_json(
                ProviderEvent.failed("disabled", "voice_stream disabled")
            )
            return
        if self._cfg.provider_id == "local":
            # 本地档免 key，但模型文件必须就绪
            if resolve_local_model_paths(self._cfg) is None:
                await self._ws.send_json(
                    ProviderEvent.failed(
                        "model_not_ready",
                        "local model files missing (model.int8.onnx / tokens.txt / silero_vad.onnx)",
                    )
                )
                return
        elif self._cfg.provider_id != "mock" and not resolve_api_key(self._cfg.provider_id):
            await self._ws.send_json(
                ProviderEvent.failed(
                    "no_api_key",
                    f"provider '{self._cfg.provider_id}' has no api_key",
                )
            )
            return

        # 3) 起 provider
        try:
            self._provider = build_provider(self._cfg)
            await self._provider.start(self._on_provider_event)
        except Exception as e:
            await self._ws.send_json(
                ProviderEvent.failed("provider_start_failed", str(e))
            )
            return
        self._session_started_at = time.monotonic()

        # 4) 收发循环
        finish_requested = False
        while True:
            message = await self._ws.receive()

            if message["type"] == "websocket.disconnect":
                return  # 浏览器断开 = 取消

            if "text" in message and message["text"] is not None:
                control = _parse_json(message["text"])
                if not control:
                    continue
                ctype = control.get("type")
                if ctype == "finish":
                    finish_requested = True
                    await self._provider.finish()
                    # finish 后 provider 会 emit finished/failed；等终态
                    await self._wait_terminal(_FINISH_WAIT_TIMEOUT)
                    return
                if ctype == "cancel":
                    return
                # 忽略未知控制帧

            elif "bytes" in message and message["bytes"] is not None:
                # 服务端二次校验时长上限（防绕过前端）
                if (
                    self._cfg.max_session_seconds > 0
                    and time.monotonic() - self._session_started_at
                    > self._cfg.max_session_seconds
                ):
                    await self._ws.send_json(
                        ProviderEvent.failed(
                            "session_too_long",
                            f"exceeded max_session_seconds={self._cfg.max_session_seconds}",
                        )
                    )
                    return
                frame = message["bytes"]
                if self._started:
                    await self._provider.feed(frame)
                else:
                    self._buffer.append(frame)
                    self._buffered_bytes += len(frame)
                    if self._buffered_bytes > _MAX_BUFFERED_BYTES:
                        await self._ws.send_json(
                            ProviderEvent.failed("buffer_overflow", "buffered audio too large")
                        )
                        return

    # ── provider 事件 → 浏览器 ─────────────────────────────

    async def _on_provider_event(self, ev: dict) -> None:
        if ev.get("type") == "started":
            self._started = True
            await self._ws.send_json(ev)
            # flush 缓存的音频
            if self._buffer:
                buffered, self._buffer = self._buffer, []
                self._buffered_bytes = 0
                for frame in buffered:
                    await self._provider.feed(frame)
        elif ev.get("type") == "final":
            # final 原稿立即上屏（不等 LLM）；index 供前端做 polished 无感替换。
            # polish 开着 → 后台加工后发 polished；关着 → 仅透传（index 照带，协议统一）。
            index = self._sentence_seq
            self._sentence_seq += 1
            if self._cfg.polish_enabled:
                await self._forward({**ev, "index": index})
                task = asyncio.create_task(
                    self._polish_and_forward(ev.get("text") or "", index)
                )
                self._polish_tasks.append(task)
            else:
                await self._forward({**ev, "index": index})
        elif ev.get("type") == "finished":
            # 等成稿队列排空再收尾，保证 polished 事件先于 finished 到达
            await self._drain_polish()
            await self._forward(ev)
        else:
            await self._forward(ev)

    async def _polish_and_forward(self, raw: str, index: int) -> None:
        """后台成稿：规则替换（确定性，先行）→ LLM 润色 → 无感替换。

        每一步结果与"当前已上屏文本"不同才发 polished；LLM 失败/超时静默保原稿。
        """
        current = apply_rules(raw)
        if current != raw:
            await self._forward({"type": "polished", "index": index, "text": current})
        if polish_available(self._cfg):
            llm = await polish_via_llm(current, self._cfg)
            if llm is not None:
                # LLM 输出再过一遍规则（用户规则优先级最高，双保险）
                final_text = apply_rules(llm)
                if final_text != current:
                    await self._forward(
                        {"type": "polished", "index": index, "text": final_text}
                    )
                return
        # 没配 LLM（或 LLM 失败）→ 本地语气词兜底（极保守，受总开关控制）
        local = local_filler_cleanup(current)
        if local != current:
            await self._forward({"type": "polished", "index": index, "text": local})

    async def _drain_polish(self) -> None:
        pending = [t for t in self._polish_tasks if not t.done()]
        if pending:
            try:
                await asyncio.wait(pending, timeout=_POLISH_DRAIN_TIMEOUT)
            except Exception:
                pass
        self._polish_tasks.clear()

    async def _forward(self, ev: dict) -> None:
        try:
            await self._ws.send_json(ev)
        except Exception:
            pass  # 浏览器已断；provider 终态由 _teardown 清理

    async def _wait_terminal(self, timeout: float) -> None:
        """finish() 已 await provider 的 drain；这里只兜底 sleep 竞态。"""
        await asyncio.sleep(0)

    # ── 清理 ──────────────────────────────────────────────

    async def _teardown(self) -> None:
        if self._provider is not None:
            try:
                await self._provider.close()
            except Exception:
                pass
            self._provider = None
        try:
            await self._ws.close()
        except Exception:
            pass


def _parse_json(text: str) -> Optional[dict]:
    try:
        import json as _json

        obj = _json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None
