"""任务管理 API（/tasks/*）。

从 main.py 拆出（路由瘦身），路径与行为保持完全一致。
所有 handler 在函数体内惰性 import task_tools，不依赖 main.py 模块级全局。

端点：
- GET    /tasks                列表（按 status/session/agent 过滤）
- DELETE /tasks/completed      清已完成
- DELETE /tasks/ended          清终态（completed/failed/stale）
- DELETE /tasks/all            清空（须带 session_id 或 agent_id）
- GET    /tasks/{task_id}      详情
- DELETE /tasks/{task_id}      删单条

注意路由顺序：固定路径（/tasks/completed 等）必须先于参数路径
/tasks/{task_id} 注册，否则会被 {task_id} 抢匹配。本模块按此顺序排列。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["tasks"])


@router.get("/tasks")
async def get_tasks(
    status: Optional[str] = None,
    limit: int = 50,
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
):
    """获取任务列表

    参数:
    - status: 可选，过滤状态 (pending/in_progress/completed/failed/stale)
    - limit: 最大返回数量（默认 50）
    - session_id: 可选，按会话过滤
    - agent_id: 可选，按角色过滤
    """
    from backend.tools.builtin.task_tools import get_task_manager
    manager = get_task_manager()
    tasks = manager.list_tasks(
        status=status,
        limit=limit,
        session_id=session_id,
        agent_id=agent_id,
    )
    return {
        "success": True,
        "count": len(tasks),
        "tasks": tasks
    }


@router.delete("/tasks/completed")
async def clear_completed_tasks(session_id: Optional[str] = None):
    """清除已完成的任务

    Args:
        session_id: 仅清该会话下的 completed 任务；不传则清全局（保留旧兼容）。
                    InlineTasksPanel phase=gone 时按 session_id 调用。
    """
    from backend.tools.builtin.task_tools import get_task_manager, _emit_tasks_changed
    manager = get_task_manager()
    count = manager.clear_completed(session_id=session_id)
    if count > 0:
        _emit_tasks_changed("clear", session_id=session_id)
    return {
        "success": True,
        "message": f"已清除 {count} 个已完成任务"
    }


@router.delete("/tasks/ended")
async def clear_ended_tasks(session_id: Optional[str] = None):
    """清除所有处于终态的任务（completed / failed / stale）。

    pending / in_progress 不动。前端 InlineTasksPanel 在整组推进到 gone 时
    按 session_id 调用——避免 failed/stale task 把 allDone 卡在 false，
    造成"任务异常后输入框上方残留不消失"。
    """
    from backend.tools.builtin.task_tools import get_task_manager, _emit_tasks_changed
    manager = get_task_manager()
    count = manager.clear_ended(session_id=session_id)
    if count > 0:
        _emit_tasks_changed("clear", session_id=session_id)
    return {
        "success": True,
        "message": f"已清除 {count} 个终态任务"
    }


@router.delete("/tasks/all")
async def clear_all_tasks(
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
):
    """清空任务

    必须至少提供 session_id 或 agent_id，避免误清全局
    """
    if not session_id and not agent_id:
        raise HTTPException(
            status_code=400,
            detail="必须提供 session_id 或 agent_id，拒绝无差别清空"
        )
    from backend.tools.builtin.task_tools import get_task_manager, _emit_tasks_changed
    manager = get_task_manager()
    count = manager.clear_all(session_id=session_id, agent_id=agent_id)
    if count > 0:
        _emit_tasks_changed("clear", session_id=session_id, agent_id=agent_id)
    return {
        "success": True,
        "deleted": count,
        "message": f"已清除 {count} 个任务"
    }


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """获取任务详情"""
    from backend.tools.builtin.task_tools import get_task_manager
    manager = get_task_manager()
    try:
        task = manager.get_task(task_id)
        return {
            "success": True,
            "task": task
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除单条任务"""
    from backend.tools.builtin.task_tools import get_task_manager, _emit_tasks_changed
    manager = get_task_manager()
    try:
        # 先取出 session/agent 用于发布事件，再删除
        task = manager.get_task(task_id)
        sid = task.get('session_id')
        aid = task.get('agent_id')
        manager.delete_task(task_id)
        _emit_tasks_changed("delete", session_id=sid, agent_id=aid, task_id=task_id)
        return {"success": True, "message": f"已删除任务 #{task_id}"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
