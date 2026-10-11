import os


def _read_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


class ToolResultBudget:
    """Applies a per-tool-result budget before results are fed back to the model."""

    def __init__(self, max_chars: int):
        self.max_chars = max_chars

    @classmethod
    def from_env(cls) -> "ToolResultBudget":
        return cls(_read_int_env("LLM_MAX_TOOL_RESULT_CHARS", default=50000))

    def apply(self, result: str) -> str:
        text, _ = self.apply_with_meta(result)
        return text

    def apply_with_meta(self, result: str):
        """同 apply，但额外返回元数据 dict 供 stats / SSE 观测层使用。

        返回 (text, meta)：
          - 未截断：meta = None
          - 已截断：meta = {"original_size": int, "visible_budget": int, "kept_chars": int}
        """
        original = len(result)
        if original <= self.max_chars:
            return result, None

        note = (
            "\n\n[system] Tool result truncated due to budget. "
            f"Original size: {original} chars. "
            f"Visible budget: {self.max_chars} chars. "
            "Ask for a narrower query if more detail is needed."
        )
        visible_limit = max(0, self.max_chars - len(note))
        truncated = result[:visible_limit]
        meta = {
            "original_size": original,
            "visible_budget": self.max_chars,
            "kept_chars": visible_limit,
        }
        return truncated + note, meta
