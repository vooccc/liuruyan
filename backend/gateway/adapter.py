"""上游转发：把客户端请求透传/互转到 provider，流式/非流式都支持。

M1 + M2-A（同协议透传）：
- forward_openai_chat_*           transport == openai_chat
- forward_anthropic_messages_*    transport == anthropic_messages

M2-B（跨协议互转，装饰在同协议函数上 + translate/* 模块）：
- forward_openai_chat_via_anthropic_*           OpenAI 客户端 ↔ Anthropic 上游
- forward_anthropic_messages_via_openai_*       Anthropic 客户端 ↔ OpenAI 上游

设计：
- httpx 直接转发，不复用 llm_stream_utils（破隔离契约）。流式带自实现的 chunk-level idle timeout。
- base_url 用 `backend.utils.api_host.format_api_host(host, True, "v1")` 规范化
- 跨协议互转完全自写（不引 litellm），详见 _docs/GATEWAY_M2B.md
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

import httpx

from ..providers.schema import ProviderConfig
from ..utils.api_host import format_api_host, normalize_anthropic_base_url


# Anthropic Messages API 的默认版本头。客户端如果自己发 anthropic-version 就用客户端的。
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


class GatewayUpstreamError(Exception):
    """上游不可用 / 超时 / 4xx-5xx。"""

    def __init__(self, message: str, *, status: int, code: str, upstream_body: Optional[bytes] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.upstream_body = upstream_body


def _build_openai_chat_url(provider: ProviderConfig) -> str:
    """`<provider.base_url 规范化>/chat/completions`。"""
    base = format_api_host(provider.base_url or "", support_api_version=True, api_version="v1")
    if not base:
        raise GatewayUpstreamError(
            f"provider '{provider.id}' has no base_url configured",
            status=500,
            code="provider_misconfigured",
        )
    return base.rstrip("/") + "/chat/completions"


def _build_embeddings_url(provider: ProviderConfig) -> str:
    """`<provider.base_url 规范化>/embeddings`。复用 OpenAI v1 风格 base url。"""
    base = format_api_host(provider.base_url or "", support_api_version=True, api_version="v1")
    if not base:
        raise GatewayUpstreamError(
            f"provider '{provider.id}' has no base_url configured",
            status=500,
            code="provider_misconfigured",
        )
    return base.rstrip("/") + "/embeddings"


def _build_upstream_headers(provider: ProviderConfig, incoming: Dict[str, str]) -> Dict[str, str]:
    """构造发给上游的 headers。

    - Content-Type / Accept 来自 incoming（如有）
    - Authorization: Bearer <provider.api_key>（覆盖 incoming 的 Authorization）
    - provider.extra_headers 透传
    - 剥掉 host / content-length / connection 等不能转发的 hop-by-hop
    """
    headers: Dict[str, str] = {}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    for k, v in (provider.extra_headers or {}).items():
        if v:
            headers[k] = v

    # 透传 incoming 的内容协议头
    for k, v in incoming.items():
        low = k.lower()
        if low in ("content-type", "accept"):
            headers[k] = v

    # 默认 content-type
    headers.setdefault("Content-Type", "application/json")
    return headers


# ─────────────────────────── 非流式 ───────────────────────────


async def forward_openai_chat_nonstream(
    provider: ProviderConfig,
    body: Dict[str, Any],
    vendor_model: str,
    *,
    incoming_headers: Dict[str, str],
    timeout_secs: float = 600.0,
) -> Tuple[int, bytes, Dict[str, str]]:
    """非流式转发。返回 (status, body_bytes, response_headers)。

    不抛 GatewayUpstreamError——上游 4xx/5xx 原样返回让 client 处理，更透明。
    仅网络层异常（连不上 / 超时）抛 GatewayUpstreamError。
    """
    url = _build_openai_chat_url(provider)
    headers = _build_upstream_headers(provider, incoming_headers)
    payload = dict(body)
    payload["model"] = vendor_model
    payload["stream"] = False

    try:
        async with httpx.AsyncClient(timeout=timeout_secs) as client:
            resp = await client.post(url, headers=headers, json=payload)
        # 过滤 hop-by-hop headers
        resp_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length", "connection")
        }
        return resp.status_code, resp.content, resp_headers
    except httpx.TimeoutException as exc:
        raise GatewayUpstreamError(
            f"upstream timeout: {exc}", status=504, code="upstream_timeout"
        ) from exc
    except httpx.HTTPError as exc:
        raise GatewayUpstreamError(
            f"upstream connection failed: {exc}", status=502, code="upstream_unreachable"
        ) from exc


# ─────────────────────────── 流式 ───────────────────────────


async def forward_openai_chat_stream(
    provider: ProviderConfig,
    body: Dict[str, Any],
    vendor_model: str,
    *,
    incoming_headers: Dict[str, str],
    idle_timeout_secs: float = 180.0,
    overall_timeout_secs: float = 1800.0,
) -> AsyncIterator[bytes]:
    """流式转发。yield 原样的 SSE 字节。

    - chunk 间 idle 超过 `idle_timeout_secs` → raise GatewayUpstreamError(504)
    - 总耗时 > `overall_timeout_secs` → 走 httpx 的总超时机制
    - 上游 4xx 时把 error body 包成一个 SSE 错误事件 yield 给 client（不是炸链路）
    """
    url = _build_openai_chat_url(provider)
    headers = _build_upstream_headers(provider, incoming_headers)
    payload = dict(body)
    payload["model"] = vendor_model
    payload["stream"] = True

    # httpx timeout: 用 read=None（无单次读超时），手动 wait_for 控 idle；
    # connect/write/pool 各给小值；总超时由调用方 overall_timeout_secs 兜
    timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    # 上游错误：读完 body 后包成单个 SSE error 事件 yield
                    err_body = await resp.aread()
                    err_msg = err_body.decode("utf-8", errors="replace") if err_body else f"upstream status {resp.status_code}"
                    err_chunk = (
                        "data: "
                        + json.dumps(
                            {
                                "error": {
                                    "code": "upstream_error",
                                    "status": resp.status_code,
                                    "message": err_msg,
                                }
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                        + "data: [DONE]\n\n"
                    )
                    yield err_chunk.encode("utf-8")
                    return

                aiter = resp.aiter_raw().__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(aiter.__anext__(), timeout=idle_timeout_secs)
                    except StopAsyncIteration:
                        return
                    except asyncio.TimeoutError as exc:
                        # idle 超时 → 给客户端一个 SSE 错误事件，再断流
                        err_chunk = (
                            "data: "
                            + json.dumps(
                                {
                                    "error": {
                                        "code": "upstream_idle_timeout",
                                        "message": f"no data from upstream for {int(idle_timeout_secs)}s",
                                    }
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                            + "data: [DONE]\n\n"
                        )
                        yield err_chunk.encode("utf-8")
                        raise GatewayUpstreamError(
                            f"upstream idle timeout {int(idle_timeout_secs)}s",
                            status=504,
                            code="upstream_idle_timeout",
                        ) from exc
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            err_chunk = (
                "data: "
                + json.dumps(
                    {
                        "error": {
                            "code": "upstream_network_error",
                            "message": str(exc),
                        }
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
                + "data: [DONE]\n\n"
            )
            yield err_chunk.encode("utf-8")
            raise GatewayUpstreamError(
                f"upstream network error: {exc}", status=502, code="upstream_network_error"
            ) from exc


# ─────────────────────── Anthropic Messages（M2 方案 A 同协议透传） ───────────────────────


def _build_anthropic_messages_url(provider: ProviderConfig) -> str:
    """`<provider.base_url 剥末尾 /v1>/v1/messages`。

    Anthropic Python SDK 风格的 base_url 处理：用户填的 base_url 可能含 /v1 也可能不含，
    `normalize_anthropic_base_url` 统一剥掉末尾 /v1（# escape hatch 保留），
    然后我们这里拼 /v1/messages。
    """
    base_raw = provider.base_url or ""
    base = normalize_anthropic_base_url(base_raw) or ""
    if not base:
        raise GatewayUpstreamError(
            f"provider '{provider.id}' has no base_url configured",
            status=500,
            code="provider_misconfigured",
        )
    return base.rstrip("/") + "/v1/messages"


def _build_anthropic_headers(provider: ProviderConfig, incoming: Dict[str, str]) -> Dict[str, str]:
    """构造发给 Anthropic 兼容上游的 headers。

    - x-api-key: provider.api_key （Anthropic 风格，不是 Bearer）
    - anthropic-version: 客户端发的优先；否则默认 2023-06-01
    - anthropic-beta: 客户端发的透传
    - Content-Type / Accept 透传
    - provider.extra_headers 透传（且能覆盖默认 anthropic-version）
    """
    headers: Dict[str, str] = {
        "x-api-key": provider.api_key or "",
        "anthropic-version": DEFAULT_ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }

    # 透传 incoming 的协议头（客户端自己设的 anthropic-version / beta / accept / content-type）
    for k, v in incoming.items():
        low = k.lower()
        if low in ("anthropic-version", "anthropic-beta", "content-type", "accept"):
            headers[k] = v

    # provider.extra_headers 最后合（用户在柳如烟里手动配的覆盖一切）
    for k, v in (provider.extra_headers or {}).items():
        if v:
            headers[k] = v

    return headers


async def forward_anthropic_messages_nonstream(
    provider: ProviderConfig,
    body: Dict[str, Any],
    vendor_model: str,
    *,
    incoming_headers: Dict[str, str],
    timeout_secs: float = 600.0,
) -> Tuple[int, bytes, Dict[str, str]]:
    """非流式：客户端 Anthropic Messages 协议 → 上游 Anthropic 兼容反代。"""
    url = _build_anthropic_messages_url(provider)
    headers = _build_anthropic_headers(provider, incoming_headers)
    payload = dict(body)
    payload["model"] = vendor_model
    payload["stream"] = False

    try:
        async with httpx.AsyncClient(timeout=timeout_secs) as client:
            resp = await client.post(url, headers=headers, json=payload)
        resp_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length", "connection")
        }
        return resp.status_code, resp.content, resp_headers
    except httpx.TimeoutException as exc:
        raise GatewayUpstreamError(
            f"upstream timeout: {exc}", status=504, code="upstream_timeout"
        ) from exc
    except httpx.HTTPError as exc:
        raise GatewayUpstreamError(
            f"upstream connection failed: {exc}", status=502, code="upstream_unreachable"
        ) from exc


async def forward_anthropic_messages_stream(
    provider: ProviderConfig,
    body: Dict[str, Any],
    vendor_model: str,
    *,
    incoming_headers: Dict[str, str],
    idle_timeout_secs: float = 180.0,
) -> AsyncIterator[bytes]:
    """流式：客户端 Anthropic Messages 协议 → 上游 Anthropic 兼容反代。

    Anthropic SSE 事件类型：message_start / content_block_start / content_block_delta /
    content_block_stop / message_delta / message_stop / ping。直接 yield 原始 bytes，
    不需要解码。错误情况构造 Anthropic 风格的 error event。
    """
    url = _build_anthropic_messages_url(provider)
    headers = _build_anthropic_headers(provider, incoming_headers)
    payload = dict(body)
    payload["model"] = vendor_model
    payload["stream"] = True

    timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    err_body = await resp.aread()
                    err_msg = err_body.decode("utf-8", errors="replace") if err_body else f"upstream status {resp.status_code}"
                    # Anthropic SSE 错误事件格式
                    err_chunk = (
                        "event: error\n"
                        + "data: "
                        + json.dumps(
                            {
                                "type": "error",
                                "error": {
                                    "type": "upstream_error",
                                    "status": resp.status_code,
                                    "message": err_msg,
                                },
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    yield err_chunk.encode("utf-8")
                    return

                aiter = resp.aiter_raw().__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(aiter.__anext__(), timeout=idle_timeout_secs)
                    except StopAsyncIteration:
                        return
                    except asyncio.TimeoutError as exc:
                        err_chunk = (
                            "event: error\n"
                            + "data: "
                            + json.dumps(
                                {
                                    "type": "error",
                                    "error": {
                                        "type": "upstream_idle_timeout",
                                        "message": f"no data from upstream for {int(idle_timeout_secs)}s",
                                    },
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        yield err_chunk.encode("utf-8")
                        raise GatewayUpstreamError(
                            f"upstream idle timeout {int(idle_timeout_secs)}s",
                            status=504,
                            code="upstream_idle_timeout",
                        ) from exc
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            err_chunk = (
                "event: error\n"
                + "data: "
                + json.dumps(
                    {
                        "type": "error",
                        "error": {
                            "type": "upstream_network_error",
                            "message": str(exc),
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            yield err_chunk.encode("utf-8")
            raise GatewayUpstreamError(
                f"upstream network error: {exc}", status=502, code="upstream_network_error"
            ) from exc


# ─────────────────────── M2-B 跨协议互转适配（_via_） ───────────────────────
#
# 这层是"装饰层"：复用上面的同协议 forward_*，前后包翻译层。
# 客户端协议 → 翻译 → 上游 provider 协议 → 翻译回 → 客户端响应
#
# 红线：
# - 仅这 4 个函数允许 import .translate.*
# - 翻译失败/上游错时降级透传原 body（即使客户端不识别也比 500 强）
# - 流式：用 parse_sse_events 拆 SSE → 喂状态机 → yield 翻译后字节
# 设计：_docs/GATEWAY_M2B.md §3-§4.7


async def forward_openai_chat_via_anthropic_nonstream(
    provider: ProviderConfig,
    openai_body: Dict[str, Any],
    vendor_model: str,
    *,
    incoming_headers: Dict[str, str],
    timeout_secs: float = 600.0,
) -> Tuple[int, bytes, Dict[str, str]]:
    """客户端 OpenAI Chat 请求 → provider 走 Anthropic Messages → 响应翻译回 OpenAI。"""
    from .translate import anthropic_response_to_openai, openai_request_to_anthropic

    anthropic_body = openai_request_to_anthropic(openai_body)
    anthropic_body.setdefault("model", vendor_model)
    status, body_bytes, resp_headers = await forward_anthropic_messages_nonstream(
        provider, anthropic_body, vendor_model,
        incoming_headers=_strip_client_headers_for_upstream(incoming_headers),
        timeout_secs=timeout_secs,
    )
    if 200 <= status < 300:
        try:
            anthropic_resp = json.loads(body_bytes)
            openai_resp = anthropic_response_to_openai(anthropic_resp, model=vendor_model)
            new_body = json.dumps(openai_resp, ensure_ascii=False).encode("utf-8")
            new_headers = {k: v for k, v in resp_headers.items() if k.lower() != "content-type"}
            new_headers["content-type"] = "application/json"
            return status, new_body, new_headers
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            logger.warning("[GATEWAY-TRANSLATE-WARN] o2a→a2o nonstream resp translation failed: %s", exc)
    return status, body_bytes, resp_headers


async def forward_openai_chat_via_anthropic_stream(
    provider: ProviderConfig,
    openai_body: Dict[str, Any],
    vendor_model: str,
    *,
    incoming_headers: Dict[str, str],
    idle_timeout_secs: float = 180.0,
) -> AsyncIterator[bytes]:
    """流式互转：OpenAI 客户端 ↔ Anthropic 上游。"""
    from .translate import openai_request_to_anthropic
    from .translate.response_stream import AnthropicToOpenAIStreamTranslator, parse_sse_events

    anthropic_body = openai_request_to_anthropic(openai_body)
    anthropic_body.setdefault("model", vendor_model)
    anthropic_body["stream"] = True

    translator = AnthropicToOpenAIStreamTranslator(model=vendor_model)
    buf = ""
    async for chunk in forward_anthropic_messages_stream(
        provider, anthropic_body, vendor_model,
        incoming_headers=_strip_client_headers_for_upstream(incoming_headers),
        idle_timeout_secs=idle_timeout_secs,
    ):
        try:
            buf += chunk.decode("utf-8", errors="replace")
        except Exception:
            continue
        events, buf = parse_sse_events(buf)
        for event_type, data in events:
            for out_chunk in translator.feed_event(event_type, data):
                yield out_chunk
    # 收尾：兜底 [DONE]
    for ev in translator.finish():
        yield ev


async def forward_anthropic_messages_via_openai_nonstream(
    provider: ProviderConfig,
    anthropic_body: Dict[str, Any],
    vendor_model: str,
    *,
    incoming_headers: Dict[str, str],
    timeout_secs: float = 600.0,
) -> Tuple[int, bytes, Dict[str, str]]:
    """客户端 Anthropic Messages 请求 → provider 走 OpenAI Chat → 响应翻译回 Anthropic。"""
    from .translate import anthropic_request_to_openai, openai_response_to_anthropic

    openai_body = anthropic_request_to_openai(anthropic_body)
    openai_body.setdefault("model", vendor_model)
    status, body_bytes, resp_headers = await forward_openai_chat_nonstream(
        provider, openai_body, vendor_model,
        incoming_headers=_strip_client_headers_for_upstream(incoming_headers),
        timeout_secs=timeout_secs,
    )
    if 200 <= status < 300:
        try:
            openai_resp = json.loads(body_bytes)
            anthropic_resp = openai_response_to_anthropic(openai_resp, model=vendor_model)
            new_body = json.dumps(anthropic_resp, ensure_ascii=False).encode("utf-8")
            new_headers = {k: v for k, v in resp_headers.items() if k.lower() != "content-type"}
            new_headers["content-type"] = "application/json"
            return status, new_body, new_headers
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            logger.warning("[GATEWAY-TRANSLATE-WARN] a2o→o2a nonstream resp translation failed: %s", exc)
    return status, body_bytes, resp_headers


async def forward_anthropic_messages_via_openai_stream(
    provider: ProviderConfig,
    anthropic_body: Dict[str, Any],
    vendor_model: str,
    *,
    incoming_headers: Dict[str, str],
    idle_timeout_secs: float = 180.0,
) -> AsyncIterator[bytes]:
    """流式互转：Anthropic 客户端 ↔ OpenAI 上游。"""
    from .translate import anthropic_request_to_openai
    from .translate.response_stream import OpenAIToAnthropicStreamTranslator, parse_sse_events

    openai_body = anthropic_request_to_openai(anthropic_body)
    openai_body.setdefault("model", vendor_model)
    openai_body["stream"] = True

    translator = OpenAIToAnthropicStreamTranslator(model=vendor_model)
    buf = ""
    async for chunk in forward_openai_chat_stream(
        provider, openai_body, vendor_model,
        incoming_headers=_strip_client_headers_for_upstream(incoming_headers),
        idle_timeout_secs=idle_timeout_secs,
    ):
        try:
            buf += chunk.decode("utf-8", errors="replace")
        except Exception:
            continue
        events, buf = parse_sse_events(buf)
        for event_type, data in events:
            # OpenAI SSE 的 [DONE] 哨兵已被 parse 转成事件，跳过即可
            if isinstance(data, dict) and data.get("_done_sentinel"):
                continue
            for out_chunk in translator.feed_chunk(data):
                yield out_chunk
    # 收尾
    for ev in translator.finish():
        yield ev


def _strip_client_headers_for_upstream(incoming: Dict[str, str]) -> Dict[str, str]:
    """跨协议时剥掉客户端协议特有的 headers（如客户端发了 anthropic-version 但上游是 OpenAI）。

    _build_upstream_headers / _build_anthropic_headers 都只 pick whitelist，所以这里
    主要剥 Authorization（防泄漏 token 给上游）和 host 等 hop-by-hop。
    """
    out = {}
    for k, v in incoming.items():
        low = k.lower()
        if low in ("authorization", "x-api-key", "host", "content-length", "connection", "anthropic-version", "anthropic-beta"):
            continue
        out[k] = v
    return out


# ─────────────────────────── M4-A：Embeddings ───────────────────────────


async def forward_embeddings(
    provider: ProviderConfig,
    body: Dict[str, Any],
    vendor_model: str,
    *,
    incoming_headers: Dict[str, str],
    timeout_secs: float = 600.0,
) -> Tuple[int, bytes, Dict[str, str]]:
    """OpenAI 风格 embeddings 透传（非流式）。

    Embeddings 协议跨厂商高度统一（OpenAI / Voyage / Cohere / 国产 BGE 都一样的请求/响应 schema），
    所以无需翻译层，httpx 直接 POST。

    限制：仅支持 transport='openai_chat' 的 provider。Anthropic 没有 embeddings API，
    routes 层应当在调用前校验 transport 并对 anthropic provider 直接返 501。

    返回 (status, body_bytes, response_headers)；上游 4xx/5xx 原样透传，
    仅网络层异常抛 GatewayUpstreamError（与 chat/completions 一致行为，方便 fallback 链复用）。
    """
    url = _build_embeddings_url(provider)
    headers = _build_upstream_headers(provider, incoming_headers)
    payload = dict(body)
    payload["model"] = vendor_model
    # embeddings 没有 stream 字段，但客户端误传也无碍——上游会忽略。

    try:
        async with httpx.AsyncClient(timeout=timeout_secs) as client:
            resp = await client.post(url, headers=headers, json=payload)
        resp_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length", "connection")
        }
        return resp.status_code, resp.content, resp_headers
    except httpx.TimeoutException as exc:
        raise GatewayUpstreamError(
            f"upstream timeout: {exc}", status=504, code="upstream_timeout"
        ) from exc
    except httpx.HTTPError as exc:
        raise GatewayUpstreamError(
            f"upstream connection failed: {exc}", status=502, code="upstream_unreachable"
        ) from exc


__all__ = [
    "GatewayUpstreamError",
    "forward_openai_chat_nonstream",
    "forward_openai_chat_stream",
    "forward_anthropic_messages_nonstream",
    "forward_anthropic_messages_stream",
    "forward_openai_chat_via_anthropic_nonstream",
    "forward_openai_chat_via_anthropic_stream",
    "forward_anthropic_messages_via_openai_nonstream",
    "forward_anthropic_messages_via_openai_stream",
    "forward_embeddings",
]
