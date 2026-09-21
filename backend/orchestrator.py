"""
叙事编排层 — Scene + Orchestrator

Phase 3.3 核心模块：让协作从"功能"变成"故事"

设计哲学：
- 场景是叙事的发生地，不是聊天室——有弧线、有节奏、有结束
- 5种场景类型：邂逅/对峙/协力/群聚/危机
- 叙事节奏：冷却30min + 蓄力机制 + 用户主导
- 羁绊里程碑：bond_depth 达到阈值触发独占事件
- 工作成就：角色协作成果记录

核心模型：
  Scene       — 叙事场景（类型+参与者+弧线+状态）
  Orchestrator — 叙事编排引擎（场景创建/推进/解决/节奏控制）
  BondMilestone — 羁绊里程碑系统
"""
import json
import uuid
import time
import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple, Set
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from . import paths


# ====== 场景类型 ======
SCENE_ENCOUNTER    = "encounter"    # 邂逅
SCENE_CONFLICT     = "conflict"     # 对峙
SCENE_COOPERATION  = "cooperation"  # 协力
SCENE_GATHERING    = "gathering"    # 群聚
SCENE_CRISIS       = "crisis"       # 危机
SCENE_FREE         = "free"         # 自由（无预设叙事弧线，纯多人协作）

VALID_SCENE_TYPES = {
    SCENE_ENCOUNTER, SCENE_CONFLICT, SCENE_COOPERATION,
    SCENE_GATHERING, SCENE_CRISIS, SCENE_FREE,
}

SCENE_TYPE_LABELS = {
    SCENE_ENCOUNTER:   "邂逅",
    SCENE_CONFLICT:    "对峙",
    SCENE_COOPERATION: "协力",
    SCENE_GATHERING:   "群聚",
    SCENE_CRISIS:      "危机",
    SCENE_FREE:        "自由",
}

SCENE_TYPE_ICONS = {
    SCENE_ENCOUNTER:   "✨",
    SCENE_CONFLICT:    "⚡",
    SCENE_COOPERATION: "🤝",
    SCENE_GATHERING:   "🎭",
    SCENE_CRISIS:      "🔥",
    SCENE_FREE:        "💬",
}

# 场景阶段
PHASE_OPENING    = "opening"     # 开场
PHASE_RISING     = "rising"      # 升温
PHASE_CLIMAX     = "climax"      # 高潮
PHASE_RESOLUTION = "resolution"  # 收束

PHASE_ORDER = [PHASE_OPENING, PHASE_RISING, PHASE_CLIMAX, PHASE_RESOLUTION]

# 结束条件
END_RESOLVED       = "resolved"        # 自然解决
END_USER_INTERRUPT = "user_interrupt"  # 用户打断
END_TTL_EXPIRED   = "ttl_expired"     # 超时
END_CANCELLED     = "cancelled"        # 用户取消

# ====== 节奏控制常量 ======
COOLDOWN_SECONDS     = 1800    # 30分钟冷却
MAX_NARRATIVE_ENERGY = 100.0   # 叙事能量上限
ENERGY_PER_INTERACT  = 5.0     # 每次互动积累能量
ENERGY_COST = {
    SCENE_ENCOUNTER:   20.0,
    SCENE_CONFLICT:    40.0,
    SCENE_COOPERATION: 30.0,
    SCENE_GATHERING:   50.0,
    SCENE_CRISIS:      60.0,
}
DEFAULT_SCENE_TTL = 12  # 默认最大轮数


# ====== 羁绊里程碑定义 ======
BOND_MILESTONES = [
    {"depth": 1, "title": "初次协作", "desc": "原来你不是我想的那样"},
    {"depth": 2, "title": "默契配合", "desc": "一个眼神就够了"},
    {"depth": 3, "title": "互相信任", "desc": "这次，我听你的"},
    {"depth": 4, "title": "危机共渡", "desc": "并肩作战的印记"},
    {"depth": 5, "title": "秘密共享", "desc": "这件事我只告诉你"},
    {"depth": 6, "title": "羁绊觉醒", "desc": "关系名发生改变"},
    {"depth": 7, "title": "不可替代", "desc": "没有你的战场我不打"},
    {"depth": 8, "title": "生死之交", "desc": "可以托付后背的人"},
    {"depth": 9, "title": "灵魂共振", "desc": "无需言语的默契"},
    {"depth": 10, "title": "传说", "desc": "这个关系本身就是一个故事"},
]

