"""任务管理类工具注册（从 clean_registry.py 拆出）。

handler 全部来自 task_tools 模块；clean_registry import 时调用 register(registry)，
行为零改动。
"""


def register(registry):
    from ..task_tools import (
        _handle_task_create,
        _handle_task_update,
        _handle_task_get,
        _handle_task_list
    )

    # task_create
    registry.register(
        name="task_create",
        description=(
            "Create a new task to track multi-step work progress. User sees tasks in a live UI list.\n\n"
            "WHEN TO USE:\n"
            "✓ User requests 3+ discrete steps (refactor / analysis / multi-file changes)\n"
            "✓ User explicitly asks 'track this' or 'todo list'\n"
            "✓ After receiving several independent requirements at once\n\n"
            "WHEN NOT TO USE:\n"
            "✗ Single-step / trivial tasks (fix typo, rename var) — just do it\n"
            "✗ Pure Q&A or explanation — no work, no task\n"
            "✗ Already-in-progress single flow — don't create a mid-flight meta-task\n\n"
            "WORKFLOW:\n"
            "1. Create all tasks up front at start of multi-step work\n"
            "2. `task_update` status → 'in_progress' BEFORE beginning each task\n"
            "3. `task_update` status → 'completed' IMMEDIATELY after finishing (don't batch)\n"
            "4. At most ONE task in 'in_progress' at a time"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Task title (brief, actionable, e.g., 'Refactor authentication module')"
                },
                "description": {
                    "type": "string",
                    "description": "Detailed description of what needs to be done"
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional metadata (e.g., {'files': ['auth.py'], 'priority': 'high'})"
                }
            },
            "required": ["title"]
        },
        handler=_handle_task_create
    )

    # task_update
    registry.register(
        name="task_update",
        description=(
            "Update a task's status or details. Always update around state changes.\n\n"
            "WHEN TO USE:\n"
            "✓ Task state transitions (pending → in_progress; in_progress → completed / failed)\n"
            "✓ Editing title / description / metadata mid-flight\n\n"
            "STATUS VALUES:\n"
            "- pending: Not started\n"
            "- in_progress: Currently working on — keep only ONE task at this state at any time\n"
            "- completed: Finished successfully\n"
            "- failed: Encountered error; create a NEW task describing what blocked you\n\n"
            "WORKFLOW:\n"
            "- Mark `in_progress` BEFORE starting work (not after you finish)\n"
            "- Mark `completed` IMMEDIATELY after done; don't batch N completions at the end\n"
            "- If blocked, keep as `in_progress` and create a new 'unblock X' task — don't silently fail\n\n"
            "If you forgot a task_id, call `task_list` first."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID (from task_create or task_list)"
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "failed"],
                    "description": "New status"
                },
                "title": {
                    "type": "string",
                    "description": "New title (optional)"
                },
                "description": {
                    "type": "string",
                    "description": "New description (optional)"
                },
                "metadata": {
                    "type": "object",
                    "description": "Metadata to merge (optional)"
                }
            },
            "required": ["task_id"]
        },
        handler=_handle_task_update
    )

    # task_get
    registry.register(
        name="task_get",
        description=(
            "Get detailed information about a specific task (full description, metadata, status, history).\n\n"
            "WHEN TO USE:\n"
            "✓ You have a task_id and need the full description before acting on it\n"
            "✓ Checking task metadata before deciding next step\n\n"
            "WHEN NOT TO USE:\n"
            "✗ Want to see all tasks — use `task_list`\n"
            "✗ Want to change state — use `task_update`"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID"
                }
            },
            "required": ["task_id"]
        },
        handler=_handle_task_get
    )

    # task_list
    registry.register(
        name="task_list",
        description=(
            "List tasks (all or filtered by status). Sorted newest-first.\n\n"
            "WHEN TO USE:\n"
            "✓ Start of a session / after an interruption — see what was pending\n"
            "✓ User asks 'what's left?' / 'what are we doing?'\n"
            "✓ You forgot a task_id and want to find it by content\n\n"
            "WHEN NOT TO USE:\n"
            "✗ Single task detail — use `task_get`\n"
            "✗ No tasks have been created in this session — skip, don't poll"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "failed"],
                    "description": "Filter by status (optional, returns all if not specified)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of tasks to return (default: 50)",
                    "default": 50
                }
            }
        },
        handler=_handle_task_list
    )
