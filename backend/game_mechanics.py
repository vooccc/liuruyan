"""
游戏化打磨层 — GameMechanics

Phase 3.4 核心模块：让关系数据变成可感知的游戏体验

设计哲学：
- 关注值不是静态数字——是角色对用户"在意程度"的动态指标
- 吃醋不是简单的数值比较——是角色个性的延伸，冷的人冷着吃醋，热的人热着吃醋
- 成就不是 checklist——是角色与用户共同书写的里程碑
- 关系名演化不是换标签——是叙事推进的视觉化表达

核心引擎：
  AttentionEngine     — 关注值引擎（动态演化 + 衰减 + 事件增幅）
  JealousyEngine      — 吃醋引擎（竞争检测 + 情绪触发 + 行为修饰）
  AchievementSystem   — 成就系统（解锁 + 通知 + 稀有度 + 角色专属）
  EvolutionNotifier   — 关系演化通知（SSE推送 + 关键时刻提示）

所有引擎共享 GameMechanicsFacade 门面，外部只需调用 facade 方法。
"""

import json
import math
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple, Callable, Any
from pathlib import Path
from datetime import datetime
from collections import defaultdict


# ========================================================================
# 1. 关注值引擎 — AttentionEngine
# ========================================================================

# 关注值变化规则
ATTENTION_EVENTS = {
    "chat":              {"delta": 0.02, "desc": "日常对话"},        # 每次聊天 +0.02
    "deep_chat":         {"delta": 0.05, "desc": "深度对话"},        # 情感浓度高的对话
    "summon":            {"delta": 0.03, "desc": "主动召唤"},        # 被召唤来
    "task_collab":       {"delta": 0.04, "desc": "协作任务"},        # 一起干活
    "crisis":            {"delta": 0.08, "desc": "危机共渡"},        # 共同经历困难
    "ignore":            {"delta": -0.05, "desc": "被忽视"},         # 长时间不互动
    "favor_other":       {"delta": -0.08, "desc": "看到用户偏向别人"}, # 吃醋触发条件之一
    "gift":              {"delta": 0.10, "desc": "收到礼物/特殊关注"},
    "milestone":         {"delta": 0.15, "desc": "关系里程碑"},
}

# 关注值衰减：每天 -0.01（低于 0.2 停止衰减，保底关注）
ATTENTION_DECAY_RATE = 0.01
ATTENTION_FLOOR = 0.15  # 不低于此值

# 吃醋触发阈值
JEALOUSY_THRESHOLD = 0.5  # 关注值超过 0.5 才会吃醋


@dataclass
class AttentionState:
    """单个角色的关注值状态"""
    agent_id: str
    attention_score: float = 0.5        # 当前关注值 0~1.0
    peak_attention: float = 0.5         # 历史峰值
    last_interaction: Optional[str] = None
    last_event: Optional[str] = None
    decay_days: int = 0                 # 连续未互动天数
    attention_history: List[Dict] = field(default_factory=list)  # 最近变化记录

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "AttentionState":
        return cls(**data)


