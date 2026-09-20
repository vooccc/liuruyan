"""
情感更新器 v2 — 语境感知 + 角色光谱 + 情感弧线

v1 问题：
- 纯关键词匹配，"不担心"和"担心"得分一样
- 所有角色情感反应相同，韩清雪和楚夜澜对同一句话反应一模一样
- 情感是孤立的点，没有连续性

v2 改造：
- Level 1: 语境感知（否定词、强度修饰、主语方向）
- Level 2: 角色光谱（spectrum 7维度影响情感生成权重）
- Level 3: 情感弧线（连续触发递增、短期/中期/长期弧线）
"""
import json
import re
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timedelta

from .agent_config import get_agent_manager, AgentConfig


# ===== 基础关键词映射 =====
EMOTION_KEYWORDS = {
    "开心": ["谢谢", "太好了", "棒", "厉害", "优秀", "哈哈", "开心", "喜欢", "爱", "赞", "不错", "牛", "👍", "😄", "哈哈"],
    "感动": ["感动", "温暖", "心疼", "谢谢你", "辛苦了", "默默付出", "不离不弃", "懂我", "真好"],
    "思念": ["想", "想念", "好久", "回来", "等待", "盼", "牵挂", "不在", "多久"],
    "担忧": ["担心", "害怕", "焦虑", "急", "问题", "出错了", "失败", "报错", "错误", "bug"],
    "委屈": ["委屈", "不公平", "为什么不", "误解", "冤枉", "不被理解", "明明"],
    "兴奋": ["激动", "期待", "终于", "成功了", "搞定", "完成", "上线", "发布"],
    "傲娇": ["哼", "才不是", "才没有", "少来", "自恋", "不要脸", "谁稀罕"],
    "专注": ["分析", "思考", "研究", "代码", "实现", "方案", "策略", "架构"],
    "疲惫": ["累", "困", "休息", "停一下", "明天", "算了", "不想动"],
}

# 否定词列表
NEGATION_WORDS = {"不", "没", "别", "不要", "不是", "没法", "无法", "不能", "不会"}

# 强度修饰符
INTENSITY_MODIFIERS = {
    # 增强
    "超级": 1.5, "非常": 1.4, "特别": 1.35, "太": 1.3, "真": 1.25,
    "真的": 1.25, "好": 1.2, "多么": 1.3, "简直": 1.4, "极度": 1.6,
    # 减弱
    "有点": 0.7, "稍微": 0.65, "还算": 0.75, "一般": 0.6, "勉强": 0.5,
}

# 光谱维度到情感的映射（该维度高时，哪些情感更容易被放大）
SPECTRUM_EMOTION_MAP = {
    "warmth": {       # 温度高 → 更容易开心/感动
        "boost": ["开心", "感动"],
        "suppress": ["傲娇", "专注"],
    },
    "vulnerability": {  # 脆弱性高 → 更容易委屈/担忧/思念
        "boost": ["委屈", "担忧", "思念"],
        "suppress": ["傲娇", "兴奋"],
    },
    "independence": {  # 独立性高 → 更容易傲娇/专注
        "boost": ["傲娇", "专注"],
        "suppress": ["委屈", "依赖"],  # "依赖"不是标准情感，但不影响
    },
    "expressiveness": {  # 表达力高 → 所有情感强度都更高
        "boost": [],  # 空表示全局增强
        "suppress": [],
        "global_boost": True,
    },
    "charm": {         # 魅力值高 → 开心/感动更有层次
        "boost": ["开心", "感动", "思念"],
        "suppress": [],
    },
    "intelligence": {   # 智慧高 → 更容易进入专注态
        "boost": ["专注", "兴奋"],
        "suppress": ["委屈"],
    },
    "loyalty": {        # 忠诚度高 → 担忧/感动更强烈
        "boost": ["感动", "担忧", "思念"],
        "suppress": ["傲娇"],
    },
}


def _detect_negation(text: str, keyword_pos: int) -> bool:
    """检测关键词附近是否有否定词
    
    检查关键词前后 4 个字符范围内是否存在否定词
    """
    start = max(0, keyword_pos - 4)
    end = min(len(text), keyword_pos + len(text[keyword_pos:].split()[0]) + 4) if keyword_pos < len(text) else keyword_pos + 4
    nearby = text[start:end]
    
    for neg_word in NEGATION_WORDS:
        if neg_word in nearby:
            return True
    return False


def _detect_intensity_modifier(text: str, keyword_pos: int) -> float:
    """检测关键词附近的强度修饰符"""
    start = max(0, keyword_pos - 6)
    end = min(len(text), keyword_pos + 8)
    nearby = text[start:end]
    
    modifier = 1.0
    for word, factor in INTENSITY_MODIFIERS.items():
        if word in nearby:
            modifier *= factor
    return modifier


