"""技能管理 API（/skills/* · /skill-packs/* · /agents/{id}/skills*）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
依赖均可直接 import，不反向依赖 main.py：
- get_skill_loader（backend.skill_loader）
- get_skill_growth_manager（backend.skill_growth）
- get_skill_promotion_manager（backend.skill_promotion）
- get_agent_manager（backend.agent_config）/ clear_agent_cache（backend.agent）
- require_dev_mode（backend.dev_mode）

注意：原 main.py 中 equip/unequip/set_agent_skills 用 `from .agent import
clear_agent_cache` 相对导入，迁到 routes/ 包后改用模块级 import（同一函数，
行为不变）。

端点：技能列表/开关/重载、skill-pack 安装管理、技能内容编辑、
角色技能装备、技能晋升评审。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.agent import clear_agent_cache
from backend.agent_config import get_agent_manager
from backend.dev_mode import require_dev_mode
from backend.skill_growth import get_skill_growth_manager
from backend.skill_loader import get_skill_loader
from backend.skill_promotion import get_skill_promotion_manager

router = APIRouter(tags=["skills"])


class SkillToggleRequest(BaseModel):
    enabled: bool


class SkillContentUpdateRequest(BaseModel):
    """编辑 SKILL.md body 请求体。frontmatter 部分由后端保留，只覆盖 body。"""
    content: str


class EquipSkillRequest(BaseModel):
    skill_name: str


class SetSkillsRequest(BaseModel):
    skill_names: List[str]


class SkillPromotionReviewRequest(BaseModel):
    review_notes: str = ""


# ----- 技能管理 -----
@router.get("/skills")
async def list_skills():
    """获取所有技能列表（含启用状态）"""
    skill_loader = get_skill_loader()
    skills = []
    for name, skill in skill_loader.skills.items():
        skills.append({
            "name": skill.name,
            "display_name": skill.display_name or skill.name,
            "description": skill.description,
            "disabled": skill.disabled,
            "path": str(skill.path)
        })
    return {"skills": skills}


@router.patch("/skills/{skill_name}/toggle")
async def toggle_skill(skill_name: str, request: SkillToggleRequest):
    """切换技能启用/禁用状态"""
    skill_loader = get_skill_loader()
    skill = skill_loader.skills.get(skill_name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"技能 '{skill_name}' 不存在")

    skill.disabled = not request.enabled
    skill_loader.reload()
    return {
        "status": "success",
        "name": skill_name,
        "enabled": request.enabled
    }


@router.post("/skills/reload")
async def reload_skills():
    """重新加载所有技能"""
    skill_loader = get_skill_loader()
    skill_loader.reload()
    return {"status": "success", "count": len(skill_loader.skills)}


# ----- Skill Pack 管理（兼容 Anthropic Claude Code Plugin 协议） -----
# 文档：_docs/skill_marketplace_research.md
# 实现：backend/skill_pack_manager.py
# UI：frontend/src/components/SkillPackPanel.tsx
#
# 安全取舍：跟 MCP server 安装一样不强制 dev mode——pack 管理是用户主动行为，
# UX 上加门会造成"我装个网文写作系统也得开开发者模式"的反直觉体验。
# 但所有 pack_name 都经过 _VALID_PACK_NAME 校验，目录写入限于 skills/<pack>/，
# git clone 用 capture_output 不直连终端。

class SkillPackInstallRequest(BaseModel):
    source: Any  # str (github 简写 / 完整 URL) 或 dict ({source, repo|url, ref?, path?})
    pack_name: Optional[str] = None
    create_venv: bool = True
    install_deps: bool = True
    overwrite: bool = False


class SkillPackUpdateRequest(BaseModel):
    install_deps: bool = True


@router.get("/skill-packs")
async def list_installed_skill_packs():
    """列出所有已安装的 skill pack（含版本、source、venv 状态等元信息）。"""
    from backend.skill_pack_manager import list_packs
    return {"packs": list_packs()}


@router.get("/skill-packs/{pack_name}")
async def get_skill_pack_status(pack_name: str):
    """单 pack 详情：manifest / 元信息 / 内含 skill 列表。"""
    from backend.skill_pack_manager import SkillPackError, pack_status
    try:
        return await asyncio.to_thread(pack_status, pack_name=pack_name)
    except SkillPackError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取失败: {exc}")


@router.post("/skill-packs/install")
async def install_skill_pack_route(request: SkillPackInstallRequest):
    """安装一个 plugin pack。

    长耗时（git clone + pip install 可能分钟级）走 asyncio.to_thread，
    HTTP 请求保持响应；前端可显示 loading 直到 200 / 错误返回。
    """
    from backend.skill_pack_manager import SkillPackError, install_pack
    try:
        result = await asyncio.to_thread(
            install_pack,
            source=request.source,
            pack_name=request.pack_name,
            create_venv=request.create_venv,
            install_deps=request.install_deps,
            overwrite=request.overwrite,
        )
    except SkillPackError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"unexpected: {exc}")
    # 装好后让 SkillLoader 立刻看到新 skill
    get_skill_loader().reload()
    return result


@router.post("/skill-packs/{pack_name}/update")
async def update_skill_pack_route(pack_name: str, request: Optional[SkillPackUpdateRequest] = None):
    """git pull --ff-only + 刷新 venv 依赖。"""
    from backend.skill_pack_manager import SkillPackError, update_pack
    install_deps = True
    if request is not None:
        install_deps = bool(request.install_deps)
    try:
        result = await asyncio.to_thread(
            update_pack,
            pack_name=pack_name,
            install_deps=install_deps,
        )
    except SkillPackError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"unexpected: {exc}")
    get_skill_loader().reload()
    return result


@router.delete("/skill-packs/{pack_name}")
async def uninstall_skill_pack_route(pack_name: str):
    """删 pack 目录。安全检查在 manager 层（必须有 .liuruyan-pack.json 或 .claude-plugin/）。"""
    from backend.skill_pack_manager import SkillPackError, uninstall_pack
    try:
        result = await asyncio.to_thread(uninstall_pack, pack_name=pack_name)
    except SkillPackError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"unexpected: {exc}")
    get_skill_loader().reload()
    return result


# 技能内容编辑安全上限（纪律全文被注入 system prompt，必须 cap）
_SKILL_CONTENT_MAX_BYTES = 30_000


@router.get("/skills/{skill_name}/content")
async def get_skill_content_raw(skill_name: str):
    """读取技能 SKILL.md body（用于 dev mode 编辑器初始化）。

    返回 body 段（不含 frontmatter）。读端无 dev mode 守门——展示用，
    跟 GET /skills 的 description 字段是同等暴露面。
    """
    skill_loader = get_skill_loader()
    skill = skill_loader.skills.get(skill_name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"技能 '{skill_name}' 不存在")
    return {
        "name": skill_name,
        "content": skill.content or "",
        "category": skill.category or "",
        "path": str(skill.path) if skill.path else "",
    }


@router.put("/skills/{skill_name}/content", dependencies=[Depends(require_dev_mode)])
async def update_skill_content(skill_name: str, request: SkillContentUpdateRequest):
    """编辑技能 SKILL.md body（仅 dev mode）。

    - 保留原 frontmatter，只覆盖 body 部分。
    - skill_name 必须已存在于 skill_loader.skills，避免任意路径写入。
    - 写入后自动 reload，后续 LLM 调用立即拿到新 content。

    主要面向 category=='discipline' 的纪律类技能（全文常驻 system prompt），
    但后端不限制 category——只要 skill 存在都可编辑。
    """
    skill_loader = get_skill_loader()
    skill = skill_loader.skills.get(skill_name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"技能 '{skill_name}' 不存在")

    new_body = (request.content or "").strip()
    if len(new_body.encode("utf-8")) > _SKILL_CONTENT_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"内容超过 {_SKILL_CONTENT_MAX_BYTES} 字节上限",
        )

    file_path = skill.path
    if file_path is None or not file_path.exists():
        raise HTTPException(status_code=500, detail=f"技能文件不存在: {file_path}")

    try:
        original = file_path.read_text(encoding="utf-8")
        if original.startswith("---"):
            parts = original.split("---", 2)
            if len(parts) >= 3:
                # parts[0]='', parts[1]=frontmatter, parts[2]=原 body
                new_text = f"---{parts[1]}---\n\n{new_body}\n"
            else:
                # 异常结构（开头是 --- 但没闭合）——拒写，避免圾圾
                raise HTTPException(
                    status_code=500,
                    detail="frontmatter 结构异常，为避免损坏原文件拒绝写入",
                )
        else:
            # 原本没 frontmatter，直接全文替换
            new_text = f"{new_body}\n"
        file_path.write_text(new_text, encoding="utf-8")
        skill_loader.reload()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"写入技能文件失败: {exc}")

    return {
        "status": "success",
        "name": skill_name,
        "bytes": len(new_text.encode("utf-8")),
        "path": str(file_path),
    }


# ----- 角色技能装备 -----
@router.get("/agents/{agent_id}/skills")
async def get_agent_skills(agent_id: str):
    """获取角色已装备的技能 + 全局技能池（供前端装备选择）"""
    skill_loader = get_skill_loader()
    agent_mgr = get_agent_manager()
    agent = agent_mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"角色 '{agent_id}' 不存在")

    growth_manager = get_skill_growth_manager(agent_id)
    equipped = skill_loader.get_equipped_skills(agent_id)
    recommended_names = {
        item["name"] for item in skill_loader.get_recommended_skills(agent_id)
    }

    def _serialize_skill(skill, *, is_equipped: bool) -> Dict[str, object]:
        mastery = growth_manager.get_skill_record(skill.name)
        return {
            "name": skill.name,
            "display_name": skill.display_name or skill.name,
            "description": skill.description,
            "category": skill.category,
            "tags": skill.tags or [],
            "equipped": is_equipped,
            "level": mastery["level"],
            "level_label": mastery["level_label"],
            "xp": mastery["xp"],
            "xp_to_next": mastery["xp_to_next"],
            "next_level_xp": mastery["next_level_xp"],
            "progress_percent": mastery["progress_percent"],
            "total_uses": mastery["total_uses"],
            "successful_uses": mastery["successful_uses"],
            "failed_uses": mastery["failed_uses"],
            "promotable": mastery["promotable"],
            "last_used_at": mastery.get("last_used_at"),
            "last_summary": mastery.get("last_summary", ""),
        }

    # 全局技能池（标注是否已装备）
    all_skills = []
    for name, skill in skill_loader.skills.items():
        all_skills.append(_serialize_skill(skill, is_equipped=name in equipped))

    equipped_details = [
        skill_info for skill_info in all_skills if skill_info["equipped"]
    ]
    recommended = [
        skill_info for skill_info in all_skills
        if skill_info["name"] in recommended_names and not skill_info["equipped"]
    ]

    # 按分类分组
    by_category = {}
    for s in all_skills:
        cat = s["category"] or "未分类"
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(s)

    return {
        "agent_id": agent_id,
        "equipped": equipped,
        "equipped_details": equipped_details,
        "recommended": recommended,
        "all_skills": all_skills,
        "by_category": by_category,
        "total_available": len(all_skills),
    }


@router.get("/skills/promotions")
async def list_skill_promotions(
    agent_id: Optional[str] = None,
    status: Optional[str] = None,
    skill_name: Optional[str] = None,
):
    """列出技能晋升候选。支持按角色、状态、技能过滤。"""
    promotion_manager = get_skill_promotion_manager()
    candidates = promotion_manager.list_candidates(
        agent_id=agent_id,
        status=status,
        skill_name=skill_name,
    )
    return {
        "candidates": candidates,
        "count": len(candidates),
    }


@router.post("/agents/{agent_id}/skills/{skill_name}/promote")
async def submit_skill_promotion(agent_id: str, skill_name: str):
    """把角色私有技能经验提交为系统级晋升候选。"""
    agent_mgr = get_agent_manager()
    agent = agent_mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"角色 '{agent_id}' 不存在")

    promotion_manager = get_skill_promotion_manager()
    try:
        result = promotion_manager.create_candidate(agent_id, skill_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "status": "success",
        "created": result["created"],
        "candidate": result["candidate"],
    }


@router.post("/skills/promotions/{candidate_id}/approve")
async def approve_skill_promotion(candidate_id: str, request: SkillPromotionReviewRequest):
    """批准技能晋升候选。批准后才能应用到全局技能。"""
    try:
        candidate = get_skill_promotion_manager().review_candidate(
            candidate_id,
            approved=True,
            review_notes=request.review_notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "status": "success",
        "candidate": candidate,
    }


@router.post("/skills/promotions/{candidate_id}/reject")
async def reject_skill_promotion(candidate_id: str, request: SkillPromotionReviewRequest):
    """驳回技能晋升候选。"""
    try:
        candidate = get_skill_promotion_manager().review_candidate(
            candidate_id,
            approved=False,
            review_notes=request.review_notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "status": "success",
        "candidate": candidate,
    }


@router.post("/skills/promotions/{candidate_id}/apply")
async def apply_skill_promotion(candidate_id: str, request: SkillPromotionReviewRequest):
    """将已批准的候选应用到共享技能文件，并重新加载技能池。"""
    try:
        candidate = get_skill_promotion_manager().apply_candidate(
            candidate_id,
            review_notes=request.review_notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "status": "success",
        "candidate": candidate,
    }


@router.post("/agents/{agent_id}/skills/equip")
async def equip_skill(agent_id: str, request: EquipSkillRequest):
    """给角色装备技能"""
    skill_loader = get_skill_loader()
    agent_mgr = get_agent_manager()
    agent = agent_mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"角色 '{agent_id}' 不存在")

    if request.skill_name not in skill_loader.skills:
        raise HTTPException(status_code=404, detail=f"技能 '{request.skill_name}' 不存在")

    success = skill_loader.equip_skill(agent_id, request.skill_name)
    if not success:
        raise HTTPException(status_code=500, detail="装备技能失败")

    # 清除 Agent 实例缓存，下次对话使用新技能配置
    clear_agent_cache(agent_id)

    return {
        "status": "success",
        "agent_id": agent_id,
        "skill_name": request.skill_name,
        "equipped": skill_loader.get_equipped_skills(agent_id),
    }


@router.post("/agents/{agent_id}/skills/unequip")
async def unequip_skill(agent_id: str, request: EquipSkillRequest):
    """给角色卸载技能"""
    skill_loader = get_skill_loader()
    agent_mgr = get_agent_manager()
    agent = agent_mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"角色 '{agent_id}' 不存在")

    success = skill_loader.unequip_skill(agent_id, request.skill_name)
    if not success:
        raise HTTPException(status_code=500, detail="卸载技能失败")

    clear_agent_cache(agent_id)

    return {
        "status": "success",
        "agent_id": agent_id,
        "skill_name": request.skill_name,
        "equipped": skill_loader.get_equipped_skills(agent_id),
    }


@router.put("/agents/{agent_id}/skills")
async def set_agent_skills(agent_id: str, request: SetSkillsRequest):
    """批量设置角色装备的技能（覆盖）"""
    skill_loader = get_skill_loader()
    agent_mgr = get_agent_manager()
    agent = agent_mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"角色 '{agent_id}' 不存在")

    success = skill_loader.set_equipped_skills(agent_id, request.skill_names)
    if not success:
        raise HTTPException(status_code=500, detail="设置技能失败")

    clear_agent_cache(agent_id)

    return {
        "status": "success",
        "agent_id": agent_id,
        "equipped": skill_loader.get_equipped_skills(agent_id),
    }