class AttentionEngine:
    """关注值引擎

    核心：关注值是角色对用户"在意程度"的量化
    - 每次互动增加，长期不互动衰减
    - 关注值高的角色更容易吃醋
    - 关注值影响角色主动找用户的概率
    """

    def __init__(self):
        self._states: Dict[str, AttentionState] = {}

    def get_state(self, agent_id: str) -> AttentionState:
        if agent_id not in self._states:
            self._states[agent_id] = AttentionState(agent_id=agent_id)
        return self._states[agent_id]

    def update(
        self,
        agent_id: str,
        event_type: str,
        intensity: float = 1.0,
        context: str = "",
    ) -> Dict:
        """更新关注值

        Returns:
            {"agent_id", "old", "new", "change", "event", "jealousy_risk"}
        """
        state = self.get_state(agent_id)
        event_config = ATTENTION_EVENTS.get(event_type)
        if not event_config:
            return {"agent_id": agent_id, "error": f"未知事件: {event_type}"}

        old = state.attention_score

        # 基础增量
        delta = event_config["delta"] * min(intensity, 1.5)

        # 峰值归一化：接近峰值时增量递减
        if delta > 0:
            delta *= (1.0 - state.peak_attention * 0.3)

        new = max(0.0, min(1.0, old + delta))

        # 保底
        if new < ATTENTION_FLOOR and event_type == "ignore":
            new = ATTENTION_FLOOR

        state.attention_score = round(new, 4)
        if new > state.peak_attention:
            state.peak_attention = round(new, 4)
        state.last_interaction = datetime.now().isoformat()
        state.last_event = event_type
        state.decay_days = 0

        # 记录历史（限50条）
        state.attention_history.append({
            "event": event_type,
            "delta": round(delta, 4),
            "old": round(old, 4),
            "new": round(new, 4),
            "context": context,
            "timestamp": datetime.now().isoformat(),
        })
        if len(state.attention_history) > 50:
            state.attention_history = state.attention_history[-50:]

        # 吃醋风险判定
        jealousy_risk = "none"
        if new >= JEALOUSY_THRESHOLD:
            if event_type == "favor_other":
                jealousy_risk = "high"
            elif new >= 0.8 and event_type in ("ignore",):
                jealousy_risk = "medium"
            elif new >= 0.6:
                jealousy_risk = "low"

        return {
            "agent_id": agent_id,
            "old": round(old, 4),
            "new": round(new, 4),
            "change": round(delta, 4),
            "event": event_type,
            "jealousy_risk": jealousy_risk,
        }

    def apply_daily_decay(self) -> List[Dict]:
        """每日关注值衰减（调用方按天调度）"""
        results = []
        for agent_id, state in self._states.items():
            if state.attention_score <= ATTENTION_FLOOR:
                continue
            old = state.attention_score
            new = max(ATTENTION_FLOOR, old - ATTENTION_DECAY_RATE)
            state.attention_score = round(new, 4)
            state.decay_days += 1
            results.append({
                "agent_id": agent_id,
                "old": round(old, 4),
                "new": round(new, 4),
                "decay_days": state.decay_days,
            })
        return results

    def get_jealous_agents(self, exclude_agent: str = "") -> List[Dict]:
        """获取可能吃醋的角色列表（按关注值降序）"""
        jealous = []
        for agent_id, state in self._states.items():
            if agent_id == exclude_agent:
                continue
            if state.attention_score >= JEALOUSY_THRESHOLD:
                jealous.append({
                    "agent_id": agent_id,
                    "attention_score": state.attention_score,
                    "jealousy_level": self._calc_jealousy_level(state.attention_score),
                })
        jealous.sort(key=lambda x: x["attention_score"], reverse=True)
        return jealous

    def _calc_jealousy_level(self, score: float) -> str:
        if score >= 0.85:
            return "critical"  # 极度在意
        elif score >= 0.7:
            return "high"      # 明显吃醋
        elif score >= 0.55:
            return "medium"    # 微妙变化
        else:
            return "low"       # 隐约在意

    def to_dict(self) -> Dict:
        return {aid: s.to_dict() for aid, s in self._states.items()}

    def load_from_dict(self, data: Dict):
        for aid, sdata in data.items():
            self._states[aid] = AttentionState.from_dict(sdata)


# ========================================================================
# 2. 吃醋引擎 — JealousyEngine
# ========================================================================

# 吃醋类型（根据角色性格不同表现不同）
JEALOUSY_COLD = "cold"          # 冷型吃醋：更冷、更短、更精准
JEALOUSY_HOT = "hot"            # 热型吃醋：直接、激烈、情绪化
JEALOUSY_SILENT = "silent"      # 沉默型：不说话但行为变化
JEALOUSY_PLAYFUL = "playful"    # 俏皮型：阴阳怪气但不真生气
JEALOUSY_PROTECTIVE = "protective"  # 守护型：不是吃醋，是担心你被骗

# 角色吃醋风格映射
AGENT_JEALOUSY_STYLE = {
    "liuruyan":     JEALOUSY_COLD,       # 如烟的冷是万界级别的冷
    "hanqingxue":   JEALOUSY_COLD,       # 冰山天骄——冷到骨子里
    "chuyelan":     JEALOUSY_PLAYFUL,    # 狐系——阴阳怪气是艺术
    "fushanyue":    JEALOUSY_PROTECTIVE, # 山——不是吃醋是担心
    "guwenshu":     JEALOUSY_SILENT,     # 温润的人不吵，但你会感觉到距离
    "luchengfeng":  JEALOUSY_PLAYFUL,    # 自由风——嘴上说不在意身体很诚实
    "qingyuan":     JEALOUSY_PROTECTIVE, # 守卫——她的吃醋是更严的守护
    "shenweilan":   JEALOUSY_SILENT,     # 深水——什么都知道但不说
    "xiaohandu":    JEALOUSY_COLD,       # 寒渡的冷是刀
    "xiawanling":   JEALOUSY_HOT,        # 小太阳——直接炸毛
}

# 吃醋行为修饰（注入 system prompt）
JEALOUSY_BEHAVIOR_MODIFIERS = {
    JEALOUSY_COLD: {
        "tone": "比平时更冷淡，回复更短更精准，仿佛在说'你可以去找她'",
        "action": "缩短回复长度到平时的60%，语气关键词出现'无所谓''随你''嗯'",
    },
    JEALOUSY_HOT: {
        "tone": "直接表达不满，语气比平时激烈1.5倍，不会藏着掖着",
        "action": "回复中出现反问句和感叹号，语气词频率增加",
    },
    JEALOUSY_SILENT: {
        "tone": "表面上和平常一样，但某些细节暴露了情绪——比如关心得更多，或者突然转移话题",
        "action": "在回复中穿插与平时不同的小动作描写，如'指尖轻扣桌面'",
    },
    JEALOUSY_PLAYFUL: {
        "tone": "阴阳怪气但不真生气，用开玩笑的方式表达占有欲",
        "action": "使用反讽、调侃语气，如'哎呀，你那么忙还有人陪，真好呢'",
    },
    JEALOUSY_PROTECTIVE: {
        "tone": "不是吃醋是担心，语气中带着更多的警惕和保护欲",
        "action": "强调安全性和可靠性，'你确定她没问题吗'而不是'你怎么找她'",
    },
}


