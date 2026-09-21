"""Computer Use 高层 API：把 sidecar 调用包成业务语义清晰的方法。

设计原则：
- async-first：所有方法 async，沿用 backend SSE 主路径
- 单实例锁透明：调用前自动 acquire，失败抛 ComputerUseBlocked
- 平台不支持时不静默降级，明确抛 ComputerUseUnsupported
- 安全栏（M4）：每个 action 调用前过 safety.check_and_record；失败抛 ComputerUseDenied
- 审计（M4）：每个 action 完成（成功/失败）都落 audit.write_event
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from backend.computer_use import audit as _audit
from backend.computer_use import lock as _lock
from backend.computer_use import safety as _safety
from backend.computer_use import visual as _visual
from backend.computer_use.sidecar import (
    SidecarError,
    call_sidecar,
    is_platform_supported,
)


# Mutating actions：执行后会真改"屏幕像素"的——这些之后自动再截一张让 LLM 看变化。
#
# 排除项及理由：
# - screenshot / list_displays / cursor_position：纯读
# - read_clipboard / write_clipboard：改的是剪贴板不是屏幕（用户粘贴时才显示）
# - wait：纯定时器
# - mouse_move / left_mouse_down / left_mouse_up：单独移光标 / 按键不松开本身不改屏幕，
#   真改屏幕的拖拽走 left_click_drag
MUTATING_ACTIONS = frozenset({
    "left_click", "right_click", "middle_click",
    "double_click", "triple_click",
    "left_click_drag",
    "scroll",
    "key", "hold_key", "type",
})

# after-screenshot 用更小的尺寸——LLM 主要看变化，不需要看清字符细节
_AFTER_MAX_WIDTH = 1280
# before/after 缓存有效窗口（ms）：超过这个时间就算过期，新的 mutating 也不附 diff（防 stale）
_BEFORE_CACHE_TTL_SEC = 60.0

logger = logging.getLogger(__name__)


class ComputerUseBlocked(RuntimeError):
    """单实例锁被另一个 session 占着。"""

    def __init__(self, holder_session_id: str, holder_pid: int):
        super().__init__(
            f"Computer Use is held by another session "
            f"(session_id={holder_session_id!r}, pid={holder_pid}). "
            f"Wait for it to finish or close that window."
        )
        self.holder_session_id = holder_session_id
        self.holder_pid = holder_pid


class ComputerUseDenied(RuntimeError):
    """安全栏拒绝（disabled / blocked / rate-limited / out-of-bounds 等）。"""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class ComputerUseUnsupported(RuntimeError):
    """当前平台没有 helper 实现（M1 只 Windows，M3 加齐）。"""


class ComputerUseExecutor:
    """高层 API，按 session 实例化（每个 chat session 拿一个）。"""

    def __init__(self, session_id: str):
        if not session_id:
            raise ValueError("session_id is required")
        self.session_id = session_id
        # 缓存最近一次 list_displays 的结果（safety.check_and_record 做坐标边界要用）
        self._cached_displays: Optional[list[Dict[str, Any]]] = None
        # M5 视觉闭环：缓存最近一次 screenshot 的 image_b64 + 尺寸 + 时间戳
        # mutating action 后再截一张，与这份"before"算 diff
        self._last_screenshot_b64: Optional[str] = None
        self._last_screenshot_w: int = 0
        self._last_screenshot_h: int = 0
        self._last_screenshot_ts: float = 0.0

    # ---- 锁管理 ----
    async def _ensure_lock(self) -> None:
        if not is_platform_supported():
            raise ComputerUseUnsupported(
                "No Computer Use helper for this platform yet. "
                "M1/M2 ship Windows only; macOS/Linux land in M3."
            )
        result = _lock.acquire(self.session_id)
        if not result.acquired:
            holder = result.holder
            raise ComputerUseBlocked(
                holder_session_id=holder.session_id if holder else "unknown",
                holder_pid=holder.pid if holder else -1,
            )

    def release(self) -> bool:
        return _lock.release(self.session_id)

    # ---- 安全栏 + 审计共用入口 ----
    async def _guarded_call(
        self,
        action: str,
        payload: Optional[Dict[str, Any]],
        *,
        timeout_sec: float,
    ) -> Any:
        """统一执行流：safety check → lock → sidecar → audit。

        失败路径都会落 audit；成功路径只在 redact 后的 result 落 audit。
        """
        coordinate = (payload or {}).get("coordinate")
        # 1) safety
        try:
            _safety.check_and_record(
                session_id=self.session_id,
                action=action,
                coordinate=coordinate,
                cached_displays=self._cached_displays,
            )
        except _safety.SafetyDenied as exc:
            _audit.write_event(
                session_id=self.session_id,
                action=action,
                payload=payload,
                error=str(exc),
                error_code=exc.code,
            )
            raise ComputerUseDenied(str(exc), code=exc.code) from exc

        # 2) lock
        await self._ensure_lock()

        # 3) sidecar + audit
        started = time.monotonic()
        try:
            result = await call_sidecar(action, payload or {}, timeout_sec=timeout_sec)
        except SidecarError as exc:
            _audit.write_event(
                session_id=self.session_id,
                action=action,
                payload=payload,
                duration_ms=(time.monotonic() - started) * 1000.0,
                error=str(exc),
                error_code=exc.code,
            )
            raise RuntimeError(f"{action} failed: {exc} (code={exc.code})") from exc
        except Exception as exc:  # noqa: BLE001 — sidecar 出意外也落审计
            _audit.write_event(
                session_id=self.session_id,
                action=action,
                payload=payload,
                duration_ms=(time.monotonic() - started) * 1000.0,
                error=str(exc),
                error_code="unexpected",
            )
            raise

        # 4) 成功 → 审计
        _audit.write_event(
            session_id=self.session_id,
            action=action,
            payload=payload,
            result=result,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

        # 5) M5 视觉闭环：mutating action 后自动截 after + 算 diff
        if (
            action in MUTATING_ACTIONS
            and _safety.get_policy().auto_screenshot_after_mutate
            and isinstance(result, dict)
        ):
            try:
                await self._attach_after_screenshot(result)
            except Exception as exc:  # noqa: BLE001 — 视觉闭环失败不能让 action 失败
                logger.warning("[computer_use] after-screenshot failed: %s", exc)
                result.setdefault("_after", {"error": str(exc)})

        # 6) 缓存 screenshot 结果给下一轮 mutating 作 before
        if action == "screenshot" and isinstance(result, dict):
            b64 = result.get("image_b64")
            if isinstance(b64, str) and b64:
                self._last_screenshot_b64 = b64
                self._last_screenshot_w = int(result.get("width") or 0)
                self._last_screenshot_h = int(result.get("height") or 0)
                self._last_screenshot_ts = time.time()

        return result

    async def _attach_after_screenshot(self, result: Dict[str, Any]) -> None:
        """mutating action 后自动截 after-screenshot；如果有 fresh before 缓存，再算 diff。

        副作用：在 result 里写入 '_after' 子 dict，含：
          - image_b64 / media_type / width / height（after 全图）
          - diff: { changed_ratio, changed_bbox, summary }（before 缺/过期则没有这个 key）

        失败路径不抛——视觉闭环只是"锦上添花"，主 action 已经成功。
        """
        # 截 after（用更小尺寸省 token）
        after_payload: Dict[str, Any] = {"format": "png", "max_width": _AFTER_MAX_WIDTH}
        try:
            after = await call_sidecar("screenshot", after_payload, timeout_sec=8.0)
        except SidecarError as exc:
            result["_after"] = {"error": f"screenshot failed: {exc}"}
            return

        if not isinstance(after, dict) or not after.get("image_b64"):
            result["_after"] = {"error": "screenshot returned no image"}
            return

        result["_after"] = {
            "image_b64": after["image_b64"],
            "media_type": after.get("media_type", "image/png"),
            "width": after.get("width"),
            "height": after.get("height"),
            "byte_size": after.get("byte_size"),
        }

        # 有 fresh before → 算 diff
        before_b64 = self._last_screenshot_b64
        age = time.time() - self._last_screenshot_ts
        if before_b64 and age <= _BEFORE_CACHE_TTL_SEC:
            try:
                diff = _visual.compute_diff(
                    before_b64,
                    after["image_b64"],
                    full_width=int(after.get("width") or 0),
                    full_height=int(after.get("height") or 0),
                )
                result["_after"]["diff"] = diff
            except Exception as exc:  # noqa: BLE001
                logger.warning("[computer_use] diff failed: %s", exc)
                result["_after"]["diff"] = {"error": str(exc)}
        else:
            result["_after"]["diff"] = {
                "skipped": True,
                "reason": (
                    "no recent screenshot to diff against — call screenshot first "
                    "if you want before/after comparison"
                ),
            }

        # 把 after 也作为下一轮的 before 缓存
        self._last_screenshot_b64 = after["image_b64"]
        self._last_screenshot_w = int(after.get("width") or 0)
        self._last_screenshot_h = int(after.get("height") or 0)
        self._last_screenshot_ts = time.time()

    # ---- 高层 action ----
    async def screenshot(
        self,
        *,
        display_number: Optional[int] = None,
        format: str = "png",
        max_width: Optional[int] = None,
    ) -> Dict[str, Any]:
        """截屏。返回 dict 含 image_b64 / media_type / width / height / display_number / byte_size。"""
        payload: Dict[str, Any] = {"format": format}
        if display_number is not None:
            payload["display_number"] = int(display_number)
        if max_width is not None:
            payload["max_width"] = int(max_width)
        result = await self._guarded_call("screenshot", payload, timeout_sec=8.0)
        if not isinstance(result, dict):
            raise RuntimeError(f"screenshot returned non-dict: {type(result).__name__}")
        return result

    async def list_displays(self) -> Dict[str, Any]:
        """枚举显示器。返回 {"displays": [...], "count": int}。"""
        result = await self._guarded_call("list_displays", {}, timeout_sec=3.0)
        if not isinstance(result, dict):
            raise RuntimeError(f"list_displays returned non-dict: {type(result).__name__}")
        # 缓存供后续 safety 坐标边界检查
        displays = result.get("displays")
        if isinstance(displays, list):
            self._cached_displays = displays
        return result

    async def run_action(
        self,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        timeout_sec: float = 6.0,
    ) -> Any:
        """通用 action 调用：M2+ 的所有 click/type/key/scroll/... 都走这条。"""
        return await self._guarded_call(action, payload, timeout_sec=timeout_sec)


# ---- per-session executor 缓存 ----
_executors: Dict[str, ComputerUseExecutor] = {}


def get_executor(session_id: str) -> ComputerUseExecutor:
    """按 session_id 取（或建）executor。"""
    if not session_id:
        raise ValueError("session_id is required for Computer Use")
    exec_ = _executors.get(session_id)
    if exec_ is None:
        exec_ = ComputerUseExecutor(session_id)
        _executors[session_id] = exec_
    return exec_
