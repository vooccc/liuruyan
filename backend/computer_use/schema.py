"""Computer Use 工具 schema（对齐 anthropic 公开协议 computer_20250124）。

公开规范：
    https://docs.claude.com/en/docs/agents-and-tools/computer-use

我们的 schema 在 anthropic 14 个 action 基础上扩展，但**保持兼容**：
- 官方 action 全部支持（LLM 训练数据里的调用形式直接能用）
- 扩展 action 走"action 名带 ext_ 前缀 或 走 anthropic 没占的命名"，避免冲突
"""
from __future__ import annotations

# ---- anthropic computer_20250124 官方 action 集合 ----
# 这些名字 LLM 在训练时见过，原样保留
_ANTHROPIC_NATIVE_ACTIONS = [
    "key",
    "hold_key",
    "type",
    "cursor_position",
    "mouse_move",
    "left_mouse_down",
    "left_mouse_up",
    "left_click",
    "left_click_drag",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "scroll",
    "screenshot",
    "wait",
]

# ---- 我们的扩展 action（高 ROI，不与 anthropic 命名冲突） ----
_LIURUYAN_EXTENDED_ACTIONS = [
    "list_displays",        # M6 多屏枚举
    "list_running_apps",    # M6 应用枚举
    "list_windows",         # M6 窗口枚举
    "frontmost_app",        # M6 前台应用
    "read_clipboard",       # M2 剪贴板读
    "write_clipboard",      # M2 剪贴板写
]

ALLOWED_ACTIONS = tuple(_ANTHROPIC_NATIVE_ACTIONS + _LIURUYAN_EXTENDED_ACTIONS)


# anthropic 官方 schema 形状：
# {
#   "type": "computer_20250124",
#   "name": "computer",
#   "display_width_px": <int>,
#   "display_height_px": <int>,
#   "display_number": <int|null>,  # 1-indexed; null = primary
# }
#
# 但 anthropic 用的是 SDK 内置工具类型（type=computer_20250124），不是 input_schema
# 形态——只有 sonnet-3.5 / sonnet-4 等支持 computer-use 的模型才认。
#
# 柳如烟支持任意模型（OpenAI/DeepSeek/Ollama 也要能用），所以我们走**通用 input_schema 路径**：
# 把官方协议的 action 字段平铺成普通工具参数，让所有模型都能调。
COMPUTER_USE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(ALLOWED_ACTIONS),
            "description": (
                "Action to perform. Anthropic-native: key, hold_key, type, cursor_position, "
                "mouse_move, left_mouse_down, left_mouse_up, left_click, left_click_drag, "
                "right_click, middle_click, double_click, triple_click, scroll, screenshot, wait. "
                "Liuruyan extensions: list_displays, list_running_apps, list_windows, "
                "frontmost_app, read_clipboard, write_clipboard."
            ),
        },
        "coordinate": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 2,
            "description": (
                "Pixel coordinates [x, y] in the target display's pixel space. "
                "Required for: mouse_move, left_click, left_click_drag, right_click, "
                "middle_click, double_click, triple_click, left_mouse_down, left_mouse_up, scroll."
            ),
        },
        "text": {
            "type": "string",
            "description": (
                "Text to type (action=type) or key sequence (action=key/hold_key). "
                "Key sequence uses xdotool-style: 'ctrl+c', 'cmd+shift+t', 'Return', 'Escape'. "
                "For action=write_clipboard, this is the clipboard content."
            ),
        },
        "scroll_direction": {
            "type": "string",
            "enum": ["up", "down", "left", "right"],
            "description": "Direction for action=scroll.",
        },
        "scroll_amount": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Number of 'clicks' to scroll (action=scroll). Default 3.",
        },
        "duration": {
            "type": "number",
            "minimum": 0,
            "maximum": 10,
            "description": (
                "Duration in seconds. For action=hold_key (how long to hold) "
                "or action=wait (how long to sleep before screenshot). Capped at 10s."
            ),
        },
        "display_number": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "1-indexed display number. Defaults to primary display. "
                "Use action=list_displays first to discover available displays."
            ),
        },
        "via_clipboard": {
            "type": "boolean",
            "description": (
                "For action=type: if true (default for non-ASCII text), copy text to "
                "clipboard then ctrl+v paste. Required for CJK/Unicode (pyautogui.typewrite "
                "cannot type Chinese characters directly)."
            ),
        },
    },
    "required": ["action"],
}
