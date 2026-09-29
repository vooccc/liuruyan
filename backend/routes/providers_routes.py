"""Provider 管理路由（v2 模型配置 P1）。

接口：
- GET    /providers                       列出全部（含完整 api_key）
- GET    /providers/{id}                  单个 provider 详情
- POST   /providers                       添加自定义 provider
- PUT    /providers/{id}                  更新 provider（base_url / api_key / extra_headers / enabled / models）
- DELETE /providers/{id}                  删除自定义 provider（内置不允许）

设计：
- GET 直接返 api_key（本地桌面应用，前端持有明文换 UX：type=password 按真实长度绘点、
  眼睛切换零延迟；secrets.json 物理分离仍保留用于 git/备份）
- 内置 provider 的 base_url 在 freeze_url=true 时不允许改
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.providers import ProviderConfig
from backend.providers.fetchers import discover_models
from backend.providers.schema import ModelEntry
from backend.providers.store import (
    delete_provider as _delete_provider,
    load_providers_config,
    reorder_providers as _reorder_providers,
    save_providers_config,
)

router = APIRouter(prefix="/providers", tags=["providers"])


# ── 请求 schema ────────────────────────────────────────────────────────


class ProviderCreateRequest(BaseModel):
    id: str
    name: str
    base_url: str = ""
    api_key: str = ""
    transport: str = "openai_chat"
    api_key_prefix: str = ""
    extra_headers: Dict[str, str] = Field(default_factory=dict)
    description: str = ""
    icon: str = ""


class ProviderUpdateRequest(BaseModel):
    """PUT /providers/{id} 的 partial-update body。

    所有字段 optional，None 表示不动。空字符串/空 dict 当成显式清空。
    """
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    transport: Optional[str] = None
    enabled: Optional[bool] = None
    extra_headers: Optional[Dict[str, str]] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    models: Optional[List[ModelEntry]] = None


# ── helpers ────────────────────────────────────────────────────────────


def _public_view(pc: ProviderConfig) -> Dict[str, Any]:
    """序列化 provider 给前端，附 tags / context_length infer 兜底。

    `ModelEntry.tags` 由 OpenRouter fetch / "添加模型" UI 写入，但有以下场景可能
    为空：用户直接编辑 models.json、老数据未 infer 过、手动 ModelEntry(id=...) 创建。
    走 capabilities.infer_capabilities 兜底后，ProviderSettings 已启用模型列表 +
    ChatModelPicker + ManageModelsPopup 三处都能拿到完整 tags 渲染彩色能力胶囊。

    与 chat_model_catalog_view 的兜底语义保持一致——前端任何展示位都不需要再
    独立推断，后端是单源真理。
    """
    from backend.providers.capabilities import infer_capabilities, infer_context_length

    d = pc.public_dict()
    for m in d.get("models", []):
        configured_context = m.get("context_length")
        if not m.get("tags"):
            m["tags"] = infer_capabilities(m.get("id", ""), m.get("name") or None)
        if not m.get("context_length"):
            m["context_length"] = infer_context_length(m.get("id", ""))
        m["context_length_source"] = "configured" if configured_context else "inferred"
    return d


# ── 路由 ───────────────────────────────────────────────────────────────


@router.get("")
async def list_providers():
    """列出所有 provider（不含 api_key）。"""
    config = load_providers_config()
    providers: Dict[str, ProviderConfig] = config["providers"]
    res = [_public_view(p) for p in providers.values()]
    # 安全挂载 Pro 算力账号虚拟通道（若 Pro 模块不存在则自动跳过）
    try:
        from backend.pro.compute_accounts.models_provider import get_pro_providers
        for vp in get_pro_providers():
            for m in vp.get("models", []):
                if not m.get("tags"):
                    m["tags"] = ["vision", "tools"]
                if not m.get("icon"):
                    m["icon"] = "⚡"
                m.setdefault("added_by_user", False)
                m.setdefault("context_length", 1048576)
                m.setdefault("context_length_source", "configured")
            res.append(vp)
    except (ImportError, ModuleNotFoundError):
        pass
    except Exception as e:
        logger.warning(f"Failed to inject pro providers: {e}")
    return {
        "providers": res,
        "current_provider": config.get("current_provider", ""),
        "current_model": config.get("current_model", ""),
    }



@router.get("/default-headers")
async def get_default_headers(transport: Optional[str] = None):
    """返回项目级默认 LLM headers（Cherry-style UA + HTTP-Referer + X-Title）。

    前端 ExtraHeadersDialog 用：打开时把 baseline 填入 textarea 给用户做参考，
    用户改两下保存。后端 merge_default_llm_headers 是动态生成（platform 相关），
    前端没法精确还原，必须靠这个 endpoint。
    """
    from backend.utils.llm_headers import get_default_llm_headers, get_claude_code_preset_headers
    return {
        "headers": get_default_llm_headers(transport=transport),
        "presets": {
            "claude_code": get_claude_code_preset_headers(),
        },
    }


# ── /providers/active（per-agent model routing 阶段 2 Commit 2.1）────────
#
# 统一的 model 配置入口，支持 scope=global|agent|effective 三种语义。
# 设计要点：
# - 老接口 PUT /models/current 保留不动（前端切全局 model 走它），新接口跟它**等价**
#   实现 scope=global，用户可以选择性迁移
# - scope=effective 是只读视图：跑 resolve_call_model_config 看实际生效的（含 fallback 链）
# - 路由必须注册在 `GET /{provider_id}` 之前，否则会被 path param 'active' 吞掉
# - 新路由不调 LLMClient.set_model（保持新接口零副作用，老路径仍读 store 自动跟随）


class ActiveModelPutRequest(BaseModel):
    """PUT /providers/active body。

    - scope='global'：写 providers store 的 current_provider/current_model；
      model 不能为空。
    - scope='agent'：写 agent meta.json 的 active_model；agent_id 必填；
      model='' 表示清除（让该 agent 回到全局默认）。
    """

    scope: str = Field(..., description="'global' 或 'agent'")
    agent_id: Optional[str] = None
    provider_id: str = ""
    model: str = ""


def _empty_active() -> Dict[str, str]:
    return {"provider_id": "", "model": ""}


@router.get("/active")
async def get_active_model(scope: str = "effective", agent_id: Optional[str] = None):
    """读 model 配置。

    Args:
        scope: 'global' / 'agent' / 'effective'（默认）
        agent_id: scope=agent 必填；scope=effective 可选（影响 fallback 链）

    Returns:
        {"provider_id": str, "model": str}；agent 未配置时两个字段都返回 ""
    """
    if scope not in ("global", "agent", "effective"):
        raise HTTPException(
            status_code=400,
            detail=f"scope 必须是 global/agent/effective，收到 '{scope}'",
        )

    if scope == "global":
        from backend.providers.store import load_providers_config_cached

        cfg = load_providers_config_cached()
        return {
            "provider_id": cfg.get("current_provider", "") or "",
            "model": cfg.get("current_model", "") or "",
        }

    if scope == "agent":
        if not agent_id:
            raise HTTPException(
                status_code=400, detail="scope=agent 需要传 agent_id"
            )
        from backend.agent_config import get_agent_active_model

        am = get_agent_active_model(agent_id)
        if am is None:
            return _empty_active()
        return {"provider_id": am.get("provider_id", ""), "model": am.get("model", "")}

    # scope=effective
    from backend.llm_call_config import (
        ModelResolutionError,
        resolve_call_model_config,
    )

    try:
        cfg = resolve_call_model_config(agent_id=agent_id)
    except ModelResolutionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"provider_id": cfg.provider_id, "model": cfg.model}


@router.put("/active")
async def set_active_model(body: ActiveModelPutRequest):
    """写 model 配置（scope=global / scope=agent）。

    返回最终落库的 {provider_id, model}（scope=agent + 清除时返回空字符串）。
    """
    from backend.providers.store import (
        find_model,
        load_providers_config,
        save_providers_config,
    )

    if body.scope == "global":
        if not body.model:
            raise HTTPException(
                status_code=400, detail="scope=global 不允许 model 为空"
            )
        config = load_providers_config()
        pc = find_model(config, body.model, body.provider_id or None)
        if pc is None and body.provider_id:
            # provider hint 失败 → 全扫描，对齐 resolve_call_model_config 的容错策略
            pc = find_model(config, body.model, None)

        is_pro = False
        effective_pid = body.provider_id
        if pc is None:
            try:
                from backend.pro.compute_accounts.models_provider import is_pro_provider, get_pro_providers
                if is_pro_provider(body.provider_id):
                    is_pro = True
                else:
                    for vp in get_pro_providers():
                        if any(m["id"] == body.model for m in vp.get("models", [])):
                            is_pro = True
                            effective_pid = vp.get("id", "google-antigravity")
                            break
            except Exception:
                pass

        if pc is None and not is_pro:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"model '{body.model}'"
                    + (f" (provider: {body.provider_id})" if body.provider_id else "")
                    + " 不在可用列表"
                ),
            )
        config["current_model"] = body.model
        config["current_provider"] = pc.id if pc else (effective_pid or "google-antigravity")
        save_providers_config(config)
        return {"provider_id": config["current_provider"], "model": body.model}

    if body.scope == "agent":
        if not body.agent_id:
            raise HTTPException(
                status_code=400, detail="scope=agent 需要传 agent_id"
            )

        from backend.agent_config import set_agent_active_model

        if not body.model:
            # 清除 agent 的 active_model（回到全局默认）
            ok = set_agent_active_model(body.agent_id, None, None)
            if not ok:
                raise HTTPException(
                    status_code=404, detail=f"agent '{body.agent_id}' 不存在"
                )
            return _empty_active()

        # 校验 model 真实存在再落库（避免写入死配置）
        config = load_providers_config()
        pc = find_model(config, body.model, body.provider_id or None)
        if pc is None and body.provider_id:
            pc = find_model(config, body.model, None)

        is_pro = False
        effective_pid = body.provider_id
        if pc is None:
            try:
                from backend.pro.compute_accounts.models_provider import is_pro_provider, get_pro_providers
                if is_pro_provider(body.provider_id):
                    is_pro = True
                else:
                    for vp in get_pro_providers():
                        if any(m["id"] == body.model for m in vp.get("models", [])):
                            is_pro = True
                            effective_pid = vp.get("id", "google-antigravity")
                            break
            except Exception:
                pass

        if pc is None and not is_pro:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"model '{body.model}'"
                    + (f" (provider: {body.provider_id})" if body.provider_id else "")
                    + " 不在可用列表"
                ),
            )
        target_pid = pc.id if pc else (effective_pid or "google-antigravity")
        ok = set_agent_active_model(body.agent_id, target_pid, body.model)
        if not ok:
            raise HTTPException(
                status_code=404, detail=f"agent '{body.agent_id}' 不存在"
            )
        return {"provider_id": target_pid, "model": body.model}

    raise HTTPException(
        status_code=400, detail=f"scope 必须是 global/agent，收到 '{body.scope}'"
    )


# ── /providers/translate-model（2026-05-25 思考块翻译 v2）────────────────────
#
# "翻译模型"独立配置：跟 /providers/active scope=global/agent 不同维度——前者
# 是"主对话用什么"，translate-model 是"思考块翻译这个特定辅助任务用什么"。
# 当用户主对话用 reasoning model（Claude 4 系 / o-series / DeepSeek-R1）时，
# 把翻译指给一个 fast non-reasoning model（gpt-4o-mini / qwen-turbo /
# deepseek-chat）能彻底规避"reasoning model 把待译文本当指令理解 → 翻译跑题"。
# 未配置时 utility/translate fallback 到当前会话 model（仍走 _disable_thinking_var
# 关 reasoning 兜底，治标）。
#
# 路由必须注册在 `GET /{provider_id}` 之前，否则会被 path param 吞掉
# （tax: 同 /active 路由的注释）。


class TranslateModelPutRequest(BaseModel):
    """PUT /providers/translate-model body。

    `provider_id` 和 `model` 同时为空（或空串）= 清除配置 → 翻译 fallback 到
    当前会话 model。任意一个非空时两个都必须有效。
    """

    provider_id: str = ""
    model: str = ""


@router.get("/translate-model")
async def get_translate_model_route():
    """读"翻译模型"配置。

    Returns:
        {"provider_id": str, "model": str}；未配置时两字段都 ""。
    """
    from backend.providers.store import get_translate_model

    tm = get_translate_model()
    if tm is None:
        return _empty_active()
    return {"provider_id": tm.get("provider_id", ""), "model": tm.get("model", "")}


@router.put("/translate-model")
async def set_translate_model_route(body: TranslateModelPutRequest):
    """写"翻译模型"配置。

    传 provider_id="" + model="" 清除，让翻译 fallback 到当前会话 model。
    校验 model 真实存在再落库（避免写入死配置）。
    """
    from backend.providers.store import set_translate_model

    try:
        result = set_translate_model(body.provider_id, body.model)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result


# ── 老路由（保留不动）─────────────────────────────────────────────────────


@router.get("/{provider_id}")
async def get_provider_detail(provider_id: str):
    """单个 provider 详情。"""
    config = load_providers_config()
    pc = config["providers"].get(provider_id)
    if pc is None:
        raise HTTPException(status_code=404, detail=f"provider '{provider_id}' 不存在")
    return _public_view(pc)


@router.post("")
async def create_provider(body: ProviderCreateRequest):
    """添加自定义 provider。"""
    config = load_providers_config()
    if body.id in config["providers"]:
        raise HTTPException(status_code=409, detail=f"provider '{body.id}' 已存在")
    if not body.id.strip():
        raise HTTPException(status_code=400, detail="id 不能为空")

    pc = ProviderConfig(
        id=body.id,
        name=body.name,
        base_url=body.base_url,
        api_key=body.api_key,
        transport=body.transport,
        api_key_prefix=body.api_key_prefix,
        extra_headers=body.extra_headers,
        description=body.description,
        icon=body.icon,
        is_builtin=False,
        require_api_key=False,
    )
    # 新加的 provider 插到字典最前（用户期望最新加的最显眼，对标 cherry studio）。
    # Python 3.7+ dict 保持插入顺序，rebuild 一遍 dict 即可。
    new_providers: Dict[str, ProviderConfig] = {body.id: pc}
    new_providers.update(config["providers"])
    config["providers"] = new_providers
    save_providers_config(config)
    return {"status": "success", "provider": _public_view(pc)}


class ProviderReorderRequest(BaseModel):
    """PUT /providers/order body：按给定 id 顺序重排 providers 列表。"""
    order: List[str]


@router.put("/order")
async def reorder_providers_route(body: ProviderReorderRequest):
    """重排 providers 顺序。前端拖拽完成后调一次。

    - 列表里有但 providers 里不存在的 id 会被忽略
    - providers 里有但列表里没列出的 id 会按原顺序追加到末尾（防意外丢数据）
    """
    final_order = _reorder_providers(body.order)
    return {"status": "success", "order": final_order}


@router.put("/{provider_id}")
async def update_provider(provider_id: str, body: ProviderUpdateRequest):
    """更新 provider 配置（partial）。"""
    config = load_providers_config()
    pc = config["providers"].get(provider_id)
    if pc is None:
        raise HTTPException(status_code=404, detail=f"provider '{provider_id}' 不存在")

    if body.name is not None and not pc.is_builtin:
        # 内置 provider name 强制锁住（防止用户改了再升级丢失）
        pc.name = body.name
    if body.base_url is not None:
        if pc.freeze_url and pc.is_builtin:
            raise HTTPException(
                status_code=400,
                detail=f"provider '{provider_id}' 是内置且 freeze_url=true，不允许改 base_url",
            )
        pc.base_url = body.base_url
    if body.api_key is not None:
        pc.api_key = body.api_key
    if body.transport is not None:
        # 内置 provider transport 锁住（绑定了固定协议，不允许改）
        if pc.is_builtin:
            raise HTTPException(
                status_code=400,
                detail=f"provider '{provider_id}' 是内置，不允许修改 transport",
            )
        if body.transport not in ("openai_chat", "anthropic_messages"):
            raise HTTPException(
                status_code=400,
                detail=f"transport 只能是 'openai_chat' 或 'anthropic_messages'，收到 '{body.transport}'",
            )
        pc.transport = body.transport
    if body.enabled is not None:
        pc.enabled = body.enabled
    if body.extra_headers is not None:
        pc.extra_headers = body.extra_headers
    if body.description is not None and not pc.is_builtin:
        pc.description = body.description
    if body.icon is not None and not pc.is_builtin:
        pc.icon = body.icon
    if body.models is not None:
        pc.models = body.models

    save_providers_config(config)

    # 同步 LLMClient：如果当前选中模型属于这个 provider，刷一下
    if config.get("current_provider") == provider_id:
        try:
            from backend.llm_client import get_llm_client

            llm = get_llm_client()
            if hasattr(llm, "set_model") and config.get("current_model"):
                llm.set_model(
                    config["current_model"],
                    base_url=pc.base_url,
                    api_key=pc.api_key,
                    provider=pc.transport.replace("_chat", "").replace("_messages", ""),
                    default_headers=pc.extra_headers,
                )
        except Exception:
            # 启动期或测试态 LLMClient 可能未初始化，不阻塞 PUT 成功
            pass

    return {"status": "success", "provider": _public_view(pc)}


class DiscoverRequest(BaseModel):
    """POST /providers/{id}/models/discover 的可选 body。

    允许覆盖 provider 当前的 api_key / base_url（不持久化），用于"先测后存"场景：
    用户填了新 key 但还没保存，想先验证能不能拉到模型。
    """
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    extra_headers: Optional[Dict[str, str]] = None


@router.post("/{provider_id}/models/discover")
async def discover_provider_models(provider_id: str, body: Optional[DiscoverRequest] = None):
    """拉取该 provider 的全量模型列表（带 free/tags/pricing 元数据）。

    body 可选：可临时覆盖 api_key / base_url / extra_headers 做"未保存试拉"。
    """
    config = load_providers_config()
    pc = config["providers"].get(provider_id)
    if pc is None:
        raise HTTPException(status_code=404, detail=f"provider '{provider_id}' 不存在")

    # 临时覆盖（不写盘）
    overrides = body or DiscoverRequest()
    runtime = pc.model_copy(deep=True)
    if overrides.api_key is not None:
        runtime.api_key = overrides.api_key
    if overrides.base_url is not None:
        runtime.base_url = overrides.base_url
    if overrides.extra_headers is not None:
        runtime.extra_headers = overrides.extra_headers

    try:
        models = await discover_models(runtime)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        try:
            err_body = e.response.json()
            msg = err_body.get("error", {}).get("message") if isinstance(err_body, dict) else None
            msg = msg or err_body.get("message") if isinstance(err_body, dict) else None
        except Exception:
            msg = None
        raise HTTPException(
            status_code=502,
            detail=f"provider '{provider_id}' 返回 {status}: {msg or e.response.text[:200]}",
        )
    except (httpx.HTTPError, httpx.RequestError) as e:
        # 异常类型保留在 detail 里（ConnectTimeout / SSLError 等），方便用户判断是网络/代理还是服务端
        raise HTTPException(status_code=502, detail=f"网络错误: {type(e).__name__}: {str(e)[:200]}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "provider_id": provider_id,
        "count": len(models),
        "free_count": sum(1 for m in models if m.is_free),
        "models": [m.to_dict() for m in models],
    }


class ImportRequest(BaseModel):
    """POST /providers/{id}/models/import body。

    支持两种用法：
    - model_ids: ["id1", "id2"]  → 内部先拉模型列表再筛选导入
    - models: [DiscoveredModel...] → 直接传完整对象（推荐：避免再次发 API）
    """
    model_ids: Optional[List[str]] = None
    models: Optional[List[Dict[str, Any]]] = None
    replace: bool = False  # true=覆盖现有 models[]；false=合并


@router.post("/{provider_id}/models/import")
async def import_provider_models(provider_id: str, body: ImportRequest):
    """把发现的模型批量导入到 provider.models[]。"""
    config = load_providers_config()
    pc = config["providers"].get(provider_id)
    if pc is None:
        raise HTTPException(status_code=404, detail=f"provider '{provider_id}' 不存在")

    # 来源：要么直接给 models[], 要么给 model_ids 我们去远端拉
    incoming: List[Dict[str, Any]] = []
    if body.models:
        incoming = list(body.models)
    elif body.model_ids:
        try:
            discovered = await discover_models(pc)
        except (httpx.HTTPError, httpx.RequestError, ValueError) as e:
            raise HTTPException(status_code=502, detail=f"拉模型失败: {str(e)[:200]}")
        wanted = set(body.model_ids)
        incoming = [m.to_dict() for m in discovered if m.id in wanted]
    else:
        raise HTTPException(status_code=400, detail="必须提供 models 或 model_ids")

    if body.replace:
        pc.models = []

    existing = {m.id for m in pc.models}
    added: List[str] = []
    for raw in incoming:
        mid = raw.get("id")
        if not mid or mid in existing:
            continue
        pc.models.append(
            ModelEntry(
                id=mid,
                name=raw.get("name") or mid,
                tags=list(raw.get("tags", [])),
                added_by_user=True,
            )
        )
        existing.add(mid)
        added.append(mid)

    save_providers_config(config)
    return {
        "status": "success",
        "added": added,
        "added_count": len(added),
        "total_models": len(pc.models),
    }


@router.delete("/{provider_id}")
async def delete_provider_route(provider_id: str):
    """删除自定义 provider（内置不允许删）。"""
    config = load_providers_config()
    pc = config["providers"].get(provider_id)
    if pc is None:
        raise HTTPException(status_code=404, detail=f"provider '{provider_id}' 不存在")
    if pc.is_builtin:
        raise HTTPException(
            status_code=400, detail=f"内置 provider '{provider_id}' 不允许删除"
        )

    ok = _delete_provider(provider_id)
    if not ok:
        raise HTTPException(status_code=500, detail="删除失败")
    return {"status": "success"}


__all__ = ["router"]
