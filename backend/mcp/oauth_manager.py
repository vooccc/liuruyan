"""OAuth 2.1 管理器。

复用 mcp 官方 SDK 的 `OAuthClientProvider`（它已经处理 PKCE / discovery / token refresh /
dynamic client registration 等细节），我们只负责：
  1. 提供 TokenStorage（FileTokenStorage）
  2. 提供 redirect_handler（webbrowser.open + SSE 通知前端）
  3. 提供 callback_handler（OAuthCallbackServer 抓 code/state）
  4. per-server 单例（避免并发重复起 OAuth 流）

集成点：
  - stateful_client `_enter_streamable_http` 在 `config.oauth is not None` 时，
    构造 OAuthClientProvider 作为 `streamablehttp_client(auth=...)` 参数。
  - SDK 自身在收到 401 时调 provider 自动跑 OAuth 流并重试。
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from typing import TYPE_CHECKING

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata

from .oauth_callback import oauth_callback_server
from .oauth_storage import FileTokenStorage
from .types import MCPServerConfig

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


_callback_tasks: dict[str, asyncio.Task] = {}
_callback_lock = asyncio.Lock()


def _build_client_metadata(server_id: str, config: MCPServerConfig, redirect_uri: str) -> OAuthClientMetadata:
    scopes = (config.oauth.scopes if config.oauth else []) or []
    return OAuthClientMetadata(
        redirect_uris=[redirect_uri],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=" ".join(scopes) if scopes else None,
        client_name=f"liuruyan:{server_id}",
        client_uri="https://github.com/vooccc/liuruyan",
    )


def build_oauth_provider(server_id: str, config: MCPServerConfig) -> OAuthClientProvider | None:
    """为一个 server 构造 OAuth provider。config.oauth 未开启时返回 None。

    注意：返回的 provider 内部持有 callback 流控制；一个 server 同一时刻只应存在
    一个 provider 实例，避免并发 callback 干扰（由 stateful_client 保证：每次
    lifecycle 重启时才新建）。
    """
    if config.oauth is None or not config.oauth.enabled:
        return None
    if not config.url:
        logger.warning("[MCP-OAUTH] %s: oauth enabled but url missing, skip", server_id)
        return None

    storage = FileTokenStorage(server_id)

    # callback_handler 每次被调用时起一个新的 OAuthCallbackServer（一次性）。
    # redirect_handler 拿到 auth URL 后 webbrowser.open；callback_handler 再 await 拿 code/state。
    # 两个 handler 共享一个 server 实例——用闭包 + 临时变量串起来。
    current: dict = {"cb": None}

    async def redirect_handler(authorize_url: str) -> None:
        # 先起 callback server，让 provider 重定向 URI 能正确匹配。注意：redirect_uri
        # 必须跟 client_metadata 注册时一致——SDK 内部会把 metadata.redirect_uris[0] 作为
        # 发给 authorize endpoint 的 redirect_uri。所以我们在 metadata 里提前把随机端口的
        # URL 填进去——但端口在启动前不知道，因此要在 build_oauth_provider 调用链里：
        #   先起 server → 拿 redirect_uri → 再构造 metadata → 再 build provider。
        # 这里 redirect_handler 只负责把浏览器打开到 authorize URL。
        logger.info("[MCP-OAUTH] %s redirecting user to authorize: %s", server_id, authorize_url)
        try:
            from .sse_bridge import broadcast_oauth_needed

            broadcast_oauth_needed(server_id, authorize_url)
        except Exception:  # noqa: BLE001
            pass
        try:
            webbrowser.open(authorize_url, new=1, autoraise=True)
        except Exception:  # noqa: BLE001
            logger.exception("[MCP-OAUTH] webbrowser.open failed")

    async def callback_handler() -> tuple[str, str | None]:
        cb = current.get("cb")
        if cb is None:
            raise RuntimeError("OAuth callback server not initialized")
        return await cb.wait()

    # 由于 SDK 要求 metadata 里的 redirect_uris 必须跟 authorize 请求一致，
    # 而端口只有 server 起来才知道，我们在这里就地起一个 server 贯穿整个 OAuth 流。
    # 它的生命周期由 `run_oauth_flow` 管理。
    async def _prepare_callback_server():
        if current.get("cb") is not None:
            return current["cb"]
        async with _callback_lock:
            if current.get("cb") is not None:
                return current["cb"]
            ctx = oauth_callback_server()
            cb = await ctx.__aenter__()
            current["cb"] = cb
            current["ctx"] = ctx
            return cb

    async def _cleanup_callback_server():
        ctx = current.pop("ctx", None)
        current["cb"] = None
        if ctx is not None:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-OAUTH] callback server cleanup failed")

    # 先同步起 callback server 拿 redirect_uri。这是 blocking coroutine，但 provider
    # 构造不在 event loop 外——调用方（stateful_client._enter_streamable_http）已经
    # 在 loop 内。
    # 返回 provider + 资源管理钩子。
    provider_holder: dict = {"provider": None, "prepare": _prepare_callback_server, "cleanup": _cleanup_callback_server}

    # 延迟构造：真正在 _enter_streamable_http 里拿到 event loop 后再 build。
    async def build():
        cb = await _prepare_callback_server()
        redirect_uri = cb.redirect_uri
        metadata = _build_client_metadata(server_id, config, redirect_uri)
        provider = OAuthClientProvider(
            server_url=config.url or "",
            client_metadata=metadata,
            storage=storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=300.0,
        )
        provider_holder["provider"] = provider
        return provider

    provider_holder["build"] = build
    return provider_holder  # type: ignore[return-value]


async def resolve_oauth_provider(
    server_id: str, config: MCPServerConfig
) -> tuple["OAuthClientProvider | None", "callable | None"]:
    """给 stateful_client 用的简化接口：构造 provider + 返回清理钩子。

    用法：
        provider, cleanup = await resolve_oauth_provider(sid, cfg)
        try:
            streams = await streamablehttp_client(url, headers, auth=provider)
            ...
        finally:
            if cleanup:
                await cleanup()
    """
    holder = build_oauth_provider(server_id, config)
    if holder is None:
        return None, None
    provider = await holder["build"]()  # type: ignore[index]
    return provider, holder["cleanup"]  # type: ignore[index]


def clear_tokens(server_id: str) -> None:
    """强制清除一个 server 的 OAuth token（触发下次启动时重新走完整流程）。"""
    FileTokenStorage(server_id).clear()


__all__ = [
    "build_oauth_provider",
    "resolve_oauth_provider",
    "clear_tokens",
]
