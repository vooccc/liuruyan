"""智能体核心 API（模板 / 头像 / CRUD）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
依赖均可直接 import，不反向依赖 main.py：
- get_agent_manager（backend.agent_config）
- clear_agent_cache（backend.agent）
- get_session_manager（backend.session）—— delete / clear-model-override 用

路由顺序：/agents/templates* 必须先于 /agents/{agent_id}，否则 {agent_id}
会抢匹配 "templates"。本模块保持原 main.py 中的声明顺序。

不含：情感 / 面具 / 文件 / 模板同步（仍在 main.py，依赖 EMOTION_STATES 等，
留后续拆分）。

端点：
- GET    /agents/templates
- GET    /agents/templates/{template_id}/avatar
- POST   /agents/{agent_id}/avatar
- POST   /agents/{agent_id}/avatar/template
- GET    /agents/{agent_id}/avatar
- DELETE /agents/{agent_id}/avatar
- GET    /agents
- GET    /agents/{agent_id}
- POST   /agents
- PUT    /agents/{agent_id}
- POST   /agents/{agent_id}/save-as-template
- DELETE /agents/{agent_id}
- POST   /agents/{agent_id}/sessions/clear-model-override
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from backend.agent import clear_agent_cache
from backend.agent_config import get_agent_manager
from backend.session import get_session_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agents"])


class AgentCreateRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    avatar: Optional[str] = "🤖"
    template: Optional[str] = "default"


class AgentUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    avatar: Optional[str] = None
    identity: Optional[str] = None
    soul: Optional[str] = None
    personality: Optional[str] = None
    order: Optional[int] = None
    prototype: Optional[str] = None
    tags: Optional[List[str]] = None
    spectrum: Optional[Dict[str, float]] = None
    charm_layers: Optional[Dict[str, List[str]]] = None


class SaveAsTemplateRequest(BaseModel):
    """角色实例 → 模板：反向冻结请求"""
    template_id: str
    overwrite: Optional[bool] = False
    include_memory: Optional[bool] = False
    include_user: Optional[bool] = False


@router.get("/agents/templates")
async def list_templates():
    """获取所有角色模板列表"""
    manager = get_agent_manager()
    return {"templates": manager.list_templates()}


@router.get("/agents/templates/{template_id}/avatar")
async def get_template_avatar(template_id: str):
    """获取角色模板头像图片"""
    from fastapi.responses import FileResponse
    manager = get_agent_manager()
    avatar_path = manager.templates_dir / template_id / "avatar.png"
    if avatar_path.exists():
        return FileResponse(avatar_path, media_type="image/png")
    # 没有 avatar.png，返回 404
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="Avatar not found")


def _normalize_agent_avatar_payload(manager, agent: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """统一实例头像解析（2026-05-21 解耦改造）：只看实例自有头像，不再回落模板目录。

    历史背景：原本有一段 "templates_dir / <source_template> / avatar.png" 的 fallback——
    现在创建时已由 create_from_template / 迁移脚本把头像复制到 agent_dir，不需要 runtime 回查。
    用户主动删 agent_dir/avatar.png = 真的删，不会被偷偷补回（前端有 "从模板选头像" 按钮可显式恢复）。
    """
    if not agent:
        return agent

    normalized = dict(agent)
    agent_id = normalized.get("id")
    if not agent_id:
        return normalized

    config = manager._get_config(agent_id)
    # 找到实际存在的头像文件并拼上 mtime 作 cache buster。
    # 背景：URL 路径 /agents/{id}/avatar 是稳定的，上传/替换头像时文件被覆盖，
    # 但 URL 字符串没变 → 浏览器缓存命中 → 前端各处（Sidebar/ChatHeader 等）
    # 看到的还是旧图。Workshop 自己拼 ?v=avatarVer 兜底，但其它消费方都靠 getAvatarSrc
    # 直接用裸 URL，必须在后端单点把版本号塞进去，所有下游零改动。
    avatar_file = None
    for filename in ("custom_avatar.png", "avatar.png"):
        candidate = config.agent_dir / filename
        if candidate.exists():
            avatar_file = candidate
            break
    if avatar_file is not None:
        try:
            mtime = int(avatar_file.stat().st_mtime)
        except OSError:
            mtime = 0
        normalized["avatar_url"] = f"/agents/{agent_id}/avatar?v={mtime}"
    else:
        normalized.pop("avatar_url", None)
    return normalized


@router.post("/agents/{agent_id}/avatar")
async def upload_agent_avatar(agent_id: str, body: dict = Body(...)):
    """上传自定义头像到智能体目录"""
    try:
        image_data = body.get("image_data", "")
        if not image_data or not image_data.startswith("data:image"):
            raise HTTPException(status_code=400, detail="Invalid image data")
        # 解码 base64 并保存为文件
        header, b64 = image_data.split(",", 1)
        import base64
        img_bytes = base64.b64decode(b64)
        if len(img_bytes) > 5 * 1024 * 1024:  # 5MB 限制
            raise HTTPException(status_code=400, detail="Image too large (max 5MB)")
        manager = get_agent_manager()
        config = manager._get_config(agent_id)
        if not config.agent_dir.exists():
            raise HTTPException(status_code=404, detail="Agent not found")
        avatar_path = config.agent_dir / "custom_avatar.png"
        with open(avatar_path, "wb") as f:
            f.write(img_bytes)
        # 更新 meta 中的 avatar_url
        meta = config.load_meta()
        if meta:
            meta["avatar_url"] = f"/agents/{agent_id}/avatar"
            meta.pop("avatar_template", None)
            config.save_meta(meta)
        clear_agent_cache(agent_id)
        normalized_agent = _normalize_agent_avatar_payload(manager, manager.get_agent(agent_id))
        # 顶层 avatar_url 从 normalize 后的 agent 拿，保证含 ?v=mtime cache buster
        # （前端调用方直接拿来用，不需要再拼 ?v=）
        return {
            "success": True,
            "avatar_url": (normalized_agent or {}).get("avatar_url") or f"/agents/{agent_id}/avatar",
            "agent": normalized_agent,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[AVATAR] Upload error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agents/{agent_id}/avatar/template")
async def apply_template_avatar(agent_id: str, body: dict = Body(...)):
    """把模板头像复制到实例目录，作为实例当前头像。"""
    template_id = (body.get("template_id") or "").strip()
    if not template_id:
        raise HTTPException(status_code=400, detail="Missing template_id")

    manager = get_agent_manager()
    config = manager._get_config(agent_id)
    if not config.agent_dir.exists():
        raise HTTPException(status_code=404, detail="Agent not found")

    template_avatar = manager.templates_dir / template_id / "avatar.png"
    if not template_avatar.exists():
        raise HTTPException(status_code=404, detail="Template avatar not found")

    import shutil

    shutil.copy2(template_avatar, config.agent_dir / "avatar.png")

    meta = config.load_meta()
    meta["avatar_url"] = f"/agents/{agent_id}/avatar"
    meta["avatar_template"] = template_id
    config.save_meta(meta)

    clear_agent_cache(agent_id)
    return {
        "success": True,
        "agent": _normalize_agent_avatar_payload(manager, manager.get_agent(agent_id)),
    }


@router.get("/agents/{agent_id}/avatar")
async def get_agent_avatar(agent_id: str):
    """获取智能体自定义头像"""
    from fastapi.responses import FileResponse
    manager = get_agent_manager()
    config = manager._get_config(agent_id)
    for name in ["custom_avatar.png", "avatar.png"]:
        avatar_path = config.agent_dir / name
        if avatar_path.exists():
            return FileResponse(avatar_path, media_type="image/png")
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="Avatar not found")


@router.delete("/agents/{agent_id}/avatar")
async def delete_agent_avatar(agent_id: str):
    """移除智能体自定义头像，回退到 emoji。

    会同时删除 custom_avatar.png 和 avatar.png（含从模板复制过来的那份），
    并清掉 meta.json 里的 avatar_url / avatar_template 字段。
    """
    manager = get_agent_manager()
    config = manager._get_config(agent_id)
    if not config.agent_dir.exists():
        raise HTTPException(status_code=404, detail="Agent not found")
    removed = []
    for name in ("custom_avatar.png", "avatar.png"):
        p = config.agent_dir / name
        if p.exists():
            try:
                p.unlink()
                removed.append(name)
            except Exception as e:
                logger.warning("[AVATAR] Delete %s failed: %s", name, e)
    meta = config.load_meta() or {}
    meta.pop("avatar_url", None)
    meta.pop("avatar_template", None)
    config.save_meta(meta)
    clear_agent_cache(agent_id)
    return {
        "success": True,
        "removed": removed,
        "agent": _normalize_agent_avatar_payload(manager, manager.get_agent(agent_id)),
    }


@router.get("/agents")
async def list_agents():
    """获取所有智能体列表"""
    manager = get_agent_manager()
    return {"agents": [_normalize_agent_avatar_payload(manager, agent) for agent in manager.list_agents()]}


@router.get("/agents/{agent_id}")
async def get_agent_info(agent_id: str):
    """获取单个智能体信息"""
    manager = get_agent_manager()
    agent = manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return {"agent": _normalize_agent_avatar_payload(manager, agent)}


@router.post("/agents")
async def create_agent(request: AgentCreateRequest):
    """创建新智能体（支持从角色模板创建）"""
    manager = get_agent_manager()
    template = request.template or "default"

    # 检查是否是角色模板（templates/ 目录下有对应的目录）
    template_dir = manager.templates_dir / template
    if template_dir.exists() and template != "default":
        agent = manager.create_from_template(
            template_name=template,
            name=request.name,
            description=request.description or "",
            avatar=request.avatar or "🤖"
        )
    else:
        agent = manager.create_agent(
            name=request.name,
            description=request.description or "",
            avatar=request.avatar or "🤖",
            template=template
        )
    return {"agent": _normalize_agent_avatar_payload(manager, agent)}


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, request: AgentUpdateRequest):
    """更新智能体信息"""
    manager = get_agent_manager()
    updates = {k: v for k, v in request.dict().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="没有需要更新的字段")

    result = manager.update_agent(agent_id, updates)
    if not result:
        raise HTTPException(status_code=404, detail="智能体不存在")

    # 清除缓存，下次请求会重新加载
    clear_agent_cache(agent_id)
    return {"agent": _normalize_agent_avatar_payload(manager, manager.get_agent(agent_id))}


@router.post("/agents/{agent_id}/save-as-template")
async def save_agent_as_template(agent_id: str, request: SaveAsTemplateRequest):
    """把角色实例反向冻结成一个新模板。

    错误码语义：
    - 404：源 agent 不存在
    - 400：template_id 格式不合法 / Schema v2 校验失败（含 errors 清单）
    - 409：template_id 已存在（用户没传 overwrite）/ 撞内置模板
    """
    from backend.agent_config import TemplateValidationError

    manager = get_agent_manager()
    try:
        tmpl_meta = manager.save_as_template(
            agent_id=agent_id,
            template_id=request.template_id,
            overwrite=bool(request.overwrite),
            include_memory=bool(request.include_memory),
            include_user=bool(request.include_user),
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except TemplateValidationError as e:
        # 字段缺失/结构不合 v2 — 给前端一个结构化清单，方便引导用户去 Workshop 补全
        raise HTTPException(
            status_code=400,
            detail={
                "code": "TEMPLATE_VALIDATION_FAILED",
                "message": "模板校验未通过，请先在工坊补全必填字段",
                "errors": e.errors,
            },
        )
    except ValueError as e:
        msg = str(e)
        # 冲突类（已存在 / 内置模板）走 409；格式不合法走 400
        if ("已存在" in msg) or ("内置" in msg):
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)

    logger.info("[TEMPLATES] saved-as-template: agent=%s -> template=%s", agent_id, request.template_id)
    return {"template": tmpl_meta}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str):
    """删除智能体"""
    if agent_id == "default":
        raise HTTPException(status_code=403, detail="不能删除默认智能体")

    manager = get_agent_manager()
    if not manager.delete_agent(agent_id):
        raise HTTPException(status_code=404, detail="智能体不存在")

    # 级联：角色目录（含 sessions/）已被 rmtree 物理带走，这里只清 SessionManager 内存索引
    try:
        evicted = await get_session_manager()._evict_agent_sessions(agent_id)
        if evicted:
            logger.info("[AGENT] evicted %s sessions for deleted agent %s", evicted, agent_id)
    except Exception as e:
        logger.warning("[AGENT] failed to evict sessions for %s: %s", agent_id, e)

    clear_agent_cache(agent_id)
    return {"status": "success", "message": "智能体已删除"}


@router.post("/agents/{agent_id}/sessions/clear-model-override")
async def clear_agent_sessions_model_override(agent_id: str):
    """清除该角色下所有 main 会话的 per-session model override。

    用户在人设工坊改了角色默认模型后，可一键把所有老会话的"曾切过的模型"清掉，
    让它们回落到 agent.active_model。仅清 session.model 字段，消息/token_usage
    /metadata 一概不动。worker 子会话不动。
    """
    manager = get_agent_manager()
    if not manager.get_agent(agent_id):
        raise HTTPException(status_code=404, detail="智能体不存在")
    cleared = await get_session_manager().clear_session_models_for_agent(agent_id)
    return {"cleared": cleared, "agent_id": agent_id}
