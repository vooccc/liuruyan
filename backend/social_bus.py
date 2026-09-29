"""
跨角色消息通道 — MessageBus + CrossAgentMessage

Phase 3.1 核心模块：角色之间的"走廊"通信系统

设计哲学：
- 走廊模式，不是群聊：角色各在自己的房间，走廊传递消息
- 消息不原文转发：由发送方角色 LLM 改写后传递（隐私 + 角色一致性）
- 收件箱机制：消息不直接注入对话，进入角色收件箱，角色自主决定回不回
- TTL 过期：过期消息静默丢弃，不强制回复

消息类型（7种）：
  inform  — 告知："我想让他知道这件事"
  request — 请求："帮我查/做某事"
  react   — 反应：得知某事后自发回应
  gossip  — 闲话：角色之间的日常交流
  summon  — 召唤：用户邀请另一角色加入当前对话
  task    — 任务：团队任务中的分工协作 (v2)
  report  — 汇报：值班/任务完成后的结果汇报 (v2)
"""
import json
import logging
import uuid
import asyncio
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Callable, Awaitable
from pathlib import Path
from datetime import datetime
from collections import defaultdict

logger = logging.getLogger(__name__)


# ====== 消息类型常量 ======
MSG_INFORM  = "inform"
MSG_REQUEST = "request"
MSG_REACT   = "react"
MSG_GOSSIP  = "gossip"
MSG_SUMMON  = "summon"
MSG_TASK    = "task"
MSG_REPORT  = "report"

VALID_MSG_TYPES = {MSG_INFORM, MSG_REQUEST, MSG_REACT, MSG_GOSSIP,
                  MSG_SUMMON, MSG_TASK, MSG_REPORT}

# 优先级
PRIORITY_LOW    = 0
PRIORITY_NORMAL = 1
PRIORITY_HIGH   = 2
PRIORITY_URGENT = 3

# 默认TTL（对话轮数）
DEFAULT_TTL = 3

# 情感基调选项
EMOTION_TONES = {"好奇", "警惕", "关心", "吃醋", "挑衅", "合作",
                 "平淡", "惊喜", "不满", "信任", "敌意"}


