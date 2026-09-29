import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional


def _read_int_env(*names: str, default: int) -> int:
    for name in names:
        raw = os.getenv(name)
        if raw is None or raw == "":
            continue
        try:
            return max(1, int(raw))
        except ValueError:
            continue
    return default


def _resolve_int(file_value, *env_names: str, default: int) -> int:
    if isinstance(file_value, (int, float)) and file_value == file_value:
        try:
            return max(1, int(file_value))
        except (TypeError, ValueError):
            pass
    return _read_int_env(*env_names, default=default)


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()


@dataclass
class ToolLoopStop:
    reason: str
    message: str
    payload: Optional[Dict[str, Any]] = None


class ToolLoopGuard:
    """Protects against runaway tool loops without forcing a tiny fixed budget."""

    def __init__(self, max_rounds: int, repeated_identical_rounds: int):
        self.max_rounds = max_rounds
        self.repeated_identical_rounds = repeated_identical_rounds
        self._last_round_fingerprint: Optional[str] = None
        self._identical_round_streak = 0

    @classmethod
    def from_env(cls, provider: str) -> "ToolLoopGuard":
        # 三层 fallback：runtime_budget.json (max_tool_rounds 字段) → env → 默认。
        # 与 AggregateRuntimeBudget.from_env 共享同一份 runtime_budget.json，让前端
        # 一处调整就同时影响 budget 总数限和 guard 轮数限（语义不同但用户视角一致）。
        from .runtime_budget_config import load_runtime_budget_config
        file_cfg = load_runtime_budget_config()

        prefix = provider.upper()
        max_rounds = _resolve_int(
            file_cfg.get("max_tool_rounds"),
            f"{prefix}_MAX_TOOL_ROUNDS",
            "LLM_MAX_TOOL_ROUNDS",
            default=120,
        )
        repeated_identical_rounds = _read_int_env(
            f"{prefix}_MAX_IDENTICAL_TOOL_ROUNDS",
            "LLM_MAX_IDENTICAL_TOOL_ROUNDS",
            default=2,
        )
        return cls(
            max_rounds=max_rounds,
            repeated_identical_rounds=repeated_identical_rounds,
        )

    def observe_round(
        self,
        tool_calls: Iterable[Dict[str, Any]],
        tool_results: Iterable[str],
    ) -> Optional[ToolLoopStop]:
        tool_calls_list = list(tool_calls)
        # tool_results 仍接受（保持调用方签名不变），但不再进指纹。
        # 原因：部分工具结果带动态字段（e.g. spawn_subagent 的 elapsed），
        # 结果 hash 参与指纹会让同意图重复调用的 streak 永远无法累积。
        # 判定"模型意图是否重复"改为只看调用参数本身；结果没变就不该再发
        # 同样的请求 —— LLM 若这么做，正是 LoopGuard 该阻断的行为。
        _ = tool_results
        fingerprint = self._build_round_fingerprint(tool_calls_list)

        if fingerprint == self._last_round_fingerprint:
            self._identical_round_streak += 1
        else:
            self._identical_round_streak = 1
            self._last_round_fingerprint = fingerprint

        if self._identical_round_streak > self.repeated_identical_rounds:
            return ToolLoopStop(
                reason="repeated_identical_rounds",
                message=(
                    "系统检测到模型连续多轮重复调用同一组工具，且结果没有变化。"
                    "不要再继续调用工具，请基于当前结果直接回答用户；如果信息仍不足，请明确说明缺口。"
                ),
            )
        return None

    def max_rounds_stop(self) -> ToolLoopStop:
        return ToolLoopStop(
            reason="max_tool_rounds",
            message=(
                f"本轮对话已经达到工具调用轮次预算上限（{self.max_rounds} 轮）。"
                "不要再继续调用工具，请基于当前信息直接回答用户；如果任务仍未完成，请明确说明剩余部分。"
            ),
        )

    def _build_round_fingerprint(
        self,
        tool_calls: List[Dict[str, Any]],
    ) -> str:
        normalized_calls = [
            {
                "name": tool_call.get("name", ""),
                "arguments": tool_call.get("arguments", {}),
            }
            for tool_call in tool_calls
        ]
        return _hash_text(_stable_json({"tool_calls": normalized_calls}))


def build_tool_loop_user_message(stop: ToolLoopStop) -> str:
    return (
        f"[系统] 工具循环保护已触发：{stop.reason}\n"
        f"{stop.message}\n\n"
        "请直接面向用户给出最终答复，不要再请求任何工具。"
    )


def build_tool_loop_notice_event(stop: ToolLoopStop) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "type": "system_notice",
        "kind": stop.reason,
        "content": stop.message,
    }
    if stop.payload is not None:
        event["payload"] = stop.payload
    return event