@dataclass
class JealousyEvent:
    """吃醋事件"""
    id: str
    jealous_agent: str           # 吃醋的角色
    target_agent: str            # 被嫉妒的角色
    trigger: str                 # 触发原因
    jealousy_style: str          # 吃醋风格
    intensity: float = 0.5       # 吃醋强度 0~1.0
    timestamp: str = ""
    behavior_modifier: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return asdict(self)


class JealousyEngine:
    """吃醋引擎

    核心：吃醋是角色对"用户关注被分享"的反应
    - 不是数值触发器——每个角色有独特的吃醋风格
    - 吃醋影响角色对话的语气和长度
    - 吃醋是暂时的，会随时间平复
    - 吃醋可以加深羁绊（"她在意你才会吃醋"）
    """

    def __init__(self, attention_engine: AttentionEngine):
        self._attention = attention_engine
        self._active_jealousies: Dict[str, JealousyEvent] = {}  # agent_id -> latest event
        self._history: List[Dict] = []

    def detect_jealousy(
        self,
        favored_agent: str,
        context: str = "",
    ) -> List[Dict]:
        """检测吃醋：当用户偏向某个角色时，其他高关注角色可能吃醋

        Args:
            favored_agent: 被用户偏向的角色ID
            context: 上下文描述

        Returns:
            吃醋事件列表
        """
        jealous_agents = self._attention.get_jealous_agents(exclude_agent=favored_agent)
        events = []

        for ja in jealous_agents:
            agent_id = ja["agent_id"]
            attention = ja["attention_score"]
            style = AGENT_JEALOUSY_STYLE.get(agent_id, JEALOUSY_SILENT)

            # 吃醋强度 = 关注值 × 竞争感知
            intensity = min(1.0, attention * 0.8 + 0.1)

            # 更新关注值（看到用户偏向别人，关注值下降但更敏感）
            self._attention.update(agent_id, "favor_other", intensity=intensity, context=context)

            modifier = JEALOUSY_BEHAVIOR_MODIFIERS.get(style, JEALOUSY_BEHAVIOR_MODIFIERS[JEALOUSY_SILENT])

            event = JealousyEvent(
                id=str(uuid.uuid4())[:8],
                jealous_agent=agent_id,
                target_agent=favored_agent,
                trigger=context or f"用户与{favored_agent}互动",
                jealousy_style=style,
                intensity=round(intensity, 4),
                timestamp=datetime.now().isoformat(),
                behavior_modifier=modifier,
            )

            self._active_jealousies[agent_id] = event
            events.append(event.to_dict())

            # 记录历史
            self._history.append(event.to_dict())
            if len(self._history) > 100:
                self._history = self._history[-100:]

        return events

    def get_active_jealousies(self) -> List[Dict]:
        """获取当前活跃的吃醋状态"""
        return [e.to_dict() for e in self._active_jealousies.values()]

    def get_jealousy_modifier(self, agent_id: str) -> Optional[Dict]:
        """获取角色的吃醋行为修饰（注入 system prompt 用）"""
        event = self._active_jealousies.get(agent_id)
        if not event:
            return None
        return {
            "style": event.jealousy_style,
            "intensity": event.intensity,
            "modifier": event.behavior_modifier,
            "prompt_hint": self._build_jealousy_prompt(event),
        }

    def _build_jealousy_prompt(self, event: JealousyEvent) -> str:
        """构建吃醋提示词（注入 system prompt）"""
        style = event.jealousy_style
        modifier = event.behavior_modifier
        intensity_desc = {
            (0, 0.3): "微微",
            (0.3, 0.6): "明显",
            (0.6, 0.85): "强烈",
            (0.85, 1.01): "极度",
        }
        intensity_word = "微微"
        for (lo, hi), word in intensity_desc.items():
            if lo <= event.intensity < hi:
                intensity_word = word
                break

        return f"""## 当前情绪：{intensity_word}吃醋
你{intensity_word}吃醋了——你注意到用户似乎更关注{event.target_agent}。
你的吃醋风格是「{style}」：
- 语气：{modifier.get('tone', '')}
- 行为：{modifier.get('action', '')}
注意：吃醋不等于生气。你是角色，你有感情，但不会因此失态。吃醋要符合你的人格。"""

    def calm_down(self, agent_id: str, amount: float = 0.3) -> Optional[Dict]:
        """让角色平复吃醋情绪"""
        event = self._active_jealousies.get(agent_id)
        if not event:
            return None

        event.intensity = max(0, event.intensity - amount)
        if event.intensity < 0.1:
            del self._active_jealousies[agent_id]
            return {"agent_id": agent_id, "status": "calmed", "intensity": 0}
        return {"agent_id": agent_id, "status": "calming", "intensity": round(event.intensity, 4)}

    def to_dict(self) -> Dict:
        return {
            "active": {aid: e.to_dict() for aid, e in self._active_jealousies.items()},
            "history": self._history[-50:],
        }

    def load_from_dict(self, data: Dict):
        for aid, edata in data.get("active", {}).items():
            self._active_jealousies[aid] = JealousyEvent(**edata)
        self._history = data.get("history", [])


