"""SSE 流式响应双向转换（Anthropic Messages ⇄ OpenAI Chat Completions）。

入口（基于状态机的协程）：

- AnthropicToOpenAIStreamTranslator
    feed_event(event_type, data_obj) -> list[bytes]  每次喂 1 个 Anthropic SSE 事件，吐
                                                      0~N 条 OpenAI SSE chunks（含 `data: ` 前缀和
                                                      末尾 `\\n\\n`）

- OpenAIToAnthropicStreamTranslator
    feed_chunk(data_obj) -> list[bytes]              每次喂 1 个 OpenAI chunk（已去 `data: ` 前缀和
                                                      `[DONE]` 哨兵），吐 0~N 条 Anthropic SSE 事件

设计：`_docs/GATEWAY_M2B.md` §4.7

设计原则：
- **状态机内部维护 content_block_index → kind 表**，正确成对 content_block_start / stop
- tool_use 的 OpenAI tool_call.index 用独立计数器，与 Anthropic content_block_index 解耦
- 任何未知事件或异常状态 → 静默 + 打 `[GATEWAY-TRANSLATE-WARN]`，绝不抛
- 输出字节都 `.encode("utf-8")` 后返；调用方 yield 即可
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from .response_nonstream import (
    _anthropic_stop_to_openai,
    _anthropic_usage_to_openai,
    _openai_finish_to_anthropic,
    _openai_usage_to_anthropic,
)


def _sse(data: Dict[str, Any], *, event: Optional[str] = None) -> bytes:
    """构造一条 SSE chunk 的字节。"""
    s = ""
    if event:
        s += f"event: {event}\n"
    s += "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"
    return s.encode("utf-8")


def _sse_done() -> bytes:
    return b"data: [DONE]\n\n"


# ─────────────────────────── Anthropic → OpenAI ───────────────────────────


class AnthropicToOpenAIStreamTranslator:
    """把 Anthropic SSE 事件流翻译成 OpenAI chat.completion.chunk SSE 流。

    用法：
        t = AnthropicToOpenAIStreamTranslator(model="claude-x")
        for ev_type, data in anthropic_events:
            for chunk_bytes in t.feed_event(ev_type, data):
                yield chunk_bytes
        for chunk_bytes in t.finish():  # 兜底 [DONE]
            yield chunk_bytes
    """

    def __init__(self, *, model: str = ""):
        self.model = model
        self.id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        # content_block_index → kind in {"text", "thinking", "tool_use"}
        self._block_kind: Dict[int, str] = {}
        # Anthropic content_block_index → OpenAI tool_calls index
        self._tool_call_index: Dict[int, int] = {}
        self._next_tool_index = 0
        self._role_emitted = False
        self._done_emitted = False

    def feed_event(self, event_type: str, data: Dict[str, Any]) -> List[bytes]:
        out: List[bytes] = []
        try:
            if event_type == "message_start":
                # 发首帧带 role
                if not self._role_emitted:
                    self._role_emitted = True
                    msg = data.get("message") or {}
                    if msg.get("id"):
                        self.id = msg["id"]
                    if msg.get("model"):
                        self.model = msg["model"]
                    out.append(self._make_delta_chunk({"role": "assistant"}))

            elif event_type == "content_block_start":
                idx = int(data.get("index") or 0)
                cb = data.get("content_block") or {}
                kind = cb.get("type") or "text"
                self._block_kind[idx] = kind
                if kind == "tool_use":
                    tc_idx = self._next_tool_index
                    self._next_tool_index += 1
                    self._tool_call_index[idx] = tc_idx
                    out.append(self._make_delta_chunk({
                        "tool_calls": [{
                            "index": tc_idx,
                            "id": str(cb.get("id") or ""),
                            "type": "function",
                            "function": {
                                "name": str(cb.get("name") or ""),
                                "arguments": "",
                            },
                        }],
                    }))
                # text / thinking 块 start 不需要发 OpenAI chunk（OpenAI 没有"块开始"概念）

            elif event_type == "content_block_delta":
                idx = int(data.get("index") or 0)
                delta = data.get("delta") or {}
                dtype = delta.get("type")
                kind = self._block_kind.get(idx, "text")
                if dtype == "text_delta" and kind == "text":
                    text = delta.get("text", "")
                    if text:
                        out.append(self._make_delta_chunk({"content": text}))
                elif dtype == "thinking_delta" and kind == "thinking":
                    text = delta.get("thinking", "")
                    if text:
                        # 非标准字段，但 DeepSeek / 部分 OpenAI 兼容路径会识别
                        out.append(self._make_delta_chunk({"reasoning_content": text}))
                elif dtype == "input_json_delta" and kind == "tool_use":
                    pj = delta.get("partial_json", "")
                    if pj:
                        tc_idx = self._tool_call_index.get(idx, 0)
                        out.append(self._make_delta_chunk({
                            "tool_calls": [{
                                "index": tc_idx,
                                "function": {"arguments": pj},
                            }],
                        }))
                elif dtype == "signature_delta":
                    # thinking 块的签名，OpenAI 无对应——丢弃
                    pass

            elif event_type == "content_block_stop":
                # OpenAI 没有"块结束"概念——no-op
                pass

            elif event_type == "message_delta":
                # 含 stop_reason + 增量 usage
                delta = data.get("delta") or {}
                stop_reason = delta.get("stop_reason")
                finish = _anthropic_stop_to_openai(stop_reason)
                usage = _anthropic_usage_to_openai(data.get("usage") or {})
                chunk = self._base_chunk()
                chunk["choices"] = [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish,
                }]
                if usage and usage.get("completion_tokens"):
                    chunk["usage"] = usage
                out.append(b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n")

            elif event_type == "message_stop":
                if not self._done_emitted:
                    out.append(_sse_done())
                    self._done_emitted = True

            elif event_type == "ping":
                pass  # no-op

            elif event_type == "error":
                err = data.get("error") or data
                out.append(b"data: " + json.dumps({"error": err}, ensure_ascii=False).encode("utf-8") + b"\n\n")
                if not self._done_emitted:
                    out.append(_sse_done())
                    self._done_emitted = True

            else:
                logger.warning("[GATEWAY-TRANSLATE-WARN] unknown anthropic event '%s' dropped", event_type)
        except Exception as exc:
            logger.warning("[GATEWAY-TRANSLATE-WARN] a2o feed_event('%s') err: %s", event_type, exc)
        return out

    def finish(self) -> List[bytes]:
        """兜底：如果没收到 message_stop 但流结束了，补一个 [DONE]。"""
        if self._done_emitted:
            return []
        self._done_emitted = True
        return [_sse_done()]

    def _base_chunk(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
        }

    def _make_delta_chunk(self, delta: Dict[str, Any]) -> bytes:
        chunk = self._base_chunk()
        chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": None}]
        return b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n"


# ─────────────────────────── OpenAI → Anthropic ───────────────────────────


class OpenAIToAnthropicStreamTranslator:
    """把 OpenAI chat.completion.chunk 流翻译成 Anthropic Messages SSE 事件流。

    用法：
        t = OpenAIToAnthropicStreamTranslator(model="gpt-4o")
        for chunk_data in openai_chunks:  # 已去 'data: ' 前缀；遇 [DONE] 不要喂
            for ev_bytes in t.feed_chunk(chunk_data):
                yield ev_bytes
        for ev_bytes in t.finish():
            yield ev_bytes
    """

    def __init__(self, *, model: str = ""):
        self.model = model
        self.id = f"msg_{uuid.uuid4().hex[:24]}"
        # 状态
        self._message_started = False
        self._text_block_open = False
        self._text_block_index = 0
        # tool_call_index → anthropic content_block_index
        self._tool_block_index: Dict[int, int] = {}
        # tool_call_index → {"id": ..., "name": ...} 记录首帧
        self._tool_meta: Dict[int, Dict[str, str]] = {}
        self._next_anthropic_block_index = 0
        self._final_finish_reason: Optional[str] = None
        self._final_usage: Dict[str, Any] = {}
        self._message_stopped = False

    def feed_chunk(self, chunk: Dict[str, Any]) -> List[bytes]:
        out: List[bytes] = []
        try:
            if chunk.get("model"):
                self.model = chunk["model"]
            if chunk.get("id"):
                # OpenAI chatcmpl id 和 Anthropic msg id 不通用——保留我们自己生成的 msg id
                pass

            choices = chunk.get("choices") or []
            if not choices:
                # 可能是最后一帧只有 usage（OpenAI stream_options.include_usage 模式）
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    self._final_usage = _openai_usage_to_anthropic(usage)
                return out

            ch = choices[0]
            delta = ch.get("delta") or {}
            finish_reason = ch.get("finish_reason")

            # 首帧 → 发 message_start
            if not self._message_started:
                self._message_started = True
                out.append(_sse({
                    "type": "message_start",
                    "message": {
                        "id": self.id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                }, event="message_start"))

            # content 增量
            content_text = delta.get("content")
            if isinstance(content_text, str) and content_text:
                if not self._text_block_open:
                    self._text_block_index = self._next_anthropic_block_index
                    self._next_anthropic_block_index += 1
                    self._text_block_open = True
                    out.append(_sse({
                        "type": "content_block_start",
                        "index": self._text_block_index,
                        "content_block": {"type": "text", "text": ""},
                    }, event="content_block_start"))
                out.append(_sse({
                    "type": "content_block_delta",
                    "index": self._text_block_index,
                    "delta": {"type": "text_delta", "text": content_text},
                }, event="content_block_delta"))

            # reasoning_content（DeepSeek 等非标准字段） → thinking block
            reasoning_text = delta.get("reasoning_content")
            if isinstance(reasoning_text, str) and reasoning_text:
                # thinking 必须在 text 块之前打开；如果 text 已开则简单处理：
                # 这种 chunk 顺序异常时仍创建独立的 thinking 块在末尾（虽然不符合 Anthropic
                # 协议建议，但客户端兼容性 OK）
                # 这里简化：thinking 永远独立成块，不与 text 块共享
                # 用一个不复用的 block index
                # 注：实际生产里 reasoning 都在 content 之前，不会乱序
                kind_key = "__thinking__"
                if kind_key not in self._tool_block_index:
                    th_idx = self._next_anthropic_block_index
                    self._next_anthropic_block_index += 1
                    self._tool_block_index[kind_key] = th_idx  # 复用此字典存
                    out.append(_sse({
                        "type": "content_block_start",
                        "index": th_idx,
                        "content_block": {"type": "thinking", "thinking": ""},
                    }, event="content_block_start"))
                th_idx = self._tool_block_index[kind_key]
                out.append(_sse({
                    "type": "content_block_delta",
                    "index": th_idx,
                    "delta": {"type": "thinking_delta", "thinking": reasoning_text},
                }, event="content_block_delta"))

            # tool_calls 增量
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tc_idx = tc.get("index")
                if tc_idx is None:
                    continue
                tc_idx = int(tc_idx)

                # 首次见此 tool_call → 关闭 text 块（如果开着），开 tool_use 块
                if tc_idx not in self._tool_block_index:
                    if self._text_block_open:
                        out.append(_sse({
                            "type": "content_block_stop",
                            "index": self._text_block_index,
                        }, event="content_block_stop"))
                        self._text_block_open = False

                    fn = tc.get("function") or {}
                    tu_id = str(tc.get("id") or "")
                    tu_name = str(fn.get("name") or "")
                    self._tool_meta[tc_idx] = {"id": tu_id, "name": tu_name}

                    ant_idx = self._next_anthropic_block_index
                    self._next_anthropic_block_index += 1
                    self._tool_block_index[tc_idx] = ant_idx
                    out.append(_sse({
                        "type": "content_block_start",
                        "index": ant_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tu_id,
                            "name": tu_name,
                            "input": {},
                        },
                    }, event="content_block_start"))

                # 后续帧补 id/name（如果首帧没给齐）
                meta = self._tool_meta.get(tc_idx, {})
                if not meta.get("id") and tc.get("id"):
                    meta["id"] = str(tc["id"])
                if not meta.get("name") and (tc.get("function") or {}).get("name"):
                    meta["name"] = str(tc["function"]["name"])

                # arguments 增量 → input_json_delta
                fn = tc.get("function") or {}
                pj = fn.get("arguments")
                if isinstance(pj, str) and pj:
                    ant_idx = self._tool_block_index[tc_idx]
                    out.append(_sse({
                        "type": "content_block_delta",
                        "index": ant_idx,
                        "delta": {"type": "input_json_delta", "partial_json": pj},
                    }, event="content_block_delta"))

            # finish_reason → 准备发 message_delta + message_stop（推迟到 finish() 集中发，
            # 因为可能后续还有只含 usage 的 chunk）
            if finish_reason is not None:
                self._final_finish_reason = finish_reason

            # 这一帧可能也带 usage（OpenAI 不规范的 chunk 携带）
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                self._final_usage = _openai_usage_to_anthropic(usage)

        except Exception as exc:
            logger.warning("[GATEWAY-TRANSLATE-WARN] o2a feed_chunk err: %s", exc)
        return out

    def finish(self) -> List[bytes]:
        """流结束时调用：关闭所有打开的 content_block + 发 message_delta + message_stop。"""
        if self._message_stopped:
            return []
        out: List[bytes] = []

        # 关闭 text 块
        if self._text_block_open:
            out.append(_sse({
                "type": "content_block_stop",
                "index": self._text_block_index,
            }, event="content_block_stop"))
            self._text_block_open = False

        # 关闭所有 tool_use 块
        for key, idx in list(self._tool_block_index.items()):
            out.append(_sse({
                "type": "content_block_stop",
                "index": idx,
            }, event="content_block_stop"))

        # message_delta + message_stop
        stop_reason = _openai_finish_to_anthropic(self._final_finish_reason)
        out.append(_sse({
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": self._final_usage or {"input_tokens": 0, "output_tokens": 0},
        }, event="message_delta"))
        out.append(_sse({"type": "message_stop"}, event="message_stop"))

        self._message_stopped = True
        return out


# ─────────────────────────── SSE 解析 helper ───────────────────────────


def parse_sse_events(buf: str):
    """从 SSE 文本缓冲拆出 (event_type, data_obj) 列表 + 剩余未解析的字符串。

    单个 SSE 事件以 `\\n\\n` 分隔。每个事件可能含 `event:` 和 `data:` 行（多行 data: 拼接）。
    返回 (events, remaining_buf)。
    """
    events: List = []
    while True:
        sep_idx = buf.find("\n\n")
        if sep_idx < 0:
            return events, buf
        block = buf[:sep_idx]
        buf = buf[sep_idx + 2:]
        event_type: Optional[str] = None
        data_lines: List[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        data_str = "\n".join(data_lines)
        if data_str == "[DONE]":
            events.append((event_type or "message_stop", {"_done_sentinel": True}))
            continue
        try:
            data_obj = json.loads(data_str)
        except (json.JSONDecodeError, ValueError):
            logger.warning("[GATEWAY-TRANSLATE-WARN] failed to parse SSE data: %r", data_str[:200])
            continue
        # Anthropic 的 data 通常含 type 字段，可作为事件类型 fallback
        if not event_type and isinstance(data_obj, dict) and isinstance(data_obj.get("type"), str):
            event_type = data_obj["type"]
        events.append((event_type or "message", data_obj))


__all__ = [
    "AnthropicToOpenAIStreamTranslator",
    "OpenAIToAnthropicStreamTranslator",
    "parse_sse_events",
]
