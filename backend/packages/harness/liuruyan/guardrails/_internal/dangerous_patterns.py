"""execute_command / execute_powershell 危险命令 regex 清单。

跟 v1 `backend/tools/defense/dangerous_patterns.py` 内容一致；放到 v2
内是为了不让 harness 反向 import backend.* —— D-4 shim 时 v1 反过来 import 这边。

未来要加新模式：直接改这里，v1 自动跟进。

根据常见终端与运维环境危险命令规范，本地化精简到核心条目。
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

DANGEROUS_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # ─── 文件系统破坏 ───
    (re.compile(r"\brm\s+(-[^\s]*\s+)*/"), "rm 直接作用于根路径"),
    (re.compile(r"\brm\s+-[^\s]*r"), "rm 递归删除（-r/-rf）"),
    (re.compile(r"\brm\s+--recursive\b"), "rm 递归删除（--recursive）"),
    (re.compile(r"\bfind\b.*-delete\b"), "find -delete 批量删除"),
    (re.compile(r"\bxargs\s+.*\brm\b"), "xargs | rm 批量删除"),

    # ─── 权限失控 ───
    (re.compile(r"\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b"), "chmod 开放写权限给所有人"),
    (re.compile(r"\bchown\s+(-[^\s]*)?R\s+root"), "chown 递归改为 root 所有"),

    # ─── 系统配置覆盖 ───
    (re.compile(r">\s*/(etc|usr|bin|sbin|boot|lib)/"), "重定向到系统目录（>）"),
    (re.compile(r">>\s*/(etc|usr|bin|sbin|boot|lib)/"), "追加到系统目录（>>）"),
    (re.compile(r"\btee\b.*\s/(etc|usr|bin|sbin|boot|lib)/"), "tee 写到系统目录"),
    (re.compile(r"\b(cp|mv|install)\b.*\s/etc/"), "cp/mv/install 写到 /etc/"),
    (re.compile(r"\bsed\s+-[^\s]*i.*\s/etc/"), "sed -i 修改 /etc/ 配置"),

    # ─── 远程脚本执行（最常见的攻击姿势）───
    (re.compile(r"\b(curl|wget)\b.*\|\s*(ba|z|k)?sh\b"), "curl/wget 管道到 shell 执行"),
    (re.compile(r"\b(bash|sh|zsh|ksh)\s+<\s*<?\s*\(\s*(curl|wget)\b"), "进程替换执行远程脚本"),

    # ─── shell 注入姿势 ───
    (re.compile(r"\b(bash|sh|zsh|ksh)\s+-[^\s]*c(\s+|$)"), "shell -c 命令注入"),
    (re.compile(r"\b(python[23]?|perl|ruby|node)\s+-[ec]\s+"), "解释器 -e/-c 内联代码"),
    (re.compile(r"\b(python[23]?|perl|ruby|node)\s+<<"), "解释器 heredoc 注入"),
    (re.compile(r"\bchmod\s+\+x\b.*[;&|]+\s*\./"), "chmod +x 后立即执行"),

    # ─── 磁盘/格式化 ───
    (re.compile(r"\bmkfs\b"), "mkfs 格式化文件系统"),
    (re.compile(r"\bdd\s+.*if="), "dd 写设备/磁盘"),
    (re.compile(r">\s*/dev/sd"), "重定向到块设备"),

    # ─── git 破坏性操作 ───
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard 销毁未提交改动"),
    (re.compile(r"\bgit\s+push\b.*--force\b"), "git push --force 重写远程历史"),
    (re.compile(r"\bgit\s+push\b.*-f\b"), "git push -f 重写远程历史"),
    (re.compile(r"\bgit\s+clean\s+-[^\s]*f"), "git clean -f 清理未跟踪文件"),
    (re.compile(r"\bgit\s+branch\s+-D\b"), "git branch -D 强制删分支"),

    # ─── SQL 灾难 ───
    (re.compile(r"\bDROP\s+(TABLE|DATABASE)\b", re.IGNORECASE), "SQL DROP TABLE/DATABASE"),
    (re.compile(r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", re.IGNORECASE), "SQL DELETE 未带 WHERE"),
    (re.compile(r"\bTRUNCATE\s+(TABLE)?\s*\w", re.IGNORECASE), "SQL TRUNCATE"),

    # ─── 进程自杀（防 LLM 干掉自己后端）───
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\bkill\s+-9\s+-1\b"), "kill -9 -1 杀所有进程"),
    (re.compile(r"\bpkill\s+-9\b"), "pkill -9 强制杀进程"),
    (re.compile(r"\b(pkill|killall)\b.*\b(uvicorn|main\.py|fastapi)\b"), "杀后端服务自身"),
    (re.compile(r"\bkill\b.*\$\(\s*pgrep\b"), "kill `pgrep …` 自杀模式"),

    # ─── 系统服务 ───
    (re.compile(r"\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b"), "systemctl 停/禁系统服务"),
]


def detect_dangerous_command(command: str) -> Optional[str]:
    """命中返 description；未命中返 None；非 str / 空 str 返 None。"""
    if not isinstance(command, str) or not command.strip():
        return None
    for pattern, description in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return description
    return None


__all__ = ["DANGEROUS_PATTERNS", "detect_dangerous_command"]