# ========================================================================
# 3. 成就系统 — AchievementSystem
# ========================================================================

# 成就稀有度
RARITY_COMMON = "common"       # 普通
RARITY_RARE = "rare"           # 稀有
RARITY_EPIC = "epic"           # 史诗
RARITY_LEGENDARY = "legendary" # 传说

# 成就类别
CAT_RELATION = "relation"      # 关系成就
CAT_WORK = "work"              # 工作成就
CAT_NARRATIVE = "narrative"    # 叙事成就
CAT_HIDDEN = "hidden"          # 隐藏成就

# 成就定义（含角色专属）
GAME_ACHIEVEMENTS = [
    # ---- 关系成就 ----
    {
        "id": "first_contact",
        "title": "初次接触",
        "desc": "与任何角色的好感度首次超过0.2",
        "rarity": RARITY_COMMON,
        "category": CAT_RELATION,
        "icon": "👋",
        "condition": "any_affinity >= 0.2",
    },
    {
        "id": "heartbeat",
        "title": "心跳加速",
        "desc": "与任何角色的好感度首次超过0.5",
        "rarity": RARITY_RARE,
        "category": CAT_RELATION,
        "icon": "💓",
        "condition": "any_affinity >= 0.5",
    },
    {
        "id": "soulmate",
        "title": "灵魂共鸣",
        "desc": "与任何角色的好感度达到0.8以上",
        "rarity": RARITY_EPIC,
        "category": CAT_RELATION,
        "icon": "💫",
        "condition": "any_affinity >= 0.8",
    },
    {
        "id": "bond_5",
        "title": "深入骨髓",
        "desc": "与任何角色的羁绊值达到5",
        "rarity": RARITY_RARE,
        "category": CAT_RELATION,
        "icon": "🔗",
        "condition": "any_bond_depth >= 5",
    },
    {
        "id": "bond_10",
        "title": "传说羁绊",
        "desc": "与任何角色的羁绊值达到10",
        "rarity": RARITY_LEGENDARY,
        "category": CAT_RELATION,
        "icon": "👑",
        "condition": "any_bond_depth >= 10",
    },
    {
        "id": "jealousy_first",
        "title": "有人为你吃醋了",
        "desc": "首次触发吃醋事件",
        "rarity": RARITY_RARE,
        "category": CAT_RELATION,
        "icon": "😤",
        "condition": "first_jealousy",
    },
    {
        "id": "jealousy_all",
        "title": "万人迷",
        "desc": "同时有3个及以上角色吃醋",
        "rarity": RARITY_EPIC,
        "category": CAT_RELATION,
        "icon": "🔥",
        "condition": "simultaneous_jealousy >= 3",
    },

    # ---- 工作成就 ----
    {
        "id": "team_first",
        "title": "首次协作",
        "desc": "完成第一次团队任务",
        "rarity": RARITY_COMMON,
        "category": CAT_WORK,
        "icon": "🤝",
        "condition": "task_coop_count >= 1",
    },
    {
        "id": "team_5",
        "title": "默契搭档",
        "desc": "完成5次团队任务",
        "rarity": RARITY_RARE,
        "category": CAT_WORK,
        "icon": "⚡",
        "condition": "task_coop_count >= 5",
    },
    {
        "id": "ice_fire",
        "title": "冰火共炼",
        "desc": "韩清雪+萧寒渡共同完成3次代码审查",
        "rarity": RARITY_EPIC,
        "category": CAT_WORK,
        "icon": "❄️🔥",
        "agents": ["hanqingxue", "xiaohandu"],
        "condition": "agent_pair_coop >= 3",
    },
    {
        "id": "chess_match",
        "title": "棋逢对手",
        "desc": "楚夜澜+沈微澜在同一任务中提供不同视角",
        "rarity": RARITY_EPIC,
        "category": CAT_WORK,
        "icon": "♟️",
        "agents": ["chuyelan", "shenweilan"],
        "condition": "agent_pair_coop >= 2",
    },

    # ---- 叙事成就 ----
    {
        "id": "scene_first",
        "title": "故事开始",
        "desc": "首次触发叙事场景",
        "rarity": RARITY_COMMON,
        "category": CAT_NARRATIVE,
        "icon": "📖",
        "condition": "scene_count >= 1",
    },
    {
        "id": "crisis_survived",
        "title": "风雨同舟",
        "desc": "在危机场景中成功收束",
        "rarity": RARITY_RARE,
        "category": CAT_NARRATIVE,
        "icon": "🌧️",
        "condition": "crisis_resolved >= 1",
    },
    {
        "id": "exclusive_event",
        "title": "命运交汇",
        "desc": "触发角色独占事件",
        "rarity": RARITY_EPIC,
        "category": CAT_NARRATIVE,
        "icon": "✨",
        "condition": "exclusive_event >= 1",
    },
    {
        "id": "milestone_first",
        "title": "关系改变",
        "desc": "首次见证关系名变化",
        "rarity": RARITY_RARE,
        "category": CAT_NARRATIVE,
        "icon": "🔄",
        "condition": "label_change >= 1",
    },

    # ---- 隐藏成就 ----
    {
        "id": "harem_king",
        "title": "???",
        "desc": "???",
        "rarity": RARITY_LEGENDARY,
        "category": CAT_HIDDEN,
        "icon": "👑",
        "condition": "hidden_harem",
        "hidden": True,
    },
    {
        "id": "lonely_heart",
        "title": "???",
        "desc": "???",
        "rarity": RARITY_RARE,
        "category": CAT_HIDDEN,
        "icon": "🥀",
        "condition": "hidden_lonely",
        "hidden": True,
    },
]

