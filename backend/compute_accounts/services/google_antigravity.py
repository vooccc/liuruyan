"""Google Antigravity 算力账号适配器实现。

通过 Google OAuth2 PKCE 授权接入，自动获取/刷新 Google 访问令牌，
并提供 Antigravity 43 款 Gemini 系列模型的原生高速调用通道。
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from typing import Any, AsyncGenerator, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from .base import BaseAccountAdapter

logger = logging.getLogger(__name__)

# 官方 Google Antigravity 原生 OAuth Client 凭证与默认回调
DEFAULT_GOOGLE_CLIENT_ID = os.getenv(
    "GOOGLE_ANTIGRAVITY_CLIENT_ID",
    "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com",
)
DEFAULT_GOOGLE_CLIENT_SECRET = os.getenv(
    "GOOGLE_ANTIGRAVITY_CLIENT_SECRET",
    "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",
)
DEFAULT_GOOGLE_REDIRECT_URI = "http://localhost:8085/callback"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_GENAI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# 官方 Antigravity 权限集合（与 Google 官方 IDE 一致）
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]

# Gemini 3 强制要求历史所有 functionCall 必须包含有效 thoughtSignature，未被各轮持久化时提供保底签名以防 400 崩溃
DEFAULT_FALLBACK_THOUGHT_SIGNATURE = (
    "EuYCCuMCAWkUfRN4/ZRUm1i/YCIA0Bv7rUX9FLJhc/H6bgc/7LiQd5hLeAdftq1jL48FO1oYZ8h92XyuBNuUYdFgGSpQS0VOFayHAuODps4hrD8+WfXSC91auqtVGNAVKmy2S0wNmc74gJOcm4lS9jGU+T47diB4/WxjpPvFqbof2v9dv4LiKSsQWJUTA5b39TsuqkGYd0xkynkvUWA+TrDRcnIQva4otfYNSRzOHzpMQzdmrT3w4H3L/RPoWqTNgMTVs7Ei+ks9aT+kQaKkcZejM1GyCkCZr2omXBj1OFpOV3nTnWeskKM/Pd4ukJtRhnVGS0z+7NFOuByVtuzgqZDd8CCghnc2IlHIdNhljYyavWTeM/lyx8dBQfz3C0q38QxI9F4ybtBAQrN8sZIoqn3XwTjgBNlZENEc4mEtBnPbPitckJuP4HXgSiYTZmKJ9Mn7x7uT6x+28fdzTI6+ZPIlGjBWBr5zvA=="
)

# 端点熔断与冷却机制：记录异常端点的冷却截止时间戳，避免永久锁定某节点，支持自动重新探活
_ENDPOINT_COOLDOWNS: Dict[str, float] = {}  # endpoint -> cooldown_expire_timestamp
_DEFAULT_COOLDOWN_SECONDS: float = 300.0     # 5 分钟常规网络/鉴权异常避让期
_QUOTA_429_COOLDOWN_SECONDS: float = 30.0    # 30 秒 429 配额限流避让期（短周期退避，避免瞬时并发限流导致节点被长期锁定）

# Google Antigravity 原生模型目录清单（严格与官方 Antigravity IDE 对齐）
# 包含 7 大系列、14 款思考分级模型；Gemini 3.1 Pro 严格仅提供 High 和 Low
ANTIGRAVITY_MODELS: List[Dict[str, Any]] = [
    # --- 1. Gemini 3.8 Flash (High, Medium, Low) ---
    {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash High", "context_window": 1048576, "vision": True, "tools": True},
    {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash Medium", "context_window": 1048576, "vision": True, "tools": True},
    {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash Low", "context_window": 1048576, "vision": True, "tools": True},

    # --- 2. Gemini 3.7 Flash (High, Medium, Low) ---
    {"id": "gemini-3.7-flash-high", "name": "Gemini 3.7 Flash High", "context_window": 1048576, "vision": True, "tools": True},
    {"id": "gemini-3.7-flash-medium", "name": "Gemini 3.7 Flash Medium", "context_window": 1048576, "vision": True, "tools": True},
    {"id": "gemini-3.7-flash-low", "name": "Gemini 3.7 Flash Low", "context_window": 1048576, "vision": True, "tools": True},

    # --- 3. Gemini 3.6 Flash (High, Medium, Low) ---
    {"id": "gemini-3.6-flash-high", "name": "Gemini 3.6 Flash High", "context_window": 1048576, "vision": True, "tools": True},
    {"id": "gemini-3.6-flash-medium", "name": "Gemini 3.6 Flash Medium", "context_window": 1048576, "vision": True, "tools": True},
    {"id": "gemini-3.6-flash-low", "name": "Gemini 3.6 Flash Low", "context_window": 1048576, "vision": True, "tools": True},

    # --- 4. Gemini 3.1 Pro (官方仅提供 High 和 Low，严格无 Medium) ---
    {"id": "gemini-3.1-pro-high", "name": "Gemini 3.1 Pro High", "context_window": 1048576, "vision": True, "tools": True},
    {"id": "gemini-3.1-pro-low", "name": "Gemini 3.1 Pro Low", "context_window": 1048576, "vision": True, "tools": True},

    # --- 5. Claude Sonnet 4.6 (Thinking) ---
    {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6 (Thinking)", "context_window": 200000, "vision": True, "tools": True},

    # --- 6. Claude Opus 4.6 (Thinking) ---
    {"id": "claude-opus-4-6-thinking", "name": "Claude Opus 4.6 (Thinking)", "context_window": 200000, "vision": True, "tools": True},

    # --- 7. GPT-OSS 120B (Medium) ---
    {"id": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)", "context_window": 131072, "vision": False, "tools": True},
]
ANTIGRAVITY_43_MODELS = ANTIGRAVITY_MODELS  # 兼容现有引用名称


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


_IMAGE_DROPPED_PLACEHOLDER = "[图片附件未能加载]"


def _inline_data(mime: str, data: str) -> Dict[str, Any]:
    """Gemini inlineData part 的唯一构造点。"""
    return {"inlineData": {"mimeType": mime or "image/png", "data": data}}


def _is_image_block(block: Any) -> bool:
    """判断消息块是否承载图片/文档负载（含各协议形态）。"""
    if not isinstance(block, dict):
        return False
    if block.get("type") in ("image", "image_url", "document", "image_ref"):
        return True
    return "inlineData" in block or "inline_data" in block


def _extract_inline_data_from_block(block: Any) -> Optional[Dict[str, Any]]:
    """从消息块中提取 Gemini 协议所需的 inlineData (mimeType + base64 data)。

    兼容以下多种上游格式：
    1. Gemini 原生格式:
       - {"type": "inlineData", "inlineData": {"mimeType": "image/png", "data": "..."}}
       - {"inlineData": {"mimeType": "image/png", "data": "..."}}
       - {"inline_data": {"mime_type": "image/png", "data": "..."}}
    2. Anthropic 格式:
       - {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
       - {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "..."}}
       - {"type": "image", "data": "...", "media_type": "image/png"}
    3. OpenAI vision 格式（柳如烟主路径——agent 层把图片统一转成 data URL）:
       - {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
       - {"type": "image_url", "image_url": "data:image/png;base64,..."}

    不可解析 → None，由调用方 `_image_parts_from_block` 决定降级形态：
    - http(s) 网络图 URL：不支持下载（本地应用不主动拉外部资源），降级占位符
    - 非 base64 data URL：载荷是 URL 编码而非 base64，直接塞 inlineData 是错的
    - image_ref：agent 层已带真实 agent_id 做 reinflate，能活到这里的都是读盘
      失败的坏引用（attachments.py 对空 agent_id 直接返 None，此层无 agent_id
      无从读盘），降级占位符
    """
    if not isinstance(block, dict):
        return None

    btype = block.get("type")

    # 1. Gemini 原生 inlineData / inline_data
    if "inlineData" in block and isinstance(block["inlineData"], dict):
        idat = block["inlineData"]
        if idat.get("data"):
            return _inline_data(idat.get("mimeType") or idat.get("mime_type") or "image/png", idat["data"])
    if "inline_data" in block and isinstance(block["inline_data"], dict):
        idat = block["inline_data"]
        if idat.get("data"):
            return _inline_data(idat.get("mime_type") or idat.get("mimeType") or "image/png", idat["data"])

    # 2. Anthropic image / document block
    if btype in ("image", "document"):
        src = block.get("source")
        if isinstance(src, dict) and src.get("type") == "base64" and src.get("data"):
            default_mime = "application/pdf" if btype == "document" else "image/png"
            return _inline_data(src.get("media_type") or default_mime, src["data"])
        if block.get("data"):
            default_mime = "application/pdf" if btype == "document" else "image/png"
            return _inline_data(
                block.get("media_type") or block.get("mime_type") or default_mime,
                block["data"],
            )

    # 3. OpenAI image_url block（仅 data URL 可解析）
    if btype == "image_url":
        img_info = block.get("image_url")
        url = img_info.get("url", "") if isinstance(img_info, dict) else str(img_info or "")
        if url.startswith("data:"):
            header, _, data = url.partition(",")
            if ";" not in header:
                # 无 ";base64" 标记 → 载荷是 URL 编码文本，塞 inlineData 是错的
                logger.debug("[GEMINI-IMAGE] non-base64 data URL dropped (mime=%s)", header[5:])
                return None
            mime = header[5:].split(";", 1)[0] or "image/png"
            if data:
                return _inline_data(mime, data)
        elif url:
            logger.debug("[GEMINI-IMAGE] http(s) image URL not supported, dropped: %s", url[:120])
        return None

    return None


def _image_parts_from_block(block: Any) -> List[Dict[str, Any]]:
    """消息块 → Gemini parts。

    图片块可解析则产出 inlineData；解析失败但确为图片块时降级为文字占位符
    （模型知道"这里曾有一张图"，而不是静默失明）；非图片块返回空。
    """
    part = _extract_inline_data_from_block(block)
    if part:
        return [part]
    if _is_image_block(block):
        logger.debug(
            "[GEMINI-IMAGE] image block unresolvable, degraded to placeholder: type=%s",
            block.get("type") if isinstance(block, dict) else type(block).__name__,
        )
        return [{"text": _IMAGE_DROPPED_PLACEHOLDER}]
    return []


def _stringify_tool_content(content: Any) -> str:
    """functionResponse.result 的文本化。

    list 形态 content 里的图片块绝不能跟着 json.dumps 进 result——否则 base64
    以"文本拷贝 + inlineData"双份进入请求体，请求暴涨且白烧 token。图片块
    替换为占位符（真身由调用方另以 inlineData 附加），文本块直接拼接，其余
    结构原样 JSON 化。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: List[str] = []
        for b in content:
            if isinstance(b, dict) and _is_image_block(b):
                out.append(_IMAGE_DROPPED_PLACEHOLDER)
            elif isinstance(b, dict) and b.get("type") == "text":
                out.append(str(b.get("text") or ""))
            elif b is not None:
                try:
                    out.append(json.dumps(b, ensure_ascii=False))
                except Exception:
                    out.append(str(b))
        return "\n".join(p for p in out if p)
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _detect_antigravity_proxy() -> Optional[str]:
    """智能检测网络代理配置（支持环境变量、系统注册表及本地常见代理客户端端口 Clash / Clash Verge / v2rayN 等）"""
    for env_k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        val = os.getenv(env_k)
        if val:
            return val
    try:
        import urllib.request
        proxies = urllib.request.getproxies()
        sys_p = proxies.get("https") or proxies.get("http")
        if sys_p:
            return sys_p
    except Exception:
        pass
    import socket
    # 常用代理端口探测：7897 (Clash Verge Rev 默认), 7890 (Clash 经典), 10809 (v2rayN http)
    for port in (7897, 7890, 10809):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return f"http://127.0.0.1:{port}"
        except Exception:
            pass
    return None


class GoogleAntigravityAdapter(BaseAccountAdapter):
    """Google Antigravity / Gemini 账号适配器"""

    provider_id = "google"
    display_name = "Google"
    sub_title = "Google Antigravity"
    author = "@masudranaxpert"
    icon = "google"

    def __init__(self, client_id: str = "", client_secret: str = ""):
        self.client_id = client_id or DEFAULT_GOOGLE_CLIENT_ID
        self.client_secret = client_secret or DEFAULT_GOOGLE_CLIENT_SECRET

    def generate_auth_params(self, redirect_uri: str = "") -> Dict[str, Any]:
        """生成符合 PKCE 标准的 Google OAuth2 授权链接及对应参数"""
        effective_redirect = redirect_uri or DEFAULT_GOOGLE_REDIRECT_URI
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())
        state = _b64url(secrets.token_bytes(32))

        params = {
            "client_id": self.client_id,
            "redirect_uri": effective_redirect,
            "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
        return {
            "auth_url": auth_url,
            "state": state,
            "code_verifier": verifier,
            "redirect_uri": effective_redirect,
        }

    async def exchange_token(
        self,
        code: str,
        redirect_uri: str = "",
        code_verifier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """使用授权码换取 access_token 与 refresh_token"""
        effective_redirect = redirect_uri or DEFAULT_GOOGLE_REDIRECT_URI
        payload: Dict[str, Any] = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": effective_redirect,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        proxy = _detect_antigravity_proxy()
        async with httpx.AsyncClient(timeout=30.0, proxy=proxy, trust_env=True) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data=payload, headers=headers)
            if resp.status_code != 200:
                logger.error("[GoogleAntigravity] 换取 Token 失败: %s - %s", resp.status_code, resp.text)
                raise RuntimeError(f"Google Token 换取失败 ({resp.status_code}): {resp.text}")
            data = resp.json()
            # 计算过期时间绝对时间戳
            expires_in = data.get("expires_in", 3599)
            data["expires_at"] = time.time() + expires_in
            return data

    async def refresh_access_token(self, refresh_token: str) -> Dict[str, Any]:
        """通过 refresh_token 刷新 access_token"""
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        proxy = _detect_antigravity_proxy()
        async with httpx.AsyncClient(timeout=30.0, proxy=proxy, trust_env=True) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data=payload)
            if resp.status_code != 200:
                logger.error("[GoogleAntigravity] 刷新 Token 失败: %s - %s", resp.status_code, resp.text)
                raise RuntimeError(f"Google Token 刷新失败 ({resp.status_code}): {resp.text}")
            data = resp.json()
            expires_in = data.get("expires_in", 3599)
            data["expires_at"] = time.time() + expires_in
            return data

    async def fetch_user_info(self, access_token: str) -> Dict[str, Any]:
        """拉取 Google 账号基本信息与关联 CloudCode Project ID"""
        headers = {"Authorization": f"Bearer {access_token}"}
        user_info = {
            "id": str(time.time()),
            "email": "google_user@gmail.com",
            "name": "Google User",
            "picture": "",
            "project_id": "bamboo-precept-lgxtn",
        }
        proxy = _detect_antigravity_proxy()
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, trust_env=True) as client:
            try:
                resp = await client.get(GOOGLE_USERINFO_URL, headers=headers)
                if resp.status_code == 200:
                    info = resp.json()
                    user_info["id"] = info.get("id", str(time.time()))
                    user_info["email"] = info.get("email", "")
                    user_info["name"] = info.get("name", info.get("email", "Google User"))
                    user_info["picture"] = info.get("picture", "")
            except Exception as e:
                logger.warning("[GoogleAntigravity] 获取 Google userinfo 异常: %s", e)

            # 获取 Antigravity CloudCode companion project
            cloudcode_headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity/2.9.1 windows/amd64",
            }
            assist_payload = {"metadata": {"ideType": "ANTIGRAVITY", "ideVersion": "2.9.1"}}
            for ep in ("https://daily-cloudcode-pa.googleapis.com",):
                try:
                    cresp = await client.post(f"{ep}/v1internal:loadCodeAssist", headers=cloudcode_headers, json=assist_payload)
                    if cresp.status_code == 200:
                        cdata = cresp.json()
                        proj = cdata.get("cloudaicompanionProject")
                        if proj:
                            user_info["project_id"] = proj
                            break
                except Exception:
                    pass

        return user_info

    # 配额防风控安全缓存（内存级 TTL 缓存：60 秒自动失效；强刷设 5 秒冷却）
    _quota_cache: Dict[str, Dict[str, Any]] = {}

    async def list_models(self, access_token: Optional[str] = None) -> List[Dict[str, Any]]:
        """返回 Antigravity 支持的原生思考分级模型列表（默认静态保底）"""
        return ANTIGRAVITY_MODELS

    async def fetch_recommended_models(
        self,
        access_token: str,
        project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """从 Google 官方 v1internal:fetchAvailableModels 动态拉取 Recommended 模型列表。

        严格与 Antigravity 官方 IDE 保持一致：
        1. 优先提取 agentModelSorts 中标记为 Recommended 的模型分组；
        2. 结合 models 元数据，提取 displayName、maxTokens、supportedMimeTypes 等属性；
        3. 若网络或上游异常，平滑降级回本地预置 ANTIGRAVITY_MODELS。
        """
        if not access_token:
            return ANTIGRAVITY_MODELS

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "antigravity/2.9.1 windows/amd64",
        }
        body = {"project": project_id or "aicode-consumers"}
        proxy = _detect_antigravity_proxy()

        endpoints = [
            "https://cloudcode-pa.googleapis.com",
            "https://daily-cloudcode-pa.googleapis.com",
        ]

        raw_data = None
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, trust_env=True) as client:
            for ep in endpoints:
                try:
                    resp = await client.post(
                        f"{ep}/v1internal:fetchAvailableModels",
                        headers=headers,
                        json=body,
                    )
                    if resp.status_code == 200:
                        raw_data = resp.json()
                        break
                    else:
                        logger.warning("[GoogleAntigravity] %s 拉取模型失败: %s", ep, resp.status_code)
                except Exception as e:
                    logger.debug("[GoogleAntigravity] 端点 %s 拉取模型异常: %s", ep, e)

        if not raw_data or not isinstance(raw_data, dict):
            logger.info("[GoogleAntigravity] 未能拉取到云端模型，降级为默认预置模型列表")
            return ANTIGRAVITY_MODELS

        agent_sorts = raw_data.get("agentModelSorts", [])
        recommended_ids: List[str] = []

        # 1. 查找 Recommended 组
        for sort_item in agent_sorts:
            if str(sort_item.get("displayName", "")).strip().lower() == "recommended":
                for grp in sort_item.get("groups", []):
                    for mid in grp.get("modelIds", []):
                        if mid and mid not in recommended_ids:
                            recommended_ids.append(mid)

        # 2. 兜底：若没有明确 Recommended 标签，取第一组
        if not recommended_ids and agent_sorts:
            for grp in agent_sorts[0].get("groups", []):
                for mid in grp.get("modelIds", []):
                    if mid and mid not in recommended_ids:
                        recommended_ids.append(mid)

        if not recommended_ids:
            return ANTIGRAVITY_MODELS

        models_dict = raw_data.get("models", {})
        parsed_models: List[Dict[str, Any]] = []

        for mid in recommended_ids:
            meta = models_dict.get(mid, {}) if isinstance(models_dict, dict) else {}
            display_name = meta.get("displayName")
            if not display_name:
                display_name = mid.replace("-", " ").title()
                display_name = display_name.replace("Gpt", "GPT").replace("Oss", "OSS")

            max_tokens = meta.get("maxTokens") or 1048576
            supported_mimes = meta.get("supportedMimeTypes", {})
            has_vision = any(
                "image" in mime or "video" in mime for mime in supported_mimes
            ) if supported_mimes else True

            parsed_models.append({
                "id": mid,
                "name": display_name,
                "context_window": int(max_tokens),
                "vision": has_vision,
                "tools": True,
                "recommended": True,
            })

        logger.info(
            "[GoogleAntigravity] 成功从云端动态同步 %d 款 Recommended 模型: %s",
            len(parsed_models),
            [m["id"] for m in parsed_models],
        )
        return parsed_models

    async def test_connection(self, access_token: str) -> Dict[str, Any]:
        """测试 Token 连通性并测量网络往返延迟 (ms)"""
        t0 = time.time()
        user_info = await self.fetch_user_info(access_token)
        latency_ms = int((time.time() - t0) * 1000)
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "email": user_info.get("email"),
            "project_id": user_info.get("project_id"),
            "model_count": len(ANTIGRAVITY_MODELS),
        }

    async def fetch_user_quota_summary(
        self,
        access_token: str,
        project_id: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """获取 Google Antigravity 配额概览（严格带 60s 内存缓存与防风控限频机制）"""
        cache_key = hashlib.md5(access_token.encode("utf-8")).hexdigest()
        now = time.time()

        # 1. 命中缓存直接返回，避免频繁调用 Google 造成账号风控
        cached_entry = self._quota_cache.get(cache_key)
        if cached_entry and not force_refresh:
            if now < cached_entry.get("expires_at", 0):
                return {**cached_entry["data"], "cached": True}

        # 强刷最小间隔限制（至少 5 秒，防止暴力狂点触发 429）
        if cached_entry and force_refresh:
            last_fetch = cached_entry.get("fetched_at", 0)
            if now - last_fetch < 5.0:
                return {**cached_entry["data"], "cached": True, "rate_limited": True}

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "antigravity/2.9.1 windows/amd64",
        }
        body = {"project": project_id or "aicode-consumers"}
        proxy = _detect_antigravity_proxy()

        endpoints = (
            "https://daily-cloudcode-pa.googleapis.com",
        )

        quota_data = None
        async with httpx.AsyncClient(timeout=15.0, proxy=proxy, trust_env=True) as client:
            for ep in endpoints:
                try:
                    resp = await client.post(
                        f"{ep}/v1internal:retrieveUserQuotaSummary",
                        headers=headers,
                        json=body,
                    )
                    if resp.status_code == 200:
                        quota_data = resp.json()
                        break
                    elif resp.status_code == 429:
                        logger.warning("[GoogleAntigravity] 配额查询遭遇 429: %s", resp.text[:200])
                        if cached_entry:
                            return {**cached_entry["data"], "cached": True, "warning": "429 限流，已展示最近一次缓存配额"}
                except Exception as e:
                    logger.debug("[GoogleAntigravity] 端点 %s 查询配额异常: %s", ep, e)

        if not quota_data:
            if cached_entry:
                return {**cached_entry["data"], "cached": True}
            return {
                "ok": False,
                "error": "未能从 Google 接口拉取到最新配额数据",
                "groups": [],
            }

        parsed = self._parse_quota_summary(quota_data)
        result = {
            "ok": True,
            "cached": False,
            "fetched_at": now,
            **parsed,
        }

        # 写入缓存：默认安全缓存 300 秒（5 分钟静默保护期）
        self._quota_cache[cache_key] = {
            "data": result,
            "fetched_at": now,
            "expires_at": now + 300.0,
        }
        return result

    @staticmethod
    def _parse_quota_summary(raw: Dict[str, Any]) -> Dict[str, Any]:
        """解析 Google 原始 retrieveUserQuotaSummary 响应为前端友好结构"""
        groups = raw.get("groups", [])
        parsed_groups = []
        gemini_info: Dict[str, Any] = {}
        claude_info: Dict[str, Any] = {}

        for g in groups:
            g_name = g.get("displayName", "")
            is_gemini = "gemini" in g_name.lower()
            buckets = []
            target_dict = gemini_info if is_gemini else claude_info

            for b in g.get("buckets", []):
                bid = b.get("bucketId", "")
                window = b.get("window", "")  # "weekly" or "5h"
                rem_frac = float(b.get("remainingFraction", 1.0))
                pct = max(0, min(100, round(rem_frac * 100)))
                reset_iso = b.get("resetTime", "")

                reset_in_desc = ""
                reset_formatted = ""
                if reset_iso:
                    try:
                        from datetime import datetime, timezone
                        iso_clean = reset_iso.replace("Z", "+00:00")
                        dt = datetime.fromisoformat(iso_clean)
                        diff_sec = int((dt - datetime.now(timezone.utc)).total_seconds())
                        local_dt = dt.astimezone()
                        reset_formatted = local_dt.strftime("%m/%d %H:%M")

                        if diff_sec > 0:
                            days = diff_sec // 86400
                            hours = (diff_sec % 86400) // 3600
                            mins = (diff_sec % 3600) // 60
                            if days > 0:
                                reset_in_desc = f"{days}d {hours}h"
                            elif hours > 0:
                                reset_in_desc = f"{hours}h {mins}m"
                            else:
                                reset_in_desc = f"{mins}m"
                        else:
                            reset_in_desc = "已重置"
                    except Exception:
                        pass

                bucket_parsed = {
                    "bucket_id": bid,
                    "display_name": b.get("displayName", ""),
                    "window": window,
                    "remaining_fraction": rem_frac,
                    "percentage": pct,
                    "reset_time": reset_iso,
                    "reset_in": reset_in_desc,
                    "reset_formatted": reset_formatted,
                    "description": b.get("description", ""),
                }
                buckets.append(bucket_parsed)

                if window == "5h" or "5h" in bid:
                    target_dict["five_hour"] = bucket_parsed
                elif window == "weekly" or "weekly" in bid:
                    target_dict["weekly"] = bucket_parsed

            parsed_groups.append({
                "display_name": g_name,
                "description": g.get("description", ""),
                "buckets": buckets,
            })

        return {
            "groups": parsed_groups,
            "gemini": gemini_info,
            "claude": claude_info,
            "raw": raw,
        }

    @classmethod
    def _convert_messages_to_contents(
        cls,
        messages: List[Dict[str, Any]],
        target_model: str = "",
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """将 OpenAI / 柳如烟统一 messages 转换到 Gemini CloudCode 格式 (systemInstruction + contents)。"""
        # 建立 tool_call_id 到工具名的映射，供 tool role 构造 functionResponse 时关联
        # 兼容两种来源：1. OpenAI std 的 msg["tool_calls"]；2. Anthropic v3 content 里的 tool_use blocks
        id_to_name: Dict[str, str] = {}
        for msg in messages:
            for tc in msg.get("tool_calls") or []:
                tid = tc.get("id")
                fn = tc.get("function") or {}
                tname = fn.get("name") or tc.get("name")
                if tid and tname:
                    id_to_name[tid] = tname
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tid = block.get("id")
                        tname = block.get("name")
                        if tid and tname:
                            id_to_name[tid] = tname

        system_instruction_text = ""
        contents: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                if isinstance(content, list):
                    s_text = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
                else:
                    s_text = str(content or "")
                system_instruction_text += (s_text + "\n")
            elif role in ("assistant", "model"):
                parts = []
                tool_calls = list(msg.get("tool_calls") or [])

                # 处理 content 是 block 数组的情况（Anthropic v3 格式）
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text" and block.get("text"):
                            parts.append({"text": block["text"]})
                        elif btype == "tool_use":
                            fn_name = block.get("name", "")
                            fn_args = block.get("input", {})
                            if isinstance(fn_args, str):
                                try:
                                    fn_args = json.loads(fn_args)
                                except Exception:
                                    fn_args = {"raw": fn_args}
                            sig = (
                                block.get("thought_signature")
                                or block.get("thoughtSignature")
                                or DEFAULT_FALLBACK_THOUGHT_SIGNATURE
                            )
                            part_dict: Dict[str, Any] = {
                                "thoughtSignature": sig,
                                "functionCall": {
                                    "name": fn_name,
                                    "args": fn_args or {},
                                },
                            }
                            if "claude" in target_model.lower() and block.get("id"):
                                part_dict["functionCall"]["id"] = block["id"]
                            parts.append(part_dict)
                        else:
                            parts.extend(_image_parts_from_block(block))
                elif content:
                    parts.append({"text": str(content)})

                # 寻找本轮中携带的任一有效 thought_signature 作为保底基线
                turn_thought_sig = None
                for tc in tool_calls:
                    sig = tc.get("thought_signature") or tc.get("thoughtSignature")
                    if sig:
                        turn_thought_sig = sig
                        break

                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    fn_name = fn.get("name") or tc.get("name", "")
                    raw_args = fn.get("arguments") if "arguments" in fn else tc.get("arguments", {})
                    if isinstance(raw_args, str):
                        try:
                            fn_args = json.loads(raw_args)
                        except Exception:
                            fn_args = {"raw": raw_args}
                    else:
                        fn_args = raw_args or {}
                    sig = (
                        tc.get("thought_signature")
                        or tc.get("thoughtSignature")
                        or turn_thought_sig
                        or DEFAULT_FALLBACK_THOUGHT_SIGNATURE
                    )
                    part_dict = {
                        "thoughtSignature": sig,
                        "functionCall": {
                            "name": fn_name,
                            "args": fn_args or {},
                        },
                    }
                    if "claude" in target_model.lower() and tc.get("id"):
                        part_dict["functionCall"]["id"] = tc["id"]
                    parts.append(part_dict)

                if not parts:
                    parts = [{"text": " "}]
                contents.append({"role": "model", "parts": parts})
            elif role == "tool":
                tid = msg.get("tool_call_id", "")
                tool_name = msg.get("name") or id_to_name.get(tid, "tool")
                # 图片块不能 json.dumps 进 result（base64 双份进请求体）：
                # 文本化时替换为占位符，真身另以 inlineData 附加
                c_str = _stringify_tool_content(content)
                parts = [{
                    "functionResponse": {
                        "name": tool_name,
                        "response": {
                            "result": c_str,
                        },
                    }
                }]
                if isinstance(content, list):
                    for b in content:
                        parts.extend(_image_parts_from_block(b))
                contents.append({
                    "role": "user",
                    "parts": parts,
                })
            else:
                # role == "user"
                parts = []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text" and block.get("text"):
                            parts.append({"text": block["text"]})
                        elif btype == "tool_result":
                            tid = block.get("tool_use_id", "")
                            tool_name = id_to_name.get(tid, "tool")
                            # 同 tool role：list content 文本化时剔除图片块，
                            # 避免 base64 双份（占位符 + inlineData）
                            res_content = _stringify_tool_content(block.get("content", ""))
                            parts.append({
                                "functionResponse": {
                                    "name": tool_name,
                                    "response": {
                                        "result": res_content,
                                    },
                                }
                            })
                            if isinstance(block.get("content"), list):
                                for sub_b in block["content"]:
                                    parts.extend(_image_parts_from_block(sub_b))
                        else:
                            parts.extend(_image_parts_from_block(block))
                elif content:
                    parts.append({"text": str(content)})

                # 兼容外置 images 字段（如部分 UI 或外部适配器传入）
                for img in msg.get("images") or []:
                    if isinstance(img, str):
                        if not img.startswith("data:"):
                            continue  # 非 data URL 字符串语义不明，维持旧行为跳过
                        img = {"type": "image_url", "image_url": {"url": img}}
                    parts.extend(_image_parts_from_block(img))

                if not parts:
                    parts = [{"text": " "}]
                contents.append({"role": "user", "parts": parts})

        # 合并相邻同角色消息（Gemini 协议要求严格按 role 交替）
        merged_contents: List[Dict[str, Any]] = []
        for c in contents:
            if merged_contents and merged_contents[-1]["role"] == c["role"]:
                prev_parts = merged_contents[-1]["parts"]
                curr_parts = c["parts"]
                if prev_parts == [{"text": " "}] and curr_parts:
                    merged_contents[-1]["parts"] = list(curr_parts)
                elif curr_parts == [{"text": " "}] and prev_parts:
                    pass
                else:
                    merged_contents[-1]["parts"].extend(curr_parts)
            else:
                merged_contents.append(c)
        contents = merged_contents

        # 保证交替出现且首条为 user
        if not contents:
            contents.append({"role": "user", "parts": [{"text": " "}]})
        elif contents[0]["role"] != "user":
            contents.insert(0, {"role": "user", "parts": [{"text": " "}]})

        return system_instruction_text, contents

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        access_token: str,
        project_id: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """向 Google Antigravity CloudCode 内部通道发起原生流式调用"""
        import uuid

        if not access_token:
            yield {
                "type": "error",
                "error": "Google 算力账号未提供有效 access_token，请重新登录。",
            }
            return

        # 模型名标准化与别名映射
        target_model = model
        if target_model.startswith("models/"):
            target_model = target_model[7:]
        # 兼容简写别名映射到对应的默认思考分级
        if target_model in ("gemini-3.8-flash", "gemini-3.8-flash-default"):
            target_model = "gemini-3.8-flash-medium"
        elif target_model in ("gemini-3.7-flash", "gemini-3.7-flash-default"):
            target_model = "gemini-3.7-flash-medium"
        elif target_model in ("gemini-3.6-flash", "gemini-3.6-flash-default"):
            target_model = "gemini-3.6-flash-medium"
        elif target_model in ("gemini-pro-agent", "gemini-3.1-pro", "gemini-3.1-pro-default", "gemini-3.1-pro-high"):
            # 目前 Google daily 端点上 Gemini 3.1 Pro 稳定开放 low 档位，high 档位上游易报 400，自动平滑兼容
            target_model = "gemini-3.1-pro-low"
        elif target_model in ("claude-opus-4-6", "claude-4-6-opus"):
            target_model = "claude-opus-4-6-thinking"
        elif target_model in ("gpt-oss-120b", "gpt-oss-120b-default"):
            target_model = "gpt-oss-120b-medium"
        elif "3-7-sonnet" in target_model.lower() or "3-5-sonnet" in target_model.lower() or "claude-sonnet" in target_model.lower():
            target_model = "claude-sonnet-4-6"
        elif "argon" in target_model.lower() and not any(k in target_model.lower() for k in ("high", "medium", "low")):
            target_model = f"{target_model}-high"

        # 动态获取账号关联的真实 Google Cloud Companion 项目 ID
        if not project_id:
            try:
                from backend.compute_accounts.manager import ComputeAccountsManager
                mgr = ComputeAccountsManager.get_instance()
                google_accs = mgr._accounts.get("google", [])
                if google_accs and google_accs[0].get("project_id"):
                    project_id = google_accs[0].get("project_id")
            except Exception:
                pass
        project_id = project_id or "aicode-consumers"

        # 转换 OpenAI/柳如烟标准 messages 格式到 Gemini 协议 (含多模态 inlineData)
        system_instruction_text, contents = self._convert_messages_to_contents(messages, target_model)

        temperature = kwargs.get("temperature", 0.7)
        max_tokens = kwargs.get("max_tokens") or 8192
        # Gemini 系列原生支持 65536 上限。开启 thinking 时若 maxOutputTokens 只有 8192，
        # 思考 token (往往占用 2k-6k) 会极易将总配额耗尽，导致模型在输出正文途中被硬件级截断。
        if "gemini" in target_model.lower():
            effective_max_tokens = max(max_tokens, 65536)
        else:
            effective_max_tokens = max_tokens

        # Antigravity 官方 identity 标识（CloudCode 内部通道校验所需）
        antigravity_identity = (
            "<identity>\n"
            "You are Antigravity, a powerful agentic AI coding assistant designed by the Google Deepmind team working on Advanced Agentic Coding.\n"
            "You are pair programming with a USER to solve their coding task. The task may require creating a new codebase, modifying or debugging an existing codebase, or simply answering a question.\n"
            "The USER will send you requests, which you must always prioritize addressing. Along with each USER request, we will attach additional metadata about their current state, such as what files they have open and where their cursor is.\n"
            "This information may or may not be relevant to the coding task, it is up for you to decide.\n"
            "</identity>"
        )
        if "You are Antigravity" not in system_instruction_text:
            system_instruction_text = (antigravity_identity + "\n\n" + system_instruction_text).strip()


        # 构建官方 Antigravity CloudCode 载荷
        cloudcode_payload: Dict[str, Any] = {
            "project": project_id,
            "model": target_model,
            "userAgent": "antigravity",
            "requestType": "agent",
            "requestId": f"agent-{uuid.uuid4()}",
            "request": {
                "contents": contents,
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": effective_max_tokens,
                },
            }
        }

        # 转换并注入工具定义（Function Calling）
        tools = kwargs.get("tools")
        if tools:
            fdecls = []
            for t in tools:
                fn = t.get("function", t)
                params = fn.get("parameters", {})
                p_clean = {
                    "type": params.get("type", "object"),
                    "properties": params.get("properties", {}),
                }
                if "required" in params:
                    p_clean["required"] = params["required"]
                fdecls.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": p_clean,
                })
            if fdecls:
                cloudcode_payload["request"]["tools"] = [{"functionDeclarations": fdecls}]
                cloudcode_payload["request"]["toolConfig"] = {
                    "functionCallingConfig": {
                        "mode": "AUTO"
                    }
                }

        if system_instruction_text.strip():
            cloudcode_payload["request"]["systemInstruction"] = {
                "parts": [{"text": system_instruction_text.strip()}]
            }

        # 如果模型带 -high / -medium / -low 等思考分级，注入 thinkingConfig
        # 必须显式携带 includeThoughts=True，否则 Google 端点默认将思考流剥离隐藏
        thinking_budget = None
        if "high" in target_model.lower():
            thinking_budget = 24576
        elif "medium" in target_model.lower():
            thinking_budget = 8192
        elif "low" in target_model.lower():
            thinking_budget = 4096
        elif "thinking" in target_model.lower() or "claude" in target_model.lower():
            thinking_budget = 8192

        if thinking_budget is not None:
            # 开启思考时，必须保证 maxOutputTokens 严格大于 thinkingBudget，
            # 否则 Anthropic/Claude 等模型会上报 400 错误 (max_tokens must be greater than thinking.budget_tokens)，
            # 同时避免思考 token 耗尽总输出配额导致回答截断
            effective_max_tokens = max(effective_max_tokens, thinking_budget + 4096)
            cloudcode_payload["request"]["generationConfig"]["maxOutputTokens"] = effective_max_tokens
            cloudcode_payload["request"]["generationConfig"]["thinkingConfig"] = {
                "thinkingBudget": thinking_budget,
                "includeThoughts": True,
            }

        cloudcode_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "antigravity/2.9.1 windows/amd64",
        }
        if "claude" in target_model.lower():
            cloudcode_headers["anthropic-beta"] = "claude-code-20250219,interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14"

        # 端点路由策略（严格与官方 Antigravity IDE 对齐）：
        # Antigravity 专享全系模型（Gemini 3.x、Claude 4.6 及 GPT-OSS）原生归属于 daily 预览集群
        endpoints_pool = [
            "https://daily-cloudcode-pa.googleapis.com",
        ]
        # 冷却表写入用 time.time() + cooldown（见下方 429/异常分支），比较基准必须同源。
        # 修复 NameError: 本方法此前引用了 fetch_user_quota_summary 作用域里的同名局部变量。
        now = time.time()
        healthy_endpoints = [ep for ep in endpoints_pool if now >= _ENDPOINT_COOLDOWNS.get(ep, 0)]
        cooling_endpoints = [ep for ep in endpoints_pool if ep not in healthy_endpoints]
        cloudcode_endpoints = healthy_endpoints + cooling_endpoints

        streamed = False
        last_status_code = None
        last_error_msg = ""
        had_429 = False
        last_429_msg = ""
        stream_last_thought_sig = None

        proxy = _detect_antigravity_proxy()
        if proxy:
            logger.info("[GoogleAntigravity] 检测到可用代理，已自动接入: %s", proxy)

        client_timeout = httpx.Timeout(connect=15.0, read=60.0, write=20.0, pool=15.0)
        async with httpx.AsyncClient(timeout=client_timeout, proxy=proxy, trust_env=True, http2=False) as client:
            for ep_idx, ep in enumerate(cloudcode_endpoints):
                url = f"{ep}/v1internal:streamGenerateContent?alt=sse"
                max_attempts = 2
                for attempt in range(max_attempts):
                    try:
                        async with client.stream("POST", url, headers=cloudcode_headers, json=cloudcode_payload) as response:
                            if response.status_code == 200:
                                if ep in _ENDPOINT_COOLDOWNS:
                                    _ENDPOINT_COOLDOWNS.pop(ep, None)
                                    logger.info("[GoogleAntigravity] 端点 %s 已恢复健康，成功解除冷却锁定", ep)
                                async for line in response.aiter_lines():
                                    if not line:
                                        continue
                                    if line.startswith("data: "):
                                        data_str = line[6:].strip()
                                        if data_str == "[DONE]":
                                            break
                                        try:
                                            chunk_json = json.loads(data_str)
                                            resp_obj = chunk_json.get("response") or chunk_json
                                            candidates = resp_obj.get("candidates", [])
                                            if candidates:
                                                cand = candidates[0]
                                                parts = cand.get("content", {}).get("parts", [])
                                                cand_sig = next((p.get("thoughtSignature") for p in parts if p.get("thoughtSignature")), None)
                                                if cand_sig:
                                                    stream_last_thought_sig = cand_sig

                                                for part in parts:
                                                    text = part.get("text", "")
                                                    if text:
                                                        streamed = True
                                                        if part.get("thought"):
                                                            yield {"type": "thinking", "content": text}
                                                        else:
                                                            yield {"type": "delta", "content": text}
                                                    fc = part.get("functionCall")
                                                    if fc:
                                                        streamed = True
                                                        fn_name = fc.get("name", "")
                                                        fn_args = fc.get("args", {})
                                                        fn_id = fc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                                                        thought_sig = part.get("thoughtSignature") or cand_sig or stream_last_thought_sig
                                                        if part.get("thoughtSignature"):
                                                            stream_last_thought_sig = part.get("thoughtSignature")
                                                        yield {
                                                            "type": "tool_call",
                                                            "id": fn_id,
                                                            "name": fn_name,
                                                            "arguments": fn_args if isinstance(fn_args, str) else json.dumps(fn_args, ensure_ascii=False),
                                                            "thought_signature": thought_sig,
                                                        }
                                            usage_meta = resp_obj.get("usageMetadata")
                                            if usage_meta:
                                                # Gemini 的 promptTokenCount 含 cached 部分，
                                                # cachedContentTokenCount 单列 → 按 OpenAI 语义
                                                # 塞进 prompt_tokens_details.cached_tokens，
                                                # 让 llm_client 归一化时剥出 cache_read
                                                yield {
                                                    "type": "usage",
                                                    "usage": {
                                                        "prompt_tokens": usage_meta.get("promptTokenCount", 0),
                                                        "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
                                                        "total_tokens": usage_meta.get("totalTokenCount", 0),
                                                        "prompt_tokens_details": {
                                                            "cached_tokens": usage_meta.get("cachedContentTokenCount", 0),
                                                        },
                                                    }
                                                }
                                        except Exception as e:
                                            logger.debug("[GoogleAntigravity] 解析 chunk 异常: %s", e)
                                if streamed:
                                    return
                                break
                            elif response.status_code in (401, 403):
                                err_text = (await response.aread()).decode("utf-8", "ignore")
                                logger.warning("[GoogleAntigravity] 端点 %s 响应 %s: %s", ep, response.status_code, err_text[:300])
                                _ENDPOINT_COOLDOWNS[ep] = time.time() + _DEFAULT_COOLDOWN_SECONDS
                                if ep_idx < len(cloudcode_endpoints) - 1:
                                    next_ep = cloudcode_endpoints[ep_idx + 1]
                                    logger.info("[GoogleAntigravity] 端点 %s 响应 %s，已进入 %.0f 秒避让，立即尝试备用端点: %s", ep, response.status_code, _DEFAULT_COOLDOWN_SECONDS, next_ep)
                                    break
                                yield {
                                    "type": "error",
                                    "error": f"【Google 算力账号】账号认证失效或权限不足 (HTTP {response.status_code})。请前往 [设置 → 算力账号] 重新授权登录您的 Google 账号。",
                                }
                                return
                            else:
                                err_bytes = await response.aread()
                                err_text = err_bytes.decode("utf-8", "ignore")
                                logger.warning("[GoogleAntigravity] 端点 %s 响应 %s: %s", ep, response.status_code, err_text[:300])

                                try:
                                    err_data = json.loads(err_text)
                                    g_msg = err_data.get("error", {}).get("message", "")
                                except Exception:
                                    g_msg = err_text[:300]

                                last_status_code = response.status_code
                                last_error_msg = g_msg or err_text[:300]

                                if response.status_code == 429:
                                    had_429 = True
                                    last_429_msg = last_error_msg
                                    _ENDPOINT_COOLDOWNS[ep] = time.time() + _QUOTA_429_COOLDOWN_SECONDS
                                    if ep_idx < len(cloudcode_endpoints) - 1:
                                        next_ep = cloudcode_endpoints[ep_idx + 1]
                                        logger.info("[GoogleAntigravity] 端点 %s 遭遇 429 限流，已标记避让 %.0f 秒，自动无缝切换至下一端点: %s", ep, _QUOTA_429_COOLDOWN_SECONDS, next_ep)
                                    break
                                elif response.status_code in (500, 502, 503, 504):
                                    if ep_idx < len(cloudcode_endpoints) - 1:
                                        logger.info("[GoogleAntigravity] 端点 %s 繁忙 (%s)，尝试备用端点: %s", ep, response.status_code, cloudcode_endpoints[ep_idx + 1])
                                    break
                    except Exception as e:
                        err_str = str(e).lower()
                        is_transient = any(k in err_str for k in ("bad record mac", "forcibly closed", "connection reset", "remoteprotocolerror", "broken pipe"))
                        if is_transient and attempt < max_attempts - 1 and not streamed:
                            logger.info("[GoogleAntigravity] 端点 %s 遭遇瞬时网络/TLS连接抖动 (%s)，1秒后自动就地重试 (第 %d/%d 次)...", ep, e, attempt + 2, max_attempts)
                            await asyncio.sleep(1.0)
                            continue
                        logger.debug("[GoogleAntigravity] 端点 %s 连接失败: %s", ep, e)
                        last_error_msg = str(e)
                        _ENDPOINT_COOLDOWNS[ep] = time.time() + _DEFAULT_COOLDOWN_SECONDS
                        if ep_idx < len(cloudcode_endpoints) - 1:
                            next_ep = cloudcode_endpoints[ep_idx + 1]
                            logger.info("[GoogleAntigravity] 端点 %s 连接异常 (%s)，临时避让并切换至下一端点: %s", ep, e, next_ep)
                        break

        if not streamed:
            if last_status_code in (500, 502, 503, 504) or "capacity" in (last_error_msg or "").lower():
                yield {
                    "type": "error",
                    "error": (
                        f"【Google 算力账号】Google 官方服务节点暂时容量超载 ({last_status_code or 503}: {last_error_msg})。"
                        f"\n\n[提示] Google 服务器当前该模型算力负荷超载 (MODEL_CAPACITY_EXHAUSTED)。"
                        f"\n由于该模型属于最新实验性预览版本，建议稍等 1-2 分钟重试，或直接切换为全天候高可用的 Claude Sonnet 4.6 (claude-sonnet-4-6) 或 Gemini 2.5 (gemini-2.5-flash) 继续对话！"
                    ),
                }
                return
            elif had_429:
                yield {
                    "type": "error",
                    "error": (
                        f"【Google 算力账号】当前 Google 账号每日免费配额已用尽 (429: {last_429_msg or 'Resource has been exhausted'})。"
                        f"\n\n[原因说明] Google Cloud Code 给每个个人账号分配了滚动周期免费配额。当配额耗尽时，上游会对该账号的所有模型请求拦截并返回 429。"
                        f"\n\n[解决建议]："
                        f"\n1. 前往 [设置 → 算力账号] 添加新的 Google 账号或切换到其他账号使用；"
                        f"\n2. 等待 Google 每日配额自动重置；"
                        f"\n3. 切换为系统中的其他第三方/本地模型渠道继续对话。"
                    ),
                }
                return
            yield {
                "type": "error",
                "error": f"【Google 算力账号】请求上游失败: {last_error_msg or '无法连接至 Google Antigravity 服务'}",
            }
