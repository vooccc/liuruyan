"""模型路由：把客户端 `model` 字段映射到 (ProviderConfig, vendor_model_id)。

支持格式：
    <provider_id>:<vendor_model_id>     # 命名空间，唯一推荐方式
    <vendor_model_id>                    # 兜底：按 current_provider → 全局扫

设计：
- 只读消费 backend.providers.store.load_providers_config_cached()
- 仅返回 ResolveResult，不做任何 IO 副作用
- 找不到 provider 或 provider.enabled=false → ResolveError
- M1 + M2-A + M2-B 后：openai_chat / anthropic_messages 两者均可访问任一端点，
  routes 层根据实际 transport 选同协议透传或跨协议互转；其他 transport 返 501
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

from ..providers.schema import ProviderConfig
from ..providers.store import load_providers_config_cached
from .aliases import load_aliases_cached, split_candidate
from .health import get_cache as _get_health_cache


@dataclass
class ResolveResult:
    provider: ProviderConfig
    vendor_model: str
    """要发给上游的真实 model id（已剥掉 provider prefix）。"""


class ResolveError(Exception):
    """模型解析失败。`code` 用于 routes 层映射 HTTP status。"""

    def __init__(self, code: str, message: str, *, status: int = 404):
        super().__init__(message)
        self.code = code
        self.status = status


def _split_namespace(model: str) -> tuple[Optional[str], str]:
    """`provider:vendor_model` → (provider_id, vendor_model)；无冒号返回 (None, model)。

    注意 vendor_model 本身可能含冒号（如 `:free` 后缀），所以只 split 第一个。
    """
    if ":" in model:
        provider_id, rest = model.split(":", 1)
        # 排除 vendor 模型 id 本身就以 ":" 开头的边界（不太可能，但兜底）
        if provider_id and rest:
            return provider_id, rest
    return None, model


def resolve_model(model: str) -> ResolveResult:
    """主入口。raise ResolveError 表示找不到。"""
    if not model or not isinstance(model, str):
        raise ResolveError("model_required", "model field is required", status=400)

    cfg = load_providers_config_cached()
    providers = cfg["providers"]
    explicit_pid, vendor_model = _split_namespace(model)

    if explicit_pid:
        provider = providers.get(explicit_pid)
        if provider is None:
            raise ResolveError(
                "provider_not_found",
                f"provider '{explicit_pid}' not configured",
                status=404,
            )
        if not provider.enabled:
            raise ResolveError(
                "provider_disabled",
                f"provider '{explicit_pid}' is disabled",
                status=503,
            )
        # 不强制 vendor_model 必须在 provider.models 里——
        # 用户可能想用某 provider 的某新模型而未在柳如烟 UI 加进 model list。
        # provider 转发到上游，上游决定能不能调。
        return ResolveResult(provider=provider, vendor_model=vendor_model)

    # 无 prefix：先 current_provider 同名，再全局扫
    current_pid = cfg.get("current_provider") or ""
    current = providers.get(current_pid)
    if current and current.enabled and any(m.id == vendor_model for m in current.models):
        return ResolveResult(provider=current, vendor_model=vendor_model)

    for _pid, p in providers.items():
        if not p.enabled:
            continue
        if any(m.id == vendor_model for m in p.models):
            return ResolveResult(provider=p, vendor_model=vendor_model)

    raise ResolveError(
        "model_not_found",
        f"model '{model}' not found in any enabled provider; "
        "use '<provider_id>:<model_id>' format to disambiguate",
        status=404,
    )


def _infer_capabilities(model_tags: list[str], model_id: str, transport: str) -> list[str]:
    """从 model tags + id + transport 推断 capability 标签（M4-B）。

    capability 词表：
      - chat              所有 openai_chat / anthropic_messages 模型
      - tool_use          tags 含 "tools" / "function_call" / id 含 "claude" / "gpt-4" 等
      - vision            tags 含 "vision" / "multimodal" / id 含 "vision" / "claude-3" / "gpt-4o"
      - reasoning         tags 含 "reasoning" / "thinking" / id 含 "o1" / "o3" / "deepseek-r1" / "thinking"
      - embeddings        id 含 embed / bge / 模型名是已知 embedding 模型

    这是 best-effort 启发式，用户可在 provider models[].tags 里手动加权威标签覆盖。
    返回 sorted 唯一列表。
    """
    caps: set[str] = set()
    tags_lower = {t.lower() for t in (model_tags or [])}
    mid = (model_id or "").lower()

    # embeddings：纯 embedding 模型不含 chat
    is_embedding = (
        "embed" in mid
        or "bge" in mid
        or "voyage" in mid
        or "embeddings" in tags_lower
        or "embedding" in tags_lower
    )
    if is_embedding:
        caps.add("embeddings")
        return sorted(caps)

    # chat（chat 类 transport 默认都能 chat）
    if transport in ("openai_chat", "anthropic_messages"):
        caps.add("chat")

    # tool_use
    if (
        {"tools", "function_call", "tool_use"} & tags_lower
        or "claude-3" in mid or "claude-4" in mid or "claude-opus" in mid or "claude-sonnet" in mid
        or "gpt-4" in mid or "gpt-5" in mid
        or "deepseek" in mid
        or "qwen" in mid
        or "kimi" in mid
    ):
        caps.add("tool_use")

    # vision
    if (
        {"vision", "multimodal", "image"} & tags_lower
        or "vision" in mid
        or "claude-3" in mid or "claude-4" in mid or "claude-opus" in mid or "claude-sonnet" in mid
        or "gpt-4o" in mid or "gpt-4-turbo" in mid
        or "qwen-vl" in mid
        or "gemini" in mid
    ):
        caps.add("vision")

    # reasoning
    if (
        {"reasoning", "thinking"} & tags_lower
        or mid.startswith("o1") or mid.startswith("o3") or "-o1-" in mid or "-o3-" in mid
        or "deepseek-r" in mid or "thinking" in mid
        or "qwq" in mid
    ):
        caps.add("reasoning")

    return sorted(caps)


def _alias_status(candidates: list[str], unhealthy_snapshot: dict) -> dict:
    """计算 alias 的实时状态：candidates 总数 / 健康候选数 / 主候选是否在黑屋（M4-B）。"""
    total = len(candidates)
    healthy = 0
    primary_unhealthy = False
    next_retry_secs: int | None = None

    for idx, cand in enumerate(candidates):
        try:
            pid, _vm = split_candidate(cand)
        except Exception:
            continue
        info = unhealthy_snapshot.get(pid)
        if info is None:
            healthy += 1
        else:
            if idx == 0:
                primary_unhealthy = True
                next_retry_secs = int(info.get("remaining_secs") or 0)

    return {
        "total_candidates": total,
        "healthy_candidates": healthy,
        "primary_unhealthy": primary_unhealthy,
        "next_retry_secs": next_retry_secs,
    }


def list_namespaced_models(*, capability: str | None = None) -> list[dict]:
    """聚合所有 enabled provider 的 model list，输出 OpenAI Models API 风格。

    `id` 字段格式：`<provider_id>:<vendor_model_id>`。

    M4-B 升级：
      - 每个 model 加 `capabilities`（chat / tool_use / vision / reasoning / embeddings）
      - 每个 alias 加 `alias_status`：实时健康候选数 + 主候选是否在黑屋
      - 可选 capability 参数过滤（让 picker UI 按能力筛选）
    """
    cfg = load_providers_config_cached()
    unhealthy = _get_health_cache().snapshot()
    out: list[dict] = []

    for pid, p in cfg["providers"].items():
        if not p.enabled:
            continue
        # M2-B 后 openai_chat / anthropic_messages 都可从任一端点访问（跨协议互转自动生效）。
        for m in p.models:
            tags = list(m.tags or [])
            caps = _infer_capabilities(tags, m.id, p.transport)
            out.append(
                {
                    "id": f"{pid}:{m.id}",
                    "object": "model",
                    "owned_by": pid,
                    "provider": pid,
                    "provider_name": p.name or pid,
                    "transport": p.transport,
                    "tags": tags,
                    "capabilities": caps,
                    "context_length": m.context_length,
                    "name": m.name or m.id,
                }
            )

    # M3：把 alias 也暴露成"virtual model"，让客户端 model picker 能选到
    for name, entry in load_aliases_cached().items():
        # alias capability = candidates 第一个真实 model 的 capability
        # （fallback 候选要求语义对等；不一致则取并集）
        alias_caps: set[str] = set()
        for cand in entry.candidates:
            try:
                cpid, cvm = split_candidate(cand)
            except Exception:
                continue
            cprov = cfg["providers"].get(cpid)
            if cprov is None or not cprov.enabled:
                continue
            for cm in cprov.models:
                if cm.id == cvm:
                    alias_caps.update(_infer_capabilities(list(cm.tags or []), cvm, cprov.transport))
                    break
            else:
                # 未在 model list 里——按 transport 兜底
                alias_caps.update(_infer_capabilities([], cvm, cprov.transport))
        out.append(
            {
                "id": name,
                "object": "model",
                "owned_by": "alias",
                "provider": "alias",
                "provider_name": "Alias (柳如烟 fallback chain)",
                "transport": "alias",
                "tags": ["alias"],
                "capabilities": sorted(alias_caps),
                "context_length": 0,
                "name": name,
                "alias_candidates": list(entry.candidates),
                "alias_status": _alias_status(list(entry.candidates), unhealthy),
                "description": entry.description,
            }
        )

    # M4-B：可选 capability 过滤
    if capability:
        cap = capability.lower().strip()
        out = [m for m in out if cap in (m.get("capabilities") or [])]

    return out


def resolve_chain(model: str) -> List[ResolveResult]:
    """M3 入口：返一条/多条 ResolveResult，按 fallback 优先级排序。

    路由优先级：
      1. model 含 ':' → [resolve_model 单条]，**不查 alias**（命名空间显式）
      2. model 命中 alias 表 → 展开 candidates 为多条 ResolveResult（破链 candidate 跳过）
      3. model 是裸 vendor_model → [resolve_model 单条]（M1 兜底）
      4. 都不是 → ResolveError(model_not_found)

    破链 candidate（provider 不存在 / disabled）会被跳过 + 打 warn 日志，不抛错；
    若所有 candidate 都破链，按 model_not_found 抛错。

    M2-B 的 routes 应当切到此函数；resolve_model 保持向后兼容（M1 调用方）。
    """
    if not model or not isinstance(model, str):
        raise ResolveError("model_required", "model field is required", status=400)

    # 路径 1：含冒号显式命名空间
    if ":" in model:
        return [resolve_model(model)]

    # 路径 2：alias 表
    aliases = load_aliases_cached()
    entry = aliases.get(model)
    if entry is not None and entry.candidates:
        cfg = load_providers_config_cached()
        providers = cfg["providers"]
        results: List[ResolveResult] = []
        for cand in entry.candidates:
            try:
                pid, vm = split_candidate(cand)
            except Exception as exc:
                logger.warning("[GATEWAY-ROUTER-WARN] alias '%s' candidate '%s' invalid: %s", model, cand, exc)
                continue
            provider = providers.get(pid)
            if provider is None:
                logger.warning("[GATEWAY-ROUTER-WARN] alias '%s' candidate '%s': provider '%s' not found", model, cand, pid)
                continue
            if not provider.enabled:
                logger.warning("[GATEWAY-ROUTER-WARN] alias '%s' candidate '%s': provider disabled", model, cand)
                continue
            results.append(ResolveResult(provider=provider, vendor_model=vm))
        if results:
            return results
        raise ResolveError(
            "alias_all_candidates_unavailable",
            f"alias '{model}' has {len(entry.candidates)} candidate(s) but none point to an enabled provider",
            status=503,
        )

    # 路径 3：裸 vendor_model 兜底（M1 行为）
    return [resolve_model(model)]


__all__ = [
    "ResolveResult",
    "ResolveError",
    "resolve_model",
    "resolve_chain",
    "list_namespaced_models",
]
