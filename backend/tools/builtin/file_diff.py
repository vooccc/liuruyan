"""file_diff —— 给文件编辑工具产出"前端可视化 diff sentinel 负载"。

设计：
- patch_tool / write_file_tool / multi_edit_tool 在写盘前后分别拿到 before/after，
  调用 ``build_file_diff_payload`` 得到结构化 hunks，merge 进 tool result JSON。
- 前端 ``FileDiffCard`` 看到 ``_runtime_block == 'file_diff'`` 就取代默认 tool_result
  渲染，照 Cascade 风格出红/绿 hunk + 行号 + 统计。

跟 ``browser_preview`` / ``ask_user_question`` 同款"工具结果 sentinel"模式。

Token 控制（关键）：
- LLM 也会看到 result 字符串。完整 unified diff 太大会污染 prompt。
- ``max_total_lines`` 默认 200：hunk 里的 line 总数（context + add + remove）超过就截断，
  保留前 N 行 + 设 ``truncated: True``。
- 大文件全替换（>500 行）走特殊路径：仅给 stats + 头 1 个 hunk + 余下 "..." 省略，
  避免一次 8KB+ JSON 灌进 prompt。
"""
from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional


# ── 输出协议常量 ─────────────────────────────────────────────────────
_SENTINEL = "file_diff"
_DEFAULT_MAX_TOTAL_LINES = 200
_DEFAULT_CONTEXT = 3

# 单行内容字符上限（对齐 read_file 的 _READ_FILE_MAX_LINE_CHARS=2000）。
# minified / 打包后的单行超长文件（如 dist/extension.js 1 行 8 万字符）做 edit 时，
# diff 会产出"删整行 + 增整行"两条超长 line，行数才 2 << 200 行上限，旧逻辑直接放行，
# 把十几万字符灌进 tool result → 爆上下文窗口。这里按字符截断堵住这条路。
_MAX_LINE_CONTENT_CHARS = 2_000

# 整个 diff 的内容字符总预算（兜底闸）。即便单行都没超 2000，海量小行变更累积起来
# 仍可能很大。超过这个总量就放弃明细 hunks，只回 stats + 标记，让模型知道"改了但内容太大已省略"。
_MAX_TOTAL_DIFF_CHARS = 40_000


def _split_lines_keepends(text: str) -> List[str]:
    """按行切，保留行尾换行。空字符串 → 空列表。"""
    if not text:
        return []
    # splitlines(keepends=True) 既保 \n 又保 \r\n，且最后一行没换行也能切出来
    return text.splitlines(keepends=True)


def _strip_trailing_newline(line: str) -> str:
    """单行做展示时去掉换行（前端 <pre> 自己渲染换行）。"""
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1]
    return line


def _clean_line(line: str) -> str:
    """去换行 + 单行字符截断。给 diff 每条 line 的 content 用。

    超过 _MAX_LINE_CONTENT_CHARS 的行（典型：minified 单行文件）截断并加标记，
    避免单条超长行原样进 prompt 撑爆上下文窗口。
    """
    text = _strip_trailing_newline(line)
    if len(text) > _MAX_LINE_CONTENT_CHARS:
        original_len = len(text)
        return (
            text[:_MAX_LINE_CONTENT_CHARS]
            + f"...[line truncated: original {original_len} chars, "
            f"kept {_MAX_LINE_CONTENT_CHARS}]"
        )
    return text


def _build_hunks(
    before_lines: List[str],
    after_lines: List[str],
    *,
    context_lines: int,
) -> List[Dict[str, Any]]:
    """用 SequenceMatcher.get_grouped_opcodes 直接出"按 hunk 分组"的 diff。

    每个 hunk 含若干 opcode；opcode 是 (tag, i1, i2, j1, j2)，tag in
    {'equal','replace','delete','insert'}。我们按 tag 展开成 lines。

    返回 hunk 结构：
      {
        "old_start": 1-indexed start in `before`,
        "old_count": int,
        "new_start": 1-indexed start in `after`,
        "new_count": int,
        "lines": [
          {"type": 'context'|'add'|'remove', "content": str,
           "old_no": int|None, "new_no": int|None},
          ...
        ],
      }

    若 before == after：返 []。
    """
    sm = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    hunks: List[Dict[str, Any]] = []
    for group in sm.get_grouped_opcodes(n=context_lines):
        if not group:
            continue
        first_tag, i1_first, _, j1_first, _ = group[0]
        _, _, i2_last, _, j2_last = group[-1]

        old_start = i1_first + 1  # 1-indexed
        new_start = j1_first + 1
        old_count = i2_last - i1_first
        new_count = j2_last - j1_first

        lines: List[Dict[str, Any]] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                # context 行，老/新行号同步推进
                for k in range(i2 - i1):
                    raw = before_lines[i1 + k]
                    lines.append({
                        "type": "context",
                        "content": _clean_line(raw),
                        "old_no": i1 + k + 1,
                        "new_no": j1 + k + 1,
                    })
            elif tag == "delete":
                for k in range(i2 - i1):
                    raw = before_lines[i1 + k]
                    lines.append({
                        "type": "remove",
                        "content": _clean_line(raw),
                        "old_no": i1 + k + 1,
                        "new_no": None,
                    })
            elif tag == "insert":
                for k in range(j2 - j1):
                    raw = after_lines[j1 + k]
                    lines.append({
                        "type": "add",
                        "content": _clean_line(raw),
                        "old_no": None,
                        "new_no": j1 + k + 1,
                    })
            elif tag == "replace":
                # delete 旧 + insert 新（先红再绿，对齐主流 diff 视图）
                for k in range(i2 - i1):
                    raw = before_lines[i1 + k]
                    lines.append({
                        "type": "remove",
                        "content": _clean_line(raw),
                        "old_no": i1 + k + 1,
                        "new_no": None,
                    })
                for k in range(j2 - j1):
                    raw = after_lines[j1 + k]
                    lines.append({
                        "type": "add",
                        "content": _clean_line(raw),
                        "old_no": None,
                        "new_no": j1 + k + 1,
                    })
        hunks.append({
            "old_start": old_start,
            "old_count": old_count,
            "new_start": new_start,
            "new_count": new_count,
            "lines": lines,
        })
    return hunks


