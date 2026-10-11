"""SubAgent 结果落盘工具。

为什么需要：
- 三个专家产出在前端 UI 终态后会被关闭丢失，无法事后复盘
- 用户报错时只能截图 + OCR 给我看，路径反斜杠之类细节经常被识别坏
- 后端结构化 print 信息散在 stdout 不好检索，且只有最后一段

设计：
- spawn_parallel 完成后批量 dump 全部 SubAgentResult 到本地文件
- 每个 result 一个 .md 文件：元数据 head + 完整 output 原文
- 默认开启，`LIURUYAN_SUBAGENT_DUMP=0` 可关
- 自动保留最近 N 个 run，超出时从最旧开始删（防磁盘爆）
- 失败也 dump（错误信息和中途 partial_output 最有调试价值）

文件位置：仓库根/docs/subagent_runs/，已 gitignore。
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Optional

if TYPE_CHECKING:
    # 避免循环 import：SubAgentResult 在 engine.py 定义
    from .engine import SubAgentResult

logger = logging.getLogger(__name__)


# 保留最近多少 run（每 run 一次 spawn_parallel 一组文件）。超过自动删旧。
# 单个 run 一般 1~3 个文件，单文件几 KB ~ 50 KB，30 个 run 也就 < 5 MB。
_KEEP_LAST_RUNS = 30


def _is_enabled() -> bool:
    """`LIURUYAN_SUBAGENT_DUMP=0` 显式关，其余默认开。

    测试环境（pytest 跑测试套时）默认禁用——避免 chunk_contract 等 5 个
    `engine.spawn_parallel(["explore"], "ping")` 测试每跑一次就污染 docs/。
    pytest 设 `PYTEST_CURRENT_TEST` 环境变量给 fixture 用，借它当探针。
    若测试本身要测 dump 行为，可在测试内显式 setenv("LIURUYAN_SUBAGENT_DUMP", "1")。
    """
    explicit = os.environ.get("LIURUYAN_SUBAGENT_DUMP")
    if explicit is not None:
        return explicit != "0"
    # 没显式设置 → 测试环境默认关，生产默认开
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return True


def _output_dir() -> Path:
    """落盘目录：仓库根/data/subagent_runs/。

    定位：本文件 backend/subagent/result_dump.py → 上溯 3 层到仓库根。
    """
    here = Path(__file__).resolve()
    repo_root = here.parents[2]  # backend/subagent/result_dump.py → repo
    target = repo_root / "data" / "subagent_runs"
    target.mkdir(parents=True, exist_ok=True)
    return target


_TIMESTAMP_RE = re.compile(r"^(\d{8}_\d{6})_")


def _prune_old(target_dir: Path) -> None:
    """保留最近 N 个 run（按文件名时间戳排序），多余的删掉。

    单 run 多文件：所有同 run_id 前缀的文件视为一组。简化做法：把所有 .md 文件
    按时间戳分组（前缀 'YYYYMMDD_HHMMSS_<runid>_...'），保留最近 _KEEP_LAST_RUNS 组。
    """
    try:
        files = list(target_dir.glob("*.md"))
        if not files:
            return
        # group by 'YYYYMMDD_HHMMSS_<runid>' prefix
        groups: dict[str, List[Path]] = {}
        for f in files:
            # filename format: YYYYMMDD_HHMMSS_<runid>_<agent_type>_<status>.md
            parts = f.stem.split("_")
            if len(parts) < 4:
                continue
            run_key = "_".join(parts[:3])  # date_time_runid
            groups.setdefault(run_key, []).append(f)

        # 按 run_key 排序（字典序==时间序）
        sorted_keys = sorted(groups.keys())
        excess = len(sorted_keys) - _KEEP_LAST_RUNS
        if excess <= 0:
            return
        for key in sorted_keys[:excess]:
            for f in groups[key]:
                try:
                    f.unlink()
                except OSError:
                    pass
    except Exception:
        # 清理失败不影响主流程
        logger.debug("[subagent-dump] prune old files failed", exc_info=True)


def _format_md(
    result: "SubAgentResult",
    run_id: str,
    task: str,
    working_directory: Optional[str] = None,
) -> str:
    """把 SubAgentResult 渲染成 markdown 文档。"""
    lines = [
        f"# SubAgent Result Dump",
        "",
        f"- **run_id**: `{run_id}`",
        f"- **agent_id**: `{result.agent_id}`",
        f"- **agent_type**: `{result.agent_type}`",
        f"- **status**: `{result.status.value if hasattr(result.status, 'value') else result.status}`",
        f"- **elapsed**: {result.elapsed:.2f}s",
        f"- **tokens_used**: {result.tokens_used}",
        f"- **tools_called**: {result.tools_called}",
        f"- **timestamp**: {datetime.now().isoformat(timespec='seconds')}",
        # working_directory 是诊断"路径解析事故"的关键字段——caller 到底传没传？
        f"- **working_directory**: `{working_directory!r}`",
    ]
    if result.error:
        lines.append(f"- **error**: `{result.error}`")
    lines.append("")
    lines.append("## Task (caller's input)")
    lines.append("")
    lines.append("```")
    lines.append(task or "(empty)")
    lines.append("```")
    lines.append("")
    lines.append("## Output (raw, full)")
    lines.append("")
    lines.append("```")
    lines.append(result.output or "(empty)")
    lines.append("```")
    return "\n".join(lines)


def dump_results(
    results: Iterable["SubAgentResult"],
    *,
    task: str = "",
    run_id: str = "",
    working_directory: Optional[str] = None,
) -> List[Path]:
    """把一组 SubAgentResult 写入磁盘。

    Args:
        results: spawn_parallel 返回的结果列表
        task: 这次 spawn 的任务描述（caller 传给 subagent 的 user message）
        run_id: 可选，本次 spawn 的标识。空则自动生成 8 字符 hex。
        working_directory: caller 传给 spawn_subagent 的目标项目根目录，
            写到 dump 元数据里供事后诊断"路径解析事故是不是因为 caller 没传"

    Returns: 实际写入的文件路径列表。`LIURUYAN_SUBAGENT_DUMP=0` 时返空。
    """
    if not _is_enabled():
        return []

    if not run_id:
        run_id = f"{int(time.time() * 1000):x}"[-8:]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_dir = _output_dir()
    written: List[Path] = []

    for result in results:
        try:
            status_str = (
                result.status.value
                if hasattr(result.status, "value") else str(result.status)
            )
            filename = f"{timestamp}_{run_id}_{result.agent_type}_{status_str}.md"
            path = target_dir / filename
            path.write_text(
                _format_md(
                    result, run_id=run_id, task=task,
                    working_directory=working_directory,
                ),
                encoding="utf-8",
            )
            written.append(path)
        except Exception:
            logger.exception(
                "[subagent-dump] failed to dump result for %s",
                getattr(result, "agent_id", "?"),
            )

    if written:
        logger.info(
            "[subagent-dump] wrote %d result(s) to %s (run_id=%s)",
            len(written), target_dir, run_id,
        )
    _prune_old(target_dir)
    return written
