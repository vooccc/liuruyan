"""computer_use 工具：让 LLM 跨平台控制屏幕、键盘、鼠标。

协议契约对齐 anthropic 公开规范 `computer_20250124`：
    https://docs.claude.com/en/docs/agents-and-tools/computer-use

实现见 backend/computer_use/。M1 范围：
- screenshot     — 截当前屏 → image block（_content_blocks 协议）走 vision 流派 2
- list_displays  — 多屏枚举

未实现的 action（M2+ 增量补齐）会返 friendly error，让 LLM 不会瞎调。

返回协议两类：
1. screenshot 走 _content_blocks（让 LLM 直接看截图）
2. 其他 action 走普通 JSON 字符串（含 _runtime_block sentinel 让前端可视化）
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from backend.computer_use import schema as cu_schema
from backend.computer_use.executor import (
    ComputerUseBlocked,
    ComputerUseDenied,
    ComputerUseUnsupported,
    get_executor,
)

logger = logging.getLogger(__name__)


# M1+M2+M6 实现的 action 集合（覆盖 anthropic computer_20250124 全部 16 个 + 扩展枚举）
# M3 加 macOS/Linux 的同套 action
_IMPLEMENTED_ACTIONS = frozenset({
    # M1
    "screenshot",
    "list_displays",
    # M6: 枚举
    "list_windows",
    "list_running_apps",
    # M2: 鼠标
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "mouse_move",
    "cursor_position",
    "left_mouse_down",
    "left_mouse_up",
    "left_click_drag",
    "scroll",
    # M2: 键盘
    "key",
    "hold_key",
    "type",
    # M2: 剪贴板
    "read_clipboard",
    "write_clipboard",
    # M2: 等待
    "wait",
})

# 哪些 action 必须带 coordinate
_REQUIRES_COORDINATE = frozenset({
    "left_click", "right_click", "middle_click",
    "double_click", "triple_click",
    "mouse_move", "left_click_drag",
})

# 哪些 action 必须带 text
_REQUIRES_TEXT = frozenset({"key", "hold_key", "type", "write_clipboard"})


def _err(message: str, *, code: str = "computer_use_error") -> str:
    return json.dumps({"error": message, "code": code}, ensure_ascii=False)


async def computer_use_tool(
    action: str,
    *,
    coordinate: Optional[list] = None,
    text: Optional[str] = None,
    scroll_direction: Optional[str] = None,
    scroll_amount: Optional[int] = None,
    duration: Optional[float] = None,
    display_number: Optional[int] = None,
    via_clipboard: Optional[bool] = None,
    session_id: Optional[str] = None,
) -> str:
    # ---- 参数预检 ----
    if not action or not isinstance(action, str):
        return _err("action is required (string)", code="bad_args")
    action = action.strip()
    if action not in cu_schema.ALLOWED_ACTIONS:
        return _err(
            f"unknown action {action!r}. Allowed: {list(cu_schema.ALLOWED_ACTIONS)}",
            code="unknown_action",
        )
    if not session_id:
        return _err(
            "session_id missing in tool call context — Computer Use requires a session "
            "for instance locking. This is a backend wiring bug; report it.",
            code="no_session",
        )

    if action not in _IMPLEMENTED_ACTIONS:
        return _err(
            f"action {action!r} not implemented yet. See backend/computer_use/__init__.py "
            f"for the roadmap (M3 = macOS/Linux helper, M6 = list_running_apps etc.).",
            code="not_implemented",
        )

    # ---- 必填参数预检（早 fail，少踏 sidecar 一次往返） ----
    if action in _REQUIRES_COORDINATE:
        if not isinstance(coordinate, list) or len(coordinate) != 2:
            return _err(
                f"action {action!r} requires 'coordinate' = [x, y] (two integers)",
                code="missing_coordinate",
            )
    if action in _REQUIRES_TEXT and not (isinstance(text, str) and text):
        return _err(
            f"action {action!r} requires 'text' (non-empty string)",
            code="missing_text",
        )

    # ---- 执行 ----
    executor = get_executor(session_id)

    try:
        if action == "screenshot":
            return await _do_screenshot(executor, display_number=display_number)
        if action == "list_displays":
            return await _do_list_displays(executor)
        if action == "list_windows":
            return await _do_list_windows(executor)
        if action == "list_running_apps":
            return await _do_list_running_apps(executor)
        # M2 通用透传：构造 sidecar payload，调 executor.run_action()
        return await _do_generic_action(
            executor,
            action=action,
            coordinate=coordinate,
            text=text,
            scroll_direction=scroll_direction,
            scroll_amount=scroll_amount,
            duration=duration,
            via_clipboard=via_clipboard,
        )
    except ComputerUseDenied as exc:
        # 用户/策略明确拒：disabled / rate_limit / out_of_bounds / not_allowed
        return _err(str(exc), code=exc.code)
    except ComputerUseUnsupported as exc:
        return _err(str(exc), code="unsupported_platform")
    except ComputerUseBlocked as exc:
        return _err(str(exc), code="blocked_by_other_session")
    except RuntimeError as exc:
        logger.warning("[computer_use] action=%s failed: %s", action, exc)
        return _err(f"{action} failed: {exc}", code="runtime_error")


# ---- screenshot ----
async def _do_screenshot(executor, *, display_number: Optional[int]) -> str:
    """截屏 → 返回带 _content_blocks sentinel 的 JSON。"""
    result = await executor.screenshot(
        display_number=display_number,
        format="png",
        max_width=1568,  # anthropic vision 推荐上限，超过会被服务端 resize 反而费 token
    )
    image_b64 = result.get("image_b64")
    media_type = result.get("media_type", "image/png")
    width = result.get("width")
    height = result.get("height")
    byte_size = result.get("byte_size")
    chosen_display = result.get("display_number")

    if not image_b64:
        return _err("screenshot returned empty image data", code="empty_image")

    summary_text = (
        f"Screenshot of display #{chosen_display} "
        f"({width}x{height} px, {media_type}, {byte_size} bytes). "
        f"Inline pixels follow."
    )

    payload = {
        # 沿用 read_file 图片协议：llm_client 检测到这个 key 会把 content 替换成 list[block]
        # 走 vision 流派 2，OpenAI 兼容路径会自动降级为 image_url（cline 派）
        "_content_blocks": [
            {"type": "text", "text": summary_text},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_b64,
                },
            },
        ],
        # 给前端/日志看的精简元数据（不含 base64）
        "_meta": {
            "action": "screenshot",
            "display_number": chosen_display,
            "width": width,
            "height": height,
            "byte_size": byte_size,
            "ts": time.time(),
        },
    }
    return json.dumps(payload, ensure_ascii=False)


# ---- M2 通用 action 透传 ----
async def _do_generic_action(
    executor,
    *,
    action: str,
    coordinate: Optional[list],
    text: Optional[str],
    scroll_direction: Optional[str],
    scroll_amount: Optional[int],
    duration: Optional[float],
    via_clipboard: Optional[bool],
) -> str:
    """统一处理 M2 全部 click/key/type/scroll/wait/clipboard action。

    职责：
    1. 把工具参数翻译成 sidecar payload（去 None 字段）
    2. 调 executor.run_action() 透传到 sidecar
    3. 包装成 _runtime_block 返给前端 + LLM
    """
    payload: dict = {}
    if coordinate is not None:
        payload["coordinate"] = list(coordinate)
    if text is not None:
        payload["text"] = text
    if scroll_direction is not None:
        payload["scroll_direction"] = scroll_direction
    if scroll_amount is not None:
        payload["scroll_amount"] = int(scroll_amount)
    if duration is not None:
        payload["duration"] = float(duration)
    if via_clipboard is not None:
        payload["via_clipboard"] = bool(via_clipboard)

    # type / hold_key 可能要更长时间（粘贴 + sleep + 恢复剪贴板）；wait 自己定义
    if action in ("hold_key", "wait"):
        timeout = max(12.0, float(duration or 0) + 4.0)
    elif action == "type":
        timeout = 8.0
    else:
        timeout = 6.0

    result = await executor.run_action(action, payload, timeout_sec=timeout)

    # M5 视觉闭环：如果 result 里有 _after 截图，翻译成 _content_blocks 让 LLM 看图
    # （走 cline 流派 2 — read_file 图片协议同款）
    if isinstance(result, dict) and isinstance(result.get("_after"), dict):
        return _format_action_with_after(action, result)

    runtime_payload = {
        "_runtime_block": "computer_use.action",
        "action": action,
        "result": result if isinstance(result, dict) else {"value": result},
        "ts": time.time(),
    }
    return json.dumps(runtime_payload, ensure_ascii=False)


def _format_action_with_after(action: str, result: dict) -> str:
    """M5：mutating action result 含 _after 截图 → 拼成 _content_blocks 让 LLM 看图。

    输出形态：
      [
        {"type": "text", "text": "Action `<name>` done. Diff: 屏幕约 8.2% 变化..."},
        {"type": "image", "source": {...PNG base64...}},
      ]
    并且不在文本里 dump base64（_meta 给前端看）。
    """
    after = result["_after"]
    after_b64 = after.get("image_b64")
    after_media = after.get("media_type", "image/png")
    after_w = after.get("width")
    after_h = after.get("height")

    # action 主结果摘要（去掉 _after 字段，避免 LLM 看到一堆 base64 文本）
    main_result = {k: v for k, v in result.items() if k != "_after"}

    # diff 摘要
    diff = after.get("diff") if isinstance(after, dict) else None
    diff_text = ""
    if isinstance(diff, dict):
        if diff.get("error"):
            diff_text = f" Diff: 计算失败 ({diff['error']})."
        elif diff.get("skipped"):
            diff_text = f" Diff: 跳过（{diff.get('reason', '无 before')}）."
        elif diff.get("summary"):
            diff_text = f" Diff: {diff['summary']}"

    summary_text = (
        f"Action `{action}` executed. Result: {json.dumps(main_result, ensure_ascii=False)}."
        f"{diff_text} 下面是动作完成后的新截图（{after_w}×{after_h}）。"
    )

    if not isinstance(after_b64, str) or not after_b64:
        # 没拿到图但走到这条路径——退化到纯 runtime_block
        return json.dumps({
            "_runtime_block": "computer_use.action",
            "action": action,
            "result": main_result,
            "after_error": after.get("error", "no image"),
            "ts": time.time(),
        }, ensure_ascii=False)

    payload = {
        "_content_blocks": [
            {"type": "text", "text": summary_text},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": after_media,
                    "data": after_b64,
                },
            },
        ],
        # 给前端看的精简元数据（不含 base64）
        "_meta": {
            "action": action,
            "after_width": after_w,
            "after_height": after_h,
            "after_byte_size": after.get("byte_size"),
            "diff": (
                {k: v for k, v in diff.items() if k != "image_b64"}
                if isinstance(diff, dict) else None
            ),
            "ts": time.time(),
        },
    }
    return json.dumps(payload, ensure_ascii=False)


# ---- list_displays ----
async def _do_list_displays(executor) -> str:
    """枚举显示器 → 返回带 _runtime_block 的 JSON（前端可显示成卡片）。"""
    result = await executor.list_displays()
    displays = result.get("displays", [])
    count = result.get("count", 0)

    payload = {
        "_runtime_block": "computer_use.displays",
        "action": "list_displays",
        "count": count,
        "displays": displays,
        "ts": time.time(),
    }
    return json.dumps(payload, ensure_ascii=False)


# ---- M6: list_windows ----
_MAX_WINDOWS_RETURN = 50  # LLM 不需要看 100 个 explorer 实例


async def _do_list_windows(executor) -> str:
    """枚举所有可见顶层窗口 → _runtime_block。

    限 _MAX_WINDOWS_RETURN 条避免 token 爆炸（按 is_foreground → 大小排序，前台 + 大窗口优先）。
    """
    result = await executor.run_action("list_windows", {}, timeout_sec=4.0)
    if not isinstance(result, dict):
        return _err("list_windows returned non-dict", code="bad_result")
    wins = result.get("windows") or []
    # 按"前台优先 → 面积大优先"排序
    wins_sorted = sorted(
        wins,
        key=lambda w: (
            not w.get("is_foreground", False),
            -(int(w.get("width") or 0) * int(w.get("height") or 0)),
        ),
    )
    truncated = len(wins_sorted) > _MAX_WINDOWS_RETURN
    payload = {
        "_runtime_block": "computer_use.windows",
        "action": "list_windows",
        "count": len(wins_sorted),
        "returned": min(len(wins_sorted), _MAX_WINDOWS_RETURN),
        "truncated": truncated,
        "windows": wins_sorted[:_MAX_WINDOWS_RETURN],
        "ts": time.time(),
    }
    return json.dumps(payload, ensure_ascii=False)


async def _do_list_running_apps(executor) -> str:
    """按进程聚合的应用列表 → _runtime_block。"""
    result = await executor.run_action("list_running_apps", {}, timeout_sec=4.0)
    if not isinstance(result, dict):
        return _err("list_running_apps returned non-dict", code="bad_result")
    apps = result.get("apps") or []
    payload = {
        "_runtime_block": "computer_use.apps",
        "action": "list_running_apps",
        "count": len(apps),
        "apps": apps,
        "ts": time.time(),
    }
    return json.dumps(payload, ensure_ascii=False)


# ---- handler 标准模板 ----
_CONTEXT_KEYS = {"session_id", "llm_client", "trace_id", "agent_id"}


def _drop(kwargs):
    return {k: v for k, v in kwargs.items() if k not in _CONTEXT_KEYS}


async def _handle_computer_use(**kwargs):
    args = _drop(kwargs)
    return await computer_use_tool(
        action=args.get("action", ""),
        coordinate=args.get("coordinate"),
        text=args.get("text"),
        scroll_direction=args.get("scroll_direction"),
        scroll_amount=args.get("scroll_amount"),
        duration=args.get("duration"),
        display_number=args.get("display_number"),
        via_clipboard=args.get("via_clipboard"),
        session_id=kwargs.get("session_id"),
    )


__all__ = ["computer_use_tool", "_handle_computer_use"]