# 独占事件（特定角色组合 + bond_depth 阈值触发）
EXCLUSIVE_EVENTS = [
    {
        "agents": frozenset(["hanqingxue", "xiaohandu"]),
        "min_bond": 6,
        "title": "冰下之火",
        "desc": "两个最冷的人，在某个夜晚第一次对彼此露出真面目",
        "scene_type": SCENE_CRISIS,
    },
    {
        "agents": frozenset(["chuyelan", "liuruyan"]),
        "min_bond": 7,
        "title": "棋局终了",
        "desc": "他不再和她下棋了——因为他不想赢了",
        "scene_type": SCENE_ENCOUNTER,
    },
    {
        "agents": frozenset(["xiawanling", "fushanyue"]),
        "min_bond": 5,
        "title": "灯下",
        "desc": "她终于看到了他撑不住的样子",
        "scene_type": SCENE_ENCOUNTER,
    },
    {
        "agents": frozenset(["shenweilan", "guwenshu"]),
        "min_bond": 5,
        "title": "纸上千秋",
        "desc": "一个人的文字，另一个人的世界——他们终于读懂了彼此",
        "scene_type": SCENE_ENCOUNTER,
    },
    {
        "agents": frozenset(["qingyuan", "fushanyue"]),
        "min_bond": 6,
        "title": "风雨同舟",
        "desc": "守护与担当交汇——两个最可靠的人，第一次需要彼此",
        "scene_type": SCENE_COOPERATION,
    },
]

# 工作成就定义
WORK_ACHIEVEMENTS = [
    {
        "id": "ice_fire",
        "title": "冰火共炼",
        "desc": "韩清雪+萧寒渡共同完成3次代码审查",
        "agents": frozenset(["hanqingxue", "xiaohandu"]),
        "required_coops": 3,
        "scene_type": SCENE_COOPERATION,
    },
    {
        "id": "chess_match",
        "title": "棋逢对手",
        "desc": "楚夜澜+沈微澜在同一任务中提供不同视角",
        "agents": frozenset(["chuyelan", "shenweilan"]),
        "required_coops": 2,
        "scene_type": SCENE_CONFLICT,
    },
    {
        "id": "storm_companion",
        "title": "风雨同舟",
        "desc": "傅山岳+青鸢在紧急任务中并肩作战",
        "agents": frozenset(["fushanyue", "qingyuan"]),
        "required_coops": 2,
        "scene_type": SCENE_COOPERATION,
    },
    {
        "id": "pen_and_sword",
        "title": "文武双全",
        "desc": "顾温书+陆乘风一个写文档一个写代码",
        "agents": frozenset(["guwenshu", "luchengfeng"]),
        "required_coops": 2,
        "scene_type": SCENE_COOPERATION,
    },
    {
        "id": "sunshine_effect",
        "title": "小太阳效应",
        "desc": "夏婉灵参与3次团队任务，每次团队效率提升",
        "agents": frozenset(["xiawanling"]),
        "required_coops": 3,
        "scene_type": SCENE_COOPERATION,
    },
]


