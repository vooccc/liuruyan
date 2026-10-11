"""Gateway FastAPI router。挂到 main.py 后 endpoint 形如 `/gateway/v1/*`。

公共端点（外部客户端用，需 token）：
    GET  /gateway/v1/health              # 无需 token；返 {ok, enabled, version}
    GET  /gateway/v1/models              # 聚合所有 enabled provider 的命名空间模型
    POST /gateway/v1/chat/completions    # OpenAI Chat 透传

管理端点（仅 loopback 127.0.0.1 可调；柳如烟前端 UI 使用）：
    GET  /gateway/admin/config           # 当前配置
    PUT  /gateway/admin/config           # 更新配置（enabled / cors / ...）
    GET  /gateway/admin/models           # 模型清单（无 token，给 UI 用）
    GET  /gateway/admin/tokens           # 令牌列表（含明文）
    POST /gateway/admin/tokens           # 新建令牌
    PUT  /gateway/admin/tokens/{id}      # 改令牌
    DELETE /gateway/admin/tokens/{id}    # 删令牌
    POST /gateway/admin/tokens/{id}/rotate  # 重置密钥

注意：Gateway 是挂在主 FastAPI 进程上的——它不独立监听端口。所以 `host` 字段
对实际监听不生效（永远跟柳如烟主服务同 host）；这是 MVP 的简化，等 M6 拆 sidecar 时再说。
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import audit, auth
from .adapter import (
    GatewayUpstreamError,
    forward_anthropic_messages_nonstream,
    forward_anthropic_messages_stream,
    forward_anthropic_messages_via_openai_nonstream,
    forward_anthropic_messages_via_openai_stream,
    forward_embeddings,
    forward_openai_chat_nonstream,
    forward_openai_chat_stream,
    forward_openai_chat_via_anthropic_nonstream,
    forward_openai_chat_via_anthropic_stream,
)
from . import aliases as aliases_mod
from . import installers as installers_mod
from . import tokens as tokens_mod
from . import usage as usage_mod
from ..providers.store import load_providers_config_cached
from ..utils.api_host import format_api_host, normalize_anthropic_base_url
from .config import GatewayConfig, load_config, save_config
from .fallback import (
    FallbackOutcome,
    forward_with_fallback_nonstream,
    forward_with_fallback_stream,
)
from .health import get_cache as get_health_cache
from .launcher import build_launchers, serialize_launchers
from .responses_compat import (
    ResponsesTranslationError,
    chat_stream_to_responses_stream,
    req_to_chat as responses_req_to_chat,
    resp_to_resp as responses_resp_to_resp,
)
from .terminal_launcher import launch_command_in_new_terminal
from .router import (
    ResolveError,
    ResolveResult,
    list_namespaced_models,
    resolve_chain,
    resolve_model,
)

GATEWAY_VERSION = "0.7.1-m6"
router = APIRouter(prefix="/gateway", tags=["gateway"])


# ─────────────────────── deps ───────────────────────


def _client_ip(req: Request) -> str:
    if req.client:
        return req.client.host or "?"
    return "?"


def _is_loopback(req: Request) -> bool:
    ip = _client_ip(req)
    return ip in ("127.0.0.1", "::1", "localhost")


def _require_loopback(req: Request) -> None:
    """管理端点护栏：只允许 127.0.0.1 / ::1。"""
    if not _is_loopback(req):
        raise HTTPException(status_code=403, detail={"error": "loopback_only"})


def _require_enabled() -> GatewayConfig:
    cfg = load_config()
    if not cfg.enabled:
        raise HTTPException(status_code=503, detail={"error": "gateway_disabled"})
    return cfg


def _require_token(
    req: Request, cfg: GatewayConfig = Depends(_require_enabled)
) -> tokens_mod.ResolvedToken:
    """M6：校验 token 并解析为 ResolvedToken（含元信息 + 配额引用）。

    匹配链：多 token 表（hash）→ legacy 单 token → 401。
    require_token=false 时返 anonymous 占位（dev mode；不推荐生产用）。

    M7：补上 IP 白名单校验——token 本身合法但调用方 IP 不在
    ``allow_ips`` 里时返 401 ``ip_not_allowed``。另外会写 audit 事件
    以便事后查泄露/误报。
    """
    if not cfg.require_token:
        return tokens_mod.anonymous()
    presented = auth.extract_token_from_request(req)
    resolved = tokens_mod.resolve_presented(presented)
    if resolved is None:
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})
    try:
        tokens_mod.check_client_ip(resolved, _client_ip(req))
    except tokens_mod.IpNotAllowed as exc:
        audit.write_event(
            endpoint=req.url.path,
            token_id=exc.token_id,
            client_ip=exc.client_ip,
            status=401,
            error="ip_not_allowed",
        )
        raise HTTPException(
            status_code=401, detail={"error": "ip_not_allowed"}
        ) from exc
    return resolved


def _quota_error_response(
    *,
    exc: tokens_mod.QuotaExceeded,
    endpoint: str,
    resolved: tokens_mod.ResolvedToken,
    client_ip: str,
    model_requested: Any,
    stream: bool,
    error_envelope,
) -> JSONResponse:
    """配额超额：429 + Retry-After + 审计。"""
    audit.write_event(
        endpoint=endpoint,
        token_id=resolved.id,
        client_ip=client_ip,
        status=429,
        model_requested=model_requested,
        stream=stream,
        error=f"quota_exceeded:{exc.reason}",
        extra={
            "quota_reason": exc.reason,
            "quota_limit": exc.limit,
            "quota_current": exc.current,
            "retry_after_secs": exc.retry_after_secs,
        },
    )
    msg = (
        f"quota exceeded ({exc.reason}): {exc.current}/{exc.limit}; "
        f"retry after {exc.retry_after_secs}s"
    )
    return JSONResponse(
        status_code=429,
        content=error_envelope("quota_exceeded", msg),
        headers={"Retry-After": str(exc.retry_after_secs)},
    )


def _model_denied_response(
    *,
    exc: tokens_mod.ModelDenied,
    endpoint: str,
    resolved: tokens_mod.ResolvedToken,
    client_ip: str,
    stream: bool,
    error_envelope,
) -> JSONResponse:
    audit.write_event(
        endpoint=endpoint,
        token_id=resolved.id,
        client_ip=client_ip,
        status=403,
        model_requested=exc.model,
        stream=stream,
        error="model_denied_for_token",
    )
    return JSONResponse(
        status_code=403,
        content=error_envelope(
            "model_denied_for_token",
            f"token '{resolved.id}' is not allowed to access model '{exc.model}'",
        ),
    )


# ─────────────────────── 公共端点 ───────────────────────


@router.get("/v1/health")
async def health() -> Dict[str, Any]:
    """不需要 token；用于外部客户端探活。"""
    cfg = load_config()
    return {
        "ok": True,
        "version": GATEWAY_VERSION,
        "enabled": cfg.enabled,
    }


@router.get("/v1/models")
async def list_models(
    req: Request,
    token_id: tokens_mod.ResolvedToken = Depends(_require_token),
    capability: str | None = None,
) -> Dict[str, Any]:
    """OpenAI Models API 风格清单。id 形如 `<provider_id>:<model_id>`。

    M4-B：支持 `?capability=embeddings|vision|tool_use|reasoning|chat` 过滤。
    每个 entry 含 `capabilities` 数组；alias 含 `alias_status` 实时健康信息。
    """
    audit.write_event(
        endpoint="/gateway/v1/models",
        token_id=token_id.id,
        client_ip=_client_ip(req),
        status=200,
    )
    return {"object": "list", "data": list_namespaced_models(capability=capability)}


@router.post("/v1/chat/completions")
async def chat_completions(
    req: Request,
    payload: Dict[str, Any] = Body(...),
    token_id: tokens_mod.ResolvedToken = Depends(_require_token),
    cfg: GatewayConfig = Depends(_require_enabled),
):
    """OpenAI Chat Completions 端点。

    - body.model 可是 `<provider_id>:<vendor_model>`、alias 名、或裸 vendor_model
    - body.stream 默认 false
    - openai_chat provider 同协议透传（M1）；anthropic_messages provider 走跨协议互转（M2-B）
    - alias 名 → candidates 链（M3），遇译败自动切换
    """
    return await _handle_request(
        req=req,
        payload=payload,
        token_id=token_id,
        cfg=cfg,
        endpoint="/gateway/v1/chat/completions",
        pick_stream_forwarder=_pick_openai_endpoint_stream_forwarder,
        pick_nonstream_forwarder=_pick_openai_endpoint_nonstream_forwarder,
        error_envelope=lambda code, msg: {"error": {"code": code, "message": msg}},
    )


@router.post("/v1/responses")
async def responses(
    req: Request,
    payload: Dict[str, Any] = Body(...),
    token_id: tokens_mod.ResolvedToken = Depends(_require_token),
    cfg: GatewayConfig = Depends(_require_enabled),
):
    """OpenAI Responses API 端点（codex CLI 0.30+ 必需）。

    Codex 移除了 ``wire_api = "chat"``，强制走 Responses 协议。柳如烟内部全部
    围绕 chat completions（fallback / health / alias），此端点在边界翻译：

    - 入参 Responses → Chat（``responses_compat.req_to_chat``）
    - 转发：复用 ``forward_with_fallback_*`` + ``_pick_openai_endpoint_*``
    - 出参 Chat → Responses（非流式 ``resp_to_resp`` / 流式状态机）

    支持的 transport：``openai_chat``（直通）/ ``anthropic_messages``（双跳）。
    失真字段（``built_in_tools`` / Responses 内置工具）一律 400。

    设计依据：``docs/architecture_spec.md``
    """
    return await _handle_responses_request(
        req=req,
        payload=payload,
        token_id=token_id,
        cfg=cfg,
    )


@router.post("/v1/embeddings")
async def embeddings(
    req: Request,
    payload: Dict[str, Any] = Body(...),
    token_id: tokens_mod.ResolvedToken = Depends(_require_token),
    cfg: GatewayConfig = Depends(_require_enabled),
):
    """OpenAI Embeddings 端点（M4-A）。

    - body.model 可是 `<provider_id>:<vendor_model>`、alias 名、或裸 vendor_model
    - 永远非流式（embedding 协议无 stream）
    - 仅支持 transport=openai_chat 的 provider；anthropic 无 embedding API → 501
    - alias 多候选时同样走 fallback 链（非流式部分逻辑）
    """
    started = time.time()
    client_ip = _client_ip(req)
    incoming_headers = {k: v for k, v in req.headers.items()}
    model_requested = payload.get("model")
    err_envelope = lambda code, msg: {"error": {"code": code, "message": msg}}  # noqa: E731

    # M6: 配额 + allow/deny
    try:
        tokens_mod.check_model_allowed(token_id, model_requested)
        tokens_mod.check_quota(token_id)
    except tokens_mod.ModelDenied as exc:
        return _model_denied_response(
            exc=exc, endpoint="/gateway/v1/embeddings", resolved=token_id,
            client_ip=client_ip, stream=False, error_envelope=err_envelope,
        )
    except tokens_mod.QuotaExceeded as exc:
        return _quota_error_response(
            exc=exc, endpoint="/gateway/v1/embeddings", resolved=token_id,
            client_ip=client_ip, model_requested=model_requested, stream=False,
            error_envelope=err_envelope,
        )

    # 1. 解析候选链
    try:
        candidates = resolve_chain(model_requested or "")
    except ResolveError as exc:
        audit.write_event(
            endpoint="/gateway/v1/embeddings",
            token_id=token_id.id,
            client_ip=client_ip,
            status=exc.status,
            model_requested=model_requested,
            stream=False,
            error=exc.code,
        )
        return JSONResponse(status_code=exc.status, content=err_envelope(exc.code, str(exc)))

    # 2. 校验所有候选都是 openai_chat（embeddings 只支持这一种）
    for c in candidates:
        if c.provider.transport != "openai_chat":
            audit.write_event(
                endpoint="/gateway/v1/embeddings",
                token_id=token_id.id,
                client_ip=client_ip,
                status=501,
                model_requested=model_requested,
                provider=c.provider.id,
                vendor_model=c.vendor_model,
                stream=False,
                error="transport_unsupported_for_embeddings",
            )
            return JSONResponse(
                status_code=501,
                content=err_envelope(
                    "transport_unsupported_for_embeddings",
                    f"provider '{c.provider.id}' uses transport '{c.provider.transport}'; "
                    f"embeddings endpoint requires transport=openai_chat. "
                    f"Anthropic providers do not expose an embeddings API.",
                ),
            )

    # 3. 转发（非流式 fallback 链复用 M3）
    try:
        status, body_bytes, resp_headers, outcome = await forward_with_fallback_nonstream(
            candidates,
            _pick_embeddings_forwarder,
            payload,
            incoming_headers=incoming_headers,
            timeout_secs=600.0,
        )
    except GatewayUpstreamError as exc:
        audit.write_event(
            endpoint="/gateway/v1/embeddings",
            token_id=token_id.id,
            client_ip=client_ip,
            status=exc.status,
            model_requested=model_requested,
            stream=False,
            upstream_latency_ms=int((time.time() - started) * 1000),
            error=exc.code,
        )
        return JSONResponse(status_code=exc.status, content=err_envelope(exc.code, str(exc)))

    prompt_t, completion_t = _extract_openai_usage(body_bytes)
    if prompt_t or completion_t:
        tokens_mod.record_response_tokens(token_id, (prompt_t or 0) + (completion_t or 0))
    audit.write_event(
        endpoint="/gateway/v1/embeddings",
        token_id=token_id.id,
        client_ip=client_ip,
        status=status,
        model_requested=model_requested,
        provider=outcome.final_provider or "",
        vendor_model=outcome.final_vendor_model or "",
        stream=False,
        upstream_latency_ms=int((time.time() - started) * 1000),
        prompt_tokens=prompt_t,
        completion_tokens=completion_t,
        extra={"fallback_chain": outcome.to_audit_dict()},
    )
    return Response(content=body_bytes, status_code=status, headers=resp_headers)


@router.post("/v1/messages")
async def anthropic_messages(
    req: Request,
    payload: Dict[str, Any] = Body(...),
    token_id: tokens_mod.ResolvedToken = Depends(_require_token),
    cfg: GatewayConfig = Depends(_require_enabled),
):
    """Anthropic Messages 端点。

    - body.model 可是 `<provider_id>:<vendor_model>`、alias 名、或裸 vendor_model
    - body.stream 默认 false
    - anthropic_messages provider 同协议透传（M2-A）；openai_chat provider 走跨协议互转（M2-B）
    - alias 名 → candidates 链（M3），遇译败自动切换
    """
    return await _handle_request(
        req=req,
        payload=payload,
        token_id=token_id,
        cfg=cfg,
        endpoint="/gateway/v1/messages",
        pick_stream_forwarder=_pick_anthropic_endpoint_stream_forwarder,
        pick_nonstream_forwarder=_pick_anthropic_endpoint_nonstream_forwarder,
        error_envelope=lambda code, msg: {"type": "error", "error": {"type": code, "message": msg}},
    )


# ─────────────────────── 端点共享 helper（M3 重构） ───────────────────────


def _pick_openai_endpoint_nonstream_forwarder(candidate: ResolveResult):
    """OpenAI 端点根据 candidate.transport 选 nonstream forwarder。"""
    t = candidate.provider.transport
    if t == "openai_chat":
        return forward_openai_chat_nonstream
    if t == "anthropic_messages":
        return forward_openai_chat_via_anthropic_nonstream
    raise GatewayUpstreamError(
        f"unsupported transport '{t}'", status=501, code="transport_unsupported"
    )


def _pick_openai_endpoint_stream_forwarder(candidate: ResolveResult):
    t = candidate.provider.transport
    if t == "openai_chat":
        return forward_openai_chat_stream
    if t == "anthropic_messages":
        return forward_openai_chat_via_anthropic_stream
    raise GatewayUpstreamError(
        f"unsupported transport '{t}'", status=501, code="transport_unsupported"
    )


def _pick_anthropic_endpoint_nonstream_forwarder(candidate: ResolveResult):
    t = candidate.provider.transport
    if t == "anthropic_messages":
        return forward_anthropic_messages_nonstream
    if t == "openai_chat":
        return forward_anthropic_messages_via_openai_nonstream
    raise GatewayUpstreamError(
        f"unsupported transport '{t}'", status=501, code="transport_unsupported"
    )


def _pick_anthropic_endpoint_stream_forwarder(candidate: ResolveResult):
    t = candidate.provider.transport
    if t == "anthropic_messages":
        return forward_anthropic_messages_stream
    if t == "openai_chat":
        return forward_anthropic_messages_via_openai_stream
    raise GatewayUpstreamError(
        f"unsupported transport '{t}'", status=501, code="transport_unsupported"
    )


def _pick_embeddings_forwarder(candidate: ResolveResult):
    """embeddings 端点：仅支持 openai_chat transport。anthropic provider 没 embeddings API。"""
    t = candidate.provider.transport
    if t == "openai_chat":
        return forward_embeddings
    raise GatewayUpstreamError(
        f"provider '{candidate.provider.id}' uses transport '{t}'; "
        f"embeddings endpoint requires transport=openai_chat",
        status=501,
        code="transport_unsupported_for_embeddings",
    )


def _extract_openai_usage(body: bytes) -> tuple[int | None, int | None]:
    """从 OpenAI 风格非流式响应里抽 usage.prompt_tokens / completion_tokens。

    解析失败一律返 (None, None)，不影响主流程。embeddings 响应也走这套
    （仅有 prompt_tokens，completion_tokens 为 0）。
    """
    try:
        import json as _json
        obj = _json.loads(body)
        usage = obj.get("usage") if isinstance(obj, dict) else None
        if not isinstance(usage, dict):
            return None, None
        p = usage.get("prompt_tokens")
        c = usage.get("completion_tokens")
        return (int(p) if isinstance(p, (int, float)) else None,
                int(c) if isinstance(c, (int, float)) else None)
    except Exception:  # noqa: BLE001
        return None, None


def _extract_anthropic_usage(body: bytes) -> tuple[int | None, int | None]:
    """Anthropic 风格 usage：{input_tokens, output_tokens}。"""
    try:
        import json as _json
        obj = _json.loads(body)
        usage = obj.get("usage") if isinstance(obj, dict) else None
        if not isinstance(usage, dict):
            return None, None
        p = usage.get("input_tokens")
        c = usage.get("output_tokens")
        return (int(p) if isinstance(p, (int, float)) else None,
                int(c) if isinstance(c, (int, float)) else None)
    except Exception:  # noqa: BLE001
        return None, None


async def _handle_request(
    *,
    req: Request,
    payload: Dict[str, Any],
    token_id: tokens_mod.ResolvedToken,
    cfg: GatewayConfig,
    endpoint: str,
    pick_stream_forwarder,
    pick_nonstream_forwarder,
    error_envelope,
):
    """两个端点共享的请求处理：resolve_chain → fallback → 写 audit → 返响应。"""
    started = time.time()
    client_ip = _client_ip(req)
    incoming_headers = {k: v for k, v in req.headers.items()}
    model_requested = payload.get("model")
    stream = bool(payload.get("stream"))

    # M6: allow/deny + 配额（在解析候选链之前；超额直接 429）
    try:
        tokens_mod.check_model_allowed(token_id, model_requested)
        tokens_mod.check_quota(token_id)
    except tokens_mod.ModelDenied as exc:
        return _model_denied_response(
            exc=exc, endpoint=endpoint, resolved=token_id,
            client_ip=client_ip, stream=stream, error_envelope=error_envelope,
        )
    except tokens_mod.QuotaExceeded as exc:
        return _quota_error_response(
            exc=exc, endpoint=endpoint, resolved=token_id,
            client_ip=client_ip, model_requested=model_requested, stream=stream,
            error_envelope=error_envelope,
        )

    # 1. 解析 model → candidates 链
    try:
        candidates = resolve_chain(model_requested or "")
    except ResolveError as exc:
        audit.write_event(
            endpoint=endpoint,
            token_id=token_id.id,
            client_ip=client_ip,
            status=exc.status,
            model_requested=model_requested,
            stream=stream,
            error=exc.code,
        )
        return JSONResponse(
            status_code=exc.status,
            content=error_envelope(exc.code, str(exc)),
        )

    # 2. 校验所有 candidate 的 transport 都受支持（任一不受支持直接 501）
    for c in candidates:
        if c.provider.transport not in ("openai_chat", "anthropic_messages"):
            audit.write_event(
                endpoint=endpoint,
                token_id=token_id.id,
                client_ip=client_ip,
                status=501,
                model_requested=model_requested,
                provider=c.provider.id,
                vendor_model=c.vendor_model,
                stream=stream,
                error="transport_unsupported",
            )
            return JSONResponse(
                status_code=501,
                content=error_envelope(
                    "transport_unsupported",
                    f"provider '{c.provider.id}' uses transport '{c.provider.transport}'; "
                    f"gateway only supports openai_chat / anthropic_messages.",
                ),
            )

    # 3. 转发（含 fallback 链）
    if stream:
        async def gen():
            outcome = FallbackOutcome()
            try:
                async for chunk in forward_with_fallback_stream(
                    candidates,
                    pick_stream_forwarder,
                    payload,
                    incoming_headers=incoming_headers,
                    idle_timeout_secs=float(cfg.idle_timeout_secs),
                    outcome=outcome,
                ):
                    yield chunk
            finally:
                audit.write_event(
                    endpoint=endpoint,
                    token_id=token_id.id,
                    client_ip=client_ip,
                    status=200 if outcome.final_provider else 502,
                    model_requested=model_requested,
                    provider=outcome.final_provider or "",
                    vendor_model=outcome.final_vendor_model or "",
                    stream=True,
                    upstream_latency_ms=int((time.time() - started) * 1000),
                    error=None if outcome.final_provider else "all_candidates_failed",
                    extra={"fallback_chain": outcome.to_audit_dict()},
                )

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 非流式
    try:
        status, body_bytes, resp_headers, outcome = await forward_with_fallback_nonstream(
            candidates,
            pick_nonstream_forwarder,
            payload,
            incoming_headers=incoming_headers,
            timeout_secs=600.0,
        )
    except GatewayUpstreamError as exc:
        audit.write_event(
            endpoint=endpoint,
            token_id=token_id.id,
            client_ip=client_ip,
            status=exc.status,
            model_requested=model_requested,
            stream=False,
            upstream_latency_ms=int((time.time() - started) * 1000),
            error=exc.code,
        )
        return JSONResponse(
            status_code=exc.status,
            content=error_envelope(exc.code, str(exc)),
        )

    # M6: 从响应抽 usage 记入 token 计量窗口
    if "/v1/messages" in endpoint:
        prompt_t, completion_t = _extract_anthropic_usage(body_bytes)
    else:
        prompt_t, completion_t = _extract_openai_usage(body_bytes)
    if prompt_t or completion_t:
        tokens_mod.record_response_tokens(token_id, (prompt_t or 0) + (completion_t or 0))

    audit.write_event(
        endpoint=endpoint,
        token_id=token_id.id,
        client_ip=client_ip,
        status=status,
        model_requested=model_requested,
        provider=outcome.final_provider or "",
        vendor_model=outcome.final_vendor_model or "",
        stream=False,
        upstream_latency_ms=int((time.time() - started) * 1000),
        prompt_tokens=prompt_t,
        completion_tokens=completion_t,
        extra={"fallback_chain": outcome.to_audit_dict()},
    )
    return Response(content=body_bytes, status_code=status, headers=resp_headers)


# ───────────────────── Responses API（codex 兼容）───────────────────────


def _responses_error_envelope(code: str, msg: str) -> Dict[str, Any]:
    """Responses API 错误信封（OpenAI 风格 ``{error:{type,code,message}}``）。"""
    return {"error": {"type": code, "code": code, "message": msg}}


async def _handle_responses_request(
    *,
    req: Request,
    payload: Dict[str, Any],
    token_id: tokens_mod.ResolvedToken,
    cfg: GatewayConfig,
):
    """POST /v1/responses 主流程：翻译 → 转发 → 反翻译。

    复用 chat 端点的 fallback/audit 设施，所以在 chat-shaped 协议层做 health/black-list
    完全等价；codex 侧只看到 responses 事件流。
    """
    endpoint = "/gateway/v1/responses"
    started = time.time()
    client_ip = _client_ip(req)
    incoming_headers = {k: v for k, v in req.headers.items()}
    model_requested = payload.get("model") if isinstance(payload, dict) else None
    stream = bool(payload.get("stream")) if isinstance(payload, dict) else False
    err_env = _responses_error_envelope

    # 1. 翻译入参 responses → chat（失真字段抛 ResponsesTranslationError → 400）
    # loss_log 收集静默丢/忽略的字段（builtin_tool / namespace_invalid / unknown_tool_type /
    # ignored:metadata 等），后续每个 audit.write_event 都用 _with_lossy 合并进 extra，
    # 便于事后在 ~/.liuruyan/gateway_audit.jsonl 里定位"为什么模型没看到 web_search /
    # 为什么 metadata 没透到上游"等问题。tag 命名见 responses_compat 模块 docstring。
    loss_log: list[str] = []

    def _with_lossy(extra: dict | None = None) -> dict | None:
        """把 loss_log 合并进 audit extra；loss_log 空时不强制创建 extra dict。"""
        if not loss_log:
            return extra
        merged = dict(extra) if extra else {}
        merged["lossy_fields"] = list(loss_log)
        return merged

    try:
        chat_body, response_id, created_at, model = responses_req_to_chat(
            payload, loss_log=loss_log
        )
    except ResponsesTranslationError as exc:
        audit.write_event(
            endpoint=endpoint,
            token_id=token_id.id,
            client_ip=client_ip,
            status=exc.status,
            model_requested=model_requested,
            stream=stream,
            error=exc.code,
            extra=_with_lossy(),
        )
        return JSONResponse(
            status_code=exc.status,
            content=err_env(exc.code, exc.message),
        )

    # 2. allow/deny + 配额
    try:
        tokens_mod.check_model_allowed(token_id, model_requested)
        tokens_mod.check_quota(token_id)
    except tokens_mod.ModelDenied as exc:
        return _model_denied_response(
            exc=exc, endpoint=endpoint, resolved=token_id,
            client_ip=client_ip, stream=stream, error_envelope=err_env,
        )
    except tokens_mod.QuotaExceeded as exc:
        return _quota_error_response(
            exc=exc, endpoint=endpoint, resolved=token_id,
            client_ip=client_ip, model_requested=model_requested, stream=stream,
            error_envelope=err_env,
        )

    # 3. resolve_chain（用原始 model 名做 alias 解析）
    try:
        candidates = resolve_chain(model_requested or model)
    except ResolveError as exc:
        audit.write_event(
            endpoint=endpoint,
            token_id=token_id.id,
            client_ip=client_ip,
            status=exc.status,
            model_requested=model_requested,
            stream=stream,
            error=exc.code,
            extra=_with_lossy(),
        )
        return JSONResponse(
            status_code=exc.status,
            content=err_env(exc.code, str(exc)),
        )

    # 4. 校验 transport（openai_chat / anthropic_messages 都能 forward 成 chat shape）
    for c in candidates:
        if c.provider.transport not in ("openai_chat", "anthropic_messages"):
            audit.write_event(
                endpoint=endpoint,
                token_id=token_id.id,
                client_ip=client_ip,
                status=501,
                model_requested=model_requested,
                provider=c.provider.id,
                vendor_model=c.vendor_model,
                stream=stream,
                error="transport_unsupported",
                extra=_with_lossy(),
            )
            return JSONResponse(
                status_code=501,
                content=err_env(
                    "transport_unsupported",
                    f"provider '{c.provider.id}' uses transport '{c.provider.transport}'; "
                    f"responses endpoint requires openai_chat / anthropic_messages.",
                ),
            )

    # 5a. 流式：把上游 chat SSE 用状态机翻成 responses 事件流
    if stream:
        async def chat_chunks_gen(_outcome: FallbackOutcome):
            async for chunk in forward_with_fallback_stream(
                candidates,
                _pick_openai_endpoint_stream_forwarder,
                chat_body,
                incoming_headers=incoming_headers,
                idle_timeout_secs=float(cfg.idle_timeout_secs),
                outcome=_outcome,
            ):
                yield chunk

        async def gen():
            outcome = FallbackOutcome()
            try:
                async for ev in chat_stream_to_responses_stream(
                    chat_chunks_gen(outcome),
                    response_id=response_id,
                    created_at=created_at,
                    model=model,
                ):
                    yield ev
            finally:
                audit.write_event(
                    endpoint=endpoint,
                    token_id=token_id.id,
                    client_ip=client_ip,
                    status=200 if outcome.final_provider else 502,
                    model_requested=model_requested,
                    provider=outcome.final_provider or "",
                    vendor_model=outcome.final_vendor_model or "",
                    stream=True,
                    upstream_latency_ms=int((time.time() - started) * 1000),
                    error=None if outcome.final_provider else "all_candidates_failed",
                    extra={"fallback_chain": outcome.to_audit_dict()},
                )

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 5b. 非流式
    try:
        status, body_bytes, _resp_headers, outcome = await forward_with_fallback_nonstream(
            candidates,
            _pick_openai_endpoint_nonstream_forwarder,
            chat_body,
            incoming_headers=incoming_headers,
            timeout_secs=600.0,
        )
    except GatewayUpstreamError as exc:
        audit.write_event(
            endpoint=endpoint,
            token_id=token_id.id,
            client_ip=client_ip,
            status=exc.status,
            model_requested=model_requested,
            stream=False,
            upstream_latency_ms=int((time.time() - started) * 1000),
            error=exc.code,
            extra=_with_lossy(),
        )
        return JSONResponse(
            status_code=exc.status,
            content=err_env(exc.code, str(exc)),
        )

    # 上游已 4xx/5xx：透传错误体（不翻译；Responses 客户端能识别 OpenAI 风格 error）
    if status >= 400:
        audit.write_event(
            endpoint=endpoint,
            token_id=token_id.id,
            client_ip=client_ip,
            status=status,
            model_requested=model_requested,
            provider=outcome.final_provider or "",
            vendor_model=outcome.final_vendor_model or "",
            stream=False,
            upstream_latency_ms=int((time.time() - started) * 1000),
            error="upstream_error",
            extra=_with_lossy({"fallback_chain": outcome.to_audit_dict()}),
        )
        return Response(
            content=body_bytes,
            status_code=status,
            media_type="application/json",
        )

    # 翻译响应 chat → responses
    try:
        import json as _json
        chat_resp = _json.loads(body_bytes)
    except (ValueError, TypeError):
        audit.write_event(
            endpoint=endpoint,
            token_id=token_id.id,
            client_ip=client_ip,
            status=502,
            model_requested=model_requested,
            provider=outcome.final_provider or "",
            vendor_model=outcome.final_vendor_model or "",
            stream=False,
            upstream_latency_ms=int((time.time() - started) * 1000),
            error="upstream_invalid_json",
            extra=_with_lossy(),
        )
        return JSONResponse(
            status_code=502,
            content=err_env("upstream_invalid_json", "upstream returned non-JSON body"),
        )

    responses_resp = responses_resp_to_resp(
        chat_resp,
        response_id=response_id,
        created_at=created_at,
        model=model,
    )

    prompt_t, completion_t = _extract_openai_usage(body_bytes)
    if prompt_t or completion_t:
        tokens_mod.record_response_tokens(token_id, (prompt_t or 0) + (completion_t or 0))

    audit.write_event(
        endpoint=endpoint,
        token_id=token_id.id,
        client_ip=client_ip,
        status=status,
        model_requested=model_requested,
        provider=outcome.final_provider or "",
        vendor_model=outcome.final_vendor_model or "",
        stream=False,
        upstream_latency_ms=int((time.time() - started) * 1000),
        prompt_tokens=prompt_t,
        completion_tokens=completion_t,
        extra=_with_lossy({"fallback_chain": outcome.to_audit_dict()}),
    )
    return JSONResponse(status_code=status, content=responses_resp)


# ───────────────────── 管理端点 ───────────────────────


@router.get("/admin/config")
async def admin_get_config(req: Request) -> Dict[str, Any]:
    _require_loopback(req)
    return load_config().to_public_dict()


@router.put("/admin/config")
async def admin_update_config(
    req: Request,
    body: Dict[str, Any] = Body(...),
) -> Dict[str, Any]:
    """部分更新。仅接受已知字段，未知字段静默忽略。"""
    _require_loopback(req)
    cur = load_config()
    fields = {f.name for f in dataclasses.fields(GatewayConfig)}
    for k in fields:
        if k in body:
            setattr(cur, k, body[k])
    save_config(cur)
    # 启用时确保 token 存在（懒生成）
    if cur.enabled:
        auth.ensure_token()
    return cur.to_public_dict()


@router.get("/admin/models")
async def admin_list_models(req: Request, capability: str | None = None) -> Dict[str, Any]:
    """模型清单——loopback 专用，无 token。

    专给前端 UI 取「外部 model 字段写啥」用。与 ``/v1/models`` 同源
    （都调 ``list_namespaced_models``），但路径无鉴权，避免 UI 为了拉清单
    去碰用户的 token 明文。
    """
    _require_loopback(req)
    return {"object": "list", "data": list_namespaced_models(capability=capability)}


@router.get("/admin/launchers")
async def admin_get_launchers(req: Request) -> Dict[str, Any]:
    """返回所有外部 CLI 的启动命令片段（已注入当前 token 和 base url）。

    给前端 Gateway 设置面板的"启动外部 CLI"区用。loopback only（命令里嵌着 token）。

    实现细节：base url 由请求的 Host header 推导，避免硬编码端口——这样 Tauri 用动态端口
    时也能正确反映。
    """
    _require_loopback(req)
    # 用请求自身的 host 推导 base，保证 dev/prod/动态端口都对
    host_header = req.headers.get("host") or "127.0.0.1:8001"
    scheme = req.url.scheme or "http"
    api_base = f"{scheme}://{host_header}"
    snippets = build_launchers(
        openai_base_url=f"{api_base}/gateway/v1",
        anthropic_base_url=f"{api_base}/gateway",
        token=auth.ensure_token(),
    )
    return {
        "openai_base_url": f"{api_base}/gateway/v1",
        "anthropic_base_url": f"{api_base}/gateway",
        "launchers": serialize_launchers(snippets),
    }


@router.post("/admin/launchers/{launcher_id}/launch_terminal")
async def admin_launch_terminal(
    req: Request,
    launcher_id: str,
    body: Dict[str, Any] = Body(default={}),
) -> Dict[str, Any]:
    """在新终端窗口里直接跑某条 launcher snippet（一键启动 CLI）。

    安全设计：
    - loopback only（路由层 _require_loopback）
    - 命令在**服务端重建**（用当前真 token + base_url），不信前端传过来的命令字符串
    - 仅 runnable=True 的 launcher 才允许启动（GUI 类型直接 400）

    body:
      { "shell": "powershell" | "posix", "cwd"?: "<absolute path>" }
      # shell: 必填；前端按当前 shellMode 传
      # cwd : 可选；给定时新终端窗口的起点目录会是该路径（适用于 claude code 等"项目级"CLI）

    返回：{ ok, message, platform, pid? }
    """
    _require_loopback(req)
    payload = body or {}
    shell = payload.get("shell", "")
    if shell not in ("powershell", "posix"):
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_shell", "message": "shell must be 'powershell' or 'posix'"}},
        )
    cwd_raw = payload.get("cwd")
    cwd: Optional[str] = None
    if cwd_raw is not None:
        if not isinstance(cwd_raw, str) or not cwd_raw.strip():
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "invalid_cwd", "message": "cwd 必须是非空字符串"}},
            )
        cwd = cwd_raw

    # 服务端重建命令（不信前端）
    host_header = req.headers.get("host") or "127.0.0.1:8001"
    scheme = req.url.scheme or "http"
    api_base = f"{scheme}://{host_header}"
    snippets = build_launchers(
        openai_base_url=f"{api_base}/gateway/v1",
        anthropic_base_url=f"{api_base}/gateway",
        token=auth.ensure_token(),
    )
    snippet = next((s for s in snippets if s.id == launcher_id), None)
    if snippet is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "launcher_not_found", "message": launcher_id}},
        )
    if not snippet.runnable:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "not_runnable", "message": f"{snippet.name} 是 GUI 配置型，无法启动终端"}},
        )

    command = snippet.powershell_command if shell == "powershell" else snippet.posix_command
    result = launch_command_in_new_terminal(command=command, shell=shell, title=snippet.name, cwd=cwd)
    if not result.ok:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": {"code": "launch_failed", "message": result.message}, "platform": result.platform},
        )
    return {"ok": True, "message": result.message, "platform": result.platform, "pid": result.pid}


# ─────────────────────── M3：alias CRUD + health snapshot ───────────────────────


@router.get("/admin/aliases")
async def admin_list_aliases(req: Request) -> Dict[str, Any]:
    """列出所有 alias。"""
    _require_loopback(req)
    return {
        "aliases": {
            name: entry.to_dict() for name, entry in aliases_mod.list_aliases().items()
        }
    }


@router.post("/admin/aliases")
async def admin_create_alias(req: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """创建一条 alias。

    body: {"name": "...", "candidates": ["provider:model", ...], "description": ""}
    若 name 已存在则整体替换（同 PUT 行为）。
    """
    _require_loopback(req)
    name = (body or {}).get("name") or ""
    candidates = (body or {}).get("candidates") or []
    description = (body or {}).get("description") or ""
    try:
        entry = aliases_mod.upsert_alias(name, candidates, description=description)
    except aliases_mod.AliasValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )
    return {"name": name, **entry.to_dict()}


@router.put("/admin/aliases/{name}")
async def admin_replace_alias(req: Request, name: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """整体替换一条 alias 的 candidates / description。"""
    _require_loopback(req)
    candidates = (body or {}).get("candidates") or []
    description = (body or {}).get("description") or ""
    try:
        entry = aliases_mod.upsert_alias(name, candidates, description=description)
    except aliases_mod.AliasValidationError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )
    return {"name": name, **entry.to_dict()}


@router.delete("/admin/aliases/{name}")
async def admin_delete_alias(req: Request, name: str) -> Dict[str, Any]:
    """删除 alias。不存在返 404。"""
    _require_loopback(req)
    if not aliases_mod.delete_alias(name):
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "alias_not_found", "message": f"alias '{name}' does not exist"}},
        )
    return {"ok": True, "deleted": name}


@router.get("/admin/health")
async def admin_health_snapshot(req: Request) -> Dict[str, Any]:
    """当前 provider 黑屋状态（调试 fallback 链用）。"""
    _require_loopback(req)
    return {"unhealthy_providers": get_health_cache().snapshot()}


@router.post("/admin/health/clear")
async def admin_clear_health(req: Request) -> Dict[str, Any]:
    """强制清空黑屋（admin 紧急恢复）。"""
    _require_loopback(req)
    get_health_cache().clear()
    return {"ok": True}


# ─────────────────────── M4-C：用量统计 ───────────────────────


@router.get("/admin/usage")
async def admin_usage(
    req: Request,
    group_by: str = "provider",
    start: str | None = None,
    end: str | None = None,
    top_n: int = 100,
) -> Dict[str, Any]:
    """从 audit jsonl 聚合用量统计。

    Query 参数：
      - group_by: provider | model | token | day | endpoint | client_ip
      - start:    起始时间戳（ISO 8601，可只传 'YYYY-MM-DD'）
      - end:      终止时间戳（同上）
      - top_n:    返回桶数上限（默认 100）

    后端每次扫一遍 jsonl 内存聚合；本地网关流量 < 100k/月时性能足够。
    详见 docs/GATEWAY.md。
    """
    _require_loopback(req)
    try:
        return usage_mod.build_usage(group_by=group_by, start=start, end=end, top_n=top_n)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_group_by", "message": str(exc)}},
        )


@router.get("/admin/usage/fallback-jumps")
async def admin_recent_fallback_jumps(req: Request, limit: int = 20) -> Dict[str, Any]:
    """最近 N 次实际触发 fallback 切换的请求（用户排查"为啥总切到备胎"）。"""
    _require_loopback(req)
    return {"jumps": usage_mod.list_recent_fallback_jumps(limit=limit)}


# ─────────────────────── M5：一键 CLI 接入 ───────────────────────


def _derive_base_urls(req: Request) -> tuple[str, str]:
    """从请求的 Host header 推导出 openai/anthropic base url（与 launchers 端点同套逻辑）。"""
    host_header = req.headers.get("host") or "127.0.0.1:8001"
    scheme = req.url.scheme or "http"
    api_base = f"{scheme}://{host_header}"
    return f"{api_base}/gateway/v1", f"{api_base}/gateway"


@router.get("/admin/installers")
async def admin_list_installers(req: Request) -> Dict[str, Any]:
    """列出所有 installer + 当前状态（loopback only）。"""
    _require_loopback(req)
    items = []
    for info in installers_mod.list_installers():
        try:
            st = installers_mod.get_status(info.id)
            items.append({
                "info": {
                    "id": info.id,
                    "name": info.name,
                    "description": info.description,
                    "protocol": info.protocol,
                    "target_path": info.target_path,
                },
                "status": {
                    "state": st.state,
                    "detail": st.detail,
                    "target_path": st.target_path,
                    "target_exists": st.target_exists,
                    "last_install_at": st.last_install_at,
                    "backup_count": st.backup_count,
                },
            })
        except Exception as exc:  # noqa: BLE001
            items.append({
                "info": {
                    "id": info.id,
                    "name": info.name,
                    "description": info.description,
                    "protocol": info.protocol,
                    "target_path": info.target_path,
                },
                "status": {"state": "error", "detail": str(exc)},
            })
    return {"installers": items}


def _build_installer_extra(installer_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """从 body 抽 installer-specific 额外参数。

    当前支持：
      - claude_code: ``default_models = {"haiku/sonnet/opus": "<provider:model>" | None}``
              ``mode = "gateway"|"direct"``（可选，默认 gateway；direct 时另需
              ``provider_id``，见 _resolve_claude_direct——anthropic_messages 协议
              服务商直连，base_url 剩末尾 /v1，default_models 剥 provider 前缀）
      - codex: ``default_model = "<alias 或 provider:model>"``（必填）
              ``reasoning_effort = "minimal"|"low"|"medium"|"high"``（可选，默认 high）
              ``mode = "gateway"|"direct"``（可选，默认 gateway；direct 见 _resolve_codex_direct）
              ``wire_api = "responses"``（兼容参数：codex 0.153+ 已移除 chat wire，
              现在两种模式均固定 responses，chat 值会被忽略）

    forward-compatible：未知字段忽略。codex.default_model 为空不在这里报错，
    交由 installer 本身抽 ``InstallerExtraInvalid``，路由层统一转 400。
    """
    extra: Dict[str, Any] = {}
    body = body or {}
    if installer_id == "claude_code":
        dm = body.get("default_models")
        if isinstance(dm, dict):
            # 只接受 haiku/sonnet/opus 三个 key；值必须是非空 string
            cleaned: Dict[str, str] = {}
            for role in ("haiku", "sonnet", "opus"):
                v = dm.get(role)
                if isinstance(v, str) and v.strip():
                    cleaned[role] = v.strip()
            if cleaned:
                extra["default_models"] = cleaned
        mode = body.get("mode")
        if isinstance(mode, str) and mode.strip():
            extra["mode"] = mode.strip()
    elif installer_id == "codex":
        dm = body.get("default_model")
        if isinstance(dm, str) and dm.strip():
            extra["default_model"] = dm.strip()
        re_eff = body.get("reasoning_effort")
        if isinstance(re_eff, str) and re_eff.strip():
            extra["reasoning_effort"] = re_eff.strip()
        mode = body.get("mode")
        if isinstance(mode, str) and mode.strip():
            extra["mode"] = mode.strip()
        wire = body.get("wire_api")
        if isinstance(wire, str) and wire.strip():
            extra["wire_api"] = wire.strip()
    return extra


def _resolve_codex_direct(body: Dict[str, Any], extra: Dict[str, Any]) -> tuple[str, str]:
    """codex 直连模式：从 provider store 解析真实 base_url / api_key。

    body 需带 ``provider_id``（柳如烟『模型管理』里配的服务商，如千刀）。
    解析成功时就地补全 extra（mode/provider_name/extra_headers，并把
    default_model 的 ``<provider_id>:`` 前缀剥掉——直连时 model 必须是
    vendor 裸 model id），返回 ``(api_key, base_url)`` 供路由层覆盖
    install() 的 token / openai_base_url 参数。

    raise ``InstallerExtraInvalid``（路由层转 400）当：provider 缺失 / 不存在 /
    禁用 / transport 不是 openai_chat（codex 只讲 OpenAI 协议，Anthropic
    上游必须走网关模式的协议互转）/ base_url 为空。
    """
    api_key, base = _resolve_direct_provider(
        body,
        extra,
        installer_label="codex",
        required_transport="openai_chat",
        normalize=lambda u: format_api_host(u, support_api_version=True, api_version="v1"),
        model_fields=("default_model",),
    )
    return api_key, base


def _resolve_claude_direct(body: Dict[str, Any], extra: Dict[str, Any]) -> tuple[str, str]:
    """claude_code 直连模式：从 provider store 解析真实 base_url / api_key。

    与 _resolve_codex_direct 同构，差异：
    - 要求 transport == anthropic_messages（Claude Code 只讲 Anthropic 协议；
      OpenAI 上游必须走网关模式做协议互转）
    - base_url 用 ``normalize_anthropic_base_url`` 剩末尾 /v1：Claude Code
      （Anthropic SDK）会在 base_url 后自己拼 /v1/messages，不剥会变 /v1/v1/messages 404
    - default_models（haiku/sonnet/opus 三个角色）的 ``<provider_id>:`` 前缀同样剥掉
    """
    return _resolve_direct_provider(
        body,
        extra,
        installer_label="claude_code",
        required_transport="anthropic_messages",
        normalize=normalize_anthropic_base_url,
        model_fields=("haiku", "sonnet", "opus"),
        models_container="default_models",
    )


def _resolve_direct_provider(
    body: Dict[str, Any],
    extra: Dict[str, Any],
    *,
    installer_label: str,
    required_transport: str,
    normalize,
    model_fields: tuple,
    models_container: Optional[str] = None,
) -> tuple[str, str]:
    """直连模式通用解析：查 provider store、校验、规范化 base_url、剥 model 前缀。

    codex（openai_chat，补 /v1）与 claude_code（anthropic_messages，剥 /v1）
    共用；差异由参数注入。model_fields 指明 extra 里哪些字段是模型引用：
    codex 是顶层 default_model（models_container=None）；claude 是
    default_models 字典里的 haiku/sonnet/opus 三个 key。命中
    ``<provider_id>:`` 前缀的值剥掉前缀（直连时必须是 vendor 裸 model id）。
    """
    body = body or {}
    pid = body.get("provider_id")
    if not isinstance(pid, str) or not pid.strip():
        raise installers_mod.InstallerExtraInvalid(
            code="direct_provider_required",
            message=f"{installer_label} 直连模式需要 provider_id：选一个柳如烟『模型管理』里已配置的服务商。",
        )
    pid = pid.strip()

    cfg = load_providers_config_cached()
    provider = cfg["providers"].get(pid)
    if provider is None:
        raise installers_mod.InstallerExtraInvalid(
            code="direct_provider_not_found",
            message=f"provider '{pid}' 不存在；请先在柳如烟『模型管理』里配置该服务商。",
        )
    if not provider.enabled:
        raise installers_mod.InstallerExtraInvalid(
            code="direct_provider_disabled",
            message=f"provider '{pid}' 已禁用；请在『模型管理』里启用后再直连。",
        )
    if provider.transport != required_transport:
        raise installers_mod.InstallerExtraInvalid(
            code="direct_transport_mismatch",
            message=(
                f"provider '{pid}' 的协议是 {provider.transport}；{installer_label} 直连只支持 "
                f"{required_transport} 协议服务商。其他协议上游请用网关模式，"
                "柳如烟会做协议互转。"
            ),
        )
    if not (provider.base_url or "").strip():
        raise installers_mod.InstallerExtraInvalid(
            code="direct_base_url_missing",
            message=f"provider '{pid}' 没有配置 base_url；请先在『模型管理』里补上。",
        )

    # 直连时 model 必须是 vendor 裸 id：前端可能传 namespaced id，剥掉前缀
    if models_container is None:
        # codex：extra["default_model"] 是字符串
        dm = extra.get("default_model")
        if isinstance(dm, str) and dm.startswith(pid + ":"):
            extra["default_model"] = dm[len(pid) + 1:]
    else:
        # claude_code：extra["default_models"] 是 {role: "<provider:model>"}
        dm = extra.get(models_container)
        if isinstance(dm, dict):
            for field in model_fields:
                val = dm.get(field)
                if isinstance(val, str) and val.startswith(pid + ":"):
                    dm[field] = val[len(pid) + 1:]

    extra["mode"] = "direct"
    extra["provider_name"] = provider.name or pid
    if provider.extra_headers:
        extra["extra_headers"] = dict(provider.extra_headers)

    # api_key 允许为空（部分本地/免费端点不鉴权）；auth 字段会写入空 key。
    #
    # base_url 规范化：与网关 adapter 走同一套逻辑——
    # - openai_chat：format_api_host 补 /v1（codex 在 base_url 后直接拼
    #   /chat/completions 或 /responses，不补 /v1 会请求到错误路径 503/404）
    # - anthropic_messages：normalize_anthropic_base_url 剩末尾 /v1
    #   （Claude Code / Anthropic SDK 自己拼 /v1/messages，不剥会 /v1/v1/messages 404）
    # `#` 结尾（按字面 URL escape hatch）两种规范化都尊重。
    base = normalize(provider.base_url) or ""
    return provider.api_key or "", base


@router.post("/admin/installers/{installer_id}/preview")
async def admin_preview_installer(
    req: Request,
    installer_id: str,
    body: Dict[str, Any] = Body(default={}),
) -> Dict[str, Any]:
    """dry-run：返将要写入的差异、是否会触发外部修改警告。

    body 可选 `default_models`：见 admin_install。body.mode="direct" + provider_id
    时走 codex 直连模式（写 provider 真实 base_url/api_key，不经本地 Gateway）。"""
    _require_loopback(req)
    openai_base, anthropic_base = _derive_base_urls(req)
    token = auth.ensure_token()
    try:
        extra = _build_installer_extra(installer_id, body)
        if installer_id == "codex" and extra.get("mode") == "direct":
            token, openai_base = _resolve_codex_direct(body, extra)
        elif installer_id == "claude_code" and extra.get("mode") == "direct":
            token, anthropic_base = _resolve_claude_direct(body, extra)
        rep = installers_mod.preview_install(
            installer_id,
            token=token,
            openai_base_url=openai_base,
            anthropic_base_url=anthropic_base,
            extra=extra,
        )
    except KeyError:
        return JSONResponse(status_code=404, content={"error": {"code": "installer_not_found", "message": installer_id}})
    except installers_mod.InstallerExtraInvalid as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
    return {
        "id": rep.id,
        "action": rep.action,
        "target_path": rep.target_path,
        "target_exists": rep.target_exists,
        "will_create": rep.will_create,
        "will_modify": rep.will_modify,
        "will_backup_to": rep.will_backup_to,
        "external_modified": rep.external_modified,
        "diff_summary": rep.diff_summary,
    }


@router.post("/admin/installers/{installer_id}/install")
async def admin_install(
    req: Request,
    installer_id: str,
    body: Dict[str, Any] = Body(default={}),
) -> Dict[str, Any]:
    """真写。body.force=true 强制覆盖外部改动过的文件。

    claude_code 支持额外 body.default_models = {"haiku":"<provider:model>", "sonnet":..., "opus":...}
    将三个角色 model 写入 settings.json 的 ANTHROPIC_DEFAULT_*_MODEL env。任一为空跳过该字段。

    codex 支持额外 body.mode="direct" + body.provider_id="<柳如烟里的 provider id>"：
    直连该服务商（base_url/api_key 直接写进 ~/.codex/，不经本地 Gateway 转发）。
    wire_api 固定 responses（codex 0.153+ 移除了 chat wire，传 chat 会被拒载）。
    """
    _require_loopback(req)
    force = bool((body or {}).get("force"))
    openai_base, anthropic_base = _derive_base_urls(req)
    token = auth.ensure_token()
    try:
        extra = _build_installer_extra(installer_id, body)
        if installer_id == "codex" and extra.get("mode") == "direct":
            token, openai_base = _resolve_codex_direct(body, extra)
        elif installer_id == "claude_code" and extra.get("mode") == "direct":
            token, anthropic_base = _resolve_claude_direct(body, extra)
        result = installers_mod.install(
            installer_id,
            token=token,
            openai_base_url=openai_base,
            anthropic_base_url=anthropic_base,
            force=force,
            extra=extra,
        )
    except KeyError:
        return JSONResponse(status_code=404, content={"error": {"code": "installer_not_found", "message": installer_id}})
    except installers_mod.InstallerExtraInvalid as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
    if not result.ok:
        return JSONResponse(
            status_code=409 if "外部" in result.message else 500,
            content={
                "ok": False,
                "error": {"code": "install_failed", "message": result.message},
                "target_path": result.target_path,
            },
        )
    return {
        "ok": True,
        "message": result.message,
        "target_path": result.target_path,
        "backup_path": result.backup_path,
    }


@router.post("/admin/installers/{installer_id}/uninstall")
async def admin_uninstall(
    req: Request,
    installer_id: str,
    body: Dict[str, Any] = Body(default={}),
) -> Dict[str, Any]:
    """还原最近备份；外部改过需 force=true。"""
    _require_loopback(req)
    force = bool((body or {}).get("force"))
    try:
        result = installers_mod.uninstall(installer_id, force=force)
    except KeyError:
        return JSONResponse(status_code=404, content={"error": {"code": "installer_not_found", "message": installer_id}})
    if not result.ok:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": {"code": "uninstall_failed", "message": result.message},
                "target_path": result.target_path,
            },
        )
    return {
        "ok": True,
        "message": result.message,
        "target_path": result.target_path,
        "restored_from": result.backup_path,
    }


# ─────────────────────── M6：多 token + 配额管理 ───────────────────────


def _token_runtime_dict(entry: tokens_mod.TokenEntry) -> Dict[str, Any]:
    """令牌 + 实时配额计数。含明文（loopback only）。"""
    out = entry.to_dict()
    out["runtime"] = tokens_mod.runtime_usage(entry.id)
    return out


@router.get("/admin/tokens")
async def admin_list_tokens(req: Request) -> Dict[str, Any]:
    """列出所有令牌（含明文）+ 实时配额计数。loopback only。

    首次访问时若 ``gateway_tokens.json`` 文件不存在，自动 bootstrap 一条「默认令牌」。
    """
    _require_loopback(req)
    tokens_mod.bootstrap_if_empty()
    return {"tokens": [_token_runtime_dict(e) for e in tokens_mod.list_tokens()]}


@router.post("/admin/tokens")
async def admin_create_token(req: Request, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """新建令牌。

    body: {name, allow_models?, deny_models?, allow_ips?,
           quota?: {calls_per_minute, calls_per_day, total_tokens_per_day}}
    allow_ips 接受 IPv4/IPv6 单 IP 或 CIDR。任一不合法返 400 invalid_ip_entry。
    """
    _require_loopback(req)
    name = str((body or {}).get("name") or "").strip()
    if not name:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_name", "message": "name required"}},
        )
    try:
        entry = tokens_mod.create_token(
            name=name,
            allow_models=list((body or {}).get("allow_models") or []),
            deny_models=list((body or {}).get("deny_models") or []),
            allow_ips=list((body or {}).get("allow_ips") or []),
            quota=(body or {}).get("quota"),
        )
    except tokens_mod.InvalidIpEntry as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_ip_entry", "bad": exc.bad},
        ) from exc
    return _token_runtime_dict(entry)


@router.put("/admin/tokens/{token_id}")
async def admin_update_token(
    req: Request, token_id: str, body: Dict[str, Any] = Body(...),
) -> Dict[str, Any]:
    """改 name / disabled / allow_models / deny_models / allow_ips / quota。

    任一字段省略表示不动；传空 list 表示清空。allow_ips 中任一不合法返
    400 invalid_ip_entry，本次更新整体不生效。"""
    _require_loopback(req)
    body = body or {}
    try:
        entry = tokens_mod.update_token(
            token_id,
            name=body.get("name"),
            disabled=body.get("disabled"),
            allow_models=body.get("allow_models"),
            deny_models=body.get("deny_models"),
            allow_ips=body.get("allow_ips"),
            quota=body.get("quota"),
        )
    except tokens_mod.InvalidIpEntry as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_ip_entry", "bad": exc.bad},
        ) from exc
    if entry is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "token_not_found", "message": token_id}},
        )
    return _token_runtime_dict(entry)


@router.delete("/admin/tokens/{token_id}")
async def admin_delete_token(req: Request, token_id: str) -> Dict[str, Any]:
    """删除 token。该 token 立即失效；配额计数器同步清掉。"""
    _require_loopback(req)
    if not tokens_mod.delete_token(token_id):
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "token_not_found", "message": token_id}},
        )
    return {"ok": True, "deleted": token_id}


@router.post("/admin/tokens/{token_id}/rotate")
async def admin_rotate_token(req: Request, token_id: str) -> Dict[str, Any]:
    """重置 token 明文；旧明文立即失效。返回新一次性明文。"""
    _require_loopback(req)
    entry = tokens_mod.rotate_token(token_id)
    if entry is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "token_not_found", "message": token_id}},
        )
    return _token_runtime_dict(entry)


@router.get("/admin/tokens/{token_id}/usage")
async def admin_token_usage(req: Request, token_id: str) -> Dict[str, Any]:
    """实时配额计数（内存滑动窗口，重启清零）。"""
    _require_loopback(req)
    e = tokens_mod.get_token(token_id)
    if e is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "token_not_found", "message": token_id}},
        )
    return {
        "id": e.id,
        "quota": e.quota.to_dict(),
        "runtime": tokens_mod.runtime_usage(token_id),
    }


# ─────────────────────── 模块加载即 bootstrap ───────────────────────
# 行为对齐行业通用标准：应用首次启动就生成一把可用令牌，UI 进来直接看到。
# 文件已存在则跳过——不会覆盖现有内容。
try:
    tokens_mod.bootstrap_if_empty()
except Exception as _exc:  # noqa: BLE001
    logger.warning("[Gateway/tokens] bootstrap 失败（忽略，访问时还会重试）：%s", _exc)


__all__ = ["router", "GATEWAY_VERSION"]
