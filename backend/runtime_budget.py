import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from .tool_loop_guard import ToolLoopStop


AGGREGATE_BUDGET_WARNING_KIND = "aggregate_budget_warning"
AGGREGATE_BUDGET_EXHAUSTION_KINDS = frozenset(
    {
        "aggregate_wall_clock_exhausted",
        "aggregate_tool_calls_exhausted",
        "aggregate_tool_output_exhausted",
    }
)
AGGREGATE_BUDGET_NOTICE_KINDS = frozenset(
    {AGGREGATE_BUDGET_WARNING_KIND, *AGGREGATE_BUDGET_EXHAUSTION_KINDS}
)


def is_runtime_budget_warning_kind(kind: str) -> bool:
    return (kind or "").strip() == AGGREGATE_BUDGET_WARNING_KIND


def is_runtime_budget_exhaustion_kind(kind: str) -> bool:
    return (kind or "").strip() in AGGREGATE_BUDGET_EXHAUSTION_KINDS


def is_runtime_budget_notice_kind(kind: str) -> bool:
    return (kind or "").strip() in AGGREGATE_BUDGET_NOTICE_KINDS


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


def _read_float_env(*names: str, default: float) -> float:
    for name in names:
        raw = os.getenv(name)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
            return min(0.98, max(0.5, value))
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


def _resolve_float(file_value, *env_names: str, default: float) -> float:
    if isinstance(file_value, (int, float)) and file_value == file_value:
        try:
            return min(0.98, max(0.5, float(file_value)))
        except (TypeError, ValueError):
            pass
    return _read_float_env(*env_names, default=default)


def get_effective_stream_idle_timeout(provider: str = "openai") -> float:
    """单次流式调用 idle timeout（秒）。

    跟 max_output_tokens 同一套三层 fallback：
        runtime_budget.json (stream_idle_timeout_ms) → env ({PROVIDER}_STREAM_IDLE_TIMEOUT_MS
        / LLM_STREAM_IDLE_TIMEOUT_MS) → 默认 600000 ms (600s)

    用于 _chat_one_round / _chat_stream 的 ev 间心跳判定：超过此秒数没收到任何
    SDK ParsedEvent → 视为 provider 挂起 → 抛 LLMError(retryable=True)。

    长思考模型 + 大 max_tokens 场景下，provider 端 thinking 阶段可能很久不发
    chunk，原 180s 会误判挂起，默认已放宽到 600s；思考更久的模型可在 dialog 再调高。
    """
    from .runtime_budget_config import load_runtime_budget_config
    file_cfg = load_runtime_budget_config()
    prefix = provider.upper()
    file_val = file_cfg.get("stream_idle_timeout_ms")
    if isinstance(file_val, (int, float)) and file_val == file_val:
        try:
            ms = max(1000, int(file_val))
            return ms / 1000.0
        except (TypeError, ValueError):
            pass
    # env 兜底（保留原 OPENAI_STREAM_IDLE_TIMEOUT_MS / ANTHROPIC_STREAM_IDLE_TIMEOUT_MS / LLM_STREAM_IDLE_TIMEOUT_MS）
    for env_name in (f"{prefix}_STREAM_IDLE_TIMEOUT_MS", "LLM_STREAM_IDLE_TIMEOUT_MS"):
        raw = os.getenv(env_name)
        if raw:
            try:
                return max(1000, int(raw)) / 1000.0
            except ValueError:
                continue
    return 600.0


def get_effective_max_output_tokens(provider: str = "openai") -> int:
    """单轮 LLM 输出 token 上限（对应 SDK max_tokens 参数）。

    跟 AggregateRuntimeBudget 用同一套三层 fallback：
        runtime_budget.json → env（{PROVIDER}_MAX_OUTPUT_TOKENS → LLM_MAX_OUTPUT_TOKENS）→ 默认 32768

    写在 runtime_budget 模块而不进 AggregateRuntimeBudget class 的原因：
      - AggregateRuntimeBudget 语义是"聚合资源消耗限额"（跨多轮工具循环累积）
      - max_output_tokens 是"单轮 LLM 调用的 SDK 参数"，不跨轮累积
      - 两者生命周期不同，共用配置文件但语义独立

    caller：anthropic_client._chat_one_round / llm_client._chat_one_round / _chat_stream
    在构造 CoreRequest 时调本函数拿当前生效值。
    """
    from .runtime_budget_config import load_runtime_budget_config
    file_cfg = load_runtime_budget_config()
    prefix = provider.upper()
    return _resolve_int(
        file_cfg.get("max_output_tokens"),
        f"{prefix}_MAX_OUTPUT_TOKENS",
        "LLM_MAX_OUTPUT_TOKENS",
        default=32768,
    )


