"""智能体状态 API（情感 / 面具 / 配置文件 / 模板同步）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
依赖均可直接 import，不反向依赖 main.py：
- get_agent_manager / EMOTION_STATES（backend.agent_config）
- get_session_manager（backend.session）
- get_mask_system（backend.mask_system）
- clear_agent_cache（backend.agent）

端点：
- GET /agents/{agent_id}/emotion          情感状态（优先 Session）
- PUT /agents/{agent_id}/emotion          手动更新情感
- GET /agents/{agent_id}/mask-state       面具觉醒状态
- POST /agents/{agent_id}/mask/return     强制面具回落
- GET /agents/{agent_id}/files/{filename} 读配置文件（实例/模板）
- PUT /agents/{agent_id}/files/{filename} 写配置文件
- GET /agents/{agent_id}/template-sync    实例↔模板版本同步检查
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException

from backend.agent import clear_agent_cache
from backend.agent_config import EMOTION_STATES, get_agent_manager
from backend.dev_features import is_gene_editor_enabled
from backend.mask_system import get_mask_system
from backend.session import get_session_manager

router = APIRouter(tags=["agent-state"])


# ====== 情感 API ======

@router.get("/agents/{agent_id}/emotion")
async def get_agent_emotion(agent_id: str, session_id: Optional[str] = None):
    """获取智能体情感状态（优先从 Session 读取）"""
    if session_id:
        session_mgr = get_session_manager()
        session = await session_mgr.get_session(session_id)
        if session:
            return {"emotion": session.emotion_state or session.load_emotion(), "states": EMOTION_STATES}

    # fallback：从 agent 目录读取（兼容无 session 的场景）
    manager = get_agent_manager()
    config = manager.get_agent_config(agent_id)
    if not config:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return {"emotion": config.load_emotion(), "states": EMOTION_STATES}


@router.put("/agents/{agent_id}/emotion")
async def update_agent_emotion(agent_id: str, body: dict, session_id: Optional[str] = None):
    """手动更新智能体情感状态（优先写入 Session）"""
    if session_id:
        session_mgr = get_session_manager()
        session = await session_mgr.get_session(session_id)
        if session:
            emotion = session.emotion_state or session.load_emotion()
            if "current" in body:
                if body["current"] not in EMOTION_STATES:
                    raise HTTPException(status_code=400, detail=f"无效的情感状态，可选: {list(EMOTION_STATES.keys())}")
                emotion["current"] = body["current"]
            if "intensity" in body:
                emotion["intensity"] = max(0.0, min(1.0, float(body["intensity"])))
            await session_mgr.update_emotion(session_id, emotion)
            return {"emotion": emotion}

    # fallback：写入 agent 目录
    manager = get_agent_manager()
    config = manager.get_agent_config(agent_id)
    if not config:
        raise HTTPException(status_code=404, detail="智能体不存在")

    emotion = config.load_emotion()
    if "current" in body:
        if body["current"] not in EMOTION_STATES:
            raise HTTPException(status_code=400, detail=f"无效的情感状态，可选: {list(EMOTION_STATES.keys())}")
        emotion["current"] = body["current"]
    if "intensity" in body:
        emotion["intensity"] = max(0.0, min(1.0, float(body["intensity"])))

    config.save_emotion(emotion)
    return {"emotion": emotion}


# ====== 面具状态 API ======

@router.get("/agents/{agent_id}/mask-state")
async def get_agent_mask_state(agent_id: str, session_id: Optional[str] = None):
    """获取智能体面具觉醒状态（优先从 Session 读取）"""
    emotion_data = None
    if session_id:
        session_mgr = get_session_manager()
        session = await session_mgr.get_session(session_id)
        if session:
            emotion_data = session.emotion_state or session.load_emotion()

    mask_sys = get_mask_system()
    status = mask_sys.get_status(agent_id, emotion_data=emotion_data)
    if not status.get("supported"):
        return {"supported": False, "message": "该智能体不支持面具系统"}
    return status


@router.post("/agents/{agent_id}/mask/return")
async def force_mask_return(agent_id: str, session_id: Optional[str] = None):
    """强制让面具状态回落到平静态"""
    emotion_data = None
    target_session = None
    if session_id:
        session_mgr = get_session_manager()
        target_session = await session_mgr.get_session(session_id)
        if target_session:
            emotion_data = target_session.emotion_state or target_session.load_emotion()

    mask_sys = get_mask_system()
    result = mask_sys.force_return(agent_id, emotion_data=emotion_data)
    if not result:
        return {"supported": False, "message": "该智能体不支持面具系统"}

    # 持久化到 Session
    if target_session:
        await session_mgr.update_emotion(session_id, result)
        target_session.emotion_state = result

    return {"status": "returned", "mask_state": result}


# ====== 智能体配置文件 API ======

@router.get("/agents/{agent_id}/files/{filename}")
async def get_agent_file(agent_id: str, filename: str):
    """获取智能体配置文件内容（支持实例和模板目录）"""
    # 允许访问的文件列表
    allowed_files = ("IDENTITY.md", "SOUL.md", "PERSONALITY.md", "MEMORY.md", "GENOME.md", "REFLECTIONS.md", "SKILL_EXPERIENCE.md")
    if filename not in allowed_files:
        raise HTTPException(status_code=400, detail="不允许访问此文件")
    if filename == "GENOME.md" and not is_gene_editor_enabled():
        raise HTTPException(status_code=404, detail="找不到此文件")

    manager = get_agent_manager()
    config = manager.get_agent_config(agent_id)

    # 实例目录优先，回退到模板目录
    target_dir = None
    if config and config.agent_dir.exists():
        target_dir = config.agent_dir
    else:
        # 尝试从模板目录读取（工坊新建模式需要）
        tpl_path = manager.templates_dir / agent_id
        if (tpl_path / filename).exists():
            target_dir = tpl_path

    if not target_dir:
        raise HTTPException(status_code=404, detail=f"找不到 {agent_id}")

    import os
    fp = target_dir / filename
    with open(fp, 'r', encoding='utf-8') as f:
        content = f.read()

    if filename == "GENOME.md":
        return {"filename": filename, "content": content, "read_only": True}
    return {"filename": filename, "content": content}


@router.put("/agents/{agent_id}/files/{filename}")
async def update_agent_file(agent_id: str, filename: str, body: dict):
    """更新智能体配置文件内容"""
    allowed_files = ("IDENTITY.md", "SOUL.md", "PERSONALITY.md", "GENOME.md", "MEMORY.md", "REFLECTIONS.md")
    if filename not in allowed_files:
        raise HTTPException(status_code=400, detail="不允许修改此文件")

    # GENOME.md 研发阶段开放编辑（生产环境可重新加锁）
    # if filename == "GENOME.md":
    #     raise HTTPException(status_code=403, detail="GENOME.md 为不可变基因文件，不允许通过常规接口修改。请使用专门的基因编辑功能。")
    if filename == "GENOME.md" and not is_gene_editor_enabled():
        raise HTTPException(status_code=404, detail="找不到此文件")

    manager = get_agent_manager()
    config = manager.get_agent_config(agent_id)
    if not config:
        raise HTTPException(status_code=404, detail="智能体不存在")

    content = body.get("content", "")
    config.write_file(config.agent_dir / filename, content)

    # 清除缓存
    clear_agent_cache(agent_id)
    return {"status": "success"}


# ====== 模板同步 API ======

@router.get("/agents/{agent_id}/template-sync")
async def check_template_sync(agent_id: str):
    """检查智能体实例与模板之间的版本同步状态"""
    manager = get_agent_manager()
    agent = manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="智能体不存在")

    template_name = agent.get("template", "")
    if not template_name or template_name == "default":
        return {
            "agent_id": agent_id,
            "template_name": template_name,
            "has_update": False,
            "message": "该智能体未从模板创建"
        }

    # 获取当前模板版本
    template_dir = manager.templates_dir / template_name
    tmpl_version = 1
    if template_dir.exists():
        tmpl_meta_file = template_dir / "meta.json"
        if tmpl_meta_file.exists():
            try:
                tmpl_meta = json.loads(tmpl_meta_file.read_text(encoding='utf-8-sig'))
                tmpl_version = tmpl_meta.get("meta", {}).get("version", 1)
            except Exception:
                pass

    # 获取实例记录的版本
    inst_version = agent.get("template_version", 1)
    sync_info = agent.get("template_sync", {})

    has_update = tmpl_version > inst_version

    return {
        "agent_id": agent_id,
        "template_name": template_name,
        "instance_version": inst_version,
        "template_latest_version": tmpl_version,
        "has_update": has_update,
        "sync_info": sync_info,
        "message": f"模板有新版本 ({inst_version} → {tmpl_version})" if has_update else "已是最新版本"
    }