def _truncate_hunks(
    hunks: List[Dict[str, Any]],
    max_total_lines: int,
) -> tuple[List[Dict[str, Any]], bool]:
    """限总行数；超过保留前 N 行（按 hunk 顺序），切到当前 hunk 内部。

    返回 (truncated_hunks, was_truncated)。
    """
    if max_total_lines <= 0:
        return hunks, False
    total = 0
    truncated_hunks: List[Dict[str, Any]] = []
    was_truncated = False
    for hunk in hunks:
        lines = hunk["lines"]
        if total + len(lines) <= max_total_lines:
            truncated_hunks.append(hunk)
            total += len(lines)
            continue
        # 当前 hunk 部分纳入
        remaining = max_total_lines - total
        if remaining > 0:
            partial = dict(hunk)
            partial["lines"] = lines[:remaining]
            truncated_hunks.append(partial)
        was_truncated = True
        break
    return truncated_hunks, was_truncated


def _stats(before_lines: List[str], after_lines: List[str]) -> Dict[str, int]:
    """对 hunks 之前、整体增删行数做统计（不含 context）。"""
    sm = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            removed += i2 - i1
        elif tag == "insert":
            added += j2 - j1
        elif tag == "replace":
            removed += i2 - i1
            added += j2 - j1
    return {"added": added, "removed": removed}


def build_file_diff_payload(
    path: str,
    before: str,
    after: str,
    *,
    max_total_lines: int = _DEFAULT_MAX_TOTAL_LINES,
    context_lines: int = _DEFAULT_CONTEXT,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造 sentinel payload 给前端 FileDiffCard。

    Args:
        path: 文件路径（应为已 resolve 的绝对路径，给前端展示用）
        before: 写盘前的文件内容（新建文件时传 ""）
        after: 写盘后的文件内容（删除文件场景这里不适用——删除应该单独工具）
        max_total_lines: hunk lines 总数上限（防 prompt 爆炸）
        context_lines: 每个 hunk 上下保留多少 context 行
        extra: 额外字段 merge 进 payload（如 success/replacements/applied）

    Returns:
        dict with sentinel + path + stats + hunks + flags。

    Note:
        before == after 时仍返回 payload（hunks 空），让前端能渲染"无变化"提示，
        避免静默吞掉用户能看到的状态。但 caller 可以根据需要选择不带 sentinel
        （比如 multi_edit 中所有 edit 都是 no-op 时，根本走不到这里）。
    """
    before_lines = _split_lines_keepends(before)
    after_lines = _split_lines_keepends(after)

    is_create = len(before_lines) == 0 and len(after_lines) > 0
    is_delete = len(before_lines) > 0 and len(after_lines) == 0
    stats = _stats(before_lines, after_lines)

    hunks = _build_hunks(
        before_lines,
        after_lines,
        context_lines=context_lines,
    )
    truncated_hunks, was_truncated = _truncate_hunks(hunks, max_total_lines)

    # 兜底闸：单行已截断 + 行数已截断后，再算一次明细总字符。仍超预算（海量小行变更
    # 累积）就丢掉 hunks 明细，只留 stats，避免巨型 diff JSON 撑爆 prompt。
    total_chars = sum(
        len(ln.get("content", "")) for h in truncated_hunks for ln in h["lines"]
    )
    omitted_large_diff = False
    if total_chars > _MAX_TOTAL_DIFF_CHARS:
        truncated_hunks = []
        was_truncated = True
        omitted_large_diff = True

    payload: Dict[str, Any] = {
        "_runtime_block": _SENTINEL,
        "path": path,
        "stats": stats,
        "hunks": truncated_hunks,
        "truncated": was_truncated,
        "is_create": is_create,
        "is_delete": is_delete,
    }
    if omitted_large_diff:
        payload["omitted_large_diff"] = True
        payload["hint"] = (
            f"Diff too large to inline ({total_chars} chars > {_MAX_TOTAL_DIFF_CHARS} budget); "
            f"hunk details omitted. Stats: +{stats['added']} / -{stats['removed']} lines."
        )
    if extra:
        # extra 字段优先于 payload（不会覆盖 sentinel/path/stats/hunks 这些核心字段，
        # 但 caller 可以通过 extra 加 success / replacements / applied 等）
        for k, v in extra.items():
            if k not in payload:
                payload[k] = v
    return payload
