"""v2 · B-1 · LoopDetectionMiddleware

检测连续重复工具调用，warn 后强制收尾。

参考：_docs/architecture_v2/03_middlewares.md § 08

算法：

1. **同 hash 滑窗**：最近 20 个 tool call，按 (name, normalized_args) hash 计数：
   - ≥ 3 次 → 注入 system 警告
   - ≥ 5 次 → strip 最后一条 assistant message 的 tool_calls，强制 LLM 文字收尾

2. **同 name 滑窗**：最近 50 个 tool call，按 name 计数（不分 args）：
   - ≥ 30 次 → 警告
   - ≥ 50 次 → 强制收尾

3. **read_file 特殊处理**：line offset 按 200 行 bucket，避免相邻行被误判为不同。
   覆盖 name ∈ {read_file, Read, view_file, read} + arg key ∈ {offset, start_line, start}。

输入消息格式：OpenAI std。
- 主路径 v1 已对齐（MEMORY 里 `history_format_align_planned`）。
- 容错：assistant 消息没 tool_calls / messages 空 / 任意异常 → 返回 None 不改 state。

异常隔离：内部 try/except 全包；任何异常 swallow + log，绝不抛出。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from typing import Any

from .base import AgentMiddleware, RunContext, ThreadState

log = logging.getLogger(__name__)


class LoopDetectionMiddleware(AgentMiddleware):
    name = "loop_detection"

    # ── 阈值（设为类属性方便测试中 monkeypatch 调小） ───────────────────
    SLIDING_WINDOW_SAME_HASH = 20
    THRESHOLD_WARN_SAME_HASH = 3
    THRESHOLD_STOP_SAME_HASH = 5

    SLIDING_WINDOW_SAME_NAME = 50
    THRESHOLD_WARN_SAME_NAME = 30
    THRESHOLD_STOP_SAME_NAME = 50

    # read_file bucket（line offset 按 200 行归并）
    READ_FILE_BUCKET_SIZE = 200
    READ_FILE_TOOL_NAMES = frozenset({"read_file", "Read", "view_file", "read"})
    READ_FILE_OFFSET_KEYS = ("offset", "start_line", "start")

    async def after_model(
        self, state: ThreadState, ctx: RunContext
    ) -> dict[str, Any] | None:
        try:
            messages = state.get("messages")
            if not isinstance(messages, list) or not messages:
                return None

            tool_calls = self._extract_tool_calls(messages)
            if not tool_calls:
                return None

            # 1. 同 hash 滑窗
            recent_hash_window = tool_calls[-self.SLIDING_WINDOW_SAME_HASH :]
            hash_counts = Counter(
                self._hash_call(name, args) for name, args in recent_hash_window
            )
            top_hash, hash_count = hash_counts.most_common(1)[0]

            # 2. 同 name 滑窗
            recent_name_window = tool_calls[-self.SLIDING_WINDOW_SAME_NAME :]
            name_counts = Counter(name for name, _ in recent_name_window)
            top_name, name_count = name_counts.most_common(1)[0]

            # 3. 触发哪一档（stop 优先于 warn）
            if (
                hash_count >= self.THRESHOLD_STOP_SAME_HASH
                or name_count >= self.THRESHOLD_STOP_SAME_NAME
            ):
                reason = self._build_reason(
                    top_name=top_name,
                    name_count=name_count,
                    hash_count=hash_count,
                    is_stop=True,
                )
                return self._force_stop(messages, reason)

            if (
                hash_count >= self.THRESHOLD_WARN_SAME_HASH
                or name_count >= self.THRESHOLD_WARN_SAME_NAME
            ):
                reason = self._build_reason(
                    top_name=top_name,
                    name_count=name_count,
                    hash_count=hash_count,
                    is_stop=False,
                )
                return self._inject_warning(messages, reason)

            return None
        except Exception:
            # 钩子契约：异常绝不向上抛
            log.exception(
                "LoopDetectionMiddleware swallowed exception (run=%s)", ctx.run_id
            )
            return None

    # ── 提取 ──────────────────────────────────────────────────────────────

    def _extract_tool_calls(
        self, messages: list[Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """从 messages 抽出 (name, args_dict) 列表，按时间序。

        只看 OpenAI std assistant.tool_calls；解析失败的单条 skip 不抛。
        """
        out: list[tuple[str, dict[str, Any]]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant":
                continue
            tcs = msg.get("tool_calls")
            if not isinstance(tcs, list):
                continue
            for tc in tcs:
                parsed = self._parse_tool_call(tc)
                if parsed is not None:
                    out.append(parsed)
        return out

    def _parse_tool_call(
        self, tc: Any
    ) -> tuple[str, dict[str, Any]] | None:
        """解析单个 tool_call 节点为 (name, args_dict)。"""
        if not isinstance(tc, dict):
            return None
        fn = tc.get("function")
        if not isinstance(fn, dict):
            return None
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            return None
        raw_args = fn.get("arguments")
        args = self._coerce_args(raw_args)
        return name, args

    @staticmethod
    def _coerce_args(raw: Any) -> dict[str, Any]:
        """arguments 字段可能是 JSON 字符串、dict、或缺失；统一成 dict。"""
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {"_raw": raw}
            except (json.JSONDecodeError, ValueError):
                return {"_raw": raw}
        return {}

    # ── hash ──────────────────────────────────────────────────────────────

    def _hash_call(self, name: str, args: dict[str, Any]) -> str:
        """name + 标准化 args 的稳定 hash。

        read_file 的 line offset 按 200 行 bucket。
        """
        normalized = self._normalize_args(name, args)
        try:
            payload = json.dumps(
                {"name": name, "args": normalized},
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
        except (TypeError, ValueError):
            payload = f"{name}::{normalized!r}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _normalize_args(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """对 read_file 类工具做 line offset bucket。"""
        if name not in self.READ_FILE_TOOL_NAMES:
            return args
        result = dict(args)
        bucket = self.READ_FILE_BUCKET_SIZE
        for key in self.READ_FILE_OFFSET_KEYS:
            if key in result:
                v = result[key]
                if isinstance(v, (int, float)):
                    result[key] = (int(v) // bucket) * bucket
        return result

    # ── 输出 ──────────────────────────────────────────────────────────────

    def _build_reason(
        self,
        *,
        top_name: str,
        name_count: int,
        hash_count: int,
        is_stop: bool,
    ) -> str:
        action = "强制结束工具调用" if is_stop else "建议改用文字回答"
        return (
            f"检测到工具循环（{action}）：tool=`{top_name}`，"
            f"同参数连续 {hash_count} 次 / 同名累计 {name_count} 次。"
        )

    def _inject_warning(
        self, messages: list[Any], reason: str
    ) -> dict[str, Any]:
        """在最后一条 assistant 消息**前**插入一条 system 警告。

        放在 assistant 前是为了下一轮 before_model 把它送给 LLM 看到。
        """
        new_messages = list(messages)
        warning = {
            "role": "system",
            "content": (
                f"<system-reminder>{reason}</system-reminder>"
            ),
        }
        # 找到最后一条 assistant，插入它前面
        insert_idx = len(new_messages)
        for i in range(len(new_messages) - 1, -1, -1):
            m = new_messages[i]
            if isinstance(m, dict) and m.get("role") == "assistant":
                insert_idx = i
                break
        new_messages.insert(insert_idx, warning)
        return {"messages": new_messages, "loop_detection": "warn"}

    def _force_stop(
        self, messages: list[Any], reason: str
    ) -> dict[str, Any]:
        """strip 最后一条 assistant.tool_calls，把 reason 拼进 content。"""
        new_messages = [self._copy_msg(m) for m in messages]
        for i in range(len(new_messages) - 1, -1, -1):
            m = new_messages[i]
            if isinstance(m, dict) and m.get("role") == "assistant":
                # 移除 tool_calls
                if "tool_calls" in m:
                    m["tool_calls"] = []
                # content 拼上说明
                existing = m.get("content")
                tail = f"\n\n（{reason}请基于已有信息文字回答用户。）"
                if isinstance(existing, str):
                    m["content"] = existing + tail
                elif existing is None:
                    m["content"] = tail.lstrip()
                else:
                    # content 是 list 或其它结构 — 不动，单独加一段
                    m["content"] = [existing, {"type": "text", "text": tail}]
                break
        return {"messages": new_messages, "loop_detection": "force_stop"}

    @staticmethod
    def _copy_msg(m: Any) -> Any:
        """浅拷贝单条 message（dict 才需要复制；其它直接返回）。"""
        if isinstance(m, dict):
            copied = dict(m)
            # tool_calls 列表也要复制，避免修改时影响原 state
            if isinstance(copied.get("tool_calls"), list):
                copied["tool_calls"] = list(copied["tool_calls"])
            return copied
        return m


__all__ = ["LoopDetectionMiddleware"]
