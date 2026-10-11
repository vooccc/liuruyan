"""v2 · B-7 · UploadsInjectMiddleware

检测 thread 内新上传的文件，注入 `<system-reminder>` 让 LLM 知道。

参考：docs/architecture_v2/03_middlewares.md § 05

钩子：before_model

输入约定（state 字段）：
- `uploaded_files`: list[str] 或 list[dict{path, size, ...}]
  由调用层（HTTP 上传 handler / 02 ThreadDataMiddleware）填充
- `uploaded_files_injected`: list[str] 已经注入过的 path（middleware 自己维护）

输出：
- 新增上传 → 注入 system message + 更新 uploaded_files_injected
- 无新增 → return None

注入位置：插在最后一条 user message 之前；这样下一轮 LLM 看到 reminder
紧邻最新提问。

异常隔离：内部 try/except；任何异常 swallow + log。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


class UploadsInjectMiddleware(AgentMiddleware):
    name = "uploads_inject"

    UPLOADS_VIRTUAL_PATH = "/mnt/user-data/uploads"

    async def before_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            uploaded = self._extract_uploads(state.get("uploaded_files"))
            if not uploaded:
                return None

            already = self._extract_already_injected(
                state.get("uploaded_files_injected")
            )

            new_items = [u for u in uploaded if self._key_of(u) not in already]
            if not new_items:
                return None

            reminder = self._build_reminder(new_items)
            new_messages = self._inject_before_last_user(
                state.get("messages") or [], reminder
            )

            new_already = sorted(already | {self._key_of(u) for u in new_items})
            return {
                "messages": new_messages,
                "uploaded_files_injected": new_already,
            }
        except Exception:
            log.exception(
                "UploadsInjectMiddleware swallowed exception (run=%s)",
                ctx.run_id,
            )
            return None

    # ── extract ──────────────────────────────────────────────────────────

    @classmethod
    def _extract_uploads(cls, raw: Any) -> list[Any]:
        """只保留有合法 path key 的 item（str 或 dict 含 path:str）。"""
        if isinstance(raw, (list, tuple, set)):
            out: list[Any] = []
            for u in raw:
                if cls._key_of(u):
                    out.append(u)
            return out
        return []

    @staticmethod
    def _extract_already_injected(raw: Any) -> set[str]:
        if isinstance(raw, (list, tuple, set)):
            return {x for x in raw if isinstance(x, str)}
        return set()

    @staticmethod
    def _key_of(item: Any) -> str:
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            p = item.get("path")
            if isinstance(p, str):
                return p
        return ""

    # ── build reminder ────────────────────────────────────────────────────

    def _build_reminder(self, items: list[Any]) -> str:
        lines: list[str] = []
        for u in items:
            if isinstance(u, str):
                lines.append(f"- {u}")
            elif isinstance(u, dict):
                path = u.get("path", "?")
                size = u.get("size")
                if isinstance(size, (int, float)) and size > 0:
                    lines.append(f"- {path} ({self._fmt_size(int(size))})")
                else:
                    lines.append(f"- {path}")
        body = "\n".join(lines)
        return (
            "<system-reminder>\n"
            "The user just uploaded these files "
            f"(visible at {self.UPLOADS_VIRTUAL_PATH}):\n"
            f"{body}\n"
            "</system-reminder>"
        )

    @staticmethod
    def _fmt_size(n: int) -> str:
        if n < 1024:
            return f"{n}B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f}KB"
        if n < 1024 * 1024 * 1024:
            return f"{n / (1024 * 1024):.1f}MB"
        return f"{n / (1024 * 1024 * 1024):.2f}GB"

    # ── inject ──────────────────────────────────────────────────────────

    @staticmethod
    def _inject_before_last_user(
        messages: list[Any], reminder: str
    ) -> list[Any]:
        """在最后一条 user message 之前插入 system reminder；找不到则末尾追加。"""
        new_msgs = list(messages)
        insert_idx = len(new_msgs)  # 默认末尾
        for i in range(len(new_msgs) - 1, -1, -1):
            m = new_msgs[i]
            if isinstance(m, dict) and m.get("role") == "user":
                insert_idx = i
                break
        new_msgs.insert(insert_idx, {"role": "system", "content": reminder})
        return new_msgs


__all__ = ["UploadsInjectMiddleware"]