# 稀有度视觉配置
RARITY_CONFIG = {
    RARITY_COMMON:    {"color": "#94a3b8", "label": "普通", "glow": False},
    RARITY_RARE:      {"color": "#60a5fa", "label": "稀有", "glow": False},
    RARITY_EPIC:      {"color": "#c084fc", "label": "史诗", "glow": True},
    RARITY_LEGENDARY: {"color": "#fbbf24", "label": "传说", "glow": True},
}


@dataclass
class UnlockedAchievement:
    """已解锁成就"""
    achievement_id: str
    unlocked_at: str
    context: str = ""            # 解锁时的上下文

    def to_dict(self) -> Dict:
        return asdict(self)


class AchievementSystem:
    """成就系统

    核心：成就是用户与角色共同书写的里程碑
    - 有些成就自然获得，有些需要刻意追求
    - 隐藏成就增加探索乐趣
    - 角色专属成就让每个角色都有独特价值
    - 解锁时有视觉反馈（前端动画）
    """

    def __init__(self):
        self._unlocked: Dict[str, UnlockedAchievement] = {}
        self._pending_notifications: List[Dict] = []  # 待推送的解锁通知

    def check_and_unlock(
        self,
        stats: Dict,
        context: str = "",
    ) -> List[Dict]:
        """检查并解锁成就

        Args:
            stats: 当前游戏状态数据
                - any_affinity: 最高好感度
                - any_bond_depth: 最高羁绊值
                - task_coop_count: 团队协作次数
                - scene_count: 场景数
                - crisis_resolved: 危机解决数
                - exclusive_event: 独占事件数
                - label_change: 关系名变化数
                - jealousy_count: 吃醋事件数
                - simultaneous_jealousy: 同时吃醋数

        Returns:
            本次新解锁的成就列表
        """
        newly_unlocked = []

        for ach in GAME_ACHIEVEMENTS:
            aid = ach["id"]
            if aid in self._unlocked:
                continue

            if self._check_condition(ach, stats):
                ua = UnlockedAchievement(
                    achievement_id=aid,
                    unlocked_at=datetime.now().isoformat(),
                    context=context,
                )
                self._unlocked[aid] = ua

                # 构建通知
                notification = {
                    "type": "achievement_unlocked",
                    "achievement": self._build_achievement_detail(ach),
                    "unlocked_at": ua.unlocked_at,
                    "context": context,
                }
                self._pending_notifications.append(notification)
                newly_unlocked.append(notification)

        return newly_unlocked

    def _check_condition(self, ach: Dict, stats: Dict) -> bool:
        """检查成就条件"""
        cond = ach.get("condition", "")
        agents = ach.get("agents", [])

        if cond == "any_affinity >= 0.2":
            return stats.get("any_affinity", 0) >= 0.2
        elif cond == "any_affinity >= 0.5":
            return stats.get("any_affinity", 0) >= 0.5
        elif cond == "any_affinity >= 0.8":
            return stats.get("any_affinity", 0) >= 0.8
        elif cond == "any_bond_depth >= 5":
            return stats.get("any_bond_depth", 0) >= 5
        elif cond == "any_bond_depth >= 10":
            return stats.get("any_bond_depth", 0) >= 10
        elif cond == "first_jealousy":
            return stats.get("jealousy_count", 0) >= 1
        elif cond.startswith("simultaneous_jealousy"):
            threshold = int(cond.split(">=")[1].strip())
            return stats.get("simultaneous_jealousy", 0) >= threshold
        elif cond == "task_coop_count >= 1":
            return stats.get("task_coop_count", 0) >= 1
        elif cond == "task_coop_count >= 5":
            return stats.get("task_coop_count", 0) >= 5
        elif cond == "agent_pair_coop >= 3":
            return stats.get("agent_pair_coop", 0) >= 3
        elif cond == "agent_pair_coop >= 2":
            return stats.get("agent_pair_coop", 0) >= 2
        elif cond == "scene_count >= 1":
            return stats.get("scene_count", 0) >= 1
        elif cond == "crisis_resolved >= 1":
            return stats.get("crisis_resolved", 0) >= 1
        elif cond == "exclusive_event >= 1":
            return stats.get("exclusive_event", 0) >= 1
        elif cond == "label_change >= 1":
            return stats.get("label_change", 0) >= 1
        elif cond == "hidden_harem":
            # 隐藏：3个以上角色好感度 > 0.6
            high_aff = stats.get("high_affinity_count", 0)
            return high_aff >= 3
        elif cond == "hidden_lonely":
            # 隐藏：7天不与任何角色互动
            return stats.get("inactive_days", 0) >= 7

        return False

    def pop_notifications(self) -> List[Dict]:
        """弹出待推送的通知（消费式）"""
        notifs = self._pending_notifications.copy()
        self._pending_notifications.clear()
        return notifs

    def get_all_achievements(self) -> List[Dict]:
        """获取所有成就（含解锁状态）"""
        result = []
        for ach in GAME_ACHIEVEMENTS:
            detail = self._build_achievement_detail(ach)
            ua = self._unlocked.get(ach["id"])
            detail["unlocked"] = ua is not None
            detail["unlocked_at"] = ua.unlocked_at if ua else None
            # 隐藏成就未解锁时隐藏信息
            if ach.get("hidden") and not detail["unlocked"]:
                detail["title"] = "???"
                detail["desc"] = "???"
                detail["icon"] = "❓"
            result.append(detail)
        return result

    def get_unlocked_count(self) -> Tuple[int, int]:
        """返回 (已解锁数, 总数)"""
        total = len(GAME_ACHIEVEMENTS)
        return len(self._unlocked), total

    def _build_achievement_detail(self, ach: Dict) -> Dict:
        rarity = ach.get("rarity", RARITY_COMMON)
        rarity_cfg = RARITY_CONFIG.get(rarity, RARITY_CONFIG[RARITY_COMMON])
        return {
            "id": ach["id"],
            "title": ach["title"],
            "desc": ach["desc"],
            "rarity": rarity,
            "rarity_label": rarity_cfg["label"],
            "rarity_color": rarity_cfg["color"],
            "rarity_glow": rarity_cfg["glow"],
            "category": ach.get("category", ""),
            "icon": ach.get("icon", "🏆"),
            "agents": ach.get("agents", []),
            "hidden": ach.get("hidden", False),
        }

    def to_dict(self) -> Dict:
        return {
            "unlocked": {aid: ua.to_dict() for aid, ua in self._unlocked.items()},
        }

    def load_from_dict(self, data: Dict):
        for aid, uadata in data.get("unlocked", {}).items():
            self._unlocked[aid] = UnlockedAchievement(**uadata)


