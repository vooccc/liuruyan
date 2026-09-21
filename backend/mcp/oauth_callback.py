"""OAuth 临时 callback 接收器。

★ 借鉴 opencode oauth-callback.ts：本地 127.0.0.1 随机端口 aiohttp server，
  接住浏览器重定向 `/callback?code=...&state=...`，把 code + state 喂回主流程。

设计：
  - 端口 127.0.0.1:0（OS 分配）—— 避开冲突 + 绝不绑外网
  - 只接 1 次 callback，拿到就自己关
  - 5 分钟超时，避免卡死 / 遗留 server
  - 浏览器里显示友好成功 / 失败页
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator

from aiohttp import web

logger = logging.getLogger(__name__)

DEFAULT_CALLBACK_TIMEOUT = 300.0  # 5 min


class OAuthCallbackServer:
    """一次性本地回调 server。

    用法：
        async with OAuthCallbackServer() as cb:
            redirect_uri = cb.redirect_uri  # 给 OAuth provider
            code, state = await cb.wait(timeout=300)
    """

    def __init__(self, host: str = "127.0.0.1", path: str = "/callback"):
        self._host = host
        self._path = path
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int | None = None
        self._future: asyncio.Future[tuple[str, str | None]] | None = None
        # 绑定一个短随机 id 到 URL，避免多次 OAuth 流混淆
        self._token = secrets.token_urlsafe(8)

    @property
    def redirect_uri(self) -> str:
        if self._port is None:
            raise RuntimeError("callback server not started")
        return f"http://{self._host}:{self._port}{self._path}?lrk={self._token}"

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()

        app = web.Application()
        app.router.add_get(self._path, self._handle)
        self._app = app

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, 0)
        await site.start()

        # 拿分配到的端口
        servers = runner.sites[0]._server.sockets  # type: ignore[attr-defined]
        self._port = servers[0].getsockname()[1]
        self._runner = runner
        self._site = site
        logger.info("[MCP-OAUTH-CB] listening on %s", self.redirect_uri)

    async def stop(self) -> None:
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:  # noqa: BLE001
                logger.exception("[MCP-OAUTH-CB] cleanup error")
        if self._future is not None and not self._future.done():
            self._future.cancel()

    async def wait(self, timeout: float = DEFAULT_CALLBACK_TIMEOUT) -> tuple[str, str | None]:
        if self._future is None:
            raise RuntimeError("callback server not started")
        try:
            return await asyncio.wait_for(asyncio.shield(self._future), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise RuntimeError(f"OAuth callback timeout after {timeout}s") from e

    async def _handle(self, request: web.Request) -> web.Response:
        # 轻防误触：只接带正确 lrk 的请求
        if request.query.get("lrk") != self._token:
            return web.Response(text="forbidden", status=403)

        if self._future is not None and self._future.done():
            return web.Response(text="already received", status=409)

        code = request.query.get("code")
        state = request.query.get("state")
        error = request.query.get("error")

        if error:
            msg = f"OAuth error: {error}"
            if self._future is not None:
                self._future.set_exception(RuntimeError(msg))
            return web.Response(text=_page_error(msg), content_type="text/html", status=400)

        if not code:
            msg = "OAuth callback missing 'code' parameter"
            if self._future is not None:
                self._future.set_exception(RuntimeError(msg))
            return web.Response(text=_page_error(msg), content_type="text/html", status=400)

        if self._future is not None:
            self._future.set_result((code, state))
        return web.Response(text=_page_success(), content_type="text/html")


@asynccontextmanager
async def oauth_callback_server() -> AsyncIterator[OAuthCallbackServer]:
    cb = OAuthCallbackServer()
    await cb.start()
    try:
        yield cb
    finally:
        await cb.stop()


# ── 浏览器页 ──────────────────────────────────────────────────────────────
def _page_success() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>授权完成</title>
<style>
  body { font-family: -apple-system, Segoe UI, sans-serif; background: #0d0f13; color: #e5e7eb;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .card { background: #161a22; border: 1px solid #262b36; border-radius: 14px; padding: 40px 48px;
          text-align: center; max-width: 420px; box-shadow: 0 18px 40px rgba(0,0,0,0.35); }
  h1 { margin: 0 0 8px; font-size: 22px; color: #ffffff; }
  p { color: #9ca3af; font-size: 14px; line-height: 1.6; }
  .check { width: 54px; height: 54px; margin: 0 auto 16px; border-radius: 50%;
           background: rgba(46, 204, 113, 0.12); color: #2ecc71;
           display: flex; align-items: center; justify-content: center; font-size: 28px; }
</style></head>
<body><div class="card">
<div class="check">✓</div>
<h1>授权完成</h1>
<p>可以关闭此窗口，返回柳如烟继续使用。</p>
</div></body></html>"""


def _page_error(msg: str) -> str:
    safe = msg.replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>授权失败</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; background: #0d0f13; color: #e5e7eb;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #161a22; border: 1px solid #4a2330; border-radius: 14px; padding: 40px 48px;
          text-align: center; max-width: 480px; box-shadow: 0 18px 40px rgba(0,0,0,0.35); }}
  h1 {{ margin: 0 0 8px; color: #ff6b6b; font-size: 22px; }}
  pre {{ color: #9ca3af; font-size: 12.5px; background: #0f1116; padding: 10px 14px; border-radius: 8px;
         text-align: left; max-width: 100%; overflow-wrap: anywhere; white-space: pre-wrap; }}
</style></head>
<body><div class="card">
<h1>授权失败</h1>
<pre>{safe}</pre>
</div></body></html>"""


__all__ = ["OAuthCallbackServer", "oauth_callback_server"]
