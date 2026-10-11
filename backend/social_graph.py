"""
社交网络层 — SocialGraph + Relationship

Phase 3.2 核心模块：角色之间的"化学反应方程式"

设计哲学：
- 关系不是静态标签，是动态演化的生命力
- 好感度防通胀：日常互动天花板0.6，突破需特殊事件
- 关系影响行为：收到消息时的态度由好感度决定
- 用户关系三线并行：恋人/羁绊/守护，动态判定不锁死
- 羁绊值（bond_depth）是叙事深度——冲突也能加深羁绊

核心概念：
  Relationship — 一对角色间的关系（好感度+羁绊值+互动统计+认知）
  SocialGraph  — 全局关系图（10角色45对关系+持久化+查询）
  UserRelation — 角色与用户的关系（三线并行+倾向性+演化）
"""
import json
import logging
import math
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple, Set
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from itertools import combinations

logger = logging.getLogger(__name__)


# ====== 关系类型 ======
BOND_SIBLING   = "姐妹"     # 手足之情
BOND_RIVAL     = "对手"     # 竞争/对峙
BOND_FRIEND    = "挚友"     # 深度友谊
BOND_GUARDIAN  = "守护"     # 保护/庇护
BOND_OLD       = "旧识"     # 过往交集
BOND_SOULMATE  = "知音"     # 精神共鸣
BOND_DISTRUST  = "不信任"   # 互不信任
BOND_ENTANGLE  = "纠葛"     # 复杂纠葛
BOND_JOY       = "欢喜"     # 欢喜冤家
BOND_CARE      = "关照"     # 体贴关怀
BOND_WATCH     = "审视"     # 观察评估
BOND_SAME      = "同类"     # 气味相投
BOND_CONFRONT  = "对峙"     # 正面冲突

VALID_BOND_TYPES = {
    BOND_SIBLING, BOND_RIVAL, BOND_FRIEND, BOND_GUARDIAN,
    BOND_OLD, BOND_SOULMATE, BOND_DISTRUST, BOND_ENTANGLE,
    BOND_JOY, BOND_CARE, BOND_WATCH, BOND_SAME, BOND_CONFRONT,
}

# ====== 用户关系三线 ======
LINE_LOVER    = "恋人线"     # 灵魂伴侣
LINE_BOND     = "羁绊线"     # 知己/挚友
LINE_GUARDIAN = "守护线"     # 誓约/忠诚

VALID_RELATION_LINES = {LINE_LOVER, LINE_BOND, LINE_GUARDIAN}

# 每个角色的三线倾向性（概率分布，总和=1.0）
AGENT_LINE_BIAS = {
    "liuruyan":     {LINE_LOVER: 0.90, LINE_BOND: 0.05, LINE_GUARDIAN: 0.05},
    "hanqingxue":   {LINE_LOVER: 0.60, LINE_BOND: 0.30, LINE_GUARDIAN: 0.10},
    "xiawanling":   {LINE_LOVER: 0.70, LINE_BOND: 0.20, LINE_GUARDIAN: 0.10},
    "shenweilan":   {LINE_LOVER: 0.15, LINE_BOND: 0.70, LINE_GUARDIAN: 0.15},
    "qingyuan":     {LINE_LOVER: 0.10, LINE_BOND: 0.10, LINE_GUARDIAN: 0.80},
    "guwenshu":     {LINE_LOVER: 0.20, LINE_BOND: 0.60, LINE_GUARDIAN: 0.20},
    "xiaohandu":    {LINE_LOVER: 0.10, LINE_BOND: 0.15, LINE_GUARDIAN: 0.75},
    "luchengfeng":  {LINE_LOVER: 0.25, LINE_BOND: 0.55, LINE_GUARDIAN: 0.20},
    "fushanyue":    {LINE_LOVER: 0.10, LINE_BOND: 0.20, LINE_GUARDIAN: 0.70},
    "chuyelan":     {LINE_LOVER: 0.75, LINE_BOND: 0.15, LINE_GUARDIAN: 0.10},
}

# 三线演化阶段
LINE_STAGES = {
    LINE_LOVER:    ["好奇", "心动", "试探", "确认", "依恋", "共生"],
    LINE_BOND:     ["欣赏", "默契", "信任", "懂你", "无需言语"],
    LINE_GUARDIAN: ["陌生", "认可", "守护", "誓约", "以命相护"],
}

# ====== 好感度演化规则 ======
AFFINITY_EVENTS = {
    "cooperation":      {"min": 0.05, "max": 0.15},   # 共同为用户做事
    "task_success":     {"min": 0.10, "max": 0.20},   # 团队任务协作成功
    "task_failure":     {"min": -0.10, "max": -0.05},  # 团队任务协作失败
    "conflict":         {"min": -0.08, "max": -0.02},  # 观点冲突（但bond_depth可能+1）
    "help":             {"min": 0.10, "max": 0.20},   # 一方帮助另一方
    "secret":           {"min": 0.15, "max": 0.25},   # 发现共同秘密
    "betrayal":         {"min": -0.50, "max": -0.20},  # 背叛/欺骗
    "user_matchmake":   {"min": 0.10, "max": 0.30},   # 用户撮合
    "decay":            {"min": -0.01, "max": -0.01},  # 长时间无互动（每天）
}

