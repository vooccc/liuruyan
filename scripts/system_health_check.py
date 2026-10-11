"""
系统自检 CLI

用法:
    python scripts/system_health_check.py
    python scripts/system_health_check.py --severity critical
    python scripts/system_health_check.py --json

退出码:
    0 = 全部 OK
    1 = 有 warning
    2 = 有 critical
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows GBK 控制台兼容：强制 stdout utf-8，避免 unicode 字符炸
try:
    sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.system_health import run_health_check, report_to_dict  # noqa: E402

SEVERITY_COLORS = {
    'critical': '\033[91m',
    'warning': '\033[93m',
    'info': '\033[96m',
    'ok': '\033[92m',
}
RESET = '\033[0m'
BOLD = '\033[1m'
DIM = '\033[90m'


def _c(text: str, color: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{RESET}"


def main() -> int:
    p = argparse.ArgumentParser(description="\u67f3\u5982\u70df \u7cfb\u7edf\u81ea\u68c0")
    p.add_argument('--severity', default='critical,warning,info',
                   help='\u8fc7\u6ee4\u95ee\u9898\u4e25\u91cd\u5ea6\uff08\u9017\u53f7\u5206\u9694\uff09')
    p.add_argument('--json', action='store_true', help='\u7eaf JSON \u8f93\u51fa\uff08\u673a\u5668\u53ef\u8bfb\uff09')
    args = p.parse_args()

    report = run_health_check()
    data = report_to_dict(report)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        sev_filter = {s.strip() for s in args.severity.split(',') if s.strip()}
        _render(data, sev_filter)

    overall = data['overall_severity']
    return {'critical': 2, 'warning': 1, 'ok': 0}.get(overall, 0)


def _render(data: dict, sev_filter: set) -> None:
    overall = data['overall_severity']
    overall_color = SEVERITY_COLORS.get(overall, '')
    sc = data['severity_counts']
    print()
    print(_c(BOLD + "\u2550" * 70 + RESET, ''))
    print(_c(BOLD + f"\u67f3\u5982\u70df \u7cfb\u7edf\u81ea\u68c0\u62a5\u544a   \u603b\u4f53\u72b6\u6001\uff1a" + RESET, '') + _c(BOLD + overall.upper() + RESET, overall_color))
    print(_c(BOLD + "\u2550" * 70 + RESET, ''))
    print(
        f"  problems: "
        f"{_c(str(sc.get('critical', 0)) + ' critical', SEVERITY_COLORS['critical'])}  "
        f"{_c(str(sc.get('warning', 0)) + ' warning', SEVERITY_COLORS['warning'])}  "
        f"{_c(str(sc.get('info', 0)) + ' info', SEVERITY_COLORS['info'])}"
    )

    # 各模块摘要
    print()
    print(_c(BOLD + "\u3010\u8bed\u6cd5\u3011" + RESET, ''))
    syn = data['syntax']
    if syn.get('failures'):
        print(_c(f"  \u2717 {len(syn['failures'])} \u4e2a\u6587\u4ef6\u8bed\u6cd5\u9519", SEVERITY_COLORS['critical']))
        for f in syn['failures'][:5]:
            print(f"    - {f['path']}:{f['line']} {f['message']}")
    else:
        print(_c(f"  \u2713 \u68c0\u67e5\u4e86 {syn.get('files_checked', 0)} \u4e2a\u6587\u4ef6\uff0c\u5168\u90e8\u901a\u8fc7", SEVERITY_COLORS['ok']))

    print()
    print(_c(BOLD + "\u3010\u5de5\u5177\u8c03\u7528\u753b\u50cf\u3011" + RESET, ''))
    ti = data['tool_insights']
    if ti.get('error'):
        print(_c(f"  ! {ti['error']}", DIM))
    elif ti.get('total_calls') == 0:
        print(_c("  \u65e0\u8c03\u7528\u8bb0\u5f55", DIM))
    else:
        print(f"  \u603b\u8c03\u7528 {ti['total_calls']:,} \u00b7 \u6210\u529f\u7387 {ti['overall_success_rate']*100:.2f}% \u00b7 \u95ee\u9898 {ti['issues_count']}")

    print()
    print(_c(BOLD + f"\u3010\u6700\u8fd1 {24}h \u540e\u7aef\u5199\u5165\u3011" + RESET, ''))
    au = data['audit_recent']
    if au.get('error'):
        print(_c(f"  ! {au['error']}", DIM))
    elif not au.get('recent_count'):
        print(_c("  \u65e0\u5199\u5165", DIM))
    else:
        print(f"  \u8c03\u7528 {au['recent_count']} \u6b21 \u00b7 \u5371\u9669 {au.get('dangerous_count', 0)} \u00b7 \u9519\u8bef {au.get('error_count', 0)}")
        if au.get('by_tool'):
            top = sorted(au['by_tool'].items(), key=lambda x: -x[1])[:3]
            print("  \u5e38\u7528\u5de5\u5177: " + ", ".join(f"{t}({c})" for t, c in top))

    print()
    print(_c(BOLD + "\u3010\u78c1\u76d8\u3011" + RESET, ''))
    dk = data['disk']
    if dk.get('error'):
        print(_c(f"  ! {dk['error']}", DIM))
    else:
        print(f"  backups {dk.get('backups_mb', 0):.2f} MB \u00b7 audit {dk.get('audit_mb', 0):.2f} MB \u00b7 tool_stats {dk.get('tool_stats_mb', 0):.2f} MB")

    print()
    print(_c(BOLD + "\u3010 TODO \u3011" + RESET, ''))
    td = data['todos']
    if td.get('docs_dir_missing'):
        print(_c("  ! docs 目录不存在", DIM))
    elif td.get('total_marks', 0) == 0:
        print(_c("  \u65e0\u672a\u5904\u7406 TODO", DIM))
    else:
        print(f"  {td['total_marks']} \u4e2a\u6807\u8bb0  \u00b7  \u6db5\u76d6 {td['files_with_todos']} \u4efd\u4ea4\u4ed8")
        for f in td.get('top_files', [])[:3]:
            print(f"    - {f['path']}: {f['count']} \u6761")

    # 问题清单
    issues = [i for i in data.get('issues', []) if i['severity'] in sev_filter]
    if issues:
        print()
        print(_c(BOLD + f"\u3010\u95ee\u9898\u6e05\u5355\uff08{len(issues)}\uff09\u3011" + RESET, ''))
        for idx, issue in enumerate(issues, 1):
            sev = issue['severity']
            print()
            print(f"  {idx}. {_c('[' + sev.upper() + ']', SEVERITY_COLORS.get(sev, ''))} "
                  f"{_c(issue['title'], BOLD)}")
            print(f"     \u5206\u7c7b\u3001{issue['category']}")
            print(f"     {issue['detail']}")
            if issue.get('suggestion'):
                print(_c(f"     \u5efa\u8bae\u3001{issue['suggestion']}", DIM))

    print()


if __name__ == '__main__':
    raise SystemExit(main())
