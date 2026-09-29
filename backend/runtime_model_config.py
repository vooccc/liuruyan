"""运行时模型配置 helper。

只负责把 providers store 里 current_model + 所属 provider 的连接信息（base_url /
api_key / transport）应用到 agent.llm_client。所有持久化都走 providers/store.py。
"""
from typing import Optional

from .providers.store import (
    find_model,
    load_providers_config,
    save_providers_config,
)


def _get_current_model() -> str:
    return load_providers_config().get("current_model", "")


def _get_prompt_caching_enabled() -> bool:
    """Anthropic Prompt Caching 全局开关。默认 ON——5min ephemeral cache 命中省 90%。"""
    return bool(load_providers_config().get("prompt_caching_enabled", True))


def _set_prompt_caching_enabled(enabled: bool) -> bool:
    config = load_providers_config()
    config["prompt_caching_enabled"] = bool(enabled)
    save_providers_config(config)
    return config["prompt_caching_enabled"]


def _resolve_runtime_model_id(
    requested_model: Optional[str] = None,
    session=None,
    agent_id: Optional[str] = None,
) -> Optional[str]:
    """计算单次调用要用的 model id。

    优先级（与 resolve_call_model_config 保持一致）：
      1. requested_model（本次请求显式给的，对应 chat body.model / worker submit.model）
      2. session.model（会话级 override，仅"用户在该会话主动切过 picker"时存在）
      3. agent.active_model（角色级默认 —— **必须先于 global 兜底**）
      4. global current_model

    历史 bug：本函数过去缺少第 3 层，导致"用户没切 picker → effective_model 走
    global 默认 → 被 chat handler 写回 session.model 持久化 → 第一次发消息就把
    global 默认焊死进会话，agent.active_model 永远命中不到"。补 agent_id 参数 +
    agent_active fallback 修复。
    """
    if requested_model:
        return requested_model
    session_model = getattr(session, "model", None) if session else None
    if session_model:
        return session_model
    if agent_id:
        try:
            from .agent_config import get_agent_active_model
            agent_active = get_agent_active_model(agent_id)
            if agent_active and agent_active.get("model"):
                return agent_active["model"]
        except Exception:
            # agent meta 读失败时静默降级到 global，不阻断 chat 流程
            pass
    return _get_current_model()


def _configure_runtime_model(
    target, requested_model: Optional[str] = None
) -> Optional[str]:
    """把当前模型配置应用到 target.llm_client。

    从 providers store 找到 model 所在 provider，把 provider.base_url + api_key +
    transport 透传到 `llm_client.set_model`。如果模型不在任何 provider 下，退回
    `set_model(model_id)` 让 llm_client 走环境变量兜底。
    """
    active_model = requested_model or _get_current_model()
    if not active_model:
        return None

    llm = getattr(target, "llm_client", None)
    if llm is None:
        return None

    config = load_providers_config()
    current_provider_id = config.get("current_provider") or None
    pc = find_model(config, active_model, current_provider_id)
    if pc is None:
        llm.set_model(active_model)
        return active_model

    # transport（openai_chat / anthropic_messages）→ llm_client 期望的 provider key
    # （'openai' / 'anthropic'）
    provider_key = pc.transport.replace("_chat", "").replace("_messages", "")
    llm.set_model(
        active_model,
        base_url=pc.base_url,
        api_key=pc.api_key,
        provider=provider_key,
        default_headers=pc.extra_headers,
    )
    return active_model
