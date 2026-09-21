"""Gateway 调用审计 JSONL 写入。

文件：`~/.liuruyan/gateway_audit.jsonl`
每条 request 一行。线程安全简化处理：用同步 open + 写 + close（追加模式）。
打开 fd 不长留——并发量可预期不会高（本地网关），原子追加用 OS 文件 lock 兜底。

设计抉择：
- 不缓冲：写盘成本对本地网关吞吐量微不足道；崩溃时能保住更多记录
- 不写 prompt 内容（默认）：防泄漏，且 prompt 体积让 jsonl 失控
- token_id 仅前 12 位：日志被翻出来也不能反推完整 token
- 失败不影响主流程：写日志炸了仅 print，不 raise
"""
from __future__ import annotations

import datetime
import json
import logging
import threading
from typing import Any, Dict, Optional

from .. import paths

logger = logging.getLogger(__name__)


_lock = threading.Lock()


def write_event(
    *,
    endpoint: str,
    token_id: str,
    client_ip: str,
    status: int,
    model_requested: Optional[str] = None,
    provider: Optional[str] = None,
    vendor_model: Optional[str] = None,
    stream: bool = False,
    upstream_latency_ms: Optional[int] = None,
    first_token_ms: Optional[int] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    error: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """写一条审计事件。失败时仅 print，不 raise。"""
    payload: Dict[str, Any] = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "endpoint": endpoint,
        "token_id": token_id,
        "client_ip": client_ip,
        "status": status,
        "model_requested": model_requested,
        "provider": provider,
        "vendor_model": vendor_model,
        "stream": stream,
        "upstream_latency_ms": upstream_latency_ms,
        "first_token_ms": first_token_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "error": error,
    }
    if extra:
        payload.update(extra)

    p = paths.gateway_audit_path()
    try:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with _lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Gateway] audit write failed (non-fatal): %s", exc)


__all__ = ["write_event"]
