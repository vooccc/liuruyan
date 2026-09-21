"""Gateway 鉴权 header 提取 + installer/launcher 用的"取一把当前可用令牌"门面。

历史背景：M1 单 token 模式用 ``~/.liuruyan/gateway_token`` 单行明文存储；M6
切到多 token 模式后该文件路径废弃。本模块现在只剩两个职责：

1. ``extract_token_from_request``：从外部客户端 HTTP 请求里提取 token 字符串。
2. ``ensure_token()``：installer / launcher 写外部 CLI 配置文件时需要一把
   可用令牌的明文——首选 ``tokens.first_active_token()``，没找到就触发
   ``bootstrap_if_empty()`` 建一条"默认令牌"。

实际鉴权链统一走 ``tokens.resolve_presented()``——本模块不再持有 verify_token。
"""
from __future__ import annotations

from typing import Optional

from fastapi import Request

from . import tokens as _tokens

TOKEN_PREFIX = _tokens.TOKEN_PREFIX


def ensure_token() -> str:
    """返一把当前可用令牌的明文。

    顺序：首条 enabled 令牌 → bootstrap 默认令牌 → 再取首条。
    供 installer / launcher 写外部 CLI 配置使用。
    """
    e = _tokens.first_active_token()
    if e is not None:
        return e.token
    booted = _tokens.bootstrap_if_empty()
    if booted is not None:
        return booted.token
    # bootstrap_if_empty 因为文件已存在跳过 → 此时 tokens 数组为空（用户清空过）
    # 主动建一条
    created = _tokens.create_token(_tokens.DEFAULT_TOKEN_NAME)
    return created.token


def extract_token_from_request(req: Request) -> Optional[str]:
    """按优先级从 request header 提取 token 字符串；提取不到返 None。"""
    auth = req.headers.get("authorization") or req.headers.get("Authorization")
    if auth:
        parts = auth.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        if auth.strip().startswith(TOKEN_PREFIX):
            return auth.strip()
    for header_name in ("x-api-key", "X-Api-Key", "api-key", "Api-Key"):
        v = req.headers.get(header_name)
        if v and v.strip():
            return v.strip()
    return None


__all__ = [
    "TOKEN_PREFIX",
    "ensure_token",
    "extract_token_from_request",
]
