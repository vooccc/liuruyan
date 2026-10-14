"""虚拟 Provider 注册器：在不篡改 models.json 的前提下，
将已配置的算力账号模型动态注入至柳如烟模型选择器与调度链路。
"""
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from .manager import ComputeAccountsManager
from .services.google_antigravity import ANTIGRAVITY_43_MODELS

logger = logging.getLogger(__name__)


def get_pro_providers() -> List[Dict[str, Any]]:
    """获取所有已激活算力账号对应的虚拟 Provider 清单"""
    mgr = ComputeAccountsManager.get_instance()
    google_accounts = mgr.list_accounts("google")
    providers = []

    if google_accounts:
        enabled_models = mgr.get_enabled_models("google")
        active_models = mgr.get_active_models("google")
        model_entries = []
        for m in active_models:
            # 若用户在插件配置中自定义了展示模型，则严格只展示所选模型
            if enabled_models is not None and m["id"] not in enabled_models:
                continue
            tags = ["vision", "tools"]
            mid = m["id"].lower()
            mname = m.get("name", "").lower()
            if "thinking" in mid or "thinking" in mname or "reason" in mid or "high" in mid:
                tags.append("reasoning")
            model_entries.append({
                "id": m["id"],
                "name": m["name"],
                "tags": tags,
                "icon": "⚡",
                "added_by_user": False,
                "context_length": m.get("context_window", 1048576),
                "context_length_source": "configured",
                "context_window": m.get("context_window", 1048576),
                "vision": m.get("vision", True),
                "tools": m.get("tools", True),
                "provider": "google-antigravity",
                "pricing": None,
            })

        providers.append({
            "id": "google-antigravity",
            "name": "Google Antigravity",
            "display_name": "Google Antigravity (Pro 账号池)",
            "base_url": "https://generativelanguage.googleapis.com",
            "api_key": "PRO_ACCOUNT_MANAGED",
            "api_key_prefix": "",
            "transport": "google-antigravity",
            "require_api_key": False,
            "freeze_url": True,
            "enabled": True,
            "is_aggregator": False,
            "is_builtin": True,
            "is_virtual": True,
            "is_pro": True,
            "extra_headers": {},
            "description": "Google Antigravity Pro 算力账号池（支持官方 OAuth 自动刷新）",
            "icon": "⚡",
            "homepage": "https://cloud.google.com",
            "api_key_url": "",
            "has_api_key": True,
            "models": model_entries,
        })

    return providers


def is_pro_provider(provider_id: Optional[str]) -> bool:
    """判断是否为 Pro 算力账号虚拟通道"""
    if not provider_id:
        return False
    return provider_id.lower() in ("google", "google-antigravity", "grok", "codex")


async def stream_pro_chat(
    provider_id: str,
    model_id: str,
    messages: List[Dict[str, Any]],
    **kwargs: Any,
) -> AsyncGenerator[Dict[str, Any], None]:
    """向 Pro 账号池发起流式调用，自动处理 Token 与 429 故障转移"""
    mgr = ComputeAccountsManager.get_instance()
    target_provider = "google" if "google" in provider_id.lower() else provider_id

    adapter = mgr.get_adapter(target_provider)
    if not adapter:
        yield {"type": "error", "error": f"未找到提供商适配器: {provider_id}"}
        return

    access_token = await mgr.get_valid_access_token(target_provider)
    if not access_token:
        yield {"type": "error", "error": f"【Pro 算力账号】{adapter.display_name} 无可用已登录账号，请前往 [设置 → 算力账号] 添加账号。"}
        return

    try:
        async for chunk in adapter.stream_chat(model_id, messages, access_token=access_token, **kwargs):
            if chunk.get("type") == "error" and "429" in str(chunk.get("error")):
                # 限流处理并尝试轮询下一账号
                mgr.mark_cooling(target_provider, cooldown_seconds=300)
                next_token = await mgr.get_valid_access_token(target_provider)
                if next_token and next_token != access_token:
                    logger.info("[ComputeAccounts] 429 触发自动故障转移，正在使用新账号重试...")
                    async for retry_chunk in adapter.stream_chat(model_id, messages, access_token=next_token, **kwargs):
                        yield retry_chunk
                    return
                else:
                    # 单账号场景：等 2.5 秒后原地退避重试一次（Google 429 往往是短周期窗口限流）
                    logger.info("[ComputeAccounts] 单账号遭遇 429，等待 2.5s 原地退避重试...")
                    import asyncio
                    await asyncio.sleep(2.5)
                    success = False
                    async for retry_chunk in adapter.stream_chat(model_id, messages, access_token=access_token, **kwargs):
                        if retry_chunk.get("type") != "error":
                            success = True
                        yield retry_chunk
                    if success:
                        return
            yield chunk
    except Exception as e:
        logger.error("[ComputeAccounts] 流式调用异常: %s", e, exc_info=True)
        yield {"type": "error", "error": f"Pro 算力调用异常: {str(e)}"}
