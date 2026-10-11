"""算力账号 REST API 路由层。
"""
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .manager import ComputeAccountsManager
from .models_provider import get_pro_providers
from .services.google_antigravity import ANTIGRAVITY_43_MODELS

logger = logging.getLogger(__name__)

router = APIRouter()

import asyncio
import urllib.parse

# 暂存 PKCE 验证态字典 (state -> {code_verifier, redirect_uri, provider_id, created_at})
_PENDING_AUTH_STATES: Dict[str, Dict[str, Any]] = {}


class OAuthLoopbackServer:
    """本地 8085 端口临时 HTTP 监听器，捕获 Google 官方回调并自动入库"""

    def __init__(self, port: int = 8085):
        self.port = port
        self.server: Optional[asyncio.Server] = None
        self._lock = asyncio.Lock()

    async def start(self):
        async with self._lock:
            if self.server:
                return
            try:
                self.server = await asyncio.start_server(self._handle_client, "127.0.0.1", self.port)
                logger.info("[ComputeAccounts] 已启动 127.0.0.1:%s 本地回调服务", self.port)
            except OSError as e:
                logger.warning("[ComputeAccounts] 端口 %s 无法监听: %s (仍支持在对话框中手动粘贴链接)", self.port, e)
                self.server = None

    async def stop(self):
        async with self._lock:
            if self.server:
                self.server.close()
                await self.server.wait_closed()
                self.server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = await reader.readline()
            if not line:
                return
            req_line = line.decode("utf-8", "ignore")
            parts = req_line.split()
            if len(parts) >= 2 and parts[0] == "GET":
                parsed = urllib.parse.urlparse(parts[1])
                if parsed.path.endswith("/callback"):
                    qs = urllib.parse.parse_qs(parsed.query)
                    code = qs.get("code", [None])[0]
                    state = qs.get("state", [None])[0]
                    error = qs.get("error", [None])[0]

                    if error:
                        html = f"""<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#18181b;color:#f87171;">
                        <h2>授权取消或失败</h2><p>{error}</p><p>您可以关闭此窗口返回柳如烟重试。</p></body></html>"""
                    elif code:
                        auth_info = _PENDING_AUTH_STATES.pop(state, {}) if state else {}
                        verifier = auth_info.get("code_verifier")
                        provider_id = auth_info.get("provider_id", "google")
                        if not verifier:
                            for s, info in reversed(list(_PENDING_AUTH_STATES.items())):
                                if info.get("provider_id") == provider_id:
                                    verifier = info.get("code_verifier")
                                    _PENDING_AUTH_STATES.pop(s, None)
                                    break

                        mgr = ComputeAccountsManager.get_instance()
                        adapter = mgr.get_adapter(provider_id)
                        if adapter:
                            try:
                                token_data = await adapter.exchange_token(
                                    code=code,
                                    redirect_uri=f"http://localhost:{self.port}/callback",
                                    code_verifier=verifier,
                                )
                                access_token = token_data.get("access_token")
                                user_info = await adapter.fetch_user_info(access_token)
                                saved = mgr.add_or_update_account(
                                    provider_id=provider_id,
                                    account_info={
                                        "id": user_info.get("id"),
                                        "email": user_info.get("email"),
                                        "name": user_info.get("name"),
                                        "picture": user_info.get("picture"),
                                        "project_id": user_info.get("project_id"),
                                        "access_token": access_token,
                                        "refresh_token": token_data.get("refresh_token"),
                                        "expires_at": token_data.get("expires_at"),
                                    },
                                )
                                email = saved.get("email", "Google User")
                                html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>授权成功</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#09090b;color:#f4f4f5;text-align:center}}
