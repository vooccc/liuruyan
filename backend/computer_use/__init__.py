"""Computer Use：让 LLM 跨平台控制屏幕、键盘、鼠标。

协议契约对齐 anthropic 公开规范 `computer_20250124`：
    https://docs.claude.com/en/docs/agents-and-tools/computer-use

形态分层：
    backend/tools/builtin/computer_use_tool.py     ← 工具注册 + LLM handler
    backend/computer_use/executor.py               ← 高层 API（screenshot/click/type/...）
    backend/computer_use/sidecar.py                ← spawn-per-call subprocess 隔离
    backend/computer_use/lock.py                   ← 单实例锁（lockfile + sessionId + signal-0）
    backend/computer_use/safety.py                 ← 白名单/黑名单/坐标越界/限流
    backend/computer_use/audit.py                  ← jsonl 审计 + replay
    backend/computer_use/diff.py                   ← 前后 screenshot 对比
    backend/computer_use/helpers/                  ← 平台特化 sidecar 主程序
        ├── _common.py
        ├── win.py       ← Windows: pyautogui + win32 + mss
        ├── mac.py       ← macOS:   pyautogui + Quartz/AppleScript + mss
        └── linux.py     ← Linux:   pyautogui + Xlib + mss

设计原则：
1. 进程隔离：每个 action spawn 独立 sidecar 进程，挂了不会牵连 backend
2. 单实例：跨进程 lockfile 防多 session 同时控屏
3. 视觉闭环：每次 screenshot 走 _content_blocks 让 LLM 直接看图（沿用 read_file 的图像协议）
4. 中文友好：type 走剪贴板粘贴模式，跳过 pyautogui.typewrite 的 unicode 限制
5. 安全可控：审计 jsonl + 前端实时停止按钮 + settings 总开关
"""

from backend.computer_use.executor import (
    ComputerUseBlocked,
    ComputerUseDenied,
    ComputerUseExecutor,
    ComputerUseUnsupported,
    get_executor,
)
from backend.computer_use.safety import (
    ComputerUsePolicy,
    SafetyDenied,
    get_policy,
    set_policy,
)
from backend.computer_use.audit import audit_path, read_recent
from backend.computer_use.schema import (
    COMPUTER_USE_TOOL_SCHEMA,
    ALLOWED_ACTIONS,
)

__all__ = [
    # executor 层
    "ComputerUseExecutor",
    "ComputerUseBlocked",
    "ComputerUseDenied",
    "ComputerUseUnsupported",
    "get_executor",
    # safety 层
    "ComputerUsePolicy",
    "SafetyDenied",
    "get_policy",
    "set_policy",
    # audit 层
    "audit_path",
    "read_recent",
    # schema
    "COMPUTER_USE_TOOL_SCHEMA",
    "ALLOWED_ACTIONS",
]
