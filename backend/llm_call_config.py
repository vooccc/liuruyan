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

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .agent_config import get_agent_active_model
from .providers.capabilities import infer_context_length
from .providers.store import find_model, load_providers_config_cached


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


def resolve_call_model_config(
    agent_id: Optional[str] = None,
    override: Optional[str] = None,
    session: Any = None,
    override_provider: Optional[str] = None,
) -> CallModelConfig:
    """解析单次 LLM 调用的 model 配置（不写任何全局/单例字段，并发安全）。

    优先级（从高到低）：
      1. `override` —— 显式 model id（消息级临时切换）
      2. `session.model` —— 会话级（来自 session.json 持久化字段）
      3. `agent_config.active_model.model` —— 角色级默认（**Commit 1.3 落地后启用**）
      4. `providers.current_model` —— 全局默认

    Args:
        agent_id: 角色 id；本 commit 暂未消费，保留参数位防止后续调用点反复改签名
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
    global_model = config.get("current_model") or None
    global_provider_hint = config.get("current_provider") or None

    # agent 级（Commit 1.3 接入）。返回的 dict 自带 provider_id 作为后续 find_model
    # 的 hint，比 global current_provider 更精准——避免"我给 agent 配了 deepseek 路
    # 上的 model，但因 global hint 是 openrouter 而被误命中"的问题。
    agent_active = get_agent_active_model(agent_id)
    agent_model = (agent_active or {}).get("model") or None
    agent_provider_hint = (agent_active or {}).get("provider_id") or None

    # 优先级：override > session > agent > global
    model = override or session_model or agent_model or global_model
    if not model:
        raise ModelResolutionError(
            "无法解析任何 model id（override / session.model / agent.active_model / "
            "global current_model 全为空）"
        )

    # 优先检查是否为算力账号虚拟通道（整目录删除时捕获异常自动跳过）
    try:
        from backend.compute_accounts.models_provider import is_pro_provider, get_pro_providers
        explicit_pro = (
            override_provider if is_pro_provider(override_provider)
            else (agent_provider_hint if (is_pro_provider(agent_provider_hint) and model == agent_model)
            else (global_provider_hint if (is_pro_provider(global_provider_hint) and model == global_model)
            else None))
        )
        if explicit_pro:
            return CallModelConfig(
                provider_id=explicit_pro,
                model=model,
                api_key="PRO_ACCOUNT_MANAGED",
                base_url="https://generativelanguage.googleapis.com",
                transport="openai_chat",
                context_length=1048576,
            )
        for vp in get_pro_providers():
            if any(m["id"] == model for m in vp.get("models", [])):
                return CallModelConfig(
                    provider_id=vp.get("id", "google-antigravity"),
                    model=model,
                    api_key="PRO_ACCOUNT_MANAGED",
                    base_url="https://generativelanguage.googleapis.com",
                    transport="openai_chat",
                    context_length=1048576,
                )
    except Exception:
        pass

    # 严格模式：override 同时带了 model 和 provider（前端 picker 显式切的二元组），
    # 必须按 (override, override_provider) 严格匹配，不放宽 fallback——这是为了

    # 解决"同一个 model id 在多个 provider 下都存在（比如 getrouter 和 llmapi 都有
    # claude-opus-4-7），仅靠 model id 反查会永远命中第一家"的 bug。
    # 找不到直接 raise，让前端立刻收到清晰错误而不是静默走错家。
    strict_mode = bool(override) and bool(override_provider) and model == override
    if strict_mode:
        pc = find_model(config, model, override_provider)
        if pc is None:
            raise ModelResolutionError(
                f"model {model!r} 不在 provider {override_provider!r} 下；"
                f"前端 picker 选了该 (model, provider) 组合但 providers store 找不到，"
                f"请检查 /providers 是否同步或换一个组合"
            )
    else:
        # 非严格路径（向后兼容）：跟着选中的 model 走那一层的 hint，否则降到全局
        if model == override:
            provider_hint = global_provider_hint
        elif model == session_model:
            provider_hint = global_provider_hint
        elif model == agent_model:
            provider_hint = agent_provider_hint or global_provider_hint
        else:
            provider_hint = global_provider_hint

        pc = find_model(config, model, provider_hint)
        if pc is None and provider_hint:
            # hint 限制下未命中 → 放宽到全 provider 扫描。
            # 场景：用户在前端选了 model 'X' 但当时 current_provider 不对，
            # 或 override 一个跨 provider 的 model 而 hint 仍是上一次的 provider。
            # 这里放宽避免假性"找不到"，实际 model 存在则照常工作。
            pc = find_model(config, model, None)
        if pc is None:
            raise ModelResolutionError(
                f"model {model!r} 不在任何 provider 下；"
                f"请在 /providers 配置或换一个 model id"
            )

    model_entry = next((m for m in pc.models if m.id == model), None)
    configured_context = getattr(model_entry, "context_length", None) if model_entry else None

    return CallModelConfig(
        provider_id=pc.id,
        model=model,
        api_key=pc.api_key or "",
        base_url=pc.base_url or "",
        transport=pc.transport,
        extra_headers=_freeze_headers(pc.extra_headers),
        context_length=configured_context or infer_context_length(model),
    )


__all__ = [
    "CallModelConfig",
    "ModelResolutionError",
    "resolve_call_model_config",
]
