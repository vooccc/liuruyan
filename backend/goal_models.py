"""
Goal 模式核心数据模型
文件：backend/goal_models.py
"""
import time
from typing import List, Literal, Optional, Dict, Any
from pydantic import BaseModel, Field

GoalStatus = Literal["none", "active", "paused", "completed", "blocked"]

class GoalIteration(BaseModel):
    """记录每一次自主推进的足迹"""
    iteration_index: int                       # 轮次序号（1-indexed）
    next_action: str                           # 上一轮校验器决定的“核心动作”
    evidence: List[str] = Field(default_factory=list)  # 本轮收集的客观证据（exit code, 测试输出片段等）
    summary: str = ""                          # 本轮改动的一句话摘要
    timestamp: float = Field(default_factory=time.time)

class GoalState(BaseModel):
    """会话级目标状态实体"""
    goal_id: str                               # 唯一目标 ID
    session_id: str                            # 绑定的会话 ID
    objective: str                             # 目标原始文字要求
    status: GoalStatus = "none"                # 当前状态
    max_turns: int = 15                        # 轮次熔断预算（默认15轮）
    used_turns: int = 0                        # 已消耗轮次
    consecutive_stagnations: int = 0           # 连续无进展计数
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    iterations: List[GoalIteration] = Field(default_factory=list)
    blocked_reason: Optional[str] = None       # 阻塞阻碍原因
    final_evidence: List[str] = Field(default_factory=list)  # 最终达标的证据集合


def get_session_goal(session: Any) -> Optional[GoalState]:
    """从 session.metadata 安全获取 GoalState"""
    if not session or not hasattr(session, "metadata") or not isinstance(session.metadata, dict):
        return None
    raw = session.metadata.get("goal_state")
    if not raw or not isinstance(raw, dict):
        return None
    try:
        return GoalState(**raw)
    except Exception:
        return None


def set_session_goal(session: Any, goal: GoalState) -> None:
    """持久化 GoalState 到 session.metadata 并保存"""
    if not session or not hasattr(session, "metadata") or not isinstance(session.metadata, dict):
        return
    goal.updated_at = time.time()
    session.metadata["goal_state"] = goal.model_dump() if hasattr(goal, "model_dump") else goal.dict()
    if hasattr(session, "save_meta"):
        session.save_meta()


def clear_session_goal(session: Any) -> None:
    """清除 session 中的活跃目标"""
    if not session or not hasattr(session, "metadata") or not isinstance(session.metadata, dict):
        return
    if "goal_state" in session.metadata:
        session.metadata.pop("goal_state", None)
        if hasattr(session, "save_meta"):
            session.save_meta()


def parse_goal_command(text: str) -> Optional[Dict[str, str]]:
    """
    解析用户输入的 /goal 系列命令
    返回格式：{"action": "set" | "pause" | "resume" | "clear" | "status", "objective": str}
    非 /goal 命令返回 None
    """
    if not text:
        return None
    trimmed = text.strip()
    if not (trimmed == "/goal" or trimmed.startswith("/goal ") or trimmed.startswith("/goal\n")):
        return None
    
    parts = trimmed.split(maxsplit=2)
    # 形式1: /goal
    if len(parts) == 1:
        return {"action": "status", "objective": ""}
    
    sub = parts[1].lower()
    # 形式2: /goal status
    if sub == "status":
        return {"action": "status", "objective": ""}
    # 形式3: /goal pause
    if sub == "pause":
        return {"action": "pause", "objective": ""}
    # 形式4: /goal resume
    if sub == "resume":
        return {"action": "resume", "objective": ""}
    # 形式5: /goal clear
    if sub == "clear":
        return {"action": "clear", "objective": ""}
    # 形式6: /goal replace <目标>
    if sub == "replace":
        obj = (parts[2] if len(parts) > 2 else "").strip()
        if not obj:
            return {"action": "status", "objective": ""}
        return {"action": "set", "objective": obj}
    # 形式7: /goal <目标描述>
    obj = trimmed[len("/goal"):].strip()
    if not obj:
        return {"action": "status", "objective": ""}
    return {"action": "set", "objective": obj}