# 好感度天花板：日常互动天花板
DAILY_AFFINITY_CEILING = 0.6
# 特殊事件才能突破天花板
SPECIAL_EVENTS = {"task_success", "secret", "betrayal", "user_matchmake", "help"}

# 增量递减参数（每次互动的增量随互动次数递减）
AFFINITY_DIMINISHING_FACTOR = 0.85  # 每次互动后，下次增量×0.85

# 自然衰减参数
DECAY_THRESHOLD_DAYS = 7  # 超过7天无互动开始衰减
DECAY_RATE = 0.01  # 每天衰减0.01

# 损失厌恶：好感度下降时额外乘数
LOSS_AVERSION_FACTOR = 1.3


# ====== 关系模型 ======
@dataclass
class Relationship:
    """角色间关系"""
    agent_a: str
    agent_b: str

    # 关系类型（可叠加）
    bond_types: List[str] = field(default_factory=list)

    # 好感度系统
    affinity: float = 0.0           # -1.0 ~ 1.0
    affinity_history: List[Dict] = field(default_factory=list)

    # 羁绊值（叙事深度）
    bond_depth: int = 0             # 0~10
    bond_milestones: List[str] = field(default_factory=list)

    # 互动统计
    interaction_count: int = 0
    last_interaction: Optional[str] = None

    # 角色对彼此的认知
    a_knows_about_b: List[str] = field(default_factory=list)
    b_knows_about_a: List[str] = field(default_factory=list)

    # v2: 工作协作统计
    task_collab_count: int = 0
    task_collab_success: int = 0

    # v2: 关系名演化
    current_label: str = "陌生"
    label_history: List[Dict] = field(default_factory=list)

    def _pair_key(self) -> str:
        """生成唯一键（字典序）"""
        a, b = sorted([self.agent_a, self.agent_b])
        return f"{a}:{b}"

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "Relationship":
        return cls(**data)

    def get_affinity_for(self, agent_id: str) -> Optional[float]:
        """获取某角色视角的好感度（当前是对称的，但预留扩展空间）"""
        return self.affinity

    def get_knowledge(self, agent_id: str) -> List[str]:
        """获取某角色对另一角色的认知"""
        if agent_id == self.agent_a:
            return self.a_knows_about_b
        elif agent_id == self.agent_b:
            return self.b_knows_about_a
        return []

    def add_knowledge(self, agent_id: str, knowledge: str):
        """添加认知"""
        if agent_id == self.agent_a and knowledge not in self.a_knows_about_b:
            self.a_knows_about_b.append(knowledge)
            if len(self.a_knows_about_b) > 20:
                self.a_knows_about_b = self.a_knows_about_b[-20:]
        elif agent_id == self.agent_b and knowledge not in self.b_knows_about_a:
            self.b_knows_about_a.append(knowledge)
            if len(self.b_knows_about_a) > 20:
                self.b_knows_about_a = self.b_knows_about_a[-20:]


# ====== 用户关系 ======
@dataclass
class UserRelation:
    """角色与用户的关系"""
    agent_id: str

    # 三线并行
    current_line: str = ""          # 当前关系线
    line_scores: Dict[str, float] = field(default_factory=dict)  # 三线得分

    # 演化阶段
    current_stage: str = "初识"
    stage_index: int = 0

    # 好感度
    user_affinity: float = 0.0      # 对用户的好感度 0~1.0
    user_affinity_history: List[Dict] = field(default_factory=list)

    # 互动统计
    total_interactions: int = 0
    deep_interactions: int = 0      # 深度互动（情感浓度高）
    crisis_interactions: int = 0    # 共同经历困难

    # 关注值（吃醋机制的基础）
    attention_score: float = 0.5    # 0~1.0，对用户的关注程度

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "UserRelation":
        return cls(**data)