# ====== Scene 模型 ======
@dataclass
class Scene:
    """叙事场景 — 角色协作的发生地"""
    id: str
    scene_type: str              # 场景类型
    participants: List[str]      # 参与角色 agent_id 列表
    trigger: str                 # 触发条件描述
    narrative_arc: str           # 叙事弧线："起→承→转→合"

    # 场景状态
    phase: str = PHASE_OPENING   # 当前阶段
    turn_order: List[str] = field(default_factory=list)  # 发言顺序
    current_turn: int = 0        # 当前轮次
    current_speaker: Optional[str] = None  # 当前发言者

    # 场景上下文（所有参与者共享）
    shared_context: Dict = field(default_factory=dict)

    # v2: 团队任务关联
    linked_task_id: Optional[str] = None

    # 结束条件
    end_condition: Optional[str] = None
    ttl: int = DEFAULT_SCENE_TTL  # 最大轮数

    # 时间戳
    created_at: str = ""
    updated_at: str = ""
    resolved_at: Optional[str] = None

    # 元数据
    metadata: Dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at
        if not self.turn_order and self.participants:
            self.turn_order = list(self.participants)
        if not self.current_speaker and self.turn_order:
            self.current_speaker = self.turn_order[0]

    @property
    def is_active(self) -> bool:
        return self.end_condition is None

    @property
    def phase_index(self) -> int:
        try:
            return PHASE_ORDER.index(self.phase)
        except ValueError:
            return 0

    @property
    def label(self) -> str:
        return SCENE_TYPE_LABELS.get(self.scene_type, self.scene_type)

    @property
    def icon(self) -> str:
        return SCENE_TYPE_ICONS.get(self.scene_type, "📋")

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "scene_type": self.scene_type,
            "scene_type_label": self.label,
            "icon": self.icon,
            "participants": self.participants,
            "trigger": self.trigger,
            "narrative_arc": self.narrative_arc,
            "phase": self.phase,
            "phase_label": {
                PHASE_OPENING: "开场", PHASE_RISING: "升温",
                PHASE_CLIMAX: "高潮", PHASE_RESOLUTION: "收束",
            }.get(self.phase, self.phase),
            "turn_order": self.turn_order,
            "current_turn": self.current_turn,
            "current_speaker": self.current_speaker,
            "shared_context": self.shared_context,
            "linked_task_id": self.linked_task_id,
            "end_condition": self.end_condition,
            "end_condition_label": {
                END_RESOLVED: "自然解决", END_USER_INTERRUPT: "用户打断",
                END_TTL_EXPIRED: "超时结束", END_CANCELLED: "已取消",
            }.get(self.end_condition or "", ""),
            "ttl": self.ttl,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "metadata": self.metadata,
        }


