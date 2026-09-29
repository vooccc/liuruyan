"""runtime_budget.json 落盘 + 校验。

落盘到 `data_dir() / "runtime_budget.json"`。AggregateRuntimeBudget.from_env 优先读
本文件、然后 env、最后 default，三层 fallback。

字段：
- max_tool_calls: int >= 1（AggregateRuntimeBudget 总调用次数上限）
- max_tool_rounds: int >= 1（ToolLoopGuard 工具循环轮数上限——LLM 一轮可能多 calls）
- max_tool_output_chars: int >= 1
- max_wall_clock_seconds: int >= 1
- max_output_tokens: int >= 1（单轮 LLM 输出 token 上限——对应 SDK max_tokens 参数；
  默认 32768，写大文件 / 长代码场景可按模型上限再调）
- stream_idle_timeout_ms: int >= 1000（单次流式调用“多久收不到下一个 chunk 算挂起”；
  默认 600000=600s。thinking 模型 + 大 max_tokens 下可再调高到 10-15 分钟）
- warn_threshold_ratio: float ∈ [0.5, 0.98]（跟现有 _read_float_env clamp 一致）

不维护 .secrets——budget 不含敏感信息。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from . import paths

logger = logging.getLogger(__name__)


_INT_FIELDS = (
    "max_tool_calls",
    "max_tool_rounds",
    "max_tool_output_chars",
    "max_wall_clock_seconds",
    "max_output_tokens",
    "stream_idle_timeout_ms",
)
_FLOAT_FIELDS = ("warn_threshold_ratio",)
_ALL_FIELDS = (*_INT_FIELDS, *_FLOAT_FIELDS)

_WARN_RATIO_MIN = 0.5
_WARN_RATIO_MAX = 0.98


def load_runtime_budget_config() -> Dict[str, Any]:
    """读 runtime_budget.json。文件不存在 / JSON 损坏 → {}。"""
    path = paths.runtime_budget_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        logger.warning("runtime_budget.json 损坏 (%s)，按空配置处理", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for key in _INT_FIELDS:
        val = raw.get(key)
        if isinstance(val, (int, float)) and val == val:  # 排除 NaN
            try:
                cleaned[key] = max(1, int(val))
            except (TypeError, ValueError):
                continue
    for key in _FLOAT_FIELDS:
        val = raw.get(key)
        if isinstance(val, (int, float)) and val == val:
            try:
                cleaned[key] = max(_WARN_RATIO_MIN, min(_WARN_RATIO_MAX, float(val)))
            except (TypeError, ValueError):
                continue
    return cleaned


def save_runtime_budget_config(cfg: Dict[str, Any]) -> None:
    path = paths.runtime_budget_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def validate_runtime_budget_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """前端 PUT body → 落盘格式。未知字段忽略；缺字段允许（部分更新）；类型/范围错抛 ValueError。"""
    if not isinstance(body, dict):
        raise ValueError("payload 必须是 object")
    out: Dict[str, Any] = {}
    for key in _INT_FIELDS:
        if key not in body:
            continue
        raw = body[key]
        try:
            ival = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是整数")
        if ival < 1:
            raise ValueError(f"{key} 必须 >= 1")
        out[key] = ival
    for key in _FLOAT_FIELDS:
        if key not in body:
            continue
        raw = body[key]
        try:
            fval = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是数字")
        if not (_WARN_RATIO_MIN <= fval <= _WARN_RATIO_MAX):
            raise ValueError(
                f"{key} 必须在 [{_WARN_RATIO_MIN}, {_WARN_RATIO_MAX}] 之间"
            )
        out[key] = fval
    return out