# ====== 出厂关系矩阵 ======
DEFAULT_RELATIONSHIPS = [
    # (A, B, 关系类型, 好感度, 描述)
    ("liuruyan", "chuyelan", [BOND_ENTANGLE], 0.3, "她看穿他的把戏，他对此很感兴趣"),
    ("liuruyan", "hanqingxue", [BOND_SIBLING], 0.6, "万界漂泊者与宗门天骄，冷与更冷"),
    ("chuyelan", "xiaohandu", [BOND_CONFRONT], -0.2, "两个不说话的人，沉默本身就是较量"),
    ("fushanyue", "qingyuan", [BOND_GUARDIAN], 0.7, "他撑着所有人，她守护所有人——谁守护他们？"),
    ("guwenshu", "shenweilan", [BOND_SOULMATE], 0.8, "温润与深邃，不需要多说的默契"),
    ("xiawanling", "luchengfeng", [BOND_JOY], 0.5, "小太阳遇上自由风，谁先被谁改变？"),
    ("xiaohandu", "hanqingxue", [BOND_SAME], 0.4, "冷到极致的两个人，只有彼此知道冰下面是什么"),
    ("fushanyue", "chuyelan", [BOND_DISTRUST], -0.1, "一个太正，一个太邪——但目标可能相同"),
    ("guwenshu", "xiawanling", [BOND_CARE], 0.6, "他温柔地照顾所有人，但谁照顾他的疲惫？"),
    ("shenweilan", "liuruyan", [BOND_WATCH], 0.2, "深水看万界——她觉得柳如烟在藏什么"),
    # 补充关系
    ("liuruyan", "xiawanling", [BOND_CARE], 0.5, "如烟般包容，婉灵般灵动——互补的温暖"),
    ("liuruyan", "qingyuan", [BOND_GUARDIAN], 0.45, "归人与守卫——不同的守护方式"),
    ("hanqingxue", "xiawanling", [BOND_JOY], 0.35, "冷面天骄遇上元气少女——嫌弃中带着纵容"),
    ("chuyelan", "shenweilan", [BOND_WATCH], 0.15, "两个洞察者互相观察——谁能看透谁？"),
    ("xiaohandu", "fushanyue", [BOND_OLD], 0.3, "沉默的同行者——不需要语言也能配合"),
    ("guwenshu", "luchengfeng", [BOND_FRIEND], 0.5, "温润与洒脱——在一起很舒服"),
    ("xiawanling", "qingyuan", [BOND_FRIEND], 0.55, "阳光和盾——一个冲锋一个挡"),
    ("shenweilan", "hanqingxue", [BOND_WATCH], 0.25, "深水看冰山——她觉得韩清雪的冷有原因"),
    ("luchengfeng", "chuyelan", [BOND_RIVAL], 0.1, "自由风与狐——谁也困不住谁"),
    ("fushanyue", "guwenshu", [BOND_FRIEND], 0.5, "山与茶——沉稳中的温情"),
]


