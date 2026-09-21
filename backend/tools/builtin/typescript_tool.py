"""
check_typescript：调 frontend 自带的 tsc 做类型检查（结构化错误）

为啥不全文写 LSP：
- frontend/ 已经有 typescript 装好（vite + tsc）
- 直接 npx tsc --noEmit 几秒钟出全量诊断，对当前规模够用
- LSP 真正价值在"按文件按位置"的精确诊断 + 跳定义；那个工程量按周计

设计：
- 默认对整个 frontend/ 跑 `tsc --noEmit -p tsconfig.json`
- path 可选：只检查某文件（仍走全项目 tsc，但筛 path 相关的报错）
- 输出：{ ok, errors: [{file, line, col, code, severity, message}], total }
- 整批 timeout 60s（大项目可能 30+s）

实现细节：
- subprocess 跑 frontend/node_modules/.bin/tsc.cmd（Windows）
- 解析 tsc 输出格式："path(line,col): error TS2304: Cannot find ..."
- 没装 tsc 或 frontend 不存在 → {"error": "..."} 而不是炸
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 整批 timeout
_DEFAULT_TIMEOUT = 90
# 单次返回最多多少条诊断
_MAX_ERRORS = 200
# tsc 错误行 regex
# 例: src/foo.ts(12,7): error TS2304: Cannot find name 'foo'.
_TSC_LINE_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+"
    r"(?P<severity>error|warning)\s+(?P<code>TS\d+):\s+(?P<message>.*)$"
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _find_frontend_dir() -> Optional[Path]:
    candidates = [
        _project_root() / "frontend",
        _project_root() / "ui",
        _project_root() / "web",
    ]
    for c in candidates:
        if (c / "tsconfig.json").exists():
            return c
    return None


def _resolve_tsc(frontend: Path) -> Optional[List[str]]:
    """返回可执行 tsc 的命令前缀（list 形式）"""
    is_windows = sys.platform == "win32"
    candidates = [
        frontend / "node_modules" / ".bin" / ("tsc.cmd" if is_windows else "tsc"),
        frontend / "node_modules" / "typescript" / "bin" / "tsc",
    ]
    for c in candidates:
        if c.exists():
            if c.suffix == ".cmd":
                return [str(c)]
            # bin/tsc 是 JS shebang 脚本，需要 node
            if "bin" in c.parts and c.name == "tsc":
                node_exe = "node"
                return [node_exe, str(c)]
            return [str(c)]
    # 尝试 npx 兜底
    return None


def check_typescript_tool(
    path: Optional[str] = None,
    project: Optional[str] = None,
) -> str:
    frontend = _find_frontend_dir()
    if not frontend:
        return json.dumps({
            "error": "no frontend tsconfig found (looked in frontend/, ui/, web/)",
        })

    tsc_cmd = _resolve_tsc(frontend)
    if not tsc_cmd:
        return json.dumps({
            "error": "tsc not found in frontend/node_modules/.bin/. "
                     "Run `npm install` in frontend/ first.",
        })

    # 用项目方式跑（保证 paths/baseUrl 等设置生效）
    project_path = project or "tsconfig.json"
    cmd = tsc_cmd + ["--noEmit", "--pretty", "false", "-p", project_path]

    try:
        run_kwargs = {
            "cwd": str(frontend),
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": _DEFAULT_TIMEOUT,
            "check": False,
        }
        if os.name == "nt":
            run_kwargs["creationflags"] = 0x08000000
        result = subprocess.run(cmd, **run_kwargs)
    except FileNotFoundError as exc:
        return json.dumps({"error": f"tsc launch failed: {exc}"})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"tsc timed out after {_DEFAULT_TIMEOUT}s"})
    except Exception as exc:
        logger.exception("tsc subprocess failed")
        return json.dumps({"error": f"tsc subprocess failed: {exc}"})

    # tsc 把诊断输出到 stdout（而非 stderr），返回码 0=干净，非 0=有错
    raw = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    errors: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        m = _TSC_LINE_RE.match(line.strip())
        if not m:
            continue
        errors.append({
            "file": m.group("file").replace("\\", "/"),
            "line": int(m.group("line")),
            "col": int(m.group("col")),
            "severity": m.group("severity"),
            "code": m.group("code"),
            "message": m.group("message"),
        })

    # 按 path 过滤（如指定）
    if path:
        path_norm = path.replace("\\", "/")
        # 用 endswith 容忍绝对/相对路径混杂
        errors = [
            e for e in errors
            if e["file"].endswith(path_norm) or path_norm.endswith(e["file"])
        ]

    truncated = False
    if len(errors) > _MAX_ERRORS:
        errors = errors[:_MAX_ERRORS]
        truncated = True

    return json.dumps({
        "ok": len(errors) == 0 and result.returncode == 0,
        "frontend": str(frontend),
        "project": project_path,
        "returncode": result.returncode,
        "errors": errors,
        "total": len(errors),
        "truncated": truncated,
    }, ensure_ascii=False)


def _handle_check_typescript(**kwargs):
    return check_typescript_tool(
        path=kwargs.get("path"),
        project=kwargs.get("project"),
    )