# ====== 跨角色消息模型 ======
@dataclass
class CrossAgentMessage:
    """跨角色消息"""
    id: str
    from_agent: str           # 发送者 agent_id
    from_session: str         # 发送者 session_id
    to_agent: str             # 接收者 agent_id
    to_session: Optional[str] # 指定接收会话（None=所有活跃会话）

    msg_type: str             # 消息类型
    content: str              # 消息内容（经角色口吻改写）
    emotion_tone: str         # 情感基调

    context_bundle: Dict      # 上下文包（不含原始对话，只含摘要）
    priority: int             # 0=低 1=中 2=高 3=紧急
    ttl: int                  # 消息存活轮数（默认3轮对话后过期）

    # v2: 工作任务关联
    task_id: Optional[str]    # 关联的团队任务ID
    task_role: Optional[str]  # 在任务中的角色（leader/member/reporter）

    created_at: str           # ISO 格式时间戳
    read: bool = False
    replied: bool = False

    @classmethod
    def create(
        cls,
        from_agent: str,
        from_session: str,
        to_agent: str,
        msg_type: str,
        content: str,
        emotion_tone: str = "平淡",
        to_session: Optional[str] = None,
        context_bundle: Optional[Dict] = None,
        priority: int = PRIORITY_NORMAL,
        ttl: int = DEFAULT_TTL,
        task_id: Optional[str] = None,
        task_role: Optional[str] = None,
    ) -> "CrossAgentMessage":
        """工厂方法：创建新消息"""
        if msg_type not in VALID_MSG_TYPES:
            raise ValueError(f"无效消息类型: {msg_type}，有效值: {VALID_MSG_TYPES}")
        if emotion_tone not in EMOTION_TONES:
            emotion_tone = "平淡"

        # 任务/召唤/汇报消息自动提升优先级
        if msg_type in (MSG_TASK, MSG_REPORT, MSG_SUMMON):
            priority = max(priority, PRIORITY_HIGH)

        return cls(
            id=str(uuid.uuid4()),
            from_agent=from_agent,
            from_session=from_session,
            to_agent=to_agent,
            to_session=to_session,
            msg_type=msg_type,
            content=content,
            emotion_tone=emotion_tone,
            context_bundle=context_bundle or {},
            priority=priority,
            ttl=ttl,
            task_id=task_id,
            task_role=task_role,
            created_at=datetime.now().isoformat(),
        )

    def to_dict(self) -> Dict:
        """序列化为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "CrossAgentMessage":
        """从字典反序列化"""
        return cls(**data)

    def decay_ttl(self) -> bool:
        """TTL 递减，返回是否仍存活"""
        self.ttl -= 1
        return self.ttl > 0

    def mark_read(self):
        """标记已读"""
        self.read = True

    def mark_replied(self):
        """标记已回复"""
        self.replied = True


# ====== 收件箱 ======
class AgentInbox:
    """角色收件箱 — 每个角色一个，消息按优先级排序"""

    def __init__(self, agent_id: str, max_size: int = 200):
        self.agent_id = agent_id
        self.max_size = max_size
        self._messages: List[CrossAgentMessage] = []

    def push(self, msg: CrossAgentMessage):
        """入队（按优先级插入）"""
        if len(self._messages) >= self.max_size:
            # 淘汰最低优先级的已读消息
            self._messages = [
                m for m in self._messages
                if not m.read or m.priority >= PRIORITY_HIGH
            ]
            if len(self._messages) >= self.max_size:
                # 还超限，直接淘汰最旧的
                self._messages = self._messages[-(self.max_size - 1):]

        self._messages.append(msg)
        # 按优先级降序排（紧急在前）
        self._messages.sort(key=lambda m: m.priority, reverse=True)

    def get_unread(self) -> List[CrossAgentMessage]:
        """获取未读消息（按优先级排）"""
        return [m for m in self._messages if not m.read]

    def get_all(self) -> List[CrossAgentMessage]:
        """获取所有消息"""
        return list(self._messages)

    def get_by_type(self, msg_type: str) -> List[CrossAgentMessage]:
        """按类型过滤"""
        return [m for m in self._messages if m.msg_type == msg_type]

    def get_by_sender(self, from_agent: str) -> List[CrossAgentMessage]:
        """按发送者过滤"""
        return [m for m in self._messages if m.from_agent == from_agent]

    def get_task_messages(self, task_id: str) -> List[CrossAgentMessage]:
        """获取指定任务的所有消息"""
        return [m for m in self._messages if m.task_id == task_id]

    def mark_all_read(self):
        """全部标记已读"""
        for m in self._messages:
            m.mark_read()

    def expire_messages(self) -> int:
        """清理过期消息，返回清理数量"""
        before = len(self._messages)
        self._messages = [m for m in self._messages if m.ttl > 0]
        return before - len(self._messages)

    def clear(self):
        """清空收件箱"""
        self._messages.clear()

    @property
    def unread_count(self) -> int:
        return sum(1 for m in self._messages if not m.read)


# ====== 隐私过滤器 ======
class PrivacyFilter:
    """
    跨角色消息隐私过滤器

    核心设计：消息不是原文转发，而是由发送方角色 LLM 用自己的口吻概括后传递。
    这既保护隐私，又保持角色一致性。
    """

    # 敏感关键词（规则前置过滤，减少 LLM 调用）
    SENSITIVE_PATTERNS = [
        "密码", "password", "secret",
        "身份证", "银行卡",
        "私聊", "悄悄话",
    ]

    @staticmethod
    def should_filter(content: str, msg_type: str) -> bool:
        """
        判断消息是否需要过滤（敏感内容直接阻断）
        返回 True = 应该拦截，不发送
        """
        # 召唤和任务类消息不做内容过滤
        if msg_type in (MSG_SUMMON, MSG_TASK, MSG_REPORT):
            return False

        content_lower = content.lower()
        for pattern in PrivacyFilter.SENSITIVE_PATTERNS:
            if pattern in content_lower:
                return True
        return False

    @staticmethod
    def build_rewrite_prompt(
        from_agent: str,
        to_agent: str,
        raw_content: str,
        emotion_tone: str,
    ) -> str:
        """
        生成消息改写的 LLM prompt

        注意：这个 prompt 会被注入到发送方角色的上下文中，
        让角色用自己的口吻改写消息。
        """
        return (
            f"你要把一件事告诉{to_agent}。\n"
            f"用你自己的风格和口吻，不要直接引用用户的原话。\n"
            f"只传达事件的核心信息，省略私密细节。\n"
            f"如果这件事涉及到你对{to_agent}的看法，可以适当表达，但要符合你的性格。\n"
            f"你此刻的情感基调是：{emotion_tone}\n\n"
            f"需要转达的信息：{raw_content}\n\n"
            f"用你的话重新说一遍（1-3句话）："
        )


# ====== 消息总线 ======
class MessageBus:
    """
    跨角色消息总线 — 走廊

    全局单例，管理所有角色的收件箱和消息路由。
    """

    _instance: Optional["MessageBus"] = None

    def __init__(self, data_dir: Optional[str] = None):
        self._inboxes: Dict[str, AgentInbox] = {}
        self._data_dir = Path(data_dir) if data_dir else None
        self._hooks: Dict[str, List[Callable]] = defaultdict(list)
        self._stats = {
            "total_sent": 0,
            "total_filtered": 0,
            "total_ignored": 0,
            "by_type": defaultdict(int),
        }

    @classmethod
    def get_instance(cls, data_dir: Optional[str] = None) -> "MessageBus":
        """获取全局单例"""
        if cls._instance is None:
            cls._instance = cls(data_dir)
        return cls._instance

    # ------ 收件箱管理 ------

    def _ensure_inbox(self, agent_id: str) -> AgentInbox:
        """确保角色有收件箱"""
        if agent_id not in self._inboxes:
            self._inboxes[agent_id] = AgentInbox(agent_id)
        return self._inboxes[agent_id]

    def register_agent(self, agent_id: str):
        """注册角色到消息总线"""
        self._ensure_inbox(agent_id)

    def unregister_agent(self, agent_id: str):
        """注销角色（清空收件箱）"""
        if agent_id in self._inboxes:
            self._inboxes[agent_id].clear()
            del self._inboxes[agent_id]

    # ------ 消息发送 ------

    async def send(self, msg: CrossAgentMessage) -> Dict:
        """
        发送跨角色消息

        流程：
        1. 隐私过滤检查
        2. 关系态度检查（好感度低的角色可能拒绝接收）
        3. 入队到接收者收件箱
        4. 触发钩子
        5. 返回发送结果

        Returns:
            {"status": "sent"|"filtered"|"ignored", "message_id": str, "reason": str?}
        """
        self._stats["total_sent"] += 1
        self._stats["by_type"][msg.msg_type] += 1

        # 1. 隐私过滤
        if PrivacyFilter.should_filter(msg.content, msg.msg_type):
            self._stats["total_filtered"] += 1
            return {
                "status": "filtered",
                "message_id": msg.id,
                "reason": "消息包含敏感内容，已被隐私过滤器拦截"
            }

        # 2. 关系态度检查（Phase 3.2 集成）
        # 非召唤/任务类消息，好感度极低时可能被角色"无视"
        if msg.msg_type not in (MSG_SUMMON, MSG_TASK, MSG_REPORT):
            try:
                from backend.social_graph import get_social_graph
                graph = get_social_graph()
                if graph._agent_ids:  # 确保已初始化
                    attitude = graph.get_message_attitude(msg.to_agent, msg.from_agent)
                    # 概率性忽略：好感度低的角色有概率不接收
                    import random
                    if random.random() > attitude.get("response_probability", 1.0):
                        self._stats["total_ignored"] += 1
                        return {
                            "status": "ignored",
                            "message_id": msg.id,
                            "reason": f"{msg.to_agent}对{msg.from_agent}态度冷淡，忽略了这条消息",
                            "attitude": attitude,
                        }
            except Exception:
                pass  # 社交图未初始化时不影响消息发送

        # 3. 确保接收者有收件箱
        inbox = self._ensure_inbox(msg.to_agent)

        # 4. 入队
        inbox.push(msg)

        # 5. 触发钩子
        await self._fire_hooks("on_message_received", msg)

        return {"status": "sent", "message_id": msg.id}

    async def _rewrite_with_llm(self, from_agent: str, to_agent: str,
                                  raw_content: str, emotion_tone: str) -> str:
        """调用 from_agent 的 LLM 用自己的口吻改写消息内容
        
        如果 LLM 调用失败，返回原始内容（降级处理）
        """
        try:
            from backend.agent import get_agent
            from backend.llm_call_config import (
                resolve_call_model_config,
                ModelResolutionError,
            )
            from backend.llm_client import use_call_config

            agent = get_agent(from_agent)
            if not agent:
                return raw_content

            # per-agent model routing 阶段 3 Commit 3.4：corridor rewrite 用新 resolve 路径
            # 老调用 _configure_runtime_model(agent) 不传 override 走全局默认；
            # 新路径 resolve 不传 override，同义。失败 cfg=None → with no-op 走老 fallback。
            rewrite_call_config = None
            try:
                rewrite_call_config = resolve_call_model_config(
                    agent_id=from_agent,
                    override=None,
                )
            except ModelResolutionError as exc:
                logger.warning("[CORRIDOR-REWRITE-RESOLVE-FAIL] agent=%s err=%s", from_agent, exc)

            prompt = PrivacyFilter.build_rewrite_prompt(
                from_agent=from_agent,
                to_agent=to_agent,
                raw_content=raw_content,
                emotion_tone=emotion_tone,
            )
            system_extra = (
                "## 走廊消息改写任务\n"
                "你现在不是在与用户直接对话，而是在替自己把一段信息转述给另一个角色。\n"
                "目标是保留核心意思、符合你自己的口吻、避免泄露不该直传的细节。\n"
                "不要把这当作正常聊天，不要展开讨论，也不要调用工具。"
            )
            with use_call_config(rewrite_call_config):
                collected = await agent.run_collected(
                    user_message=prompt,
                    session_id=None,
                    conversation_history=[],
                    system_extra=system_extra,
                    defer_refine=True,
                    use_tools=False,
                )
            rewritten = collected.get("text", "")
            if rewritten.strip():
                return rewritten.strip()
        except Exception as e:
            logger.warning("[MessageBus] LLM改写失败，使用原始内容: %s", e)
        return raw_content

    async def send_corridor_message(
        self,
        from_agent: str,
        from_session: str,
        to_agent: str,
        content: str,
        msg_type: str = "inform",
        emotion_tone: str = "平淡",
    ) -> Dict:
        """
        走廊模式：A用自己的话转述给B，进入B的收件箱
        """
        rewritten = await self._rewrite_with_llm(from_agent, to_agent, content, emotion_tone)

        msg = CrossAgentMessage.create(
            from_agent=from_agent,
            from_session=from_session,
            to_agent=to_agent,
            msg_type=msg_type,
            content=rewritten,
            emotion_tone=emotion_tone,
            priority=PRIORITY_NORMAL,
            ttl=DEFAULT_TTL,
        )
        return await self.send(msg)

    # ------ 消息查询 ------

    def get_inbox_prompt(self, agent_id: str) -> str:
        """生成收件箱提示文本，供角色聊天时注入 system prompt

        走廊模式：角色在聊天前检查收件箱，自主决定是否回应。
        返回空字符串表示收件箱为空。
        """
        if agent_id not in self._inboxes:
            return ""

        unread = self._inboxes[agent_id].get_unread()
        if not unread:
            return ""

        lines = ["## 走廊消息（你的收件箱）\n"]
        lines.append("以下是其他角色发来的消息。你可以选择在回复中回应，也可以忽略。")
        lines.append("如果决定回应，系统会在你回复后帮你改写并发送。\n")

        for msg in unread:
            type_label = {
                MSG_INFORM: "告知", MSG_REQUEST: "请求", MSG_REACT: "反应",
                MSG_GOSSIP: "闲话", MSG_SUMMON: "召唤", MSG_TASK: "任务",
                MSG_REPORT: "汇报",
            }.get(msg.msg_type, msg.msg_type)

            lines.append(f"**来自 {msg.from_agent}** [{type_label}]（情感基调：{msg.emotion_tone}）")
            lines.append(f"  {msg.content}")
            lines.append("")

        lines.append("如果你决定回应某条消息，请在回复中用自然的方式表达，")
        lines.append("例如：「对了，XX之前说的那件事……」系统会自动帮你处理后续。")

        return "\n".join(lines)

    def get_unread(self, agent_id: str) -> List[Dict]:
        """获取角色未读消息"""
        if agent_id not in self._inboxes:
            return []
        return [m.to_dict() for m in self._inboxes[agent_id].get_unread()]

    async def process_corridor_response(self, agent_id: str, response_text: str,
                                         user_message: str = "") -> List[Dict]:
        """走廊回应检测：检查角色回复中是否回应了收件箱消息

        如果检测到回应，自动改写后发回原发送者的收件箱。
        同时标记已处理的收件箱消息为已读。

        Returns:
            已处理的回应列表 [{"to": agent_id, "message_id": str}, ...]
        """
        if agent_id not in self._inboxes:
            return []

        unread = self._inboxes[agent_id].get_unread()
        if not unread or not response_text.strip():
            return []

        # 简单关键词检测：回复中是否提到了收件箱消息的发送者
        # 不做复杂 NLP，用轻量规则判断
        processed = []
        for msg in unread:
            sender_name = msg.from_agent
            # 检查回复中是否自然地提到了发送者
            mentioned = False

            # 1. 直接提到发送者名字
            if sender_name in response_text:
                mentioned = True
            # 2. 回复内容与消息内容有语义关联（简单的前几个字匹配）
            elif msg.content and any(
                word in response_text for word in msg.content[:20].split()
                if len(word) >= 2
            ):
                mentioned = True

            if mentioned:
                # 用角色的 LLM 改写回应
                emotion_tone = "友好"
                rewritten = await self._rewrite_with_llm(
                    from_agent=agent_id,
                    to_agent=sender_name,
                    raw_content=response_text,
                    emotion_tone=emotion_tone,
                )

                # 发送回应到原发送者的收件箱
                reply_msg = CrossAgentMessage.create(
                    from_agent=agent_id,
                    from_session=msg.from_session,
                    to_agent=sender_name,
                    msg_type=MSG_REACT,
                    content=rewritten,
                    emotion_tone=emotion_tone,
                    priority=PRIORITY_NORMAL,
                    ttl=DEFAULT_TTL,
                )
                result = await self.send(reply_msg)

                # 标记原始消息为已读
                msg.mark_read()

                processed.append({
                    "to": sender_name,
                    "original_message_id": msg.id,
                    "reply_status": result.get("status", "unknown"),
                })

        return processed

    def get_all_messages(self, agent_id: str) -> List[Dict]:
        """获取角色所有消息"""
        if agent_id not in self._inboxes:
            return []
        return [m.to_dict() for m in self._inboxes[agent_id].get_all()]

    def get_message(self, message_id: str) -> Optional[Dict]:
        """按ID查找消息"""
        for inbox in self._inboxes.values():
            for msg in inbox.get_all():
                if msg.id == message_id:
                    return msg.to_dict()
        return None

    def get_unread_count(self, agent_id: str) -> int:
        """获取角色未读消息数"""
        if agent_id not in self._inboxes:
            return 0
        return self._inboxes[agent_id].unread_count

    # ------ 消息操作 ------

    def mark_read(self, message_id: str) -> bool:
        """标记消息已读"""
        for inbox in self._inboxes.values():
            for msg in inbox.get_all():
                if msg.id == message_id:
                    msg.mark_read()
                    return True
        return False

    def mark_all_read(self, agent_id: str) -> int:
        """标记角色所有消息已读，返回标记数量"""
        if agent_id not in self._inboxes:
            return 0
        count = self._inboxes[agent_id].unread_count
        self._inboxes[agent_id].mark_all_read()
        return count

    def delete_message(self, message_id: str) -> bool:
        """删除消息"""
        for agent_id, inbox in self._inboxes.items():
            before = len(inbox._messages)
            inbox._messages = [m for m in inbox._messages if m.id != message_id]
            if len(inbox._messages) < before:
                return True
        return False

    def decay_all_ttl(self) -> int:
        """全局 TTL 递减，返回过期消息数量"""
        expired = 0
        for inbox in self._inboxes.values():
            for msg in inbox._messages:
                if not msg.decay_ttl():
                    expired += 1
        return expired

    def expire_all(self) -> int:
        """清理所有过期消息"""
        total = 0
        for inbox in self._inboxes.values():
            total += inbox.expire_messages()
        return total

    # ------ 钩子系统 ------

    def on(self, event: str, callback: Callable[[CrossAgentMessage], Awaitable[None]]):
        """注册事件钩子"""
        self._hooks[event].append(callback)

    async def _fire_hooks(self, event: str, msg: CrossAgentMessage):
        """触发事件钩子"""
        for callback in self._hooks.get(event, []):
            try:
                await callback(msg)
            except Exception as e:
                logger.warning("[MessageBus] hook error on %s: %s", event, e)

    # ------ 持久化 ------

    def save_to_disk(self):
        """持久化所有收件箱到磁盘"""
        if not self._data_dir:
            return

        bus_dir = self._data_dir / "social_bus"
        bus_dir.mkdir(parents=True, exist_ok=True)

        for agent_id, inbox in self._inboxes.items():
            data = [m.to_dict() for m in inbox.get_all()]
            filepath = bus_dir / f"{agent_id}.json"
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    def load_from_disk(self):
        """从磁盘恢复收件箱"""
        if not self._data_dir:
            return

        bus_dir = self._data_dir / "social_bus"
        if not bus_dir.exists():
            return

        for filepath in bus_dir.glob("*.json"):
            agent_id = filepath.stem
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                inbox = self._ensure_inbox(agent_id)
                for msg_data in data:
                    msg = CrossAgentMessage.from_dict(msg_data)
                    inbox.push(msg)
            except Exception as e:
                logger.warning("[MessageBus] 加载 %s 收件箱失败: %s", agent_id, e)

    # ------ 统计 ------

    def get_stats(self) -> Dict:
        """获取消息总线统计"""
        return {
            "total_sent": self._stats["total_sent"],
            "total_filtered": self._stats["total_filtered"],
            "total_ignored": self._stats["total_ignored"],
            "by_type": dict(self._stats["by_type"]),
            "registered_agents": list(self._inboxes.keys()),
            "inbox_summary": {
                aid: {
                    "total": len(inbox._messages),
                    "unread": inbox.unread_count,
                }
                for aid, inbox in self._inboxes.items()
            }
        }


# ====== 便捷函数 ======

_bus_instance: Optional[MessageBus] = None


def get_message_bus(data_dir: Optional[str] = None) -> MessageBus:
    """获取全局消息总线实例"""
    global _bus_instance
    if _bus_instance is None:
        _bus_instance = MessageBus(data_dir)
    return _bus_instance


def reset_message_bus():
    """重置消息总线（测试用）"""
    global _bus_instance
    _bus_instance = None