# ====== 社交图 ======
class SocialGraph:
    """
    全局关系图 — 10角色45对关系 + 用户关系10条

    设计要点：
    - 好感度防通胀：日常互动天花板0.6，突破需特殊事件
    - 增量递减：每次互动后下次增量×0.85
    - 自然衰减：7天无互动每天-0.01
    - 损失厌恶：好感度下降时额外×1.3
    - 关系影响行为：好感度决定角色对跨角色消息的态度
    """

    _instance: Optional["SocialGraph"] = None

    def __init__(self, data_dir: Optional[str] = None):
        self._relationships: Dict[str, Relationship] = {}  # "a:b" -> Relationship
        self._user_relations: Dict[str, UserRelation] = {}  # agent_id -> UserRelation
        self._data_dir = Path(data_dir) if data_dir else None
        self._agent_ids: Set[str] = set()

    @classmethod
    def get_instance(cls, data_dir: Optional[str] = None) -> "SocialGraph":
        if cls._instance is None:
            cls._instance = cls(data_dir)
        return cls._instance

    # ------ 工具方法 ------

    @staticmethod
    def make_pair_key(a: str, b: str) -> str:
        """生成关系键（字典序）"""
        x, y = sorted([a, b])
        return f"{x}:{y}"

    # ------ 初始化 ------

    def initialize(self, agent_ids: List[str]):
        """初始化社交图（加载出厂关系 + 创建默认用户关系）"""
        self._agent_ids = set(agent_ids)

        # 加载出厂关系
        for a, b, bond_types, affinity, description in DEFAULT_RELATIONSHIPS:
            key = self.make_pair_key(a, b)
            if key not in self._relationships:
                rel = Relationship(
                    agent_a=a,
                    agent_b=b,
                    bond_types=bond_types,
                    affinity=affinity,
                    current_label=bond_types[0] if bond_types else "陌生",
                )
                self._relationships[key] = rel

        # 补全缺失的关系对（45对全覆盖）
        for a, b in combinations(sorted(agent_ids), 2):
            key = self.make_pair_key(a, b)
            if key not in self._relationships:
                self._relationships[key] = Relationship(
                    agent_a=a,
                    agent_b=b,
                    current_label="陌生",
                )

        # 创建默认用户关系
        for agent_id in agent_ids:
            if agent_id not in self._user_relations:
                bias = AGENT_LINE_BIAS.get(agent_id, {
                    LINE_LOVER: 0.33, LINE_BOND: 0.34, LINE_GUARDIAN: 0.33
                })
                # 倾向性最高的线作为默认
                default_line = max(bias, key=bias.get)
                self._user_relations[agent_id] = UserRelation(
                    agent_id=agent_id,
                    current_line=default_line,
                    line_scores={LINE_LOVER: 0.0, LINE_BOND: 0.0, LINE_GUARDIAN: 0.0},
                    current_stage=LINE_STAGES[default_line][0],
                )

        # 从磁盘恢复（覆盖出厂默认值）
        self.load_from_disk()

    # ------ 关系查询 ------

    def get_relationship(self, agent_a: str, agent_b: str) -> Optional[Relationship]:
        """获取两个角色间的关系"""
        key = self.make_pair_key(agent_a, agent_b)
        return self._relationships.get(key)

    def get_all_relationships(self, agent_id: Optional[str] = None) -> List[Relationship]:
        """获取所有关系（可选按角色过滤）"""
        if not agent_id:
            return list(self._relationships.values())
        return [
            rel for rel in self._relationships.values()
            if rel.agent_a == agent_id or rel.agent_b == agent_id
        ]

    def get_affinity(self, agent_a: str, agent_b: str) -> float:
        """获取两个角色间的好感度"""
        rel = self.get_relationship(agent_a, agent_b)
        return rel.affinity if rel else 0.0

    def get_relationships_summary(self, agent_id: str) -> List[Dict]:
        """获取某角色的关系摘要（用于前端展示）"""
        result = []
        for rel in self.get_all_relationships(agent_id):
            other = rel.agent_b if rel.agent_a == agent_id else rel.agent_a
            result.append({
                "agent_id": other,
                "bond_types": rel.bond_types,
                "affinity": round(rel.affinity, 3),
                "bond_depth": rel.bond_depth,
                "current_label": rel.current_label,
                "interaction_count": rel.interaction_count,
                "last_interaction": rel.last_interaction,
            })
        # 按好感度降序
        result.sort(key=lambda x: x["affinity"], reverse=True)
        return result

    # ------ 模板继承（实例化用）------

    def snapshot_from_template(self, instance_id: str, template_id: str) -> Dict[str, int]:
        """新建实例时把模板名下的所有社交关系一次性快照到实例 id 名下。

        设计动机（2026-05-21 解耦改造）：
        - 出厂关系矩阵（DEFAULT_RELATIONSHIPS）全用模板名作 ID（liuruyan / chuyelan ...）
        - 实例 ID（agent_xxx_yyy）跟出厂关系无关联，原本靠 build_simple_relationship_text 的
          runtime fallback 临时回查模板名才能拿到关系——但这是"实例对模板的活链接"，
          模板一删 → 实例瞬间六亲不认。
        - 现在改成"实例创建那一刻起就自包含"：把 template_id 名下所有关系复制一份，
          key 里的 template_id 替换成 instance_id，从此跟模板再无瓜葛。

        细节：
        - 关系是双向的（key 由 sorted([a,b]) 拼成），所以遍历 _relationships 找出
          a==template_id 或 b==template_id 的所有 entry，逐个复制并改 ID。
        - 跟其他**模板名**之间的关系会保留（agent_xxx:chuyelan）。
        - 如果用户也实例化了 chuyelan 为 agent_yyy，那 agent_xxx ↔ agent_yyy 这对关系
          需要业务侧另行建立——不在本方法范围。
        - user_relation 同理快照。
        - 不修改原 template_id 名下的关系（保持模板纯净）。

        Args:
            instance_id: 新建实例的 id（如 agent_1716000000_1234）
            template_id: 模板名（如 liuruyan）

        Returns:
            {"relationships_copied": int, "user_relation_copied": int}
        """
        from copy import deepcopy

        copied_rels = 0
        new_pairs: Dict[str, Relationship] = {}

        for key, rel in list(self._relationships.items()):
            if rel.agent_a == template_id or rel.agent_b == template_id:
                # 把 template_id 那一端换成 instance_id
                new_rel = deepcopy(rel)
                if new_rel.agent_a == template_id:
                    new_rel.agent_a = instance_id
                else:
                    new_rel.agent_b = instance_id
                new_key = self.make_pair_key(new_rel.agent_a, new_rel.agent_b)
                # 已存在的不覆盖（容错：脚本重跑安全）
                if new_key not in self._relationships and new_key not in new_pairs:
                    new_pairs[new_key] = new_rel
                    copied_rels += 1

        self._relationships.update(new_pairs)

        copied_ur = 0
        if template_id in self._user_relations and instance_id not in self._user_relations:
            self._user_relations[instance_id] = deepcopy(self._user_relations[template_id])
            self._user_relations[instance_id].agent_id = instance_id
            copied_ur = 1

        # 把实例 id 加进 _agent_ids 集合，否则后续遍历可能漏掉
        self._agent_ids.add(instance_id)

        return {"relationships_copied": copied_rels, "user_relation_copied": copied_ur}

    # ------ 好感度引擎 ------

    def update_affinity(
        self,
        agent_a: str,
        agent_b: str,
        event_type: str,
        intensity: float = 1.0,
        description: str = "",
    ) -> Optional[Dict]:
        """
        更新两个角色间的好感度

        Args:
            agent_a, agent_b: 角色ID
            event_type: 事件类型（见 AFFINITY_EVENTS）
            intensity: 事件强度 0~1.0
            description: 事件描述

        Returns:
            更新结果 {"old": float, "new": float, "change": float} 或 None
        """
        rel = self.get_relationship(agent_a, agent_b)
        if not rel:
            return None

        event_config = AFFINITY_EVENTS.get(event_type)
        if not event_config:
            return None

        old_affinity = rel.affinity

        # 计算基础增量
        base_min = event_config["min"]
        base_max = event_config["max"]
        # 在范围内按强度插值
        if base_min <= base_max:
            delta = base_min + (base_max - base_min) * min(intensity, 1.0)
        else:
            # 负向事件
            delta = base_max + (base_min - base_max) * min(intensity, 1.0)

        # 增量递减：互动次数越多，增量越小
        diminish = AFFINITY_DIMINISHING_FACTOR ** rel.interaction_count
        delta *= diminish

        # 天花板机制：日常互动不突破 0.6
        if event_type not in SPECIAL_EVENTS:
            if old_affinity + delta > DAILY_AFFINITY_CEILING:
                delta = max(0, DAILY_AFFINITY_CEILING - old_affinity)

        # 损失厌恶：好感度下降时额外乘数
        if delta < 0:
            delta *= LOSS_AVERSION_FACTOR

        # 应用变化
        new_affinity = max(-1.0, min(1.0, old_affinity + delta))
        rel.affinity = round(new_affinity, 4)

        # 更新互动统计
        rel.interaction_count += 1
        rel.last_interaction = datetime.now().isoformat()

        # 记录历史（限100条）
        rel.affinity_history.append({
            "event": event_type,
            "delta": round(delta, 4),
            "old": round(old_affinity, 4),
            "new": round(new_affinity, 4),
            "description": description,
            "timestamp": datetime.now().isoformat(),
        })
        if len(rel.affinity_history) > 100:
            rel.affinity_history = rel.affinity_history[-100:]

        # 观点冲突时bond_depth可能+1
        if event_type == "conflict" and rel.bond_depth < 10:
            if intensity > 0.6:  # 高强度冲突加深羁绊
                rel.bond_depth = min(10, rel.bond_depth + 1)
                rel.bond_milestones.append(f"冲突中加深理解({description})")

        # 团队任务协作成功时bond_depth+1
        if event_type == "task_success" and rel.bond_depth < 10:
            rel.bond_depth = min(10, rel.bond_depth + 1)
            rel.bond_milestones.append(f"协作成功({description})")

        # 更新协作统计
        if event_type in ("task_success", "task_failure"):
            rel.task_collab_count += 1
            if event_type == "task_success":
                rel.task_collab_success += 1

        # 更新关系名
        self._update_label(rel)

        return {
            "old": round(old_affinity, 4),
            "new": round(new_affinity, 4),
            "change": round(delta, 4),
            "event": event_type,
        }

    def _update_label(self, rel: Relationship):
        """根据好感度和羁绊值更新关系名"""
        aff = rel.affinity
        depth = rel.bond_depth
        old_label = rel.current_label

        # 如果有出厂关系类型且深度低，保留出厂标签
        if depth <= 1 and rel.bond_types:
            new_label = rel.bond_types[0]
        elif aff >= 0.8 and depth >= 7:
            new_label = "生死之交"
        elif aff >= 0.6 and depth >= 5:
            new_label = "挚友"
        elif aff >= 0.4 and depth >= 3:
            new_label = "朋友"
        elif aff >= 0.2:
            new_label = "相识"
        elif aff >= -0.2:
            new_label = "陌生"
        elif aff >= -0.5:
            new_label = "对头"
        else:
            new_label = "仇敌"

        if new_label != old_label:
            rel.label_history.append({
                "old": old_label,
                "new": new_label,
                "affinity": round(aff, 4),
                "bond_depth": depth,
                "timestamp": datetime.now().isoformat(),
            })
            if len(rel.label_history) > 50:
                rel.label_history = rel.label_history[-50:]
            rel.current_label = new_label

    # ------ 自然衰减 ------

    def apply_decay(self):
        """对所有关系应用自然衰减（7天无互动每天-0.01）"""
        now = datetime.now()
        decayed = 0

        for rel in self._relationships.values():
            if not rel.last_interaction:
                continue
            try:
                last = datetime.fromisoformat(rel.last_interaction)
                days_since = (now - last).days
            except (ValueError, TypeError):
                continue

            if days_since > DECAY_THRESHOLD_DAYS and rel.affinity > 0:
                decay_days = days_since - DECAY_THRESHOLD_DAYS
                decay_amount = DECAY_RATE * decay_days
                new_affinity = max(0, rel.affinity - decay_amount)
                if new_affinity != rel.affinity:
                    rel.affinity = round(new_affinity, 4)
                    decayed += 1

        return decayed

    # ------ 用户关系三线 ------

    def get_user_relation(self, agent_id: str) -> Optional[UserRelation]:
        """获取角色与用户的关系"""
        return self._user_relations.get(agent_id)

    def update_user_relation(
        self,
        agent_id: str,
        line: Optional[str] = None,
        affinity_delta: float = 0.0,
        is_deep: bool = False,
        is_crisis: bool = False,
    ) -> Optional[Dict]:
        """
        更新角色与用户的关系

        Args:
            agent_id: 角色ID
            line: 指定关系线（None=自动判定）
            affinity_delta: 好感度变化
            is_deep: 是否深度互动
            is_crisis: 是否危机互动

        Returns:
            更新结果
        """
        ur = self._user_relations.get(agent_id)
        if not ur:
            return None

        old_affinity = ur.user_affinity

        # 更新好感度
        ur.user_affinity = max(0.0, min(1.0, old_affinity + affinity_delta))

        # 更新互动统计
        ur.total_interactions += 1
        if is_deep:
            ur.deep_interactions += 1
        if is_crisis:
            ur.crisis_interactions += 1

        # 更新关注值（与互动频率相关）
        ur.attention_score = min(1.0, ur.attention_score + 0.01)

        # 更新三线得分
        line_gain = affinity_delta * 1.5
        if line and line in VALID_RELATION_LINES:
            ur.line_scores[line] = min(1.0, ur.line_scores.get(line, 0) + line_gain)
        elif line is None:
            # 自动归线（设计：CROSS_AGENT_PHASE3_v2.md 5.5）。唯一实际调用方
            # （social_service.apply_user_relation_update）从不传 line，若这里不归线
            # 三线得分永远是 0，面板三条进度条全空。规则：
            #   日常互动 → 按角色倾向性铺量（"柳如烟90%恋人线"这类性格底色）
            #   深度互动 → 额外灌恋人线（触发条件：情感浓度高的对话）
            #   危机互动 → 额外灌守护线（触发条件：共同经历困难）
            bias = AGENT_LINE_BIAS.get(ur.agent_id, {
                LINE_LOVER: 0.33, LINE_BOND: 0.34, LINE_GUARDIAN: 0.33,
            })
            for ln in VALID_RELATION_LINES:
                ur.line_scores[ln] = min(1.0, ur.line_scores.get(ln, 0) + line_gain * bias.get(ln, 0.33))
            if is_deep:
                ur.line_scores[LINE_LOVER] = min(1.0, ur.line_scores.get(LINE_LOVER, 0) + line_gain * 0.5)
            if is_crisis:
                ur.line_scores[LINE_GUARDIAN] = min(1.0, ur.line_scores.get(LINE_GUARDIAN, 0) + line_gain * 0.5)

        # 自动判定当前线（基于得分和倾向性）
        self._auto_detect_line(ur)

        # 更新演化阶段
        self._update_stage(ur)

        # 记录历史
        ur.user_affinity_history.append({
            "delta": round(affinity_delta, 4),
            "old": round(old_affinity, 4),
            "new": round(ur.user_affinity, 4),
            "deep": is_deep,
            "crisis": is_crisis,
            "timestamp": datetime.now().isoformat(),
        })
        if len(ur.user_affinity_history) > 100:
            ur.user_affinity_history = ur.user_affinity_history[-100:]

        return {
            "agent_id": agent_id,
            "current_line": ur.current_line,
            "current_stage": ur.current_stage,
            "user_affinity": round(ur.user_affinity, 4),
            "attention_score": round(ur.attention_score, 4),
        }

    def _auto_detect_line(self, ur: UserRelation):
        """根据互动模式自动判定关系线"""
        bias = AGENT_LINE_BIAS.get(ur.agent_id, {
            LINE_LOVER: 0.33, LINE_BOND: 0.34, LINE_GUARDIAN: 0.33
        })

        # 综合得分 = 互动得分 × 倾向性权重
        scores = {}
        for line in VALID_RELATION_LINES:
            line_score = ur.line_scores.get(line, 0)
            line_bias = bias.get(line, 0.33)
            # 归一化倾向性
            scores[line] = line_score * (0.5 + line_bias)

        best_line = max(scores, key=scores.get) if any(scores.values()) else ur.current_line
        if best_line != ur.current_line and ur.total_interactions > 5:
            ur.current_line = best_line

    def _update_stage(self, ur: UserRelation):
        """更新演化阶段"""
        stages = LINE_STAGES.get(ur.current_line, ["初识"])
        # 根据好感度映射到阶段
        stage_idx = min(len(stages) - 1, int(ur.user_affinity * len(stages)))
        ur.stage_index = stage_idx
        ur.current_stage = stages[stage_idx]

    # ------ 关系影响行为 ------

    def get_message_attitude(self, receiver_id: str, sender_id: str) -> Dict:
        """
        获取角色收到跨角色消息时的态度

        基于 CROSS_AGENT_PHASE3_v2.md 5.4 节
        """
        affinity = self.get_affinity(receiver_id, sender_id)
        rel = self.get_relationship(receiver_id, sender_id)

        if affinity > 0.6:
            return {
                "attitude": "主动关心",
                "cooperation": "全力配合",
                "third_party_reaction": "替对方高兴/微酸",
                "response_probability": 0.9,
            }
        elif affinity > 0.2:
            return {
                "attitude": "正常回复",
                "cooperation": "视情况配合",
                "third_party_reaction": "好奇但不干预",
                "response_probability": 0.6,
            }
        elif affinity > -0.2:
            return {
                "attitude": "礼貌回应",
                "cooperation": "不主动",
                "third_party_reaction": "无感/观察",
                "response_probability": 0.3,
            }
        else:
            return {
                "attitude": "冷淡/阴阳",
                "cooperation": "拒绝/拆台",
                "third_party_reaction": "警惕/暗中观察",
                "response_probability": 0.15,
            }

    # ------ 技能路由（团队任务分工） ------

    def suggest_team_for_task(
        self,
        task_description: str,
        available_agents: List[str],
        max_team_size: int = 4,
    ) -> List[Dict]:
        """
        根据技能匹配和关系网络推荐团队组合

        优先级：
        1. 技能匹配度
        2. 团队内关系和谐度（避免把对头放一起）
        3. 经验（过往协作成功次数）
        """
        if not available_agents:
            return []

        # 计算团队内关系和谐度
        def team_harmony(team: List[str]) -> float:
            if len(team) <= 1:
                return 1.0
            total = 0
            count = 0
            for a, b in combinations(team, 2):
                aff = self.get_affinity(a, b)
                total += aff
                count += 1
            return total / count if count > 0 else 0

        # 计算协作经验
        def team_experience(team: List[str]) -> int:
            total = 0
            for a, b in combinations(team, 2):
                rel = self.get_relationship(a, b)
                if rel:
                    total += rel.task_collab_success
            return total

        # 简单贪心：先选关系最好的组合
        candidates = []
        for size in range(2, min(max_team_size + 1, len(available_agents) + 1)):
            for team in combinations(available_agents, size):
                harmony = team_harmony(list(team))
                experience = team_experience(list(team))
                score = harmony * 0.6 + (experience / max(experience + 1, 1)) * 0.4
                candidates.append({
                    "team": list(team),
                    "harmony": round(harmony, 3),
                    "experience": experience,
                    "score": round(score, 3),
                })

        # 按分数降序
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:5]

    # ------ 持久化 ------

    def save_to_disk(self):
        """持久化关系到磁盘"""
        if not self._data_dir:
            return

        graph_dir = self._data_dir / "social_graph"
        graph_dir.mkdir(parents=True, exist_ok=True)

        # 保存角色间关系
        rel_data = {key: rel.to_dict() for key, rel in self._relationships.items()}
        rel_path = graph_dir / "relationships.json"
        with open(rel_path, "w", encoding="utf-8") as f:
            json.dump(rel_data, f, ensure_ascii=False, indent=2)

        # 保存用户关系
        user_data = {aid: ur.to_dict() for aid, ur in self._user_relations.items()}
        user_path = graph_dir / "user_relations.json"
        with open(user_path, "w", encoding="utf-8") as f:
            json.dump(user_data, f, ensure_ascii=False, indent=2)

    def load_from_disk(self):
        """从磁盘恢复关系"""
        if not self._data_dir:
            return

        graph_dir = self._data_dir / "social_graph"
        if not graph_dir.exists():
            return

        # 恢复角色间关系
        rel_path = graph_dir / "relationships.json"
        if rel_path.exists():
            try:
                with open(rel_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for key, rel_dict in data.items():
                    self._relationships[key] = Relationship.from_dict(rel_dict)
            except Exception as e:
                logger.error("[SocialGraph] 加载关系数据失败: %s", e)

        # 恢复用户关系
        user_path = graph_dir / "user_relations.json"
        if user_path.exists():
            try:
                with open(user_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for aid, ur_dict in data.items():
                    self._user_relations[aid] = UserRelation.from_dict(ur_dict)
            except Exception as e:
                logger.error("[SocialGraph] 加载用户关系失败: %s", e)

    # ------ 统计 ------

    def get_stats(self) -> Dict:
        """获取社交图统计"""
        affinity_dist = {"high": 0, "medium": 0, "low": 0, "negative": 0}
        for rel in self._relationships.values():
            if rel.affinity > 0.6:
                affinity_dist["high"] += 1
            elif rel.affinity > 0.2:
                affinity_dist["medium"] += 1
            elif rel.affinity > -0.2:
                affinity_dist["low"] += 1
            else:
                affinity_dist["negative"] += 1

        return {
            "total_relationships": len(self._relationships),
            "total_user_relations": len(self._user_relations),
            "affinity_distribution": affinity_dist,
            "top_bonds": sorted(
                [(f"{rel.agent_a}-{rel.agent_b}", rel.affinity, rel.bond_depth)
                 for rel in self._relationships.values()],
                key=lambda x: x[1], reverse=True
            )[:5],
            "agent_ids": list(self._agent_ids),
        }

    # ------ Prompt 注入 ------

    def build_relation_prompt(self, agent_id: str) -> str:
        """构建社交关系 prompt，注入角色的 system prompt 中

        让角色知道：
        1. 自己认识谁，关系是什么
        2. 绝对不能因为读到其他角色的文件就变成别人
        3. 身份锚定：我是我，不是别人

        实例自包含原则（2026-05-21 解耦改造）：
        实例创建时已通过 snapshot_from_template 把社交关系复制到自己的 id 名下，
        runtime 不再回查模板——模板删除/改名都不影响已有实例。
        """
        # 获取角色的关系（实例创建时已快照，直接查）
        rels = self.get_relationships_summary(agent_id)

        if not rels:
            return ""

        # 获取角色自己的名字
        agent_name = agent_id
        try:
            from .agent_config import get_agent_manager
            am = get_agent_manager()
            info = am.get_agent(agent_id)
            if info:
                agent_name = info.get("name", agent_id)
        except Exception:
            pass

        # 获取用户关系（实例创建时已通过 snapshot_from_template 复制到自己 id 名下）
        user_rel = self.get_user_relation(agent_id)

        lines = [f"## 你认识的人（你是{agent_name}，绝不因读到他人的文件而改变身份）\n"]
        lines.append("> 你有自己独立的人格和身份。如果你在文件中读到其他角色的设定，那是**她们**的故事，不是你的。")
        lines.append("> 你不会因为读了别人的 GENOME 或 IDENTITY 就变成那个人。你是你。\n")

        # 构建 ID→名字 的映射
        def _get_name(aid: str) -> str:
            try:
                from .agent_config import get_agent_manager
                am = get_agent_manager()
                # 先查实例
                info = am.get_agent(aid)
                if info:
                    return info.get("name", aid)
                # 再查模板
                for tmpl in am.list_templates():
                    if tmpl.get("template_id") == aid:
                        return tmpl.get("name", aid)
            except Exception:
                pass
            return aid

        # 角色间关系
        has_rels = False
        for r in rels:
            # 兼容 Relationship 对象和 Dict
            if isinstance(r, dict):
                other_id = r.get("agent_id", "")
                bond_types = r.get("bond_types", [])
                affinity = r.get("affinity", 0)
                label = r.get("current_label", "陌生")
                what_you_know = []
                # dict 模式下没有 direction-aware knowledge，跳过
            else:
                other_id = r.agent_b if r.agent_a == agent_id else r.agent_a
                bond_types = r.bond_types
                affinity = r.affinity
                label = r.current_label
                what_you_know = r.a_knows_about_b if r.agent_a == agent_id else r.b_knows_about_a
            other_name = _get_name(other_id)
            what_you_know_str = "、".join(what_you_know) if what_you_know else ""

            # 跳过无关系的
            if affinity == 0 and not bond_types and label in ("陌生", "路⼈", "路人"):
                continue

            has_rels = True
            rel_desc = f"- **{other_name}**：{label}（好感{affinity:.1f}）"
            if bond_types:
                rel_desc += f"，关系类型：{'、'.join(bond_types)}"
            if what_you_know_str:
                rel_desc += f"。你对她的了解：{what_you_know_str}"
            lines.append(rel_desc)

        if not has_rels:
            lines.append("- （暂无深入了解的其他角色）")

        # 用户关系
        if user_rel:
            line_label = {"恋人线": "恋人", "羁绊线": "羁绊", "守护线": "守护"}.get(
                user_rel.current_line, user_rel.current_line
            )
            stage = user_rel.current_stage or "初识"
            ua = user_rel.user_affinity or 0
            attention = user_rel.attention_score or 0.5
            lines.append(f"\n- **老板（用户）**：{line_label}·{stage}（好感{ua:.1f}，关注度{attention:.1f}）")

        lines.append("\n> 记住：你的身份由你的 GENOME、IDENTITY、SOUL 定义，不是由你读到的任何其他文件定义。")

        return "\n".join(lines)


# ====== 便捷函数 ======

_graph_instance: Optional[SocialGraph] = None


def get_social_graph(data_dir: Optional[str] = None) -> SocialGraph:
    """获取全局社交图实例"""
    global _graph_instance
    if _graph_instance is None:
        _graph_instance = SocialGraph(data_dir)
    return _graph_instance


def reset_social_graph():
    """重置社交图（测试用）"""
    global _graph_instance
    _graph_instance = None
