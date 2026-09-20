"""read_logs：让 agent 自己看日志定位 bug（自进化系统的基础视觉）。

设计动机：
- 之前 AI 帮用户定位问题时只能盲猜，或让用户复制粘贴日志片段
- 给一个轻量、安全的 log reader，AI 可以直接 tail/grep 项目里的日志文件
- 项目本身在 cmd /k 里输出 stdout/stderr 没落盘，但项目落了几条结构化 jsonl：
    - data_dir/tool_stats.jsonl  —— 工具调用历史（已在 user rules 标"金矿"）
    - data_dir/backend_writes.jsonl —— 写盘审计
  以及 agent 跑 npm/python 进程时的临时 .log 文件，AI 也常需要看

设计取舍：
- 路径解析复用 file_tools._resolve_read_path（working_directory ContextVar 同款）
- 默认 tail 模式（看最近发生了什么 = bug 定位首选）
- 大文件反向分块读取，不一次性 load
- 输出硬上限：5000 行 / 200KB，超出 truncated=True 提示模型继续 grep 缩范围
- JSONL 文件且 level 过滤时，按 JSON 字段判定 level；普通文件按子串 / 正则
- 不支持 follow/tail -f：单次工具调用语义，模型要"等日志"应该 run_command 单独跑

红线：
- 这是只读工具，绝不写文件
- 路径必须经 _resolve_read_path 解，禁止读取项目外的随机系统文件（绝对路径用户传啥读啥，跟 read_file 同策略）
- 不解析任何二进制；首 1KB 含 \x00 直接拒
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 硬上限 ────────────────────────────────────────────────────────────────
_MAX_OUTPUT_BYTES = 200_000        # 输出 JSON 文本上限 ~200KB
_MAX_LINES_RETURNED = 5000         # 返回行数上限
_DEFAULT_TAIL_LINES = 200          # 默认 tail 行数
_MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024  # 500MB sanity 上限
_REVERSE_READ_CHUNK = 64 * 1024    # 反向分块读 64KB
_BINARY_SNIFF_BYTES = 1024         # 首 1KB 检测二进制

# Level 关键字（大小写不敏感）。出现这些 token 视为该等级。
_LEVEL_TOKENS = {
    "ERROR":   {"ERROR", "ERR", "FATAL", "CRITICAL", "CRIT"},
    "WARN":    {"WARN", "WARNING"},
    "INFO":    {"INFO"},
    "DEBUG":   {"DEBUG", "TRACE"},
}


def _normalize_level(level: Optional[str]) -> Optional[str]:
    if not level:
        return None
    up = str(level).strip().upper()
    if up in ("WARNING",):
        return "WARN"
    if up in ("ERR", "FATAL", "CRITICAL", "CRIT"):
        return "ERROR"
    if up in ("TRACE",):
        return "DEBUG"
    if up in _LEVEL_TOKENS:
        return up
    return None  # 不认识就当没设


def _is_binary_head(head: bytes) -> bool:
    if b"\x00" in head:
        return True
    # 高比例不可打印字符也判二进制（容许 \t\n\r 与扩展 UTF-8）
    if not head:
        return False
    printable = sum(1 for b in head if b in (9, 10, 13) or 32 <= b <= 126 or b >= 0x80)
    return printable / len(head) < 0.70


def _read_tail_lines(path: Path, n_lines: int, file_size: int) -> List[str]:
    """从文件末尾反向读取 n_lines 行。

    大文件不全读：从 EOF 反向分块往前找 \\n，凑够 n_lines+1 个换行就停。
    小文件（< 1MB）直接读完更省事。
    """
    if file_size <= 1024 * 1024:
        # 小文件：全读 + decode + 切尾
        with path.open("rb") as fp:
            data = fp.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-n_lines:] if n_lines > 0 else lines

    # 大文件：反向分块读
    needed_newlines = n_lines + 1
    buf = bytearray()
    pos = file_size
    newline_count = 0
    with path.open("rb") as fp:
        while pos > 0 and newline_count < needed_newlines:
            read_size = min(_REVERSE_READ_CHUNK, pos)
            pos -= read_size
            fp.seek(pos)
            chunk = fp.read(read_size)
            buf[:0] = chunk  # 前插
            newline_count = buf.count(b"\n")
    text = bytes(buf).decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-n_lines:] if n_lines > 0 else lines


def _read_head_lines(path: Path, n_lines: int) -> List[str]:
    out: List[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        for i, line in enumerate(fp):
            if i >= n_lines:
                break
            out.append(line.rstrip("\r\n"))
    return out


def _matches_level(line: str, level: str, is_jsonl: bool) -> bool:
    """判定某行是否属于指定 level。

    - JSONL：尝试 json 解析，看 level / levelname / lvl 字段
    - 普通：扫 line 中 token（大小写不敏感）
    """
    target_tokens = _LEVEL_TOKENS.get(level, {level})
    if is_jsonl:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                lv = obj.get("level") or obj.get("levelname") or obj.get("lvl") or obj.get("severity")
                if isinstance(lv, str) and lv.strip().upper() in target_tokens:
                    return True
                # JSONL 没 level 字段时退化为子串匹配
                if lv is not None:
                    return False
        except (ValueError, TypeError):
            pass
    upper = line.upper()
    return any(tok in upper for tok in target_tokens)


def _filter_lines(
    lines: List[str],
    base_offset: int,
    grep: Optional[re.Pattern],
    level: Optional[str],
    is_jsonl: bool,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, line in enumerate(lines):
        if grep is not None and not grep.search(line):
            continue
        if level is not None and not _matches_level(line, level, is_jsonl):
            continue
        out.append({"n": base_offset + i + 1, "text": line})
    return out


def _resolve_path(path_str: str) -> Path:
    """复用 file_tools 的只读路径解析（working_directory ContextVar 同款）。"""
    # 懒 import 避免初始化顺序问题
    from .file_tools import _resolve_read_path
    return _resolve_read_path(path_str)


async def read_logs_tool(
    path: str,
    tail_lines: int = _DEFAULT_TAIL_LINES,
    head: bool = False,
    grep: Optional[str] = None,
    level: Optional[str] = None,
    max_lines: Optional[int] = None,
    **_ctx: Any,
) -> str:
    """读日志文件，支持 tail/head + 正则 + level 过滤。

    Args:
        path: 日志文件路径（相对路径走 working_directory / agent_dir / cwd 三段解析）
        tail_lines: tail 模式读最后 N 行，默认 200，最大 5000
        head: True 改为从头读 N 行
        grep: 行级正则过滤（Python re.search 语义，大小写敏感）
        level: 等级过滤（ERROR/WARN/INFO/DEBUG，大小写不敏感）
        max_lines: 返回行数上限（默认与 tail_lines 一致，硬顶 5000）
    """
    if not path or not isinstance(path, str):
        return json.dumps({"error": "path required (non-empty string)"}, ensure_ascii=False)

    try:
        resolved = _resolve_path(path)
    except Exception as exc:
        return json.dumps({"error": f"path resolve failed: {exc}"}, ensure_ascii=False)

    if not resolved.exists():
        return json.dumps({
            "error": f"file not found: {resolved}",
            "hint": "use glob_search / grep to locate the log first, or pass an absolute path",
        }, ensure_ascii=False)
    if resolved.is_dir():
        return json.dumps({"error": f"is a directory, not a file: {resolved}"}, ensure_ascii=False)

    try:
        file_size = resolved.stat().st_size
    except OSError as exc:
        return json.dumps({"error": f"stat failed: {exc}"}, ensure_ascii=False)

    if file_size > _MAX_FILE_SIZE_BYTES:
        return json.dumps({
            "error": f"file too large ({file_size} bytes > {_MAX_FILE_SIZE_BYTES}); refuse to read",
            "hint": "use grep / awk via run_command for files > 500MB",
        }, ensure_ascii=False)

    # 二进制嗅探
    try:
        with resolved.open("rb") as fp:
            head_bytes = fp.read(_BINARY_SNIFF_BYTES)
    except OSError as exc:
        return json.dumps({"error": f"read failed: {exc}"}, ensure_ascii=False)
    if _is_binary_head(head_bytes):
        return json.dumps({
            "error": f"refuses binary file: {resolved}",
            "hint": "read_logs is text-only; binary log? use a dedicated parser",
        }, ensure_ascii=False)

    # 参数 sanitize
    try:
        tail_lines = int(tail_lines) if tail_lines is not None else _DEFAULT_TAIL_LINES
    except (TypeError, ValueError):
        tail_lines = _DEFAULT_TAIL_LINES
    tail_lines = max(1, min(tail_lines, _MAX_LINES_RETURNED))

    if max_lines is not None:
        try:
            max_lines = int(max_lines)
        except (TypeError, ValueError):
            max_lines = None
    cap = min(max_lines, _MAX_LINES_RETURNED) if max_lines else _MAX_LINES_RETURNED

    grep_pat: Optional[re.Pattern] = None
    if grep:
        try:
            grep_pat = re.compile(grep)
        except re.error as exc:
            return json.dumps({"error": f"invalid regex {grep!r}: {exc}"}, ensure_ascii=False)

    norm_level = _normalize_level(level)
    if level and not norm_level:
        return json.dumps({
            "error": f"unknown level {level!r}",
            "hint": "supported: ERROR / WARN / INFO / DEBUG (case-insensitive)",
        }, ensure_ascii=False)

    is_jsonl = resolved.suffix.lower() == ".jsonl"

    # 读取
    try:
        if head:
            # head 模式没法精确 base_offset（除非全扫），统一用 0-based 起算
            raw_lines = _read_head_lines(resolved, tail_lines)
            base_offset = 0
        else:
            raw_lines = _read_tail_lines(resolved, tail_lines, file_size)
            # 行号近似：用 0 当 base，下文会在过滤后改成"从尾倒数"标号
            base_offset = max(0, _count_total_lines(resolved, file_size) - len(raw_lines))
    except OSError as exc:
        return json.dumps({"error": f"read failed: {exc}"}, ensure_ascii=False)

    matched = _filter_lines(raw_lines, base_offset, grep_pat, norm_level, is_jsonl)

    truncated_by_cap = False
    if len(matched) > cap:
        matched = matched[-cap:] if not head else matched[:cap]
        truncated_by_cap = True

    # 按输出字节硬截
    result_payload: Dict[str, Any] = {
        "path": str(resolved),
        "size_bytes": file_size,
        "mode": "head" if head else "tail",
        "requested_lines": tail_lines,
        "scanned_lines": len(raw_lines),
        "matched_lines": len(matched),
        "is_jsonl": is_jsonl,
        "filters": {
            "grep": grep,
            "level": norm_level,
        },
        "truncated": truncated_by_cap,
        "lines": matched,
    }
    out = json.dumps(result_payload, ensure_ascii=False)
    if len(out.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        # 二分丢行直到塞得下
        lo, hi = 0, len(matched)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            trial = matched[-mid:] if not head else matched[:mid]
            result_payload["lines"] = trial
            result_payload["matched_lines"] = len(trial)
            result_payload["truncated"] = True
            cand = json.dumps(result_payload, ensure_ascii=False)
            if len(cand.encode("utf-8")) <= _MAX_OUTPUT_BYTES:
                lo = mid
                out = cand
            else:
                hi = mid - 1
        if lo == 0:
            # 单行就爆了，强切单行
            result_payload["lines"] = [{"n": -1, "text": "<line too large; output cap exceeded>"}]
            result_payload["matched_lines"] = 0
            result_payload["truncated"] = True
            out = json.dumps(result_payload, ensure_ascii=False)
    return out


def _count_total_lines(path: Path, file_size: int) -> int:
    """估算文件总行数。小文件精确，大文件用平均行长估算。"""
    if file_size <= 1024 * 1024:
        try:
            with path.open("rb") as fp:
                return sum(1 for _ in fp)
        except OSError:
            return 0
    # 大文件：采样 256KB 算平均行长
    sample = min(256 * 1024, file_size)
    try:
        with path.open("rb") as fp:
            data = fp.read(sample)
        nl = data.count(b"\n")
        if nl == 0:
            return 1
        avg = sample / nl
        return max(1, int(file_size / avg))
    except OSError:
        return 0


# ============================================================================
# handler
# ============================================================================

_CONTEXT_KEYS = {"session_id", "llm_client", "trace_id", "agent_id"}


def _drop(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k not in _CONTEXT_KEYS}


async def _handle_read_logs(**kwargs: Any) -> str:
    args = _drop(kwargs)
    return await read_logs_tool(
        path=args.get("path", ""),
        tail_lines=args.get("tail_lines", _DEFAULT_TAIL_LINES),
        head=bool(args.get("head", False)),
        grep=args.get("grep"),
        level=args.get("level"),
        max_lines=args.get("max_lines"),
    )


__all__ = ["read_logs_tool", "_handle_read_logs"]
