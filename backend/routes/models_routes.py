"""模型管理 API（/models/*）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
所有 handler 均在函数体内惰性 import（providers.store / liuruyan.llm /
anthropic SDK 等），不反向依赖 main.py 模块级全局，无循环 import 风险。

端点：
- GET  /models                       主聊天页模型目录（providers store 平铺）
- PUT  /models/prompt-caching        全局 Anthropic Prompt Caching 开关
- GET  /models/anthropic-presets     Anthropic 预设模型列表
- POST /models/test-connection       轻量连接检测（+ 中继转接探测）
- POST /models/test-connection-deep  深度检测（prompt cache 探针，仅 anthropic）
- POST /models/fetch                 拉取远端可用模型列表
- PUT  /models/current               切换当前模型 + 同步 LLMClient
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["models"])


@router.get("/models")
async def list_models():
    """主聊天页用的模型目录。从 providers store 直接平铺。"""
    from backend.providers.store import chat_model_catalog_view

    flat = chat_model_catalog_view()
    pro_list = []

    # 动态并入 Pro 算力账号虚拟通道模型（优先排在顶部，便于用户快速选择）
    try:
        from backend.pro.compute_accounts.models_provider import get_pro_providers
        for vp in get_pro_providers():
            for m in vp.get("models", []):
                pro_list.append({
                    "id": m["id"],
                    "name": m.get("name", m["id"]),
                    "base_url": vp.get("base_url", ""),
                    "api_key": vp.get("api_key", ""),
                    "provider": vp.get("id"),
                    "provider_name": vp.get("name") or vp.get("id"),
                    "transport": vp.get("transport", "google-antigravity"),
                    "tags": ["chat", "vision", "tools"],
                    "context_length": m.get("context_window", 1048576),
                    "context_length_source": "configured",
                })
    except Exception:
        pass

    custom_list = pro_list + list(flat["custom_models"])

    custom_models_display = [
        {
            "id": cm["id"],
            "name": cm.get("name", cm["id"]),
            "icon": "⚡" if cm.get("provider") == "google-antigravity" else "⊕",
            "description": "",
            "is_custom": True,
            "provider": cm["provider"],
            "provider_name": cm.get("provider_name") or cm["provider"],
            "transport": cm.get("transport", "openai_chat"),
            # 后端权威 tags / context_length（chat_model_catalog_view 已 infer 兜底过）
            # 让前端 vision 拦截 / 能力胶囊直接读，不再各自维护一套正则
            "tags": cm.get("tags") or [],
            "context_length": cm.get("context_length"),
            "context_length_source": cm.get("context_length_source"),
        }
        for cm in custom_list
    ]
    return {
        "custom_models_display": custom_models_display,
        "current_model": flat["current_model"],
        "current_provider": flat["current_provider"],
        "custom_models": custom_list,
        "prompt_caching_enabled": flat["prompt_caching_enabled"],
    }


@router.put("/models/prompt-caching")
async def set_prompt_caching(body: dict):
    """全局 Anthropic Prompt Caching 开关。"""
    enabled = bool(body.get("enabled", True))
    from backend.runtime_model_config import _set_prompt_caching_enabled
    saved = _set_prompt_caching_enabled(enabled)
    return {"status": "success", "prompt_caching_enabled": saved}


@router.get("/models/anthropic-presets")
async def get_anthropic_presets():
    """获取 Anthropic 预设模型列表"""
    from backend.anthropic_client import ANTHROPIC_MODELS
    return {"models": ANTHROPIC_MODELS}


class TestConnectionRequest(BaseModel):
    provider: str = "openai"
    base_url: Optional[str] = None
    api_key: str
    model: str
    # 可选：caller 显式带 extra_headers（前端 ProviderSettings 改成 anthropic_messages
    # 时把 provider 字段写成 'anthropic' 字面量，按 id 反查会拿到内置 anthropic
    # 而不是 custom-getrouter，store lookup 会丢用户配的 Cherry-style headers。
    # 显式字段优先于 id lookup。
    extra_headers: Optional[Dict[str, str]] = None
    # transport 协议名（'anthropic_messages' / 'openai_compatible' / ...）。
    # relay_detection 用它决定启用哪些响应形态检测。前端 ProviderSettings 直接
    # 透传 provider.transport；老 caller 不传时按 provider 字段推断
    # （provider=='anthropic' → anthropic_messages，否则 openai_compatible）。
    transport: Optional[str] = None


@router.post("/models/test-connection")
async def test_model_connection(request: TestConnectionRequest):
    """测试模型连接是否有效。

    v2 B 迁移：从直调 SDK 切到 ApiClient.send_completion（registry 单例
    + plugin 链；max_tokens=1 只测连通不烧 token）。
    """
    try:
        from liuruyan.llm import (
            CoreRequest,
            ProviderNotFoundError,
            RequestContext,
            get_default_registry,
        )

        # 从 store 读 ProviderConfig.extra_headers——
        # getrouter / openrouter 等网关靠 User-Agent / HTTP-Referer 鉴别合法
        # 客户端，缺则返 "Your request was blocked"。chat 主链路一直透传，
        # 检测路由历史上漏了。
        # extra_headers 解析三档：
        # 1. caller 显式传 request.extra_headers（前端按 provider.extra_headers 直接发）
        # 2. store 按 request.provider 反查（兼容老前端 / 命令行 caller）
        # 3. 都没有 → None，裸 SDK 默认 UA 出去（getrouter 等会拦）
        provider_extra_headers: Optional[Dict[str, str]] = (
            dict(request.extra_headers) if request.extra_headers else None
        )
        if provider_extra_headers is None:
            try:
                from backend.providers.store import get_provider as _get_provider_cfg
                _pc = _get_provider_cfg(request.provider) if request.provider else None
                if _pc is not None and _pc.extra_headers:
                    provider_extra_headers = dict(_pc.extra_headers)
            except Exception as _e:
                logger.warning("[TEST-CONN] 读 ProviderConfig.extra_headers 失败（忽略）: %s", _e)

        if request.provider == "anthropic":
            from liuruyan.llm.clients.anthropic import AnthropicApiClient
            api_client = AnthropicApiClient(
                api_key=request.api_key,
                model=request.model,
                base_url=request.base_url,
                default_headers=provider_extra_headers,
            )
        else:
            # OpenAI 兼容：先走 registry（builtin / 已注册 custom）；查不到时
            # （用户通过 /providers 加的 opencode 等自定义 provider，store 跟
            # registry 没双向同步）→ 直接构造 OpenAICompatibleApiClient，让
            # 检测能拿 api_key + base_url 真发请求验证连通性。
            factory_kwargs: Dict[str, Any] = {}
            if provider_extra_headers:
                factory_kwargs["default_headers"] = provider_extra_headers
            try:
                api_client = get_default_registry().get_client(
                    request.provider or "openai",
                    request.model,
                    api_key=request.api_key,
                    base_url_override=request.base_url,
                    **factory_kwargs,
                )
            except ProviderNotFoundError:
                from liuruyan.llm.clients.openai_compat import OpenAICompatibleApiClient
                api_client = OpenAICompatibleApiClient(
                    api_key=request.api_key,
                    model=request.model,
                    base_url=request.base_url,
                    default_headers=provider_extra_headers,
                )

        # max_tokens=16：cherry-studio 同档，必须给 reasoning 模型（gpt-5/claude
        # reasoning/deepseek-r1）留 reasoning token 空间。设 1 时 OpenRouter 转发到
        # 上游 reasoning provider 会被拒绝返 500（"max_tokens must be >= reasoning_budget"）。
        # 16 足够触发响应又几乎不烧 token（约 0.0001 美分/次）。
        result = await api_client.send_completion(
            CoreRequest(
                messages=[{"role": "user", "content": "hi"}],
                system=None,
                tools=None,
                model=request.model,
                max_tokens=16,
            ),
            RequestContext(),
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"send_completion returned invalid result: {type(result).__name__}")
        identified_model = result.get("model")

        # ── 中继转接探测（A + B）────────────────────────────────────────
        # transport：前端 ProviderSettings 显式传 provider.transport；老 caller
        # 没传时按 provider 字段推断（仅 anthropic / 非 anthropic 二分够用）。
        from backend.providers.relay_detection import build_relay_diagnostics
        effective_transport = request.transport or (
            "anthropic_messages" if request.provider == "anthropic" else "openai_compatible"
        )
        diagnostics = build_relay_diagnostics(
            base_url=request.base_url,
            transport=effective_transport,
            usage=result.get("usage") if isinstance(result, dict) else None,
            raw_message=result.get("_raw_response") if isinstance(result, dict) else None,
        )

        return {
            "success": True,
            "message": "连接成功",
            "identified_model": identified_model,
            "relay_diagnostics": diagnostics.as_dict(),
        }
    except Exception as e:
        # OpenAI / Anthropic SDK 1.x 的 APIStatusError 子类把详情放在 .body / .message
        # 而非 str(e)。str(e) 只返简短 status text（比如 "Internal Server Error"），
        # OpenRouter / 中继网关的真实错误（"Provider returned error", upstream raw 等）
        # 全在 .body 里。这里把它提取出来拼到 error_msg。
        base_msg = str(e) or e.__class__.__name__
        body_detail = ""
        try:
            body = getattr(e, "body", None)
            if isinstance(body, dict):
                # OpenRouter 标准错误体：{"error": {"message": "...", "metadata": {...}}}
                err = body.get("error") if isinstance(body.get("error"), dict) else None
                if err:
                    parts = []
                    if err.get("message"):
                        parts.append(str(err["message"]))
                    meta = err.get("metadata")
                    if isinstance(meta, dict):
                        if meta.get("raw"):
                            parts.append(f"raw={meta['raw']}")
                        if meta.get("provider_name"):
                            parts.append(f"provider={meta['provider_name']}")
                    body_detail = " | ".join(parts)
                else:
                    # 非标准 OpenRouter 形态：直接 str dict 截断
                    body_detail = str(body)[:300]
            elif isinstance(body, str) and body:
                body_detail = body[:300]
            # Anthropic SDK：APIError.message 可能比 str(e) 详细
            sdk_message = getattr(e, "message", None)
            if sdk_message and sdk_message != base_msg and not body_detail:
                body_detail = str(sdk_message)[:300]
        except Exception as _exc_dig:  # noqa: BLE001
            logger.warning("[TEST-CONN] 解析 SDK 异常 .body 失败: %s", _exc_dig)

        error_msg = f"{base_msg}: {body_detail}" if body_detail else base_msg
        lower = error_msg.lower()
        # 简化错误信息：分类提示，不能识别的把上游原文截断 300 字符给用户排查。
        if "authentication" in lower or ("invalid" in lower and "api" in lower):
            return {"success": False, "message": "API Key 无效"}
        elif "not_found" in lower or "does not exist" in lower or "model_not_found" in lower:
            return {"success": False, "message": "模型不存在或无权访问"}
        elif "rate" in lower or "429" in error_msg:
            return {"success": False, "message": "请求频率超限，请稍后重试"}
        elif "context_length_exceeded" in lower or "context length" in lower:
            return {"success": False, "message": "context 超限（探测请求理论极小，可能上游路由异常）"}
        elif "internal server error" in lower or "500" in error_msg:
            # OpenRouter / 中继网关常见：上游 provider 拒绝 max_tokens 太小、限流、临时不可用。
            # 把上游原文给用户——OpenRouter 会塞 "Provider returned error: <真实原因>" 在里面。
            return {
                "success": False,
                "message": (
                    f"上游服务 500（多为路由/上游 provider 不可用、reasoning 模型 "
                    f"max_tokens 不足、模型暂时下线）: {error_msg[:300]}"
                ),
            }
        elif "502" in error_msg or "503" in error_msg or "504" in error_msg or "bad gateway" in lower or "gateway timeout" in lower:
            return {"success": False, "message": f"网关错误（中继/反代不稳定）: {error_msg[:200]}"}
        elif "timeout" in lower or "timed out" in lower:
            return {"success": False, "message": "请求超时（网络或上游响应慢）"}
        elif "permission" in lower or "blocked" in lower or "403" in error_msg:
            return {
                "success": False,
                "message": (
                    f"被拒绝（403/权限不足，常见于 getrouter/openrouter 缺 User-Agent / "
                    f"HTTP-Referer headers）: {error_msg[:200]}"
                ),
            }
        else:
            return {"success": False, "message": f"连接失败: {error_msg[:300]}"}


# ── 深度连接检测（prompt cache 探针，仅 anthropic_messages） ─────────────


# 长 system prompt：必须 ≥ 2048 token 才能保证所有 claude 模型都触发 cache
# （haiku 门槛 2048，sonnet/opus 门槛 1024；保险用 2048+）。
# 内容是无害的"测试角色"描述 + 大段 lorem ipsum 填料；填料用一段独特 marker
# 避免被某些反代缓存层误判为已知 prompt。
_DEEP_PROBE_SYSTEM = (
    "You are a connection diagnostic helper for a developer tool. "
    "Your job is to confirm the underlying model is Anthropic Claude by responding "
    "to a short ping. Reply concisely with 'ok'. "
    "The following filler is intentionally long to exceed the prompt cache threshold:\n\n"
    + (
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
        "Curabitur eget mi vehicula, blandit risus eu, fermentum est. "
        "Vestibulum ante ipsum primis in faucibus orci luctus et ultrices "
        "posuere cubilia curae; Aliquam erat volutpat. Nullam molestie "
        "lobortis nisi, vitae sagittis lacus dignissim sit amet. "
    ) * 24
    + "\n\n[liuruyan-relay-probe-marker-2026-05-12]\n"
)
_DEEP_PROBE_USER = "ping"


@router.post("/models/test-connection-deep")
async def test_model_connection_deep(request: TestConnectionRequest):
    """深度连接检测：prompt cache 探针确认底层是真 Anthropic。

    仅对 transport='anthropic_messages' 有意义。机制：
      1. 构造 ≥2048 token 的固定 system prompt + cache_control:{type:'ephemeral'}
      2. 发两次完全相同的请求（max_tokens=8 控制成本）
      3. 真 Anthropic：第一次 cache_creation_input_tokens > 0，第二次
         cache_read_input_tokens > 0（cache 命中）
      4. OpenAI 反代或其他后端伪装：要么字段为 0，要么字段不存在 → 决定性证据

    返回：
      {success, verdict, confidence, cache_creation_input_tokens, cache_read_input_tokens,
       message, signals[]}

    成本：两次 max_tokens=8 的请求，约 ~5 秒。比轻检测贵但能给决定性结论。
    """
    from backend.providers.relay_detection import RelaySignal, synthesize_verdict

    effective_transport = request.transport or (
        "anthropic_messages" if request.provider == "anthropic" else "openai_compatible"
    )

    if effective_transport != "anthropic_messages":
        # OpenAI 兼容协议没有 prompt cache 标准，深度探针不适用。
        return {
            "success": False,
            "applicable": False,
            "message": "深度检测目前仅支持 anthropic_messages 协议（prompt cache 探针）",
        }

    # extra_headers 解析（同 test_model_connection 三档兜底逻辑）
    provider_extra_headers: Optional[Dict[str, str]] = (
        dict(request.extra_headers) if request.extra_headers else None
    )
    if provider_extra_headers is None:
        try:
            from backend.providers.store import get_provider as _get_provider_cfg
            _pc = _get_provider_cfg(request.provider) if request.provider else None
            if _pc is not None and _pc.extra_headers:
                provider_extra_headers = dict(_pc.extra_headers)
        except Exception as _e:
            logger.warning("[DEEP-PROBE] 读 ProviderConfig.extra_headers 失败（忽略）: %s", _e)

    try:
        # 直接构造 anthropic SDK client，绕过 outgoing plugin 链：
        # 探针需要严格控制请求形态（cache_control 字段、max_tokens、system 内容），
        # 让 plugin 改写会污染探针结果。
        from anthropic import AsyncAnthropic
        from backend.utils.llm_headers import merge_default_llm_headers

        # 注入项目级默认 headers（防 getrouter / openrouter 反爬），caller 显式
        # 传的 provider_extra_headers 会 override 同 key 项。
        merged_headers = merge_default_llm_headers(provider_extra_headers)

        client_kwargs: Dict[str, Any] = {
            "api_key": request.api_key,
            "default_headers": merged_headers,
        }
        if request.base_url:
            client_kwargs["base_url"] = request.base_url

        client = AsyncAnthropic(**client_kwargs)

        async def _probe() -> Any:
            return await client.messages.create(
                model=request.model,
                max_tokens=8,
                system=[
                    {
                        "type": "text",
                        "text": _DEEP_PROBE_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": _DEEP_PROBE_USER}],
            )

        # 第一次请求：建 cache
        resp1 = await _probe()
        # 第二次请求：测 cache 命中
        # 短间隔（cache TTL 是 5 分钟，0.5s 足够）
        import asyncio
        await asyncio.sleep(0.5)
        resp2 = await _probe()

        usage1 = getattr(resp1, "usage", None)
        usage2 = getattr(resp2, "usage", None)

        cache_create = (
            getattr(usage1, "cache_creation_input_tokens", None)
            if usage1 is not None else None
        )
        cache_read = (
            getattr(usage2, "cache_read_input_tokens", None)
            if usage2 is not None else None
        )

        signals: List[RelaySignal] = []

        if cache_read is None and cache_create is None:
            signals.append(RelaySignal(
                level="critical",
                name="cache_fields_missing",
                detail=f"两次请求响应都缺失 cache_creation_input_tokens / "
                       f"cache_read_input_tokens 字段。真 Anthropic SDK 必返这两个字段（"
                       f"即便 cache miss 也返 0）。字段缺失 → 后端不是真 Anthropic 协议实现。",
            ))
        elif (cache_read or 0) == 0 and (cache_create or 0) == 0:
            signals.append(RelaySignal(
                level="critical",
                name="cache_never_built",
                detail=f"两次相同长 prompt（≥2048 token + cache_control）的请求，cache_creation_input_tokens "
                       f"和 cache_read_input_tokens 都为 0。真 Anthropic 必建 cache + 第二次必命中。"
                       f"完全无 cache 行为 → 底层不是 Anthropic（极可能是 OpenAI 或其他后端反代）。",
            ))
        elif (cache_read or 0) == 0 and (cache_create or 0) > 0:
            signals.append(RelaySignal(
                level="warning",
                name="cache_created_but_no_hit",
                detail=f"第一次请求建 cache 成功（cache_creation_input_tokens={cache_create}），"
                       f"但第二次未命中（cache_read_input_tokens=0）。可能是 cache 配置异常 / 反代部分实现。",
            ))
        # else: cache_read > 0 → 命中，真 Anthropic，无 signal

        verdict, confidence = synthesize_verdict(signals)

        return {
            "success": True,
            "applicable": True,
            "verdict": verdict,
            "confidence": confidence,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
            "signals": [s.as_dict() for s in signals],
            "message": (
                "深度检测通过：底层确认为真 Anthropic（prompt cache 命中）"
                if verdict == "native"
                else "深度检测发现可疑信号，详见 signals"
            ),
        }

    except Exception as e:
        error_msg = str(e)
        return {
            "success": False,
            "applicable": True,
            "message": f"深度检测异常: {error_msg[:300]}",
        }


@router.post("/models/fetch")
async def fetch_remote_models(body: dict):
    """根据 base_url + api_key + provider 自动拉取可用模型列表"""
    provider = body.get("provider", "openai")
    base_url = body.get("base_url", "")
    api_key = body.get("api_key", "")

    if not api_key:
        raise HTTPException(status_code=400, detail="缺少 api_key")

    if provider == "anthropic":
        # Anthropic 没有模型列表 API，返回预设
        from backend.anthropic_client import ANTHROPIC_MODELS
        return {"provider": "anthropic", "models": ANTHROPIC_MODELS, "source": "preset"}

    # OpenAI 兼容：调 /v1/models
    try:
        from openai import OpenAI
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = OpenAI(**client_kwargs)
        models_page = client.models.list()
        models = []
        for m in models_page:
            models.append({
                "id": m.id,
                "name": getattr(m, "name", None) or m.id,
                "owned_by": getattr(m, "owned_by", ""),
            })
        # 按id排序
        models.sort(key=lambda x: x["id"])
        return {"provider": "openai", "models": models, "source": "api"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取模型列表失败: {str(e)[:200]}")


@router.put("/models/current")
async def set_current_model(body: dict):
    """切换当前使用的模型。

    从 providers store 查 (model_id, provider_id) 是否存在，存在则写回 current_*
    并同步 LLMClient。添加/删除模型请走 /providers/{id} 路由。
    """
    from backend.providers.store import (
        find_model,
        load_providers_config,
        save_providers_config,
    )

    model_id = body.get("model_id")
    provider = body.get("provider", "")
    if not model_id:
        raise HTTPException(status_code=400, detail="缺少 model_id 参数")

    config = load_providers_config()
    pc = find_model(config, model_id, provider or None)

    # 兼容 Pro 算力账号虚拟通道
    is_pro = False
    if pc is None:
        try:
            from backend.pro.compute_accounts.models_provider import is_pro_provider, get_pro_providers
            if is_pro_provider(provider):
                is_pro = True
            else:
                for vp in get_pro_providers():
                    if any(m["id"] == model_id for m in vp.get("models", [])):
                        is_pro = True
                        provider = vp.get("id", "google-antigravity")
                        break
        except Exception:
            pass

    if pc is None and not is_pro:
        raise HTTPException(
            status_code=404,
            detail=f"模型 '{model_id}'" + (f" (provider: {provider})" if provider else "") + " 不在可用列表中",
        )

    effective_provider = pc.id if pc else (provider or "google-antigravity")
    config["current_model"] = model_id
    config["current_provider"] = effective_provider
    save_providers_config(config)

    from backend.llm_client import get_llm_client
    llm = get_llm_client()
    if hasattr(llm, "set_model"):
        if pc:
            provider_key = pc.transport.replace("_chat", "").replace("_messages", "")
            llm.set_model(
                model_id,
                base_url=pc.base_url,
                api_key=pc.api_key,
                provider=provider_key,
                default_headers=pc.extra_headers,
            )
        else:
            llm.set_model(
                model_id,
                base_url="https://generativelanguage.googleapis.com",
                api_key="PRO_ACCOUNT_MANAGED",
                provider="google-antigravity",
            )

    return {"status": "success", "current_model": model_id, "provider": effective_provider}