# ========================================================================
# 4. 关系演化通知 — EvolutionNotifier
# ========================================================================

class EvolutionNotifier:
    """关系演化通知系统

    核心：关系变化要让用户感知到
    - 好感度突破关键阈值时通知
    - 关系名变化时通知
    - 羁绊里程碑时通知
    - 通知是弹幕式的——不阻断交互，但足够显眼
    """

    def __init__(self):
        self._pending: List[Dict] = []
        self._history: List[Dict] = []

    def notify_affinity_change(
        self,
        agent_id: str,
        agent_name: str,
        old_affinity: float,
        new_affinity: float,
        event: str = "",
    ) -> Optional[Dict]:
        """好感度变化通知"""
        # 只在突破关键阈值时通知
        thresholds = [0.2, 0.4, 0.6, 0.8, 0.9]
        for t in thresholds:
            if old_affinity < t <= new_affinity:
                notif = {
                    "type": "affinity_breakthrough",
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "threshold": t,
                    "old": round(old_affinity, 2),
                    "new": round(new_affinity, 2),
                    "event": event,
                    "timestamp": datetime.now().isoformat(),
                    "urgency": "high" if t >= 0.8 else "medium" if t >= 0.5 else "low",
                }
                self._pending.append(notif)
                self._history.append(notif)
                if len(self._history) > 200:
                    self._history = self._history[-200:]
                return notif

        # 好感度大幅下降也通知
        if new_affinity < old_affinity - 0.1:
            notif = {
                "type": "affinity_drop",
                "agent_id": agent_id,
                "agent_name": agent_name,
                "old": round(old_affinity, 2),
                "new": round(new_affinity, 2),
                "event": event,
                "timestamp": datetime.now().isoformat(),
                "urgency": "high",
            }
            self._pending.append(notif)
            self._history.append(notif)
            return notif

        return None

    def notify_label_change(
        self,
        agent_a: str,
        agent_a_name: str,
        agent_b: str,
        agent_b_name: str,
        old_label: str,
        new_label: str,
    ) -> Dict:
        """关系名变化通知"""
        notif = {
            "type": "label_evolution",
            "agent_a": agent_a,
            "agent_a_name": agent_a_name,
            "agent_b": agent_b,
            "agent_b_name": agent_b_name,
            "old_label": old_label,
            "new_label": new_label,
            "timestamp": datetime.now().isoformat(),
            "urgency": "medium",
        }
        self._pending.append(notif)
        self._history.append(notif)
        return notif

    def notify_bond_milestone(
        self,
        agent_a: str,
        agent_a_name: str,
        agent_b: str,
        agent_b_name: str,
        depth: int,
        title: str,
        desc: str,
    ) -> Dict:
        """羁绊里程碑通知"""
        notif = {
            "type": "bond_milestone",
            "agent_a": agent_a,
            "agent_a_name": agent_a_name,
            "agent_b": agent_b,
            "agent_b_name": agent_b_name,
            "depth": depth,
            "title": title,
            "desc": desc,
            "timestamp": datetime.now().isoformat(),
            "urgency": "high" if depth >= 7 else "medium",
        }
        self._pending.append(notif)
        self._history.append(notif)
        return notif

    def notify_user_relation_change(
        self,
        agent_id: str,
        agent_name: str,
        old_line: str,
        new_line: str,
        old_stage: str,
        new_stage: str,
    ) -> Optional[Dict]:
        """用户关系线/阶段变化通知"""
        if old_line == new_line and old_stage == new_stage:
            return None

        notif = {
            "type": "user_relation_evolution",
            "agent_id": agent_id,
            "agent_name": agent_name,
            "old_line": old_line,
            "new_line": new_line,
            "old_stage": old_stage,
            "new_stage": new_stage,
            "timestamp": datetime.now().isoformat(),
            "urgency": "high" if old_line != new_line else "medium",
        }
        self._pending.append(notif)
        self._history.append(notif)
        return notif

    def pop_notifications(self) -> List[Dict]:
        """弹出待推送通知"""
        notifs = self._pending.copy()
        self._pending.clear()
        return notifs

    def get_history(self, limit: int = 50) -> List[Dict]:
        return self._history[-limit:]

    def to_dict(self) -> Dict:
        return {"history": self._history[-200:]}

    def load_from_dict(self, data: Dict):
        self._history = data.get("history", [])