def _detect_subject_direction(user_msg: str, asst_msg: str) -> str:
    """判断情感方向：关于用户还是关于助手
    
    Returns:
        "user" | "assistant" | "mutual" | "neutral"
    """
    # 简单启发式规则
    user_lower = user_msg.lower()
    
    # 用户说自己怎么样
    user_self_patterns = ["我", "我们", "咱们", "老板", "我自己"]
    # 关于助手
    asst_patterns = ["你", "你们", "你的", "你"]

    has_user_self = any(p in user_lower for p in user_self_patterns)
    has_asst = any(p in user_lower for p in asst_patterns)
    
    if has_user_self and has_asst:
        return "mutual"
    elif has_user_self:
        return "user"
    elif has_asst:
        return "assistant"
    return "neutral"


class EmotionUpdaterV2:
    """根据对话内容更新智能体情感状态（v2: 语境感知 + 角色光谱 + 弧线）"""

    def __init__(self):
        self.agent_manager = get_agent_manager()

    def _load_spectrum(self, agent_id: str) -> Dict[str, float]:
        """加载角色的天赋光谱（2026-05-21 解耦改造：只读实例自己的 meta.json）。

        历史背景：原来有一段"实例 meta 不存在则回落 templates_dir/<template_id>/meta.json"的 fallback。
        现在 create_from_template 已经把模板的 meta.json 复制到实例目录，spectrum 字段一并继承——
        runtime 不再回查模板，模板删除/改名都不影响实例的天赋光谱。
        """
        config = self.agent_manager.get_agent_config(agent_id)
        if not config:
            return {}

        try:
            meta_path = config.agent_dir / "meta.json"
            if meta_path.exists():
                content = meta_path.read_text(encoding="utf-8-sig")
                meta = json.loads(content)
                return meta.get("spectrum", {})
        except Exception:
            pass

        return {}

    def analyze_emotion(self, user_message: str, assistant_response: str,
                        agent_id: str = None) -> Optional[Dict[str, Any]]:
        """分析对话内容，推断情感变化（v2: 语境感知）

        Args:
            user_message: 用户消息
            assistant_response: 助手回复
            agent_id: 智能体ID（用于加载角色光谱）

        Returns:
            情感更新信息，或 None（无变化）
        """
        combined = f"{user_message} {assistant_response}"
        combined_lower = combined.lower()

        # 加载角色光谱
        spectrum = self._load_spectrum(agent_id) if agent_id else {}

        # ===== Step 1: 基础关键词匹配 + 语境过滤 =====
        raw_scores: Dict[str, float] = {}
        context_info: List[str] = []

        for emotion, keywords in EMOTION_KEYWORDS.items():
            score = 0.0
            matched_keywords = []
            
            for keyword in keywords:
                pos = combined_lower.find(keyword)
                if pos == -1:
                    continue
                
                # 语境否定检测
                if _detect_negation(combined_lower, pos):
                    context_info.append(f"否定: 「{keyword}」被附近否定词抵消")
                    continue
                
                # 强度修饰
                intensity_mod = _detect_intensity_modifier(combined_lower, pos)
                
                # 基础分 + 修饰
                kw_score = 1.0 * intensity_mod
                score += kw_score
                matched_keywords.append(keyword)

            if score > 0:
                raw_scores[emotion] = score

        if not raw_scores:
            return {
                "emotion": "平静",
                "intensity": 0.3,
                "reason": "对话内容平静",
                "context_level": 1,
            }

        # ===== Step 2: 角色光谱加权 =====
        weighted_scores = {}
        spectrum_info = []

        for emotion, base_score in raw_scores.items():
            final_score = base_score
            
            for dim, value in spectrum.items():
                if dim not in SPECTRUM_EMOTION_MAP:
                    continue
                
                mapping = SPECTRUM_EMOTION_MAP[dim]
                
                # 全局增强（如 expressiveness）
                if mapping.get("global_boost"):
                    # 高于平均值则增强，低于则减弱
                    final_score *= (0.8 + 0.4 * value)
                
                # 特定情感增强/抑制
                if emotion in mapping["boost"]:
                    boost_factor = 1.0 + (value - 0.5) * 0.6  # 0.7~1.6
                    final_score *= boost_factor
                    spectrum_info.append(f"{dim}({value:.1f})×{emotion} ×{boost_factor:.2f}")
                
                elif emotion in mapping["suppress"]:
                    suppress_factor = 1.0 - (value - 0.5) * 0.4  # 0.8~1.2
                    final_score *= suppress_factor
                    spectrum_info.append(f"{dim}({value:.1f})÷{emotion} ×{suppress_factor:.2f}")

            weighted_scores[emotion] = round(final_score, 2)

        # 选择加权后最高的情感
        best_emotion = max(weighted_scores, key=weighted_scores.get)
        max_score = weighted_scores[best_emotion]

        # 计算强度（0.1 ~ 1.0）
        intensity = min(1.0, 0.25 + max_score * 0.12)

        # 方向检测
        direction = _detect_subject_direction(user_message, assistant_response)

        reason_parts = [f"关键词匹配: {best_emotion}"]
        if spectrum_info:
            reason_parts.append(f"光谱调整: {'; '.join(spectrum_info[:3])}")
        if direction != "neutral":
            reason_parts.append(f"方向: {direction}")
        
        result = {
            "emotion": best_emotion,
            "intensity": round(intensity, 2),
            "reason": " | ".join(reason_parts),
            "context_level": 2 if spectrum else 1,
            "raw_scores": raw_scores,
            "weighted_scores": weighted_scores,
            "direction": direction,
        }

        return result

    def update_emotion(self, agent_id: str, user_message: str,
                       assistant_response: str,
                       current_emotion: Dict = None) -> Optional[Dict[str, Any]]:
        """计算情感状态更新（v2: 带弧线追踪）

        v3 改造：不再自行持久化，由调用方决定写入位置（Session级别）
        
        Args:
            agent_id: 智能体 ID
            user_message: 用户消息
            assistant_response: 助手回复
            current_emotion: 当前情感状态（由调用方传入，不再从文件读取）

        Returns:
            更新后的完整情感状态（未持久化，由调用方负责写入）
        """
        # 分析情感
        emotion_update = self.analyze_emotion(user_message, assistant_response, agent_id)
        if not emotion_update:
            return None

        # 加载当前情感：优先使用调用方传入的状态，回退到文件读取
        if current_emotion is None:
            config = self.agent_manager.get_agent_config(agent_id)
            if not config:
                return None
            current_emotion = config.load_emotion()

        new_emotion_name = emotion_update["emotion"]
        new_intensity = emotion_update["intensity"]

        current_name = current_emotion.get("current", "平静")
        current_intensity = current_emotion.get("intensity", 0.5)

        # ===== 弧线逻辑 =====
        arc = current_emotion.get("arc", {})
        consecutive_count = arc.get("consecutive_count", 0)
        arc_history = arc.get("history", [])  # 最近N次情感记录

        if new_emotion_name == current_name:
            # 同情感连续触发 → 弧线递增
            consecutive_count += 1
            arc_bonus = min(0.3, consecutive_count * 0.05)  # 最多+0.3
            final_intensity = min(1.0, new_intensity + arc_bonus)
            
            arc_state = "rising" if consecutive_count >= 3 else "stable"
        else:
            # 情感切换 → 弧线断裂，但保留部分连续性
            if consecutive_count >= 3:
                # 连续3次以上才叫"弧线"，断开时平滑过渡
                final_intensity = new_intensity * 0.7 + current_intensity * 0.3
            else:
                final_intensity = new_intensity * 0.85 + current_intensity * 0.15
            
            consecutive_count = 0
            arc_state = "transitioning"

        # 更新弧线历史（最近10次）
        arc_history.append({
            "emotion": new_emotion_name,
            "intensity": round(final_intensity, 2),
            "timestamp": datetime.now().isoformat(),
        })
        if len(arc_history) > 10:
            arc_history = arc_history[-10:]

        # 记录历史（保留最近 20 条）
        history = current_emotion.get("history", [])
        history.append({
            "emotion": current_name,
            "intensity": round(current_intensity, 2),
            "timestamp": datetime.now().isoformat(),
        })
        if len(history) > 20:
            history = history[-20:]

        # 构建新的弧线数据
        new_arc = {
            "state": arc_state,
            "consecutive_count": consecutive_count,
            "history": arc_history,
            "peak_emotion": self._find_peak_emotion(arc_history),
        }

        # 更新状态
        updated_emotion = {
            "current": new_emotion_name,
            "intensity": round(final_intensity, 2),
            "direction": emotion_update.get("direction", "neutral"),
            "context_level": emotion_update.get("context_level", 1),
            "arc": new_arc,
            "history": history,
            "updated_at": datetime.now().isoformat(),
        }

        return updated_emotion

    def _find_peak_emotion(self, arc_history: List[Dict]) -> Dict:
        """从弧线历史中找到峰值情感"""
        if not arc_history:
            return {"emotion": "平静", "intensity": 0, "count": 0}
        
        # 统计最近弧线中最频繁的高强度情感
        emotion_totals = {}
        for entry in arc_history:
            e = entry["emotion"]
            if e not in emotion_totals:
                emotion_totals[e] = {"total": 0, "count": 0}
            emotion_totals[e]["total"] += entry["intensity"]
            emotion_totals[e]["count"] += 1
        
        peak = max(emotion_totals.keys(), key=lambda e: emotion_totals[e]["total"])
        peak_data = emotion_totals[peak]
        
        return {
            "emotion": peak,
            "avg_intensity": round(peak_data["total"] / max(peak_data["count"], 1), 2),
            "count": peak_data["count"],
        }


# 全局实例
_emotion_updater: Optional[EmotionUpdaterV2] = None


def get_emotion_updater() -> EmotionUpdaterV2:
    """获取情感更新器单例"""
    global _emotion_updater
    if _emotion_updater is None:
        _emotion_updater = EmotionUpdaterV2()
    return _emotion_updater
