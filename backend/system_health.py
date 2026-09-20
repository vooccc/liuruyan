"""
系统自检模块

输出维度：
- syntax: backend/*.py 的 AST 语法校验
- tool_insights: tool_stats 深度洞察的问题摘要
- audit_recent: 最近 N 小时 backend 写改审计日志
- disk: backup 目录、jsonl 文件大小
- todos: handover 文档里 pending 的 TODO 标记数

被 /system/health 接口和 scripts/system_health_check.py CLI 共用。
"""
from __future__ import annotations

import ast
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# 阈值
DISK_BACKUPS_WARN_MB = 500
DISK_AUDIT_WARN_MB = 50
AUDIT_RECENT_HOURS = 24
TODO_PATTERN = re.compile(r"\b(?:TODO|FIXME|XXX|\u672a\u505a|\u5f85\u89e3\u51b3|\u5f85\u8865)\b", re.IGNORECASE)


@dataclass
class HealthIssue:
    severity: str  # 'critical' | 'warning' | 'info'
    category: str
    title: str
    detail: str
    suggestion: str = ""


@dataclass
class HealthReport:
    timestamp: float = field(default_factory=time.time)
    severity_counts: Dict[str, int] = field(default_factory=dict)
    syntax: Dict[str, Any] = field(default_factory=dict)
    tool_insights: Dict[str, Any] = field(default_factory=dict)
    audit_recent: Dict[str, Any] = field(default_factory=dict)
    disk: Dict[str, Any] = field(default_factory=dict)
    todos: Dict[str, Any] = field(default_factory=dict)
    issues: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def overall_severity(self) -> str:
        if self.severity_counts.get('critical', 0):
            return 'critical'
        if self.severity_counts.get('warning', 0):
            return 'warning'
        return 'ok'


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _check_syntax(backend_dir: Path) -> tuple[Dict[str, Any], List[HealthIssue]]:
    """AST 校验所有 backend/*.py（递归，跳过 venv/__pycache__/build/dist）"""
    skip_dirs = {'venv', '__pycache__', 'build', 'dist', '.pytest_cache', 'node_modules'}
    files_checked = 0
    failures: List[Dict[str, str]] = []
    for path in backend_dir.rglob('*.py'):
        if any(part in skip_dirs for part in path.parts):
            continue
        files_checked += 1
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                ast.parse(f.read(), filename=str(path))
        except SyntaxError as e:
            failures.append({
                "path": str(path.relative_to(_project_root())),
                "line": e.lineno or 0,
                "message": e.msg or "syntax error",
            })
        except Exception as e:
            failures.append({
                "path": str(path.relative_to(_project_root())),
                "line": 0,
                "message": f"read error: {e}",
            })

    issues = [
        HealthIssue(
            severity='critical',
            category='syntax',
            title=f"{f['path']}:{f['line']} 语法错",
            detail=f["message"],
            suggestion="按 self-evolution-rollback workflow 用 backup_path 回滚该文件。",
        )
        for f in failures
    ]
    return {
        "files_checked": files_checked,
        "failures": failures,
    }, issues


def _check_tool_insights() -> tuple[Dict[str, Any], List[HealthIssue]]:
    try:
        from .tool_stats_insights import compute_insights
    except Exception as e:
        return {"error": f"compute_insights unavailable: {e}"}, []
    try:
        result = compute_insights()
    except Exception as e:
        return {"error": str(e)}, []
    summary = result.get('summary', {})
    issues_raw = result.get('issues', [])
    issues = [
        HealthIssue(
            severity=i['severity'],
            category=f"tool/{i['category']}",
            title=i['title'],
            detail=i['detail'],
            suggestion=i['suggestion'],
        )
        for i in issues_raw
    ]
    return {
        "total_calls": summary.get('total_calls', 0),
        "overall_success_rate": summary.get('overall_success_rate', 0),
        "issues_count": summary.get('issues_count', 0),
        "issues_by_severity": summary.get('issues_by_severity', {}),
    }, issues


