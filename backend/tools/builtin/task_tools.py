#!/usr/bin/env python3
"""
任务管理工具 - 让 AI 能展示工作计划和进度

功能：
1. task_create - 创建新任务
2. task_update - 更新任务状态
3. task_list - 列出所有任务
4. task_get - 获取任务详情

任务状态：
- pending: 待处理
- in_progress: 进行中
- completed: 已完成
- failed: 失败
- stale: 本轮结束时未推进，自动归档
"""

import asyncio
import json
import time
import uuid
from typing import Dict, List, Optional
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def _emit_tasks_changed(
    action: str,
    *,
    task: Optional[dict] = None,
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> None:
    """前端 InlineTasksPanel 订阅这个事件做免轮询刷新。

    bus 没装配（test/CI）或不在 event loop 中（sync 单测）静默跳过；
    publish 异常 swallow 不影响主路径。

    范式参考：cron_scheduler._emit_changed (backend/cron_scheduler.py:128)
    """
    try:
        from liuruyan.buses import AgentEvent, get_event_bus
        bus = get_event_bus()
        if bus is None:
            return
        sid = (task and task.get('session_id')) or session_id or ""
        aid = (task and task.get('agent_id')) or agent_id or ""
        tid = (task and task.get('id')) or task_id
        event = AgentEvent(
            type="tasks_changed",
            trace_id=f"task-{action}-{uuid.uuid4().hex[:6]}",
            session_id=sid,
            agent_id=aid,
            user_id="local",
            payload={
                "action": action,
                "task_id": tid,
                "session_id": sid,
                "agent_id": aid,
            },
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(bus.publish(event))
    except Exception as e:
        logger.warning("[task_tools] tasks_changed publish failed: %s", e)


class TaskManager:
    """任务管理器"""

    def __init__(self, storage_path: str = None):
        """初始化任务管理器

        Args:
            storage_path: 任务存储路径（默认：backend/tasks.json）
        """
        if storage_path is None:
            from backend import paths
            storage_path = paths.tasks_json_path()

        self.storage_path = Path(storage_path)
        self.tasks: Dict[str, Dict] = {}
        self.next_id = 1

        # 加载已有任务
        self._load_tasks()

    def _load_tasks(self):
        """从文件加载任务"""
        if self.storage_path.exists():
            try:
                with open(self.storage_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.tasks = data.get('tasks', {})
                    self.next_id = data.get('next_id', 1)
                logger.info(f"Loaded {len(self.tasks)} tasks from {self.storage_path}")
            except Exception as e:
                logger.warning(f"Failed to load tasks: {e}")

    def _save_tasks(self):
        """保存任务到文件"""
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.storage_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'tasks': self.tasks,
                    'next_id': self.next_id
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save tasks: {e}")

    def create_task(
        self,
        title: str,
        description: str = "",
        status: str = "pending",
        metadata: Dict = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Dict:
        """创建新任务

        Args:
            title: 任务标题
            description: 任务描述
            status: 初始状态（默认：pending）
            metadata: 额外元数据
            session_id: 归属会话 ID（由工具调用上下文注入）
            agent_id: 归属角色 ID（由工具调用上下文注入）

        Returns:
            创建的任务对象
        """
        task_id = str(self.next_id)
        self.next_id += 1

        task = {
            'id': task_id,
            'title': title,
            'description': description,
            'status': status,
            'created_at': time.time(),
            'started_at': time.time() if status == 'in_progress' else None,
            'updated_at': time.time(),
            'metadata': metadata or {},
            'session_id': session_id,
            'agent_id': agent_id,
        }

        self.tasks[task_id] = task
        self._save_tasks()

        logger.info(f"Created task #{task_id} (session={session_id}, agent={agent_id}): {title}")
        return task

    def update_task(
        self,
        task_id: str,
        status: str = None,
        title: str = None,
        description: str = None,
        metadata: Dict = None
    ) -> Dict:
        """更新任务

        Args:
            task_id: 任务 ID
            status: 新状态
            title: 新标题
            description: 新描述
            metadata: 新元数据（会合并到现有元数据）

        Returns:
            更新后的任务对象
        """
        if task_id not in self.tasks:
            raise ValueError(f"Task #{task_id} not found")

        task = self.tasks[task_id]

        if status is not None:
            if not task.get('started_at'):
                if task.get('status') == 'in_progress':
                    task['started_at'] = task.get('updated_at') or time.time()
                elif status == 'in_progress':
                    task['started_at'] = time.time()
            task['status'] = status
        if title is not None:
            task['title'] = title
        if description is not None:
            task['description'] = description
        if metadata is not None:
            task['metadata'].update(metadata)

        task['updated_at'] = time.time()
        self._save_tasks()

        logger.info(f"Updated task #{task_id}: status={status}")
        return task

    def get_task(self, task_id: str) -> Dict:
        """获取任务详情

        Args:
            task_id: 任务 ID

        Returns:
            任务对象
        """
        if task_id not in self.tasks:
            raise ValueError(f"Task #{task_id} not found")

        return self.tasks[task_id]

    def list_tasks(
        self,
        status: str = None,
        limit: int = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> List[Dict]:
        """列出任务

        Args:
            status: 过滤状态（可选）
            limit: 最大返回数量（可选）
            session_id: 过滤会话 ID（可选，None 表示不按会话过滤）
            agent_id: 过滤角色 ID（可选，None 表示不按角色过滤）

        Returns:
            任务列表
        """
        tasks = list(self.tasks.values())

        if status:
            tasks = [t for t in tasks if t.get('status') == status]
        if session_id is not None:
            tasks = [t for t in tasks if t.get('session_id') == session_id]
        if agent_id is not None:
            tasks = [t for t in tasks if t.get('agent_id') == agent_id]

        tasks.sort(key=lambda t: t['created_at'], reverse=True)

        if limit:
            tasks = tasks[:limit]

        return tasks

    def clear_all(
        self,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> int:
        """批量删除匹配的任务

        Args:
            session_id: 只删除匹配此 session 的任务（None 表示不按会话过滤）
            agent_id: 只删除匹配此 agent 的任务（None 表示不按角色过滤）

        Returns:
            删除的任务数
        """
        to_delete = []
        for task_id, task in self.tasks.items():
            if session_id is not None and task.get('session_id') != session_id:
                continue
            if agent_id is not None and task.get('agent_id') != agent_id:
                continue
            to_delete.append(task_id)

        for task_id in to_delete:
            del self.tasks[task_id]

        self._save_tasks()
        logger.info(
            f"clear_all removed {len(to_delete)} tasks "
            f"(session={session_id}, agent={agent_id})"
        )
        return len(to_delete)

    def close_stale_tasks(
        self,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        cutoff_ts: float = 0.0,
    ) -> int:
        """把匹配范围内仍处于 pending/in_progress 的任务归档为 stale

        Args:
            session_id: 匹配会话（None 表示不过滤）
            agent_id: 匹配角色（None 表示不过滤）
            cutoff_ts: 只收口 created_at >= cutoff_ts 的任务（0 表示全部）

        Returns:
            被归档的任务数
        """
        closed = 0
        now = time.time()
        for task in self.tasks.values():
            if task.get('status') not in ('pending', 'in_progress'):
                continue
            if session_id is not None and task.get('session_id') != session_id:
                continue
            if agent_id is not None and task.get('agent_id') != agent_id:
                continue
            if task.get('created_at', 0) < cutoff_ts:
                continue
            task['status'] = 'stale'
            task['updated_at'] = now
            closed += 1

        if closed:
            self._save_tasks()
            logger.info(
                f"close_stale_tasks archived {closed} tasks "
                f"(session={session_id}, agent={agent_id}, cutoff={cutoff_ts})"
            )
        return closed

    def delete_task(self, task_id: str):
        """删除任务

        Args:
            task_id: 任务 ID
        """
        if task_id not in self.tasks:
            raise ValueError(f"Task #{task_id} not found")

        del self.tasks[task_id]
        self._save_tasks()
        logger.info(f"Deleted task #{task_id}")

    def clear_completed(self, session_id: Optional[str] = None) -> int:
        """清除已完成的任务

        Args:
            session_id: 仅清除该会话下的 completed 任务；None 表示全局清除（保留兼容）。
                       前端 InlineTasksPanel 在 phase=gone 时按 session_id 调用，
                       避免误清其他活跃会话的已完成任务。
        """
        completed_ids = [
            task_id for task_id, task in self.tasks.items()
            if task['status'] == 'completed'
            and (session_id is None or task.get('session_id') == session_id)
        ]

        for task_id in completed_ids:
            del self.tasks[task_id]

        if completed_ids:
            self._save_tasks()
        logger.info(f"Cleared {len(completed_ids)} completed tasks (session_id={session_id})")
        return len(completed_ids)

    def clear_ended(self, session_id: Optional[str] = None) -> int:
        """清除所有处于终态的任务（completed / failed / stale）。

        pending / in_progress 不动，保证"活跃中的任务"不会被误清。
        前端 InlineTasksPanel 在整组推进到 gone 时调，用来把混在里面的
        failed/stale 也一并归档——否则它们会把 allDone 判定永远卡在 false，
        造成"任务异常后输入框上方残留不消失"。
        """
        ended_ids = [
            task_id for task_id, task in self.tasks.items()
            if task.get('status') in ('completed', 'failed', 'stale')
            and (session_id is None or task.get('session_id') == session_id)
        ]

        for task_id in ended_ids:
            del self.tasks[task_id]

        if ended_ids:
            self._save_tasks()
        logger.info(f"Cleared {len(ended_ids)} ended tasks (session_id={session_id})")
        return len(ended_ids)


# 全局任务管理器实例
_task_manager = None


def get_task_manager() -> TaskManager:
    """获取全局任务管理器实例"""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager


# ============================================================================
# 工具处理函数
# ============================================================================

def _handle_task_create(
    title: str,
    description: str = "",
    metadata: dict = None,
    *,
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    **_,
) -> str:
    """创建任务"""
    try:
        manager = get_task_manager()
        task = manager.create_task(
            title=title,
            description=description,
            metadata=metadata,
            session_id=session_id,
            agent_id=agent_id,
        )
        _emit_tasks_changed("create", task=task)

        return json.dumps({
            'success': True,
            'task_id': task['id'],
            'task': task
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("Failed to create task")
        return json.dumps({
            'success': False,
            'error': str(e)
        })


def _handle_task_update(
    task_id: str,
    status: str = None,
    title: str = None,
    description: str = None,
    metadata: dict = None,
    **_,
) -> str:
    """更新任务"""
    try:
        manager = get_task_manager()
        task = manager.update_task(
            task_id=task_id,
            status=status,
            title=title,
            description=description,
            metadata=metadata
        )
        _emit_tasks_changed("update", task=task)

        return json.dumps({
            'success': True,
            'task': task
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("Failed to update task")
        return json.dumps({
            'success': False,
            'error': str(e)
        })


def _handle_task_get(task_id: str, **_) -> str:
    """获取任务详情"""
    try:
        manager = get_task_manager()
        task = manager.get_task(task_id)

        return json.dumps({
            'success': True,
            'task': task
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("Failed to get task")
        return json.dumps({
            'success': False,
            'error': str(e)
        })


def _handle_task_list(
    status: str = None,
    limit: int = 50,
    *,
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    **_,
) -> str:
    """列出任务（LLM 调用时自动只看本会话）"""
    try:
        manager = get_task_manager()
        tasks = manager.list_tasks(
            status=status,
            limit=limit,
            session_id=session_id,
            agent_id=agent_id,
        )

        return json.dumps({
            'success': True,
            'count': len(tasks),
            'tasks': tasks
        }, ensure_ascii=False)

    except Exception as e:
        logger.exception("Failed to list tasks")
        return json.dumps({
            'success': False,
            'error': str(e)
        })