@dataclass(frozen=True)
class RuntimeBudgetSnapshot:
    tool_calls_used: int
    tool_calls_remaining: int
    tool_call_usage_ratio: float
    tool_output_chars_used: int
    tool_output_chars_remaining: int
    tool_output_usage_ratio: float
    wall_clock_seconds_elapsed: int
    wall_clock_seconds_remaining: int
    wall_clock_usage_ratio: float

    @property
    def highest_usage_ratio(self) -> float:
        return max(
            self.tool_call_usage_ratio,
            self.tool_output_usage_ratio,
            self.wall_clock_usage_ratio,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_calls_used": self.tool_calls_used,
            "tool_calls_remaining": self.tool_calls_remaining,
            "tool_call_usage_ratio": self.tool_call_usage_ratio,
            "tool_output_chars_used": self.tool_output_chars_used,
            "tool_output_chars_remaining": self.tool_output_chars_remaining,
            "tool_output_usage_ratio": self.tool_output_usage_ratio,
            "wall_clock_seconds_elapsed": self.wall_clock_seconds_elapsed,
            "wall_clock_seconds_remaining": self.wall_clock_seconds_remaining,
            "wall_clock_usage_ratio": self.wall_clock_usage_ratio,
            "highest_usage_ratio": self.highest_usage_ratio,
        }


@dataclass(frozen=True)
class RuntimeBudgetNotice:
    kind: str
    message: str
    prompt: str
    snapshot: RuntimeBudgetSnapshot
    payload: Dict[str, Any]


