"""单次 LLM 调用的不可变模型配置（per-agent model routing 阶段 1 Commit 1.2）。

为什么需要这个模块：
- 老路径 `_configure_runtime_model` 通过 `LLMClient.set_model()` 改全局单例字段——
  并发会话 A/B 同时切不同模型时互相覆盖，是当前并发串扰的根因。
- 新路径：每次调用解析出一个 frozen `CallModelConfig` 快照，往下传给底层 registry，
  全程不写任何单例字段，并发安全。

字段语义：
- `provider_id`：providers store 里的 ProviderConfig.id（如 "deepseek" / "openrouter"）
- `transport`：底层传输协议（"openai_chat" / "anthropic_messages"）
- `provider_kind`（property）：底层 ApiClient registry 期望的简短 key
  （"openai" / "anthropic"）—— 由 transport 派生，对齐老代码 `_configure_runtime_model:64`

调用方契约：
- 只读，不要修改返回的实例字段（dataclass frozen 已强制）
- 拿到后直接传给 `registry.get_client(...)` 或 `llm_client.chat(call_config=...)`
- 阶段 1 还没人调；阶段 3 把六个调用点切到这个函数（参见 handover 文档 §5）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .agent_config import get_agent_active_model
from .providers.capabilities import infer_capabilities, infer_context_length, resolve_model_capabilities
from .providers.store import find_model, load_providers_config_cached

logger = logging.getLogger(__name__)


HeadersTuple = Optional[Tuple[Tuple[str, str], ...]]


def _freeze_headers(headers: Optional[Dict[str, str]]) -> HeadersTuple:
    """dict → 排序 tuple-of-tuples，用于让 CallModelConfig frozen 且 hashable。

    排序保证两次解析得到同 headers 时 hash 一致，便于上层做 cache key。
    """
    if not headers:
        return None
    return tuple(sorted((str(k), str(v)) for k, v in headers.items()))


@dataclass(frozen=True)
class CallModelConfig:
    """单次 LLM 调用的不可变模型配置快照。

    frozen=True 强制不可变；extra_headers 用 tuple-of-tuples 保证整个 dataclass
    hashable，方便上层（如未来的请求级 cache）拿来做 key。
    """

    provider_id: str
    """providers store 里的 ProviderConfig.id（'deepseek'/'openai'/'openrouter'...）"""

    model: str
    """模型 id"""

    api_key: str
    """API key；空字符串 '' 合法（无 key provider，如 ollama / lmstudio）。

    ⚠️ 即便为空也必须显式保留 ''，**不要**让下游 fallback 读 env 里的
    OPENAI_API_KEY——这是 provider_registry.py:132-140 修过的坑。"""

    base_url: str
    """API base url，已应用 freeze_url 规则"""

    transport: str
    """'openai_chat' / 'anthropic_messages'"""

    extra_headers: HeadersTuple = None
    """provider 自定义 headers（已 frozen 为 sorted tuple-of-tuples）"""

    context_length: Optional[int] = None
    """当前模型的有效上下文窗口。显式配置优先，否则按官方模型目录推断。"""

    tags: Optional[Tuple[str, ...]] = None
    """模型能力标签快照（优先来自模型配置/算力通道权威 tags，缺失时由 infer_capabilities 兜底）。"""

    @property
    def provider_kind(self) -> str:
        """底层 ApiClient registry 期望的简短 key（'openai' / 'anthropic'）。

        由 transport 派生，对齐老 `_configure_runtime_model:64` 的转换规则。
        """
        return self.transport.replace("_chat", "").replace("_messages", "")

    def headers_dict(self) -> Optional[Dict[str, str]]:
        """把冻结 headers 还原为普通 dict，给下游需要 dict 的 API 用。"""
        if self.extra_headers is None:
            return None
        return dict(self.extra_headers)


class ModelResolutionError(Exception):
    """无法解析到任何可用 model 配置。

    raise 场景：
    - 所有层级（override / session / global）都没给出 model id
    - 解析到的 model id 不在任何 provider 下（让漏网的 model id 早暴露而非
      静默走环境变量兜底）
    """


def _build_pro_call_config(
    model: str,
    provider_id: str,
    matched_pro_model: Dict[str, Any],
) -> CallModelConfig:
    pro_tags = tuple(
        resolve_model_capabilities(
            model,
            provider_id=provider_id,
            explicit_tags=matched_pro_model.get("tags"),
            name=matched_pro_model.get("name"),
        )
    )
    pro_ctx = (
        matched_pro_model.get("context_window")
        or matched_pro_model.get("context_length")
        or infer_context_length(model)
        or 1048576
    )
    return CallModelConfig(
        provider_id=provider_id,
        model=model,
        api_key="PRO_ACCOUNT_MANAGED",
        base_url="https://generativelanguage.googleapis.com",
        transport="openai_chat",
        context_length=pro_ctx,
        tags=pro_tags,
    )


def _match_pro_model(
    pro_providers: list,
    model: str,
    provider_hint: Optional[str] = None,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    if not pro_providers:
        return None
    if provider_hint:
        pid_l = provider_hint.lower()
        for vp in pro_providers:
            vp_id = vp.get("id") or ""
            if vp_id.lower() == pid_l or (pid_l == "google" and vp_id == "google-antigravity"):
                matched = next((m for m in vp.get("models", []) if m.get("id") == model), None)
                if matched is not None:
                    return provider_hint, matched
        return None
    for vp in pro_providers:
        matched = next((m for m in vp.get("models", []) if m.get("id") == model), None)
        if matched is not None:
            return vp.get("id", "google-antigravity"), matched
    return None


def _build_standard_call_config(pc, model: str) -> CallModelConfig:
    model_entry = next((m for m in pc.models if m.id == model), None)
    configured_context = getattr(model_entry, "context_length", None) if model_entry else None
    configured_tags = getattr(model_entry, "tags", None) if model_entry else None
    model_name = getattr(model_entry, "name", None) if model_entry else None
    effective_tags = (
        tuple(configured_tags)
        if configured_tags
        else tuple(infer_capabilities(model, model_name))
    )

    return CallModelConfig(
        provider_id=pc.id,
        model=model,
        api_key=pc.api_key or "",
        base_url=pc.base_url or "",
        transport=pc.transport,
        extra_headers=_freeze_headers(pc.extra_headers),
        context_length=configured_context or infer_context_length(model),
        tags=effective_tags,
    )


def _resolve_candidate(
    config: Dict[str, Any],
    pro_providers: list,
    is_pro_fn,
    model: str,
    own_provider_hint: Optional[str],
    fallback_provider_hint: Optional[str],
    strict_provider: bool = False,
) -> Optional[CallModelConfig]:
    """尝试解析单层 (model, own_provider_hint)。

    - strict_provider=True（override 同时传了 override_provider）：严格按指定 provider 匹配，
      不放宽到其他 provider。
    - own_provider_hint 非空（session.provider / agent.active_model.provider_id 明确指定了某家）：
      只在该 provider 下匹配（不静默跳去另一家同名模型）；找不到返回 None，由上层决定报错或回退。
    - own_provider_hint 为空（老数据或未指定 provider）：先用 fallback_provider_hint 试探，
      再放宽扫描算力账号与全部普通 provider。
    """
    if strict_provider and own_provider_hint:
        if is_pro_fn and is_pro_fn(own_provider_hint):
            hit = _match_pro_model(pro_providers, model, own_provider_hint)
            if hit is not None:
                return _build_pro_call_config(model, hit[0], hit[1])
            return None
        pc = find_model(config, model, own_provider_hint)
        if pc is None:
            return None
        return _build_standard_call_config(pc, model)

    if own_provider_hint:
        if is_pro_fn and is_pro_fn(own_provider_hint):
            hit = _match_pro_model(pro_providers, model, own_provider_hint)
            if hit is not None:
                return _build_pro_call_config(model, hit[0], hit[1])
            return None
        pc = find_model(config, model, own_provider_hint)
        if pc is not None:
            return _build_standard_call_config(pc, model)
        return None

    if fallback_provider_hint:
        if is_pro_fn and is_pro_fn(fallback_provider_hint):
            hit = _match_pro_model(pro_providers, model, fallback_provider_hint)
            if hit is not None:
                return _build_pro_call_config(model, hit[0], hit[1])
        else:
            pc = find_model(config, model, fallback_provider_hint)
            if pc is not None:
                return _build_standard_call_config(pc, model)

    hit = _match_pro_model(pro_providers, model, None)
    if hit is not None:
        return _build_pro_call_config(model, hit[0], hit[1])

    pc = find_model(config, model, None)
    if pc is not None:
        return _build_standard_call_config(pc, model)
    return None


def resolve_call_model_config(
    agent_id: Optional[str] = None,
    override: Optional[str] = None,
    session: Any = None,
    override_provider: Optional[str] = None,
) -> CallModelConfig:
    """解析单次 LLM 调用的 model 配置（不写任何全局/单例字段，并发安全）。

    优先级（从高到低）：
      1. `override` —— 显式 model id（消息级临时切换；找不到直接 raise，不静默回退）
      2. `session.model` —— 会话级（来自 session.json 持久化字段；若已失效则回退下一级）
      3. `agent_config.active_model.model` —— 角色级默认（若已失效/禁用则回退到全局）
      4. `providers.current_model` —— 全局默认

    Args:
        agent_id: 角色 id
        override: 单次 override 的 model id
        session: 任意带 `.model` 属性的对象（一般是 Session 实例）
        override_provider: 单次 override 的 provider id（前端 picker 切的 provider）。
            **严格模式 hint**：当 `override` 与 `override_provider` 同时给出时，
            会按 (override, override_provider) 联合定位 provider，找不到直接 raise，
            **不放宽**到全 provider 扫描——避免"同一个 model id 跨多 provider 时
            静默走错家"。该参数为 None 时退回老的全局 hint + 放宽 fallback 行为。

    Returns:
        frozen CallModelConfig

    Raises:
        ModelResolutionError: 解析失败（详见 class docstring）；严格模式下找不到
            (override, override_provider) 组合也会 raise。
    """
    config = load_providers_config_cached()

    session_model = getattr(session, "model", None) if session is not None else None
    session_provider_hint = getattr(session, "provider", None) if session is not None else None
    global_model = config.get("current_model") or None
    global_provider_hint = config.get("current_provider") or None

    # agent 级（Commit 1.3 接入）。返回的 dict 自带 provider_id 作为后续 find_model
    # 的 hint，比 global current_provider 更精准——避免"我给 agent 配了 deepseek 路
    # 上的 model，但因 global hint 是 openrouter 而被误命中"的问题。
    agent_active = get_agent_active_model(agent_id)
    agent_model = (agent_active or {}).get("model") or None
    agent_provider_hint = (agent_active or {}).get("provider_id") or None

    is_pro_fn = None
    pro_providers: list = []
    try:
        from backend.compute_accounts.models_provider import is_pro_provider, get_pro_providers
        is_pro_fn = is_pro_provider
        pro_providers = get_pro_providers()
    except Exception:
        pass

    # 1. override（显式请求级）：找不到直接报错，绝不静默降级
    if override:
        strict_mode = bool(override_provider)
        resolved = _resolve_candidate(
            config,
            pro_providers,
            is_pro_fn,
            override,
            own_provider_hint=override_provider or None,
            fallback_provider_hint=global_provider_hint,
            strict_provider=strict_mode,
        )
        if resolved is not None:
            return resolved
        if strict_mode:
            raise ModelResolutionError(
                f"model {override!r} 不在 provider {override_provider!r} 下；"
                f"前端 picker 选了该 (model, provider) 组合但 providers store 找不到，"
                f"请检查 /providers 是否同步或换一个组合"
            )
        raise ModelResolutionError(
            f"model {override!r} 不在任何 provider 下；"
            f"请在 /providers 配置或换一个 model id"
        )

    # 2~4. session > agent > global：若上层持久化的模型已被禁用或移除，自动回退下一级
    candidates = [
        ("session", session_model, session_provider_hint),
        ("agent", agent_model, agent_provider_hint),
        ("global", global_model, global_provider_hint),
    ]
    first_non_empty_model: Optional[str] = None
    for layer, cand_model, cand_provider in candidates:
        if not cand_model:
            continue
        if first_non_empty_model is None:
            first_non_empty_model = cand_model
        resolved = _resolve_candidate(
            config,
            pro_providers,
            is_pro_fn,
            cand_model,
            own_provider_hint=cand_provider or None,
            fallback_provider_hint=None if layer == "global" else global_provider_hint,
            strict_provider=False,
        )
        if resolved is not None:
            return resolved
        logger.debug(
            "[LLMCallConfig] %s 层模型 (%r, provider=%r) 已失效或未启用，回退下一级",
            layer,
            cand_model,
            cand_provider,
        )

    if not first_non_empty_model:
        raise ModelResolutionError(
            "无法解析任何 model id（override / session.model / agent.active_model / "
            "global current_model 全为空）"
        )
    raise ModelResolutionError(
        f"model {first_non_empty_model!r} 不在任何 provider 下；"
        f"请在 /providers 配置或换一个 model id"
    )


__all__ = [
    "CallModelConfig",
    "ModelResolutionError",
    "resolve_call_model_config",
]