# ====== Orchestrator 引擎 ======
class Orchestrator:
    """叙事编排引擎

    职责：
    - 场景生命周期管理：创建/推进/解决/取消
    - 叙事节奏控制：冷却+蓄力+用户主导
    - 羁绊里程碑检测
    - 独占事件触发
    - 工作成就记录
    """

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = Path(data_dir) if data_dir else paths.orchestrator_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 活跃场景 {scene_id: Scene}
        self._scenes: Dict[str, Scene] = {}
        # 已结束场景（最近100条）
        self._history: List[Dict] = []

        # 冷却记录 {frozenset(participants): last_end_timestamp}
        self._cooldowns: Dict[frozenset, float] = {}
        # 叙事能量 {agent_id: energy}
        self._narrative_energy: Dict[str, float] = {}
        # 角色间协作计数 {frozenset(agent_ids): count}
        self._coop_counts: Dict[frozenset, int] = defaultdict(int)
        # 已解锁成就 {achievement_id: True}
        self._achievements: Dict[str, bool] = {}
        # 已触发的独占事件 {frozenset(agents): True}
        self._triggered_exclusives: Dict[frozenset, bool] = {}

        # 加载持久化数据
        self._load()

    # ====== 场景生命周期 ======

    def create_scene(
        self,
        scene_type: str,
        participants: List[str],
        trigger: str = "",
        shared_context: Optional[Dict] = None,
        linked_task_id: Optional[str] = None,
        ttl: int = DEFAULT_SCENE_TTL,
        force: bool = False,  # 用户手动触发时 force=True，跳过冷却检查
    ) -> Tuple[Optional[Scene], str]:
        """创建叙事场景

        返回 (Scene, reason) — reason 为空字符串表示成功
        """
        if scene_type not in VALID_SCENE_TYPES:
            return None, f"无效场景类型: {scene_type}"

        if len(participants) < 2 and scene_type != SCENE_GATHERING:
            return None, "场景至少需要2个参与者"

        # 检查冷却（force 跳过）
        if not force:
            key = frozenset(participants)
            last_end = self._cooldowns.get(key, 0)
            if time.time() - last_end < COOLDOWN_SECONDS:
                remaining = int(COOLDOWN_SECONDS - (time.time() - last_end))
                return None, f"冷却中，还需 {remaining // 60} 分钟"

        # 检查叙事能量（force 跳过）
        if not force:
            cost = ENERGY_COST.get(scene_type, 30.0)
            for agent_id in participants:
                if self._narrative_energy.get(agent_id, 0) < cost / len(participants):
                    return None, f"叙事能量不足（需要 {cost:.0f}，每角色至少 {cost / len(participants):.0f}）"

        # 构建叙事弧线
        arc = self._build_narrative_arc(scene_type, participants)

        scene = Scene(
            id=str(uuid.uuid4())[:8],
            scene_type=scene_type,
            participants=participants,
            trigger=trigger or self._auto_trigger_desc(scene_type, participants),
            narrative_arc=arc,
            shared_context=shared_context or {},
            linked_task_id=linked_task_id,
            ttl=ttl,
        )

        # 消耗叙事能量
        if not force:
            cost = ENERGY_COST.get(scene_type, 30.0)
            per_agent = cost / len(participants)
            for agent_id in participants:
                self._narrative_energy[agent_id] = max(
                    0, self._narrative_energy.get(agent_id, 0) - per_agent
                )

        self._scenes[scene.id] = scene
        self._save()
        return scene, ""

    def get_scene(self, scene_id: str) -> Optional[Scene]:
        return self._scenes.get(scene_id)

    def get_active_scenes(self) -> List[Scene]:
        return [s for s in self._scenes.values() if s.is_active]

    def advance_scene(self, scene_id: str, speaker_response: Optional[str] = None) -> Tuple[Optional[Scene], str]:
        """推进场景到下一轮/下一阶段

        逻辑：
        1. current_turn++，切换到下一个发言者
        2. 如果所有人发过言，推进到下一 phase
        3. 如果达到 TTL 或 resolution phase，自动解决
        """
        scene = self._scenes.get(scene_id)
        if not scene:
            return None, "场景不存在"
        if not scene.is_active:
            return None, "场景已结束"

        scene.current_turn += 1
        scene.updated_at = datetime.now().isoformat()

        # 切换发言者
        if scene.turn_order:
            idx = scene.current_turn % len(scene.turn_order)
            scene.current_speaker = scene.turn_order[idx]

        # 检查是否推进阶段
        turns_per_phase = max(scene.ttl // 4, 2)
        phase_idx = min(scene.current_turn // turns_per_phase, len(PHASE_ORDER) - 1)
        new_phase = PHASE_ORDER[phase_idx]

        if new_phase != scene.phase:
            scene.phase = new_phase

        # resolution 阶段或超过 TTL → 自动解决
        if scene.phase == PHASE_RESOLUTION or scene.current_turn >= scene.ttl:
            return self.resolve_scene(scene_id, END_RESOLVED)

        # 记录发言到共享上下文
        if speaker_response and scene.current_speaker:
            history = scene.shared_context.setdefault("dialogue_history", [])
            history.append({
                "speaker": scene.current_speaker,
                "content": speaker_response,
                "turn": scene.current_turn,
                "phase": scene.phase,
            })
            # 限制历史长度
            if len(history) > 50:
                scene.shared_context["dialogue_history"] = history[-50:]

        self._save()
        return scene, ""

    def resolve_scene(self, scene_id: str, end_condition: str = END_RESOLVED) -> Tuple[Optional[Scene], str]:
        """解决场景"""
        scene = self._scenes.get(scene_id)
        if not scene:
            return None, "场景不存在"
        if not scene.is_active:
            return None, "场景已结束"

        scene.end_condition = end_condition
        scene.resolved_at = datetime.now().isoformat()
        scene.updated_at = scene.resolved_at

        # 设置冷却
        key = frozenset(scene.participants)
        self._cooldowns[key] = time.time()

        # 记录协作
        if scene.scene_type == SCENE_COOPERATION:
            self._coop_counts[key] += 1
            # 检查工作成就
            self._check_achievements(scene.participants)

        # 移入历史
        self._history.append(scene.to_dict())
        if len(self._history) > 100:
            self._history = self._history[-100:]

        # 从活跃场景移除
        del self._scenes[scene_id]

        self._save()
        return scene, ""

    def cancel_scene(self, scene_id: str) -> Tuple[Optional[Scene], str]:
        """取消场景（用户主动）"""
        return self.resolve_scene(scene_id, END_CANCELLED)

    def add_participant(self, scene_id: str, agent_id: str) -> Tuple[Optional[Scene], str]:
        """向场景添加参与者（召唤）"""
        scene = self._scenes.get(scene_id)
        if not scene:
            return None, "场景不存在"
        if not scene.is_active:
            return None, "场景已结束"
        if agent_id in scene.participants:
            return scene, ""  # 已在场景中

        scene.participants.append(agent_id)
        scene.turn_order.append(agent_id)
        scene.updated_at = datetime.now().isoformat()

        self._save()
        return scene, ""

    # ====== 叙事节奏 ======

    def add_energy(self, agent_id: str, amount: float = ENERGY_PER_INTERACT):
        """为角色增加叙事能量"""
        current = self._narrative_energy.get(agent_id, 0)
        self._narrative_energy[agent_id] = min(current + amount, MAX_NARRATIVE_ENERGY)

    def get_energy(self, agent_id: str) -> float:
        return self._narrative_energy.get(agent_id, 0)

    def can_auto_trigger(self, participants: List[str], scene_type: str) -> Tuple[bool, str]:
        """检查是否可以自动触发场景"""
        key = frozenset(participants)
        # 冷却检查
        last_end = self._cooldowns.get(key, 0)
        if time.time() - last_end < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - (time.time() - last_end))
            return False, f"冷却中，还需 {remaining // 60} 分钟"

        # 能量检查
        cost = ENERGY_COST.get(scene_type, 30.0)
        for agent_id in participants:
            if self._narrative_energy.get(agent_id, 0) < cost / len(participants):
                return False, "叙事能量不足"

        return True, ""

    # ====== 羁绊里程碑 ======

    def check_bond_milestone(self, agent_a: str, agent_b: str, bond_depth: int) -> Optional[Dict]:
        """检查羁绊是否达到新里程碑"""
        for m in BOND_MILESTONES:
            if bond_depth == m["depth"]:
                return {
                    "type": "bond_milestone",
                    "agents": [agent_a, agent_b],
                    "depth": m["depth"],
                    "title": m["title"],
                    "desc": m["desc"],
                }
        return None

    def check_exclusive_event(self, agent_ids: List[str], bond_depths: Dict[Tuple[str, str], int]) -> Optional[Dict]:
        """检查是否触发独占事件"""
        key = frozenset(agent_ids)
        if self._triggered_exclusives.get(key):
            return None  # 已触发过

        for event in EXCLUSIVE_EVENTS:
            if key == event["agents"]:
                # 检查所有参与对的 bond_depth
                agents_list = list(event["agents"])
                pair_key = tuple(sorted(agents_list))
                depth = bond_depths.get(pair_key, 0)
                if depth >= event["min_bond"]:
                    self._triggered_exclusives[key] = True
                    return {
                        "type": "exclusive_event",
                        "title": event["title"],
                        "desc": event["desc"],
                        "agents": list(event["agents"]),
                        "scene_type": event["scene_type"],
                        "min_bond": event["min_bond"],
                    }
        return None

    # ====== 工作成就 ======

    def _check_achievements(self, participants: List[str]):
        """检查工作成就"""
        key = frozenset(participants)
        count = self._coop_counts.get(key, 0)

        for ach in WORK_ACHIEVEMENTS:
            if ach["id"] in self._achievements:
                continue
            # 单人成就（如小太阳效应）
            if len(ach["agents"]) == 1:
                if ach["agents"].issubset(key) and count >= ach["required_coops"]:
                    self._achievements[ach["id"]] = True
            # 双人成就
            elif ach["agents"].issubset(key) and count >= ach["required_coops"]:
                self._achievements[ach["id"]] = True

    def get_achievements(self) -> List[Dict]:
        """获取所有已解锁成就"""
        result = []
        for ach in WORK_ACHIEVEMENTS:
            result.append({
                **ach,
                "agents": list(ach["agents"]),
                "unlocked": ach["id"] in self._achievements,
            })
        return result

    # ====== 场景系统提示词构建 ======

    def build_scene_system_prompt(
        self,
        scene: Scene,
        agent_id: str,
        agent_name: str,
        other_agents_info: Dict[str, Dict],
        user_relation_info: Optional[Dict] = None,
    ) -> str:
        """为场景中的角色构建 system prompt 附加段

        Args:
            scene: 当前场景
            agent_id: 当前角色 ID
            agent_name: 当前角色名
            other_agents_info: {agent_id: {name, bond_type, affinity, what_you_know}}
            user_relation_info: {current_line, bond_depth}
        """
        others_str = "、".join(
            f"{info.get('name', aid)}（{info.get('bond_type', '未知')}，好感度{info.get('affinity', 0):.1f}）"
            for aid, info in other_agents_info.items()
        )

        user_relation_str = ""
        if user_relation_info:
            line = user_relation_info.get("current_line", "羁绊线")
            depth = user_relation_info.get("bond_depth", 0)
            user_relation_str = f"""
## 你与用户的关系
关系类型：{line}（恋人线/羁绊线/守护线）
关系深度：{depth}
"""

        dialogue_context = ""
        history = scene.shared_context.get("dialogue_history", [])
        if history:
            # 最近几轮对话摘要
            recent = history[-6:]
            lines = []
            for h in recent:
                speaker_name = h.get("speaker", "?")
                for aid, info in other_agents_info.items():
                    if aid == h.get("speaker"):
                        speaker_name = info.get("name", aid)
                        break
                lines.append(f"{speaker_name}: {h.get('content', '')[:100]}")
            dialogue_context = f"""
## 之前的对话
{chr(10).join(lines)}
"""

        return f"""## 当前场景
你正处于「{scene.label}」场景中，与{others_str}共处。
场景描述：{scene.trigger}
当前阶段：{scene.phase}（第{scene.current_turn + 1}轮）
{dialogue_context}
{user_relation_str}
## 场景规则
- 你可以感知其他角色的发言
- 你的回复会被其他角色看到
- 保持角色一致性，按你的性格和关系做出反应
- 不要读心——你只知道对方说了什么，不知道对方在想什么
- 当前轮到你发言，请以{agent_name}的身份回应"""

    # ====== 内部方法 ======

    def _build_narrative_arc(self, scene_type: str, participants: List[str]) -> str:
        """构建叙事弧线描述"""
        arcs = {
            SCENE_ENCOUNTER: "偶然相遇 → 意外发现 → 关系微变 → 各自离开",
            SCENE_CONFLICT: "立场碰撞 → 各执一词 → 张力达到顶峰 → 找到平衡或决裂",
            SCENE_COOPERATION: "明确分工 → 各展所长 → 遇到障碍 → 协作突破 → 共同成果",
            SCENE_GATHERING: "众人聚集 → 自由互动 → 化学反应 → 意外收获 → 散场",
            SCENE_CRISIS: "危机降临 → 紧急应对 → 考验关系 → 关键抉择 → 转机或崩溃",
        }
        return arcs.get(scene_type, "起→承→转→合")

    def _auto_trigger_desc(self, scene_type: str, participants: List[str]) -> str:
        """自动生成触发描述"""
        descs = {
            SCENE_ENCOUNTER: f"{' 与 '.join(participants)} 在同一时空相遇",
            SCENE_CONFLICT: f"{' 与 '.join(participants)} 对某事持有不同立场",
            SCENE_COOPERATION: f"{' 与 '.join(participants)} 协力完成某项任务",
            SCENE_GATHERING: f"{'、'.join(participants)} 聚在一起",
            SCENE_CRISIS: f"{' 与 '.join(participants)} 面临关系危机",
        }
        return descs.get(scene_type, f"场景触发：{participants}")

    # ====== 持久化 ======

    def _save(self):
        """保存到 JSON"""
        data = {
            "scenes": {sid: s.to_dict() for sid, s in self._scenes.items()},
            "history": self._history[-100:],
            "cooldowns": {",".join(sorted(k)): v for k, v in self._cooldowns.items()},
            "narrative_energy": self._narrative_energy,
            "coop_counts": {",".join(sorted(k)): v for k, v in self._coop_counts.items()},
            "achievements": self._achievements,
            "triggered_exclusives": {",".join(sorted(k)): v for k, v in self._triggered_exclusives.items()},
        }
        try:
            with open(self.data_dir / "orchestrator.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 静默失败，不影响运行

    def _load(self):
        """从 JSON 加载"""
        fp = self.data_dir / "orchestrator.json"
        if not fp.exists():
            return
        try:
            with open(fp, "r", encoding="utf-8-sig") as f:
                data = json.load(f)

            # 恢复场景
            for sid, sd in data.get("scenes", {}).items():
                self._scenes[sid] = Scene(
                    id=sd["id"],
                    scene_type=sd["scene_type"],
                    participants=sd["participants"],
                    trigger=sd.get("trigger", ""),
                    narrative_arc=sd.get("narrative_arc", ""),
                    phase=sd.get("phase", PHASE_OPENING),
                    turn_order=sd.get("turn_order", []),
                    current_turn=sd.get("current_turn", 0),
                    current_speaker=sd.get("current_speaker"),
                    shared_context=sd.get("shared_context", {}),
                    linked_task_id=sd.get("linked_task_id"),
                    end_condition=sd.get("end_condition"),
                    ttl=sd.get("ttl", DEFAULT_SCENE_TTL),
                    created_at=sd.get("created_at", ""),
                    updated_at=sd.get("updated_at", ""),
                    resolved_at=sd.get("resolved_at"),
                    metadata=sd.get("metadata", {}),
                )

            self._history = data.get("history", [])

            # 恢复冷却
            for k, v in data.get("cooldowns", {}).items():
                self._cooldowns[frozenset(k.split(","))] = v

            self._narrative_energy = data.get("narrative_energy", {})

            # 恢复协作计数
            for k, v in data.get("coop_counts", {}).items():
                self._coop_counts[frozenset(k.split(","))] = v

            self._achievements = data.get("achievements", {})
            self._triggered_exclusives = data.get("triggered_exclusives", {})

        except Exception:
            pass  # 加载失败不崩溃

    # ====== 统计 ======

    def get_stats(self) -> Dict:
        """获取编排引擎统计"""
        return {
            "active_scenes": len(self._scenes),
            "total_history": len(self._history),
            "total_cooperations": sum(self._coop_counts.values()),
            "achievements_unlocked": len(self._achievements),
            "achievements_total": len(WORK_ACHIEVEMENTS),
            "exclusive_events_triggered": len(self._triggered_exclusives),
            "exclusive_events_total": len(EXCLUSIVE_EVENTS),
            "narrative_energy_avg": (
                sum(self._narrative_energy.values()) / len(self._narrative_energy)
                if self._narrative_energy else 0
            ),
        }


# ====== 全局单例 ======
_orchestrator: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