class AggregateRuntimeBudget:
    """Tracks whole-run resource usage across tool rounds."""

    def __init__(
        self,
        *,
        max_tool_calls: int,
        max_tool_output_chars: int,
        max_wall_clock_seconds: int,
        warn_threshold_ratio: float,
    ):
        self.max_tool_calls = max_tool_calls
        self.max_tool_output_chars = max_tool_output_chars
        self.max_wall_clock_seconds = max_wall_clock_seconds
        self.warn_threshold_ratio = warn_threshold_ratio
        self._started_at = time.monotonic()
        self._tool_calls_used = 0
        self._tool_output_chars_used = 0
        self._near_limit_warning_emitted = False

    @classmethod
    def from_env(cls, provider: str) -> "AggregateRuntimeBudget":
        # 三层 fallback：runtime_budget.json → env → 默认值
        # 用户在前端调过 → 落盘到 runtime_budget.json → 这里读
        # 否则继续走原来的 env / default 路径，老用户行为不变
        from .runtime_budget_config import load_runtime_budget_config
        file_cfg = load_runtime_budget_config()

        prefix = provider.upper()
        return cls(
            max_tool_calls=_resolve_int(
                file_cfg.get("max_tool_calls"),
                f"{prefix}_MAX_TOOL_CALLS",
                "LLM_MAX_TOOL_CALLS",
                default=240,
            ),
            max_tool_output_chars=_resolve_int(
                file_cfg.get("max_tool_output_chars"),
                f"{prefix}_MAX_TOOL_OUTPUT_CHARS",
                "LLM_MAX_TOOL_OUTPUT_CHARS",
                default=1800000,
            ),
            max_wall_clock_seconds=_resolve_int(
                file_cfg.get("max_wall_clock_seconds"),
                f"{prefix}_MAX_WALL_CLOCK_SECONDS",
                "LLM_MAX_WALL_CLOCK_SECONDS",
                default=1800,
            ),
            warn_threshold_ratio=_resolve_float(
                file_cfg.get("warn_threshold_ratio"),
                f"{prefix}_BUDGET_WARN_THRESHOLD_RATIO",
                "LLM_BUDGET_WARN_THRESHOLD_RATIO",
                default=0.9,
            ),
        )

    def snapshot(self) -> RuntimeBudgetSnapshot:
        elapsed = max(0, int(time.monotonic() - self._started_at))
        tool_calls_remaining = max(0, self.max_tool_calls - self._tool_calls_used)
        tool_output_chars_remaining = max(
            0,
            self.max_tool_output_chars - self._tool_output_chars_used,
        )
        wall_clock_seconds_remaining = max(0, self.max_wall_clock_seconds - elapsed)
        return RuntimeBudgetSnapshot(
            tool_calls_used=self._tool_calls_used,
            tool_calls_remaining=tool_calls_remaining,
            tool_call_usage_ratio=self._tool_calls_used / self.max_tool_calls,
            tool_output_chars_used=self._tool_output_chars_used,
            tool_output_chars_remaining=tool_output_chars_remaining,
            tool_output_usage_ratio=self._tool_output_chars_used / self.max_tool_output_chars,
            wall_clock_seconds_elapsed=elapsed,
            wall_clock_seconds_remaining=wall_clock_seconds_remaining,
            wall_clock_usage_ratio=elapsed / self.max_wall_clock_seconds,
        )

    def _build_notice_payload(
        self,
        *,
        kind: str,
        snapshot: RuntimeBudgetSnapshot,
        limit_type: str,
        pressure_axes: Iterable[str] = (),
        exhausted: bool,
    ) -> Dict[str, Any]:
        axes = [axis for axis in pressure_axes if axis]
        if not axes and limit_type and limit_type != "mixed":
            axes = [limit_type]
        return {
            "budget_scope": "aggregate_runtime",
            "notice_kind": kind,
            "limit_type": limit_type,
            "pressure_axes": axes,
            "exhausted": exhausted,
            "snapshot": snapshot.to_dict(),
        }

    def observe_before_round(self) -> ToolLoopStop | None:
        snapshot = self.snapshot()
        if snapshot.wall_clock_seconds_elapsed >= self.max_wall_clock_seconds:
            return ToolLoopStop(
                reason="aggregate_wall_clock_exhausted",
                message=(
                    f"Runtime budget exhausted: wall clock reached {self.max_wall_clock_seconds}s. "
                    "Stop calling tools and answer with the information already collected. "
                    "If the task is still incomplete, state the missing gap clearly."
                ),
                payload=self._build_notice_payload(
                    kind="aggregate_wall_clock_exhausted",
                    snapshot=snapshot,
                    limit_type="wall_clock",
                    exhausted=True,
                ),
            )
        return None

    def observe_tool_batch(
        self,
        tool_calls: Iterable[Dict[str, Any]],
        tool_results: Iterable[str],
    ) -> ToolLoopStop | None:
        tool_calls_list: List[Dict[str, Any]] = list(tool_calls)
        tool_results_list: List[str] = [str(result) for result in tool_results]

        self._tool_calls_used += len(tool_calls_list)
        self._tool_output_chars_used += sum(len(result) for result in tool_results_list)
        snapshot = self.snapshot()

        if self._tool_calls_used > self.max_tool_calls:
            return ToolLoopStop(
                reason="aggregate_tool_calls_exhausted",
                message=(
                    f"Runtime budget exhausted: tool calls exceeded the limit of {self.max_tool_calls}. "
                    "Stop calling tools and answer from the current evidence. "
                    "If more work remains, state the remaining gap clearly."
                ),
                payload=self._build_notice_payload(
                    kind="aggregate_tool_calls_exhausted",
                    snapshot=snapshot,
                    limit_type="tool_calls",
                    exhausted=True,
                ),
            )

        if self._tool_output_chars_used > self.max_tool_output_chars:
            return ToolLoopStop(
                reason="aggregate_tool_output_exhausted",
                message=(
                    f"Runtime budget exhausted: tool output exceeded the limit of {self.max_tool_output_chars} chars. "
                    "Do not expand the search surface further. Converge and answer directly."
                ),
                payload=self._build_notice_payload(
                    kind="aggregate_tool_output_exhausted",
                    snapshot=snapshot,
                    limit_type="tool_output_chars",
                    exhausted=True,
                ),
            )

        return self.observe_before_round()

    def build_near_limit_notice(self) -> RuntimeBudgetNotice | None:
        if self._near_limit_warning_emitted:
            return None

        snapshot = self.snapshot()
        pressure_parts: List[tuple[float, str]] = []
        pressure_axes: List[str] = []

        if snapshot.tool_call_usage_ratio >= self.warn_threshold_ratio:
            pressure_parts.append(
                (
                    snapshot.tool_call_usage_ratio,
                    f"{snapshot.tool_calls_remaining} tool calls left",
                )
            )
            pressure_axes.append("tool_calls")
        if snapshot.tool_output_usage_ratio >= self.warn_threshold_ratio:
            pressure_parts.append(
                (
                    snapshot.tool_output_usage_ratio,
                    f"about {snapshot.tool_output_chars_remaining} tool-output chars left",
                )
            )
            pressure_axes.append("tool_output_chars")
        if snapshot.wall_clock_usage_ratio >= self.warn_threshold_ratio:
            pressure_parts.append(
                (
                    snapshot.wall_clock_usage_ratio,
                    f"about {snapshot.wall_clock_seconds_remaining}s wall clock left",
                )
            )
            pressure_axes.append("wall_clock")

        if not pressure_parts:
            return None

        pressure_parts.sort(key=lambda item: item[0], reverse=True)
        pressure_summary = ", ".join(part for _, part in pressure_parts)
        message = (
            f"Runtime budget is getting tight: {pressure_summary}. "
            "Use the current evidence to converge instead of starting another broad tool sweep."
        )
        prompt = (
            f"[Runtime scheduler] {message}\n"
            "Prefer answering the user directly, or do only one final necessary action. "
            "Do not request more tools unless a missing fact would block a useful answer."
        )
        self._near_limit_warning_emitted = True
        limit_type = pressure_axes[0] if len(pressure_axes) == 1 else "mixed"
        return RuntimeBudgetNotice(
            kind=AGGREGATE_BUDGET_WARNING_KIND,
            message=message,
            prompt=prompt,
            snapshot=snapshot,
            payload=self._build_notice_payload(
                kind=AGGREGATE_BUDGET_WARNING_KIND,
                snapshot=snapshot,
                limit_type=limit_type,
                pressure_axes=pressure_axes,
                exhausted=False,
            ),
        )
