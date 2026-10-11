"""
Git 工具：status / log / diff / blame

为啥要专门工具化（vs 让模型用 execute_command 拼命令）：
- 结构化输出：不用每次 LLM 解析自由文本
- 参数校验：路径、ref 格式校验在 Python 层做掉
- 安全：subprocess 显式 list 形式，避免 shell injection
- 一致性：跨平台行为一致（Windows / Unix）

设计：
- 所有命令底层走 `git --no-pager <subcmd>` 子进程
- 默认 cwd = 调用方的 os.getcwd()（后端启动后一般是项目根）
- 失败统一返回 {"error": "..."}；成功返回结构化 dict
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 默认 timeout 30s（git 大仓 log 可能慢）
_DEFAULT_TIMEOUT = 30
# blame / log / diff 输出截断
_MAX_OUTPUT_BYTES = 200_000


def _run_git(args: List[str], cwd: Optional[str] = None, timeout: int = _DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """统一 git 命令封装。返回 {"stdout": str, "stderr": str, "returncode": int} 或 {"error": str}

    cwd 默认 None → 继承调用方的 os.getcwd()。后端正常启动时 cwd 是项目根，
    测试场景 os.chdir(临时 repo) 后也能直接生效。
    """
    cmd = ["git", "--no-pager"] + args
    try:
        run_kwargs = {
            "cwd": cwd,  # None = 继承
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            "check": False,
        }
        if os.name == "nt":
            run_kwargs["creationflags"] = 0x08000000
        result = subprocess.run(cmd, **run_kwargs)
        return {
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "returncode": result.returncode,
        }
    except FileNotFoundError:
        return {"error": "git executable not found in PATH"}
    except subprocess.TimeoutExpired:
        return {"error": f"git command timed out after {timeout}s: {' '.join(cmd)}"}
    except Exception as exc:
        logger.exception("git subprocess failed")
        return {"error": f"git subprocess failed: {exc}"}


def _truncate(text: str, max_bytes: int = _MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    if len(text.encode("utf-8", errors="ignore")) <= max_bytes:
        return text, False
    # 字节级截断后从最近 \n 切回去
    encoded = text.encode("utf-8", errors="ignore")[:max_bytes]
    truncated = encoded.decode("utf-8", errors="ignore")
    last_nl = truncated.rfind("\n")
    if last_nl > 0:
        truncated = truncated[: last_nl + 1]
    return truncated, True


# ============================================================================
# git_status
# ============================================================================

def git_status_tool() -> str:
    """工作区状态：modified / added / untracked / deleted / branch"""
    res = _run_git(["status", "--porcelain=v1", "--branch"])
    if "error" in res:
        return json.dumps({"error": res["error"]})
    if res["returncode"] != 0:
        return json.dumps({"error": f"git status failed: {res['stderr']}"})

    lines = res["stdout"].splitlines()
    branch = ""
    modified: List[str] = []
    added: List[str] = []
    deleted: List[str] = []
    untracked: List[str] = []
    renamed: List[Dict[str, str]] = []

    for line in lines:
        if line.startswith("##"):
            branch = line[3:].split("...")[0].strip()
            continue
        if len(line) < 3:
            continue
        code = line[:2]
        path = line[3:].strip()
        if code == "??":
            untracked.append(path)
        elif "M" in code:
            modified.append(path)
        elif "A" in code:
            added.append(path)
        elif "D" in code:
            deleted.append(path)
        elif "R" in code:
            # "old -> new"
            if " -> " in path:
                old, new = path.split(" -> ", 1)
                renamed.append({"from": old, "to": new})
            else:
                modified.append(path)
        else:
            modified.append(path)

    return json.dumps({
        "branch": branch,
        "modified": modified,
        "added": added,
        "deleted": deleted,
        "untracked": untracked,
        "renamed": renamed,
        "clean": not (modified or added or deleted or untracked or renamed),
    }, ensure_ascii=False)


# ============================================================================
# git_log
# ============================================================================

def git_log_tool(
    path: Optional[str] = None,
    limit: int = 10,
    author: Optional[str] = None,
    since: Optional[str] = None,
) -> str:
    """最近 N 条 commit。"""
    if limit < 1 or limit > 200:
        return json.dumps({"error": "limit must be between 1 and 200"})

    # 用单行 format 自定义分隔符避免 commit message 含 \t 干扰
    SEP = "\x1f"
    REC = "\x1e"
    fmt = SEP.join(["%H", "%an", "%ae", "%ad", "%s"]) + REC
    args = ["log", f"--max-count={limit}", "--date=iso-strict", f"--format={fmt}"]
    if author:
        args.append(f"--author={author}")
    if since:
        args.append(f"--since={since}")
    if path:
        args.extend(["--", path])

    res = _run_git(args)
    if "error" in res:
        return json.dumps({"error": res["error"]})
    if res["returncode"] != 0:
        return json.dumps({"error": f"git log failed: {res['stderr']}"})

    commits: List[Dict[str, str]] = []
    for raw in res["stdout"].split(REC):
        raw = raw.strip("\n")
        if not raw:
            continue
        parts = raw.split(SEP)
        if len(parts) >= 5:
            commits.append({
                "hash": parts[0],
                "author": parts[1],
                "email": parts[2],
                "date": parts[3],
                "message": parts[4],
            })

    return json.dumps({"commits": commits, "count": len(commits)}, ensure_ascii=False)


# ============================================================================
# git_diff
# ============================================================================

def git_diff_tool(
    ref_a: Optional[str] = None,
    ref_b: Optional[str] = None,
    path: Optional[str] = None,
    staged: bool = False,
    stat_only: bool = False,
) -> str:
    """diff 输出（unified format）。

    形态：
    - 无 ref_a, 无 ref_b → 工作区 vs HEAD
    - staged=true → 暂存区 vs HEAD
    - 有 ref_a, 无 ref_b → ref_a vs 工作区
    - 有 ref_a, 有 ref_b → ref_a vs ref_b
    """
    args = ["diff"]
    if staged:
        args.append("--staged")
    if stat_only:
        args.append("--stat")
    if ref_a:
        args.append(ref_a)
    if ref_b:
        args.append(ref_b)
    if path:
        args.extend(["--", path])

    res = _run_git(args)
    if "error" in res:
        return json.dumps({"error": res["error"]})
    if res["returncode"] not in (0, 1):  # 0 = no diff, 1 = differences exist (with --exit-code, but we don't use it)
        return json.dumps({"error": f"git diff failed: {res['stderr']}"})

    diff_text, truncated = _truncate(res["stdout"])
    return json.dumps({
        "diff": diff_text,
        "empty": not diff_text.strip(),
        "truncated": truncated,
        "stat_only": stat_only,
    }, ensure_ascii=False)


# ============================================================================
# git_blame
# ============================================================================

def git_blame_tool(
    path: str,
    line_start: Optional[int] = None,
    line_end: Optional[int] = None,
) -> str:
    """每行的最后修改人 + commit hash。"""
    if not path:
        return json.dumps({"error": "path is required"})

    args = ["blame", "--line-porcelain"]
    if line_start is not None:
        ls = max(1, int(line_start))
        le = max(ls, int(line_end)) if line_end is not None else ls
        args.extend(["-L", f"{ls},{le}"])
    args.append(path)

    res = _run_git(args)
    if "error" in res:
        return json.dumps({"error": res["error"]})
    if res["returncode"] != 0:
        return json.dumps({"error": f"git blame failed: {res['stderr']}"})

    # porcelain 格式按 commit-hash 行起始解析
    entries: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    line_no = 0
    for raw in res["stdout"].splitlines():
        if raw.startswith("\t"):
            current["content"] = raw[1:]
            current["line"] = line_no
            entries.append(current)
            current = {}
            continue
        parts = raw.split(" ", 1)
        head = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
            # 新一行的开头："<hash> <orig-line> <final-line> [n-lines]"
            chunks = rest.split(" ")
            try:
                line_no = int(chunks[1]) if len(chunks) >= 2 else 0
            except (ValueError, IndexError):
                line_no = 0
            current = {"hash": head}
        elif head == "author":
            current["author"] = rest
        elif head == "author-mail":
            current["author_mail"] = rest.strip("<>")
        elif head == "author-time":
            try:
                current["author_time"] = int(rest)
            except ValueError:
                pass
        elif head == "summary":
            current["summary"] = rest

    return json.dumps({
        "path": path,
        "entries": entries,
        "count": len(entries),
    }, ensure_ascii=False)


# ============================================================================
# handler 适配
# ============================================================================

_CONTEXT_KEYS = {"agent_id", "session_id", "llm_client", "trace_id"}


def _drop_context(kwargs):
    return {k: v for k, v in kwargs.items() if k not in _CONTEXT_KEYS}


def _handle_git_status(**kwargs):
    # git_status 不接受任何参数
    return git_status_tool()


def _handle_git_log(**kwargs):
    return git_log_tool(**_drop_context(kwargs))


def _handle_git_diff(**kwargs):
    return git_diff_tool(**_drop_context(kwargs))


def _handle_git_blame(**kwargs):
    return git_blame_tool(**_drop_context(kwargs))