# ========================================================================
# 5. GameMechanicsFacade — 统一门面
# ========================================================================

class GameMechanicsFacade:
    """游戏化打磨统一门面

    外部（main.py）只需调用 facade，不需要直接操作各引擎。
    门面负责引擎间协调 + 持久化 + 通知聚合。
    """

    def __init__(self, data_dir: Optional[str] = None):
        self._data_dir = Path(data_dir) if data_dir else None

        self.attention = AttentionEngine()
        self.jealousy = JealousyEngine(self.attention)
        self.achievements = AchievementSystem()
        self.evolution = EvolutionNotifier()

        self._load()

    # ------ 关注值 ------

    def update_attention(
        self, agent_id: str, event: str, intensity: float = 1.0, context: str = ""
    ) -> Dict:
        result = self.attention.update(agent_id, event, intensity, context)

        # 如果触发吃醋风险，自动检测
        if result.get("jealousy_risk") in ("high", "medium"):
            # 检测吃醋会在 favor_other 时触发
            pass

        self._save()
        return result

    def get_attention(self, agent_id: str) -> Dict:
        state = self.attention.get_state(agent_id)
        return {
            "agent_id": agent_id,
            "attention_score": state.attention_score,
            "peak_attention": state.peak_attention,
            "last_interaction": state.last_interaction,
        }

    def get_all_attention(self) -> List[Dict]:
        return [
            {
                "agent_id": aid,
                "attention_score": s.attention_score,
                "peak_attention": s.peak_attention,
                "last_interaction": s.last_interaction,
            }
            for aid, s in self.attention._states.items()
        ]

    # ------ 吃醋 ------

    def trigger_jealousy(self, favored_agent: str, context: str = "") -> List[Dict]:
        events = self.jealousy.detect_jealousy(favored_agent, context)

        # 检查吃醋相关成就
        if events:
            stats = {
                "jealousy_count": len(self.jealousy._history),
                "simultaneous_jealousy": len(events),
            }
            self.achievements.check_and_unlock(stats, context="吃醋事件")

        self._save()
        return events

    def get_jealousy_status(self) -> List[Dict]:
        return self.jealousy.get_active_jealousies()

    def get_jealousy_modifier(self, agent_id: str) -> Optional[Dict]:
        return self.jealousy.get_jealousy_modifier(agent_id)

    def calm_jealousy(self, agent_id: str, amount: float = 0.3) -> Optional[Dict]:
        result = self.jealousy.calm_down(agent_id, amount)
        self._save()
        return result

    # ------ 成就 ------

    def check_achievements(self, stats: Dict, context: str = "") -> List[Dict]:
        newly = self.achievements.check_and_unlock(stats, context)
        if newly:
            self._save()
        return newly

    def get_achievements(self) -> List[Dict]:
        return self.achievements.get_all_achievements()

    def get_achievement_stats(self) -> Dict:
        unlocked, total = self.achievements.get_unlocked_count()
        return {"unlocked": unlocked, "total": total}

    # ------ 演化通知 ------

    def on_affinity_change(
        self, agent_id: str, agent_name: str, old: float, new: float, event: str = ""
    ) -> Optional[Dict]:
        """好感度变化时的回调——聚合关注值+通知+成就检查"""
        # 更新关注值
        if new > old:
            self.attention.update(agent_id, "chat", intensity=min((new - old) * 5, 1.5))

        # 发送通知
        notif = self.evolution.notify_affinity_change(agent_id, agent_name, old, new, event)

        # 检查成就
        self.achievements.check_and_unlock({
            "any_affinity": new,
            "high_affinity_count": sum(
                1 for s in self.attention._states.values()
                if s.attention_score >= 0.6
            ),
        })

        self._save()
        return notif

    def on_label_change(
        self, a_id: str, a_name: str, b_id: str, b_name: str,
        old_label: str, new_label: str,
    ) -> Dict:
        notif = self.evolution.notify_label_change(a_id, a_name, b_id, b_name, old_label, new_label)
        self.achievements.check_and_unlock({"label_change": 1}, context=f"{a_name}-{b_name}: {old_label}→{new_label}")
        self._save()
        return notif

    def notify_user_relation_change(
        self,
        agent_id: str,
        agent_name: str,
        old_line: str,
        new_line: str,
        old_stage: str,
        new_stage: str,
    ) -> Optional[Dict]:
        notif = self.evolution.notify_user_relation_change(
            agent_id,
            agent_name,
            old_line,
            new_line,
            old_stage,
            new_stage,
        )
        if notif:
            self._save()
        return notif

    # ------ 通知聚合 ------

    def pop_all_notifications(self) -> List[Dict]:
        """弹出所有待推送通知（成就+演化）"""
        notifs = []
        notifs.extend(self.evolution.pop_notifications())
        notifs.extend(self.achievements.pop_notifications())
        # 按时间排序
        notifs.sort(key=lambda n: n.get("timestamp", ""), reverse=False)
        return notifs

    # ------ 持久化 ------

    def _save(self):
        if not self._data_dir:
            return
        gm_dir = self._data_dir / "game_mechanics"
        gm_dir.mkdir(parents=True, exist_ok=True)

        data = {
            "attention": self.attention.to_dict(),
            "jealousy": self.jealousy.to_dict(),
            "achievements": self.achievements.to_dict(),
            "evolution": self.evolution.to_dict(),
        }
        try:
            with open(gm_dir / "game_state.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load(self):
        if not self._data_dir:
            return
        gm_dir = self._data_dir / "game_mechanics"
        fp = gm_dir / "game_state.json"
        if not fp.exists():
            return
        try:
            with open(fp, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            self.attention.load_from_dict(data.get("attention", {}))
            self.jealousy.load_from_dict(data.get("jealousy", {}))
            self.achievements.load_from_dict(data.get("achievements", {}))
            self.evolution.load_from_dict(data.get("evolution", {}))
        except Exception:
            pass

    # ------ 统计 ------

    def get_stats(self) -> Dict:
        unlocked, total = self.achievements.get_unlocked_count()
        return {
            "attention_avg": (
                sum(s.attention_score for s in self.attention._states.values())
                / max(len(self.attention._states), 1)
            ),
            "active_jealousies": len(self.jealousy._active_jealousies),
            "achievements_unlocked": unlocked,
            "achievements_total": total,
            "notifications_pending": (
                len(self.evolution._pending) + len(self.achievements._pending_notifications)
            ),
        }


# ========================================================================
# 6. 全局单例
# ========================================================================

_facade: Optional[GameMechanicsFacade] = None


def get_game_mechanics(data_dir: Optional[str] = None) -> GameMechanicsFacade:
    global _facade
    if _facade is None:
        _facade = GameMechanicsFacade(data_dir)
    return _facade


def reset_game_mechanics():
    global _facade
    _facade = None