.card{{background:#18181b;border:1px solid #27272a;border-radius:16px;padding:36px 28px;max-width:440px;box-shadow:0 8px 30px rgba(0,0,0,0.5)}}
.icon{{width:56px;height:56px;line-height:56px;border-radius:50%;background:#10b981;color:#fff;font-size:28px;margin:0 auto 16px}}
h2{{margin:0 0 10px;font-size:20px;color:#fff}}
p{{margin:0 0 16px;color:#a1a1aa;font-size:14px}}
.sub{{color:#71717a;font-size:13px;margin:0}}
</style></head><body>
<div class="card">
<div class="icon">✓</div>
<h2>Google 账号授权绑定成功！</h2>
<p>已成功接入 Google 账号: <strong style="color:#60a5fa;">{email}</strong></p>
<p class="sub">已激活 43 款 Antigravity 模型。您可以直接关闭此标签页，返回柳如烟继续使用。</p>
</div>
</body></html>"""
                            except Exception as ex:
                                logger.error("[ComputeAccounts] 8085 本地兑换失败: %s", ex, exc_info=True)
                                html = f"""<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding:50px;background:#18181b;color:#f87171;">
                                <h2>Token 兑换异常</h2><p>{str(ex)}</p></body></html>"""
                        else:
                            html = "<h3>未知渠道适配器</h3>"
                    else:
                        html = "<h3>缺少授权参数 code</h3>"

                    res = (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: text/html; charset=utf-8\r\n"
                        f"Content-Length: {len(html.encode('utf-8'))}\r\n"
                        "Connection: close\r\n\r\n"
                        + html
                    )
                    writer.write(res.encode("utf-8"))
                    await writer.drain()
        except Exception as e:
            logger.error("[ComputeAccounts] 处理本地回调失败: %s", e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


_GLOBAL_OAUTH_SERVER: Optional[OAuthLoopbackServer] = None


def get_oauth_server() -> OAuthLoopbackServer:
    global _GLOBAL_OAUTH_SERVER
    if _GLOBAL_OAUTH_SERVER is None:
        _GLOBAL_OAUTH_SERVER = OAuthLoopbackServer(port=8085)
    return _GLOBAL_OAUTH_SERVER


class ManualExchangeRequest(BaseModel):
    code: Optional[str] = None
    redirect_url: Optional[str] = None
    state: Optional[str] = None


@router.get("/overview")
async def get_overview():
    """获取所有算力账号渠道卡片（匹配 Cursor 助手概览布局）"""
    mgr = ComputeAccountsManager.get_instance()
    return {"ok": True, "channels": mgr.get_overview()}


@router.post("/{provider_id}/auth-url")
async def generate_auth_url(provider_id: str, request: Request):
    """生成 OAuth2 授权链接，启动登录流程"""
    mgr = ComputeAccountsManager.get_instance()
    adapter = mgr.get_adapter(provider_id)
    if not adapter:
        raise HTTPException(status_code=400, detail=f"不支持的算力渠道: {provider_id}")

    if provider_id == "google":
        # Google 官方 Antigravity client_id 白名单回调固定为 localhost:8085/callback
        redirect_uri = "http://localhost:8085/callback"
        # 启动本地 8085 端口监听服务
        try:
            await get_oauth_server().start()
        except Exception as e:
            logger.warning("[ComputeAccounts] 启动 8085 监听失败: %s", e)
    else:
        port = request.url.port or os.environ.get("LIURUYAN_ACTUAL_PORT", "8001")
        redirect_uri = f"http://127.0.0.1:{port}/api/pro/compute-accounts/{provider_id}/callback"

    params = adapter.generate_auth_params(redirect_uri)
    state = params["state"]

    _PENDING_AUTH_STATES[state] = {
        "provider_id": provider_id,
        "code_verifier": params.get("code_verifier"),
        "redirect_uri": redirect_uri,
        "created_at": time.time(),
    }

    # 清理 10 分钟以前的过期 state
    now = time.time()
    for s in list(_PENDING_AUTH_STATES.keys()):
        if now - _PENDING_AUTH_STATES[s]["created_at"] > 600:
            _PENDING_AUTH_STATES.pop(s, None)

    return {
        "ok": True,
        "auth_url": params["auth_url"],
        "state": state,
        "redirect_uri": redirect_uri,
    }


@router.get("/{provider_id}/callback", response_class=HTMLResponse)
async def oauth_callback(
    provider_id: str,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """OAuth 浏览器授权重定向端点"""
    if error:
        logger.warning("[ComputeAccounts] OAuth 回调错误: %s", error)
        return HTMLResponse(
            content=f"<html><body style='font-family:sans-serif;text-align:center;padding:50px;background:#18181b;color:#f87171;'>"
            f"<h2>授权失败或已取消</h2><p>{error}</p><p>您可以关闭此窗口返回柳如烟重试。</p></body></html>",
            status_code=400,
        )

    if not code:
        return HTMLResponse(content="<h3>缺少授权码 code</h3>", status_code=400)

    auth_info = _PENDING_AUTH_STATES.pop(state, {}) if state else {}
    verifier = auth_info.get("code_verifier")
    if not verifier:
        for s, info in reversed(list(_PENDING_AUTH_STATES.items())):
            if info.get("provider_id") == provider_id:
                verifier = info.get("code_verifier")
                _PENDING_AUTH_STATES.pop(s, None)
                break

    if provider_id == "google":
        redirect_uri = "http://localhost:8085/callback"
    else:
        port = os.environ.get("LIURUYAN_ACTUAL_PORT", "8001")
        redirect_uri = auth_info.get("redirect_uri") or f"http://127.0.0.1:{port}/api/pro/compute-accounts/{provider_id}/callback"

    mgr = ComputeAccountsManager.get_instance()
    adapter = mgr.get_adapter(provider_id)
    if not adapter:
        return HTMLResponse(content=f"<h3>未知提供商: {provider_id}</h3>", status_code=400)

    try:
        token_data = await adapter.exchange_token(code=code, redirect_uri=redirect_uri, code_verifier=verifier)
        access_token = token_data.get("access_token")
        user_info = await adapter.fetch_user_info(access_token)

        saved = mgr.add_or_update_account(
            provider_id=provider_id,
            account_info={
                "id": user_info.get("id"),
                "email": user_info.get("email"),
                "name": user_info.get("name"),
                "picture": user_info.get("picture"),
                "project_id": user_info.get("project_id"),
                "access_token": access_token,
                "refresh_token": token_data.get("refresh_token"),
                "expires_at": token_data.get("expires_at"),
            },
        )

        return HTMLResponse(
            content=f"""
            <html>
            <head><meta charset="utf-8"><title>授权成功</title></head>
            <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,sans-serif;text-align:center;padding:60px 20px;background:#09090b;color:#f4f4f5;">
                <div style="max-width:440px;margin:0 auto;background:#18181b;border:1px solid #27272a;border-radius:16px;padding:32px 24px;box-shadow:0 8px 30px rgba(0,0,0,0.5);">
                    <div style="width:52px;height:52px;line-height:52px;border-radius:50%;background:#10b981;color:#fff;font-size:26px;margin:0 auto 16px;">✓</div>
                    <h2 style="margin:0 0 10px;font-size:20px;color:#fff;">账号授权绑定成功！</h2>
                    <p style="margin:0 0 20px;color:#a1a1aa;font-size:14px;">已连接 Google 账号: <strong>{saved.get('email')}</strong></p>
                    <p style="margin:0;color:#71717a;font-size:13px;">已激活 43 款高性能模型。您可以直接关闭此标签页，返回柳如烟继续使用。</p>
                </div>
            </body>
            </html>
            """
        )
    except Exception as e:
        logger.error("[ComputeAccounts] 换取 Token 异常: %s", e, exc_info=True)
        return HTMLResponse(
            content=f"<html><body style='font-family:sans-serif;text-align:center;padding:50px;background:#18181b;color:#f87171;'>"
            f"<h2>Token 兑换失败</h2><p>{str(e)}</p></body></html>",
            status_code=500,
        )


@router.post("/{provider_id}/manual-exchange")
async def manual_exchange(provider_id: str, body: ManualExchangeRequest):
    """支持手动粘贴完整重定向 URL 进行 Token 兑换"""
    code = body.code
    state = body.state

    if body.redirect_url:
        parsed = urllib.parse.urlparse(body.redirect_url)
        queries = urllib.parse.parse_qs(parsed.query)
        if "code" in queries:
            code = queries["code"][0]
        if "state" in queries:
            state = queries["state"][0]

    if not code:
        raise HTTPException(status_code=400, detail="未解析到授权码 code，请粘贴包含 code=... 的完整链接或授权码")

    auth_info = _PENDING_AUTH_STATES.pop(state, {}) if state else {}
    verifier = auth_info.get("code_verifier")
    if not verifier:
        for s, info in reversed(list(_PENDING_AUTH_STATES.items())):
            if info.get("provider_id") == provider_id:
                verifier = info.get("code_verifier")
                _PENDING_AUTH_STATES.pop(s, None)
                break

    if provider_id == "google":
        redirect_uri = "http://localhost:8085/callback"
    else:
        port = os.environ.get("LIURUYAN_ACTUAL_PORT", "8001")
        redirect_uri = auth_info.get("redirect_uri") or f"http://127.0.0.1:{port}/api/pro/compute-accounts/{provider_id}/callback"

    mgr = ComputeAccountsManager.get_instance()
    adapter = mgr.get_adapter(provider_id)
    if not adapter:
        raise HTTPException(status_code=400, detail=f"未知渠道: {provider_id}")

    try:
        token_data = await adapter.exchange_token(code=code, redirect_uri=redirect_uri, code_verifier=verifier)
        access_token = token_data.get("access_token")
        user_info = await adapter.fetch_user_info(access_token)

        saved = mgr.add_or_update_account(
            provider_id=provider_id,
            account_info={
                "id": user_info.get("id"),
                "email": user_info.get("email"),
                "name": user_info.get("name"),
                "picture": user_info.get("picture"),
                "project_id": user_info.get("project_id"),
                "access_token": access_token,
                "refresh_token": token_data.get("refresh_token"),
                "expires_at": token_data.get("expires_at"),
            },
        )
        return {"ok": True, "account": saved}
    except Exception as e:
        logger.error("[ComputeAccounts] 手动换取 Token 失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{provider_id}/accounts")
async def list_accounts(provider_id: str):
    """获取指定提供商下的账号列表"""
    mgr = ComputeAccountsManager.get_instance()
    return {"ok": True, "accounts": mgr.list_accounts(provider_id)}


@router.delete("/{provider_id}/accounts/{account_id}")
async def delete_account(provider_id: str, account_id: str):
    """删除指定提供商下的账号"""
    mgr = ComputeAccountsManager.get_instance()
    deleted = mgr.delete_account(provider_id, account_id)
    return {"ok": deleted}


@router.post("/{provider_id}/accounts/{account_id}/primary")
async def set_primary_account_endpoint(provider_id: str, account_id: str):
    """将指定账号设为主力调度节点"""
    mgr = ComputeAccountsManager.get_instance()
    ok = mgr.set_primary_account(provider_id, account_id)
    return {"ok": ok}


@router.post("/{provider_id}/accounts/{account_id}/toggle")
async def toggle_account_endpoint(provider_id: str, account_id: str, enabled: Optional[bool] = None):
    """切换账号的入池状态（开启/停用调度）"""
    mgr = ComputeAccountsManager.get_instance()
    new_state = mgr.toggle_account_enabled(provider_id, account_id, enabled)
    return {"ok": True, "enabled": new_state}


@router.post("/{provider_id}/test")
async def test_connection(provider_id: str):
    """测试指定提供商连通性与测速"""
    mgr = ComputeAccountsManager.get_instance()
    adapter = mgr.get_adapter(provider_id)
    if not adapter:
        raise HTTPException(status_code=400, detail="未知适配器")

    access_token = await mgr.get_valid_access_token(provider_id)
    if not access_token:
        raise HTTPException(status_code=400, detail="无可用账号，请先添加账号")

    result = await adapter.test_connection(access_token)
    return {"ok": True, **result}


@router.get("/{provider_id}/models")
async def list_models(provider_id: str):
    """列出该渠道支持的模型"""
    if provider_id == "google":
        return {"ok": True, "models": ANTIGRAVITY_43_MODELS}
    return {"ok": True, "models": []}


class SetEnabledModelsRequest(BaseModel):
    enabled_models: Optional[List[str]] = None


@router.get("/{provider_id}/enabled-models")
async def get_enabled_models_endpoint(provider_id: str):
    """获取该提供商配置的显示模型列表（None 表示全量）"""
    mgr = ComputeAccountsManager.get_instance()
    return {"ok": True, "enabled_models": mgr.get_enabled_models(provider_id)}


@router.post("/{provider_id}/enabled-models")
async def set_enabled_models_endpoint(provider_id: str, body: SetEnabledModelsRequest):
    """保存该提供商配置的显示模型列表"""
    mgr = ComputeAccountsManager.get_instance()
    mgr.set_enabled_models(provider_id, body.enabled_models)
    return {"ok": True, "enabled_models": mgr.get_enabled_models(provider_id)}


@router.get("/pro-providers")
async def get_virtual_providers():
    """获取所有激活的 Pro 虚拟 Providers（供模型选择器动态拉取）"""
    return {"ok": True, "providers": get_pro_providers()}


@router.get("/{provider_id}/quota")
async def get_provider_quota(
    provider_id: str,
    account_id: Optional[str] = Query(None),
    force_refresh: bool = Query(False),
):
    """获取指定提供商及账号的配额信息（严格带 60s 缓存与防风控机制）"""
    mgr = ComputeAccountsManager.get_instance()
    result = await mgr.get_account_quota(
        provider_id=provider_id,
        account_id=account_id,
        force_refresh=force_refresh,
    )
    return result


