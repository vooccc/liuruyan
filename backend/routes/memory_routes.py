"""记忆系统 API（/memory/*）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
依赖均可直接 import，不反向依赖 main.py：
- get_memory_manager（backend.memory_manager）—— 日记/长期记忆/反思读写
- get_agent（backend.agent）—— 仅 POST /memory 写入记忆时用

端点：
- POST   /memory                       添加记忆
- GET    /memory/today                 今日日记
- GET    /memory/long-term             长期记忆
- GET    /memory/diaries               日记日期列表
- GET    /memory/diary/{date}          指定日期日记
- PUT    /memory/diary/{date}          更新指定日期日记
- PUT    /memory/long-term             更新长期记忆
- GET    /memory/reflections           行为洞察
- POST   /memory/reflections           保存行为洞察
- DELETE /memory/reflections/{agent_id} 删除某条洞察
- GET    /memory/temp-stats            记忆温度统计
- GET    /memory/report                记忆大小报告
- GET    /memory/user                  用户画像 USER.md
- PUT    /memory/user                  更新用户画像

v5 项目命名空间（2026-10-24，详见 backend/namespace_manager.py）：
- GET    /memory/namespaces            项目注册表（含启用状态与统计）
- POST   /memory/namespaces            注册项目
- PUT    /memory/namespaces/{ns_id}    更新项目（名称/别名/工作区/摘要）
- DELETE /memory/namespaces/{ns_id}    注销项目（数据归档不物理删除）
- GET    /memory/namespaces/{ns_id}    项目状态卡 + timeline 尾部
- PUT    /memory/namespaces/{ns_id}/card  覆写状态卡
- GET    /memory/global                全局层（关系记忆）
- PUT    /memory/global                覆写全局层
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.agent import get_agent
from backend.memory_manager import get_memory_manager

router = APIRouter(tags=["memory"])


class MemoryRequest(BaseModel):
    content: str
    memory_type: str = "diary"
    agent_id: Optional[str] = None


class MemoryUpdateRequest(BaseModel):
    content: str


class NamespaceCreateRequest(BaseModel):
    name: str
    aliases: List[str] = []
    workspace_path: Optional[str] = None
    agent_id: Optional[str] = None


class NamespaceUpdateRequest(BaseModel):
    name: Optional[str] = None
    aliases: Optional[List[str]] = None
    workspace_path: Optional[str] = None
    summary: Optional[str] = None
    status: Optional[str] = None
    agent_id: Optional[str] = None


@router.post("/memory")
async def add_memory(request: MemoryRequest):
    """添加记忆"""
    agent_id = request.agent_id or "default"
    agent = get_agent(agent_id)
    agent.remember(request.content, request.memory_type)
    return {"status": "success", "message": "已记住"}


@router.get("/memory/today")
async def get_today_memory(agent_id: Optional[str] = None):
    """获取今日日记"""
    memory_manager = get_memory_manager(agent_id=agent_id or "default")
    content = memory_manager.read_diary()
    return {"date": memory_manager.get_today_diary_path().stem, "content": content}


@router.get("/memory/long-term")
async def get_long_term_memory(agent_id: Optional[str] = None):
    """获取长期记忆"""
    memory_manager = get_memory_manager(agent_id=agent_id or "default")
    content = memory_manager.read_long_term_memory()
    return {"content": content}


@router.get("/memory/diaries")
async def list_diaries(agent_id: Optional[str] = None):
    """列出所有日记日期"""
    memory_manager = get_memory_manager(agent_id=agent_id or "default")
    diaries = []
    for f in memory_manager.list_diary_files():
        diaries.append({"date": f.stem, "path": str(f)})
    return {"diaries": diaries}


@router.get("/memory/diary/{date}")
async def get_diary(date: str, agent_id: Optional[str] = None):
    """获取指定日期的日记"""
    memory_manager = get_memory_manager(agent_id=agent_id or "default")
    content = memory_manager.read_diary(date)
    if not content:
        raise HTTPException(status_code=404, detail="该日期无日记记录")
    return {"date": date, "content": content}


@router.put("/memory/diary/{date}")
async def update_diary(date: str, request: MemoryUpdateRequest, agent_id: Optional[str] = None):
    """更新指定日期的日记"""
    memory_manager = get_memory_manager(agent_id=agent_id or "default")
    memory_manager.write_diary(request.content, date=date, append=False)
    return {"status": "success"}


@router.put("/memory/long-term")
async def update_long_term_memory(request: MemoryUpdateRequest, agent_id: Optional[str] = None):
    """更新长期记忆"""
    memory_manager = get_memory_manager(agent_id=agent_id or "default")
    memory_manager.write_long_term_memory(request.content)
    return {"status": "success"}


@router.get("/memory/reflections")
async def get_reflections(agent_id: Optional[str] = None):
    """获取行为洞察 REFLECTIONS.md"""
    memory_manager = get_memory_manager(agent_id=agent_id or "default")
    return {"content": memory_manager.read_reflections()}


@router.post("/memory/reflections")
async def save_reflections(request: Request):
    """保存 REFLECTIONS.md 内容（用户手动编辑）"""
    data = await request.json()
    content = data.get("content", "")
    agent_id = data.get("agent_id", "default")
    memory_manager = get_memory_manager(agent_id=agent_id)
    memory_manager.write_reflections(content)
    return {"ok": True, "message": f"{agent_id} 的行为洞察已保存"}


@router.delete("/memory/reflections/{agent_id}")
async def delete_reflection_item(agent_id: str, request: Request):
    """删除 REFLECTIONS 中的某条洞察（按索引）"""
    data = await request.json()
    index = data.get("index", -1)
    memory_manager = get_memory_manager(agent_id=agent_id)
    text = memory_manager.read_reflections()

    if not text:
        raise HTTPException(status_code=404, detail="REFLECTIONS.md 不存在")

    # 按段落分割，找到 ## 开头的洞察条目
    lines = text.split("\n")
    sections = []
    current_section = []
    for line in lines:
        if line.startswith("## ") and current_section:
            sections.append(current_section)
            current_section = [line]
        else:
            current_section.append(line)
    if current_section:
        sections.append(current_section)

    # index 指向的是从0开始的第几个 ## 条目
    if 0 <= index < len(sections):
        removed_name = ""
        for s in sections[index]:
            if s.startswith("## "):
                removed_name = s[3:]
                break
        del sections[index]
        new_text = "\n".join(s for sec in sections for s in sec)
        memory_manager.write_reflections(new_text)
        return {"ok": True, "message": f'已删除洞察: "{removed_name}"'}
    else:
        raise HTTPException(status_code=400, detail=f"索引 {index} 超出范围（共 {len(sections)} 条）")


@router.get("/memory/temp-stats")
async def get_temp_stats(agent_id: Optional[str] = None):
    """获取记忆温度统计"""
    memory_manager = get_memory_manager(agent_id=agent_id or "default")
    return memory_manager.get_temp_stats()


@router.get("/memory/report")
async def get_memory_report(agent_id: Optional[str] = None):
    """获取记忆系统大小报告"""
    memory_manager = get_memory_manager(agent_id=agent_id or "default")
    return memory_manager.get_memory_size_report()


@router.get("/memory/user")
async def get_user_profile(agent_id: Optional[str] = None):
    """获取用户画像 USER.md"""
    memory_manager = get_memory_manager(agent_id=agent_id or "default")
    content = memory_manager.read_user_profile()
    return {"content": content}


@router.put("/memory/user")
async def update_user_profile(request: MemoryUpdateRequest, agent_id: Optional[str] = None):
    """更新用户画像 USER.md"""
    memory_manager = get_memory_manager(agent_id=agent_id or "default")
    memory_manager.write_user_profile(request.content)
    return {"status": "success", "ok": True}


@router.post("/memory/user/reset")
async def reset_user_profile(agent_id: Optional[str] = None):
    """重置用户画像为默认模板"""
    from backend.agent_config import DEFAULT_USER_MD
    memory_manager = get_memory_manager(agent_id=agent_id or "default")
    memory_manager.write_user_profile(DEFAULT_USER_MD)
    return {"status": "success", "ok": True, "content": DEFAULT_USER_MD}


# ====== v5 项目命名空间（2026-10-24）======


def _ns_manager(agent_id: Optional[str]):
    return get_memory_manager(agent_id=agent_id or "default").namespace_manager


@router.get("/memory/namespaces")
async def list_namespaces(agent_id: Optional[str] = None):
    """项目注册表 + 启用状态。namespaces 未启用（无 REGISTRY.json）时 active=false，
    前端据此提示先跑迁移/创建首个项目。"""
    manager = _ns_manager(agent_id)
    return {
        "active": manager.is_active(),
        "namespaces": manager.list_namespaces(),
        "stats": manager.stats(),
    }


@router.post("/memory/namespaces")
async def create_namespace(request: NamespaceCreateRequest):
    """注册新项目命名空间。首个项目创建时自动落盘 REGISTRY.json（启用 v5 模式）。"""
    if not request.name.strip():
        raise HTTPException(status_code=400, detail="项目名不能为空")
    manager = _ns_manager(request.agent_id)
    entry = manager.create_namespace(
        name=request.name,
        aliases=request.aliases,
        workspace_path=request.workspace_path,
    )
    return {"status": "success", "namespace": entry, "active": manager.is_active()}


@router.put("/memory/namespaces/{ns_id}")
async def update_namespace(ns_id: str, request: NamespaceUpdateRequest):
    """更新项目元数据（名称/别名/工作区锚/一行摘要/归档状态）。"""
    manager = _ns_manager(request.agent_id)
    entry = manager.update_namespace(
        ns_id,
        name=request.name,
        aliases=request.aliases,
        workspace_path=request.workspace_path,
        summary=request.summary,
        status=request.status,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail=f"项目命名空间不存在: {ns_id}")
    return {"status": "success", "namespace": entry}


@router.delete("/memory/namespaces/{ns_id}")
async def delete_namespace(ns_id: str, agent_id: Optional[str] = None):
    """注销项目：摘注册表 + 数据挪 _archived/ 留底（可手工恢复）。"""
    manager = _ns_manager(agent_id)
    if not manager.delete_namespace(ns_id):
        raise HTTPException(status_code=404, detail=f"项目命名空间不存在: {ns_id}")
    return {"status": "success", "message": f"项目 {ns_id} 已注销（数据已归档）"}


@router.get("/memory/namespaces/{ns_id}")
async def get_namespace(ns_id: str, agent_id: Optional[str] = None):
    """读取项目详情：注册表项 + 状态卡 + timeline 尾部。"""
    manager = _ns_manager(agent_id)
    entry = manager.get_namespace(ns_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"项目命名空间不存在: {ns_id}")
    return {
        "namespace": entry,
        "card": manager.read_card(ns_id),
        "timeline": manager.read_timeline(ns_id, limit=50),
    }


@router.put("/memory/namespaces/{ns_id}/card")
async def update_namespace_card(
    ns_id: str, request: MemoryUpdateRequest, agent_id: Optional[str] = None
):
    """覆写项目状态卡（用户在记忆面板手动编辑）。timeline 不受影响。"""
    manager = _ns_manager(agent_id)
    if manager.get_namespace(ns_id) is None:
        raise HTTPException(status_code=404, detail=f"项目命名空间不存在: {ns_id}")
    manager.write_card(ns_id, request.content)
    return {"status": "success", "ok": True}


@router.get("/memory/global")
async def get_global_memory(agent_id: Optional[str] = None):
    """读取全局层（关系里程碑 + 跨项目认知）GLOBAL.md"""
    return {"content": _ns_manager(agent_id).read_global()}


@router.put("/memory/global")
async def update_global_memory(
    request: MemoryUpdateRequest, agent_id: Optional[str] = None
):
    """覆写全局层（用户在记忆面板手动编辑）"""
    _ns_manager(agent_id).write_global(request.content)
    return {"status": "success", "ok": True}
