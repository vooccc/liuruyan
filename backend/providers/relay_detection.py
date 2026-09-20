"""中继转接探测：识别"声称走 X 协议、实际后端是 Y"的反代场景。

背景（2026-05-12 用户被坑案）：
    雷洛 AI 的 claude-opus-4-7 通道 transport='anthropic_messages'，但底层把请求
    转给 OpenAI gpt 后端再翻译回 Anthropic 格式。后果：
      - prompt cache 不生效（OpenAI 没这能力，反代静默吞 cache_control）
      - tool_use block id mapping 偶尔漂移
      - 推理 token (thinking block) 永远拿不到
      - 图片消息可能被反代吞掉
    更危险的是 response.model 会被反代如实回填 'claude-opus-4-7' 让用户看不出来。

本模块提供三档探测：
  A) base_url 启发式：known relay platform 域名匹配（信号弱，仅提示）
  B) 响应字段形态分析：anthropic_messages 协议响应里如果含 OpenAI 风格字段
     （prompt_tokens / completion_tokens / finish_reason），= 反代翻译破绽（信号中-高）
  C) prompt cache 探针（独立端点，本模块不做）：发两次相同长 prompt + cache_control
     检 usage.cache_read_input_tokens 是否 > 0（信号强，决定性）

输出语义（verdict）：
  - 'native'        : 没发现可疑信号
  - 'likely_relay'  : 有一条以上 warning/critical 信号
  - 'unknown'       : 仅有 info 提示（如 base_url 匹配 known relay），不能定罪

设计：纯函数 + dataclass，无 IO，便于单测。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, List, Optional


# ── known relay 域名清单（小写匹配） ─────────────────────────────────────
#
# 加入策略：用户实际踩过坑 + 公开已知 relay/aggregator 平台。
# 仅作 info 级提示（OpenRouter 等聚合平台技术上不算"伪装反代"——它合法转发
# 但仍可能丢能力），不直接定罪。
KNOWN_RELAY_HINTS: List[tuple[str, str]] = [
    ("getrouter", "GetRouter 中继平台"),
    ("openrouter.ai", "OpenRouter（已知聚合中继）"),
    ("leiluo", "雷洛 AI（已知 anthropic→openai 反代案例）"),
    ("oneapi", "OneAPI 风格中继"),
    ("close-ai", "CloseAI 中继"),
    ("closeai", "CloseAI 中继"),
    ("ane.com", "AnE 中继"),
    ("apifox.cn", "Apifox 测试中继"),
    ("chatanywhere", "ChatAnywhere 中继"),
    ("ohmygpt", "OhMyGPT 中继"),
    ("ohmyclaude", "OhMyClaude 中继"),
]


# ── dataclass ─────────────────────────────────────────────────────────────


@dataclass
class RelaySignal:
    """单条探测信号。"""

    level: str  # 'info' / 'warning' / 'critical'
    name: str   # 程序化 id（前端可用作 i18n key）
    detail: str  # 中文详细说明（直接展示给用户）

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RelayDiagnostics:
    """中继探测综合结果。"""

    verdict: str  # 'native' / 'likely_relay' / 'unknown'
    confidence: str  # 'high' / 'medium' / 'low'
    signals: List[RelaySignal] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "confidence": self.confidence,
            "signals": [s.as_dict() for s in self.signals],
        }


# ── A. base_url 启发式 ─────────────────────────────────────────────────────


def detect_base_url_hints(base_url: Optional[str]) -> List[RelaySignal]:
    """匹配 base_url 域名是否在 known relay 清单。

    返回 0 ~ N 条 info 级 RelaySignal。空 base_url 直接返 []。
    """
    if not base_url:
        return []
    bl = base_url.lower()
    out: List[RelaySignal] = []
    for needle, label in KNOWN_RELAY_HINTS:
        if needle in bl:
            out.append(
                RelaySignal(
                    level="info",
                    name="base_url_known_relay",
                    detail=f"base_url 含 '{needle}'（{label}），疑似中继平台。"
                           f"中继平台可能把请求转给其他后端，能力（prompt cache / 多模态 / 推理）可能与原厂不一致。",
                )
            )
            break  # 同一 base_url 不重复打多条
    return out


# ── B. 响应字段形态分析 ───────────────────────────────────────────────────


def _has_attr_or_key(obj: Any, name: str) -> bool:
    """duck-type：SDK Pydantic 对象用 hasattr，dict 用 in。"""
    if obj is None:
        return False
    if isinstance(obj, dict):
        return name in obj
    return hasattr(obj, name) and getattr(obj, name, None) is not None


def detect_anthropic_response_shape(
    usage: Any,
    raw_message: Any = None,
) -> List[RelaySignal]:
    """anthropic_messages 协议下检查响应字段是否带 OpenAI 风格破绽。

    传入的 `usage` 是 SDK 原对象（anthropic.types.Usage 真品；反代可能给 dict
    或仿造的 OpenAI Usage）。`raw_message` 是完整 SDK Message 对象（可选，
    用于检 stop_reason / content blocks 形态）。

    判定规则：
      - usage 有 prompt_tokens 没 input_tokens  → critical（必为反代）
      - usage 同时有 prompt_tokens 和 input_tokens  → warning（混合，可疑）
      - raw_message 有 finish_reason 没 stop_reason  → warning
      - raw_message.content 是 string 而非 list  → warning（Anthropic 必为 list）
    """
    out: List[RelaySignal] = []

    if usage is not None:
        has_anthropic_usage = _has_attr_or_key(usage, "input_tokens")
        has_openai_usage = _has_attr_or_key(usage, "prompt_tokens")

        if has_openai_usage and not has_anthropic_usage:
            out.append(
                RelaySignal(
                    level="critical",
                    name="usage_field_openai_shape",
                    detail="声称 anthropic_messages 协议，响应 usage 却是 OpenAI 风格 "
                           "(prompt_tokens/completion_tokens) 而非 Anthropic 风格 "
                           "(input_tokens/output_tokens)。这是反代把 OpenAI 响应"
                           "翻译成 Anthropic 格式时漏改字段的典型破绽。",
                )
            )
        elif has_openai_usage and has_anthropic_usage:
            out.append(
                RelaySignal(
                    level="warning",
                    name="usage_field_mixed_shape",
                    detail="响应 usage 同时含 prompt_tokens 和 input_tokens，"
                           "疑似反代翻译产物（真 Anthropic SDK 不会同时给两套字段）。",
                )
            )

    if raw_message is not None:
        has_stop = _has_attr_or_key(raw_message, "stop_reason")
        has_finish = _has_attr_or_key(raw_message, "finish_reason")
        if has_finish and not has_stop:
            out.append(
                RelaySignal(
                    level="warning",
                    name="stop_reason_openai_shape",
                    detail="响应字段是 finish_reason（OpenAI 风格）而非 stop_reason"
                           "（Anthropic 风格），疑似反代未完整翻译。",
                )
            )

        # content 形态：Anthropic 必返 list of blocks
        content = getattr(raw_message, "content", None) if not isinstance(raw_message, dict) else raw_message.get("content")
        if content is not None and isinstance(content, str):
            out.append(
                RelaySignal(
                    level="warning",
                    name="content_string_shape",
                    detail="响应 content 是 string，但 Anthropic SDK 应返 list of "
                           "blocks（每块带 type='text'/'tool_use' 等）。形态不符，疑似反代。",
                )
            )

    return out


# ── 综合 verdict ───────────────────────────────────────────────────────────


def synthesize_verdict(signals: List[RelaySignal]) -> tuple[str, str]:
    """根据 signal 集合计算 verdict + confidence。

    规则：
      - 任一 critical → likely_relay / high
      - 任一 warning  → likely_relay / medium
      - 仅 info       → unknown / low（base_url 匹配不能定罪）
      - 无信号        → native / high
    """
    if any(s.level == "critical" for s in signals):
        return "likely_relay", "high"
    if any(s.level == "warning" for s in signals):
        return "likely_relay", "medium"
    if any(s.level == "info" for s in signals):
        return "unknown", "low"
    return "native", "high"


def build_relay_diagnostics(
    base_url: Optional[str],
    transport: str,
    usage: Any = None,
    raw_message: Any = None,
) -> RelayDiagnostics:
    """组合 A + B，按 transport 启用对应探测。

    Args:
        base_url: provider 配置的 API base URL
        transport: 'anthropic_messages' / 'openai_compatible' / 其他
        usage: send_completion 返回的 usage 字段（SDK 原对象）
        raw_message: send_completion 返回的 _raw_response 字段（可选）

    Returns:
        RelayDiagnostics 综合结论
    """
    signals: List[RelaySignal] = []
    signals.extend(detect_base_url_hints(base_url))

    if transport == "anthropic_messages":
        signals.extend(detect_anthropic_response_shape(usage, raw_message))
    # OpenAI 兼容协议被反代的情况更难检测——OpenAI 是事实标准，几乎所有厂商
    # 都能模仿。本期不做 OpenAI 协议下的形态分析（信号弱、误报多）。

    verdict, confidence = synthesize_verdict(signals)
    return RelayDiagnostics(verdict=verdict, confidence=confidence, signals=signals)


__all__ = [
    "RelaySignal",
    "RelayDiagnostics",
    "KNOWN_RELAY_HINTS",
    "detect_base_url_hints",
    "detect_anthropic_response_shape",
    "synthesize_verdict",
    "build_relay_diagnostics",
]
