"""LoopGuard：跨 agent 循环 + 房间无 user 安全阀（handover §5.3 安全阀 2）

职责：
1. agent-to-agent 跳数限制（hop_depth ≥ 5 拒绝）
2. 环路检测（同一 agent 在 chain 中出现 ≥ 2 次拒绝）
3. 协作房间无 user 暂停（连续 N 条无 user 消息）

设计：
- 纯函数式判定（不持有状态），所有数据由调用方传入
- 房间无 user 检查接受"最近 N 条消息"列表，不直接读 DB（保持独立性）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from backend.core.inbox_message import MessageLineage

logger = logging.getLogger(__name__)


# ─────── 常量（handover §5.3）─────────

MAX_AGENT_HOP_DEPTH = 5
ROOM_NO_USER_PAUSE_THRESHOLD = 6


@dataclass
class LoopGuardConfig:
    max_hop_depth: int = MAX_AGENT_HOP_DEPTH
    room_no_user_threshold: int = ROOM_NO_USER_PAUSE_THRESHOLD


class LoopGuard:
    """循环安全阀（process 单例）

    全部方法都是纯函数式判定，无副作用。
    """

    def __init__(self, config: Optional[LoopGuardConfig] = None) -> None:
        self.config = config or LoopGuardConfig()

    # ─────── 跳数 + 环路 ─────────

    def check_hop_allowed(
        self,
        lineage: Optional[MessageLineage],
        target_agent: str,
    ) -> bool:
        """检查跨 agent 传播是否允许

        允许条件：
        1. hop_depth < max_hop_depth（默认 5）
        2. 同一 agent 在 chain 中出现 < 2 次（防 A→B→A→B 死循环）

        lineage 为 None 时（首次入站）始终允许。
        """
        if lineage is None:
            return True

        if lineage.hop_depth >= self.config.max_hop_depth:
            logger.warning(
                "LoopGuard: hop_depth=%d ≥ max=%d, refuse propagation to agent=%s "
                "(chain=%s)",
                lineage.hop_depth,
                self.config.max_hop_depth,
                target_agent,
                lineage.chain,
            )
            return False

        if lineage.chain.count(target_agent) >= 2:
            logger.warning(
                "LoopGuard: agent=%s already in chain %d times (chain=%s), refuse",
                target_agent,
                lineage.chain.count(target_agent),
                lineage.chain,
            )
            return False

        return True

    # ─────── 房间无 user 暂停 ─────────

    def check_room_should_pause(
        self,
        recent_messages: Sequence,
        *,
        threshold: Optional[int] = None,
    ) -> bool:
        """检查房间是否应该暂停（连续 N 条无 user 消息）

        Args:
            recent_messages: 最近 N 条消息列表，每条必须有 .sender_type 属性
                             或者是 dict 含 "sender_type" 键
            threshold: 阈值，默认 ROOM_NO_USER_PAUSE_THRESHOLD

        Returns:
            True = 应该暂停（连续 ≥ threshold 条无 user）
        """
        n = threshold if threshold is not None else self.config.room_no_user_threshold

        if len(recent_messages) < n:
            return False

        tail = list(recent_messages)[-n:]
        all_non_user = all(self._extract_sender_type(m) != "user" for m in tail)

        if all_non_user:
            logger.warning(
                "LoopGuard: room has %d consecutive non-user messages, should pause",
                n,
            )
        return all_non_user

    @staticmethod
    def _extract_sender_type(msg) -> str:
        """从消息（dict 或对象）提取 sender_type，未知 → 'agent'（保守）"""
        if isinstance(msg, dict):
            return str(msg.get("sender_type") or msg.get("role") or "agent")
        sender_type = getattr(msg, "sender_type", None)
        if sender_type:
            return str(sender_type)
        role = getattr(msg, "role", None)
        if role:
            return str(role)
        return "agent"


# ─────── 全局单例 ─────────

_singleton: Optional[LoopGuard] = None


def get_loop_guard() -> LoopGuard:
    global _singleton
    if _singleton is None:
        _singleton = LoopGuard()
    return _singleton


def reset_for_testing() -> None:
    global _singleton
    _singleton = None