def _check_audit_recent(jsonl_path: Path, hours: int = AUDIT_RECENT_HOURS) -> tuple[Dict[str, Any], List[HealthIssue]]:
    """扫审计 jsonl 最近 N 小时的记录"""
    if not jsonl_path.exists():
        return {"exists": False, "recent_count": 0}, []
    cutoff = time.time() - hours * 3600
    recent_count = 0
    by_tool: Counter = Counter()
    by_agent: Counter = Counter()
    dangerous_count = 0
    error_count = 0
    try:
        with open(jsonl_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get('timestamp') or rec.get('ts')
                if isinstance(ts, str):
                    # ISO 时间字符串
                    try:
                        from datetime import datetime
                        ts = datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()
                    except Exception:
                        ts = None
                if not isinstance(ts, (int, float)):
                    continue
                if ts < cutoff:
                    continue
                recent_count += 1
                tool = rec.get('tool_name') or rec.get('tool') or 'unknown'
                by_tool[tool] += 1
                agent = rec.get('agent_id') or 'unknown'
                by_agent[agent] += 1
                if rec.get('dangerous'):
                    dangerous_count += 1
                if rec.get('error') or rec.get('failed'):
                    error_count += 1
    except Exception as e:
        return {"error": str(e)}, []

    issues: List[HealthIssue] = []
    if dangerous_count > 0:
        issues.append(HealthIssue(
            severity='critical',
            category='audit',
            title=f"最近 {hours}h 检测到 {dangerous_count} 次危险写操作",
            detail=f"audit log 中标记为 dangerous 的记录，请审查 {jsonl_path.name}。",
            suggestion="检查 Guardian 配置是否生效；审查具体路径是否在红区外。",
        ))
    if recent_count > 50:
        issues.append(HealthIssue(
            severity='warning',
            category='audit',
            title=f"最近 {hours}h 后端被写改 {recent_count} 次",
            detail=f"主要工具：{', '.join(f'{t}({c})' for t, c in by_tool.most_common(3))}",
            suggestion="确认是否符合预期；自进化频率过高可能掩盖 bug。",
        ))
    return {
        "recent_count": recent_count,
        "by_tool": dict(by_tool),
        "by_agent": dict(by_agent),
        "dangerous_count": dangerous_count,
        "error_count": error_count,
    }, issues


def _check_disk() -> tuple[Dict[str, Any], List[HealthIssue]]:
    info: Dict[str, Any] = {}
    issues: List[HealthIssue] = []
    try:
        from . import paths
        backups_dir = paths.backend_backups_dir()
        audit_path = paths.backend_writes_jsonl_path()
        tool_stats_path = paths.tool_stats_jsonl_path()
    except Exception as e:
        return {"error": str(e)}, []

    def _du(p: Path) -> int:
        if not p.exists():
            return 0
        if p.is_file():
            return p.stat().st_size
        total = 0
        for entry in p.rglob('*'):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
        return total

    backups_bytes = _du(backups_dir)
    audit_bytes = _du(audit_path)
    tool_stats_bytes = _du(tool_stats_path)
    info = {
        "backups_mb": round(backups_bytes / 1024 / 1024, 2),
        "audit_mb": round(audit_bytes / 1024 / 1024, 2),
        "tool_stats_mb": round(tool_stats_bytes / 1024 / 1024, 2),
    }
    if info["backups_mb"] > DISK_BACKUPS_WARN_MB:
        issues.append(HealthIssue(
            severity='warning',
            category='disk',
            title=f"backup 目录 {info['backups_mb']:.1f} MB",
            detail=f"路径 {backups_dir}",
            suggestion="跑 `cleanup_old_backups(retention_days=7)` 清旧；启动时已自动跑，可缩短保留期。",
        ))
    if info["audit_mb"] > DISK_AUDIT_WARN_MB:
        issues.append(HealthIssue(
            severity='info',
            category='disk',
            title=f"backend_writes.jsonl 已 {info['audit_mb']:.1f} MB",
            detail="审计日志单文件偏大",
            suggestion="`rotate_audit_log_if_needed(max_bytes=10*1024*1024)` 已挂启动；按需调阈值。",
        ))
    return info, issues


def _check_todos(docs_dir: Path) -> tuple[Dict[str, Any], List[HealthIssue]]:
    if not docs_dir.exists():
        return {"docs_dir_missing": True}, []
    total_marks = 0
    by_file: List[Dict[str, Any]] = []
    for path in docs_dir.glob('handover_*.md'):
        try:
            text = path.read_text(encoding='utf-8')
        except Exception:
            continue
        marks = TODO_PATTERN.findall(text)
        if marks:
            total_marks += len(marks)
            by_file.append({"path": path.name, "count": len(marks)})
    by_file.sort(key=lambda x: x['count'], reverse=True)
    return {
        "total_marks": total_marks,
        "files_with_todos": len(by_file),
        "top_files": by_file[:5],
    }, []


def run_health_check() -> HealthReport:
    """主入口：跑全部自检，返回 HealthReport"""
    root = _project_root()
    backend_dir = root / "backend"
    docs_dir = root / "_docs"

    report = HealthReport()
    all_issues: List[HealthIssue] = []

    # 1. 语法
    syn_data, syn_issues = _check_syntax(backend_dir)
    report.syntax = syn_data
    all_issues.extend(syn_issues)

    # 2. 工具洞察
    ti_data, ti_issues = _check_tool_insights()
    report.tool_insights = ti_data
    all_issues.extend(ti_issues)

    # 3. 审计
    try:
        from . import paths
        audit_path = paths.backend_writes_jsonl_path()
    except Exception:
        audit_path = root / "backend" / "data" / "backend_writes.jsonl"
    au_data, au_issues = _check_audit_recent(audit_path)
    report.audit_recent = au_data
    all_issues.extend(au_issues)

    # 4. 磁盘
    dk_data, dk_issues = _check_disk()
    report.disk = dk_data
    all_issues.extend(dk_issues)

    # 5. TODO
    td_data, td_issues = _check_todos(docs_dir)
    report.todos = td_data
    all_issues.extend(td_issues)

    sev_order = {'critical': 0, 'warning': 1, 'info': 2}
    all_issues.sort(key=lambda i: (sev_order.get(i.severity, 99), i.category))

    report.issues = [asdict(i) for i in all_issues]
    report.severity_counts = dict(Counter(i.severity for i in all_issues))
    return report


def report_to_dict(report: HealthReport) -> Dict[str, Any]:
    return {
        "timestamp": report.timestamp,
        "overall_severity": report.overall_severity,
        "severity_counts": report.severity_counts,
        "syntax": report.syntax,
        "tool_insights": report.tool_insights,
        "audit_recent": report.audit_recent,
        "disk": report.disk,
        "todos": report.todos,
        "issues": report.issues,
    }
