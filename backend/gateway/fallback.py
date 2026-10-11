"""Fallback 链 + health 集成（M3）。

在 adapter 之上的高阶包装。给定 List[ResolveResult]，依次尝试每个 candidate，
失败时根据状态码 / 异常分类决定是否切换到下一个。

核心规则：
- 第一候选总是 try（即使 health 缓存说不健康——尊重用户的"主选择"）
- 后续候选若处于 health 黑屋则跳过
- 4xx (除 401/403/429) 视为请求自身有问题，**不切**直接返
- 401/403/429/5xx/timeout/network → 切下一个 candidate
- 流式：仅在首帧前可切（已 yield 给 client 的 SSE bytes 不能撤回）

设计：docs/GATEWAY.md §5
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from .adapter import GatewayUpstreamError
from .health import classify_status, get_cache, is_retryable_status
from .router import ResolveResult


@dataclass
class CandidateAttempt:
    """记录单个 candidate 的尝试结果（供 audit 记日志）。"""
    provider_id: str
    vendor_model: str
    result: str  # "ok" / "auth_failed" / "rate_limited" / "server_error" / "timeout" / "network" / "skipped_unhealthy" / "non_retryable"
    status: Optional[int] = None
    error: str = ""


@dataclass
class FallbackOutcome:
    """完整 fallback 过程的审计结果。"""
    attempts: List[CandidateAttempt] = field(default_factory=list)
    final_provider: Optional[str] = None
    final_vendor_model: Optional[str] = None

    def to_audit_dict(self) -> Dict[str, Any]:
        return {
            "attempts": [a.__dict__ for a in self.attempts],
            "final_provider": self.final_provider,
            "final_vendor_model": self.final_vendor_model,
        }


# ─────────────────────────── 非流式 ───────────────────────────


# Forwarder 类型签名：(provider, body, vendor_model, *, incoming_headers, ...) → (status, body_bytes, headers)
NonstreamForwarder = Callable[..., Awaitable[Tuple[int, bytes, Dict[str, str]]]]


async def forward_with_fallback_nonstream(
    candidates: List[ResolveResult],
    forwarder_picker: Callable[[ResolveResult], NonstreamForwarder],
    body: Dict[str, Any],
    *,
    incoming_headers: Dict[str, str],
    timeout_secs: float = 600.0,
) -> Tuple[int, bytes, Dict[str, str], FallbackOutcome]:
    """非流式：完整 try-each。

    - forwarder_picker(candidate) 返回该 candidate 应用的 forwarder（同协议透传 / 跨协议互转）
    - 第一候选总试；后续若处于 health 黑屋直接跳过
    - 4xx other (400/404/422) 直接返（请求自身错，换 provider 没用）
    - 401/403/429/5xx/timeout/network → 切下一个
    - 全失败返 502 + outcome 给调用方写 audit
    """
    health = get_cache()
    outcome = FallbackOutcome()
    last_status: Optional[int] = None
    last_body: Optional[bytes] = None
    last_headers: Optional[Dict[str, str]] = None

    for idx, candidate in enumerate(candidates):
        pid = candidate.provider.id
        vm = candidate.vendor_model

        # 后续候选若黑屋内 → 跳过
        if idx > 0 and not health.should_try(pid):
            outcome.attempts.append(CandidateAttempt(pid, vm, "skipped_unhealthy"))
            continue

        forwarder = forwarder_picker(candidate)
        try:
            status, body_bytes, resp_headers = await forwarder(
                candidate.provider, body, vm,
                incoming_headers=incoming_headers, timeout_secs=timeout_secs,
            )
        except GatewayUpstreamError as exc:
            kind = classify_status(exc.status)
            health.record_failure(pid, kind, error_message=str(exc))
            result = {
                "auth": "auth_failed", "rate_limit": "rate_limited",
                "server_error": "server_error", "timeout": "timeout", "network": "network",
            }.get(kind, "server_error")
            outcome.attempts.append(CandidateAttempt(pid, vm, result, status=exc.status, error=str(exc)))
            if is_retryable_status(exc.status):
                continue
            raise
        except httpx.HTTPError as exc:
            health.record_failure(pid, "network", error_message=str(exc))
            outcome.attempts.append(CandidateAttempt(pid, vm, "network", error=str(exc)))
            continue

        if status < 400:
            health.record_success(pid)
            outcome.attempts.append(CandidateAttempt(pid, vm, "ok", status=status))
            outcome.final_provider = pid
            outcome.final_vendor_model = vm
            return status, body_bytes, resp_headers, outcome

        # 上游返非 2xx → 分类
        kind = classify_status(status)
        # 抽 200 字 upstream body 预览：health 缓存和 audit 都用同一份（让审计日志能直观看到上游真实错误）
        body_preview = body_bytes.decode("utf-8", errors="replace")[:200]
        health.record_failure(pid, kind, error_message=body_preview)
        result = {
            "auth": "auth_failed", "rate_limit": "rate_limited",
            "server_error": "server_error", "timeout": "timeout",
        }.get(kind, "server_error")
        outcome.attempts.append(CandidateAttempt(pid, vm, result, status=status, error=body_preview))

        if is_retryable_status(status):
            last_status, last_body, last_headers = status, body_bytes, resp_headers
            continue
        # 4xx other：直接返
        outcome.final_provider = pid
        outcome.final_vendor_model = vm
        outcome.attempts[-1].result = "non_retryable"
        return status, body_bytes, resp_headers, outcome

    # 所有候选都试过了仍失败
    if last_status is not None and last_body is not None and last_headers is not None:
        # 把最后一个候选的真实错误返给客户端
        return last_status, last_body, last_headers, outcome
    raise GatewayUpstreamError(
        "all candidates exhausted (network/timeout)",
        status=502,
        code="all_candidates_failed",
    )


# ─────────────────────────── 流式 ───────────────────────────


# Stream forwarder 类型签名：返 AsyncIterator[bytes]
StreamForwarder = Callable[..., AsyncIterator[bytes]]


def _looks_like_error_chunk(chunk: bytes) -> Optional[int]:
    """判断 SSE chunk 是否是 forwarder 在 status>=400 时构造的 error 包装。

    返 status code 或 None（None = 不是 error chunk，是真实数据）。
    现有 adapter forwarder 在 status>=400 时 yield 的形态：
      OpenAI:    'data: {"error":{"code":"upstream_error","status":NNN,"message":"..."}}\\n\\ndata: [DONE]\\n\\n'
      Anthropic: 'event: error\\ndata: {"type":"error","error":{...}}\\n\\nevent: message_stop\\ndata: ...'
    """
    if not chunk:
        return None
    text = chunk.decode("utf-8", errors="replace")
    # OpenAI 风格
    if '"error"' in text and "upstream_error" in text:
        # 提 status
        try:
            # 找 'data: {' 后面的 json
            idx = text.find("data: ")
            if idx >= 0:
                data_str = text[idx + 6:].split("\n", 1)[0]
                obj = json.loads(data_str)
                err = obj.get("error", {})
                return int(err.get("status") or 502)
        except (json.JSONDecodeError, ValueError, AttributeError):
            return 502
    # Anthropic 风格
    if "event: error" in text:
        return 502  # Anthropic 错误事件无显式 status，按 502 处理
    # idle timeout / network error 也视作 error
    if "upstream_idle_timeout" in text or "upstream_network_error" in text:
        return 504 if "idle_timeout" in text else 502
    return None


async def forward_with_fallback_stream(
    candidates: List[ResolveResult],
    forwarder_picker: Callable[[ResolveResult], StreamForwarder],
    body: Dict[str, Any],
    *,
    incoming_headers: Dict[str, str],
    idle_timeout_secs: float = 180.0,
    outcome: Optional[FallbackOutcome] = None,
) -> AsyncIterator[bytes]:
    """流式 fallback：仅在首帧前可切。

    - 首帧探测：消费 generator 第一个 chunk
    - 若首帧是 error 包装 / 拿不到首帧 → 切下一个
    - 首帧成功（真实数据）→ 提交给 client，后续 chunk 透传，不再切
    - outcome 由调用方传入用于 audit；这里追加 attempt 记录
    """
    health = get_cache()
    if outcome is None:
        outcome = FallbackOutcome()

    for idx, candidate in enumerate(candidates):
        pid = candidate.provider.id
        vm = candidate.vendor_model

        if idx > 0 and not health.should_try(pid):
            outcome.attempts.append(CandidateAttempt(pid, vm, "skipped_unhealthy"))
            continue

        forwarder = forwarder_picker(candidate)
        gen = forwarder(
            candidate.provider, body, vm,
            incoming_headers=incoming_headers, idle_timeout_secs=idle_timeout_secs,
        )

        first_chunk: Optional[bytes] = None
        try:
            first_chunk = await gen.__anext__()
        except StopAsyncIteration:
            health.record_failure(pid, "network", error_message="upstream returned empty stream")
            outcome.attempts.append(CandidateAttempt(pid, vm, "network", error="empty stream"))
            continue
        except GatewayUpstreamError as exc:
            kind = classify_status(exc.status)
            health.record_failure(pid, kind, error_message=str(exc))
            outcome.attempts.append(CandidateAttempt(
                pid, vm,
                {"auth": "auth_failed", "rate_limit": "rate_limited", "timeout": "timeout"}.get(kind, "server_error"),
                status=exc.status, error=str(exc),
            ))
            if is_retryable_status(exc.status):
                continue
            # 不可重试 → 透传错误
            yield (
                b"data: " + json.dumps({"error": {"code": exc.code, "message": str(exc)}}).encode("utf-8") + b"\n\n"
                + b"data: [DONE]\n\n"
            )
            return
        except (httpx.HTTPError, Exception) as exc:
            health.record_failure(pid, "network", error_message=str(exc))
            outcome.attempts.append(CandidateAttempt(pid, vm, "network", error=str(exc)))
            continue

        # 检查首帧是不是 error 包装
        if first_chunk is not None:
            err_status = _looks_like_error_chunk(first_chunk)
            if err_status is not None:
                kind = classify_status(err_status)
                # 抽 200 字 upstream body 预览：health 缓存和 audit 都用同一份（同非流式路径对称）
                body_preview = first_chunk.decode("utf-8", errors="replace")[:200]
                health.record_failure(pid, kind, error_message=body_preview)
                outcome.attempts.append(CandidateAttempt(
                    pid, vm,
                    {"auth": "auth_failed", "rate_limit": "rate_limited", "timeout": "timeout"}.get(kind, "server_error"),
                    status=err_status,
                    error=body_preview,
                ))
                # 关闭这个 generator（消耗剩余 chunks 让 httpx 释放连接）
                try:
                    async for _ in gen:
                        pass
                except Exception:
                    pass
                try:
                    await gen.aclose()
                except Exception:
                    pass
                if is_retryable_status(err_status):
                    continue
                # 不可重试 → 透传 error chunk
                yield first_chunk
                return

        # 首帧是真实数据 → 提交并继续
        health.record_success(pid)
        outcome.attempts.append(CandidateAttempt(pid, vm, "ok", status=200))
        outcome.final_provider = pid
        outcome.final_vendor_model = vm
        yield first_chunk
        async for chunk in gen:
            yield chunk
        return

    # 所有候选都失败 → yield 最终 error
    err_msg = "all candidates exhausted"
    yield (
        b"data: " + json.dumps({"error": {"code": "all_candidates_failed", "message": err_msg}}).encode("utf-8") + b"\n\n"
        + b"data: [DONE]\n\n"
    )


__all__ = [
    "CandidateAttempt",
    "FallbackOutcome",
    "forward_with_fallback_nonstream",
    "forward_with_fallback_stream",
]
