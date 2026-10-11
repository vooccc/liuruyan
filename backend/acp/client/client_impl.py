"""LiuruyanACPClient：实现 acp.Client Protocol，作为外部 agent 的宿主（client 侧）。

外部 agent（如 Claude Code）通过 ACP 跟我们交互时调本类方法：
- session_update：流式推消息块 → 我们累积到 collected 列表
- request_permission：要批准 → MVP 自动 allow
- read_text_file / write_text_file：让我们帮它读写文件 → 直接走本地 IO
- create_terminal / terminal_*：让我们帮它跑命令 → MVP 拒绝（防意外副作用）
- ext_method / ext_notification：扩展协议 → 拒绝

红线：write_text_file 直接落盘是因为"用户主动 delegate_acp_agent"行为已经表明信任。
Phase 5.5 会接入柳如烟前端的 inline diff / permission 卡片做二次确认。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional

import acp
from acp.exceptions import RequestError
from acp.schema import (
    AllowedOutcome,
    CreateTerminalResponse,
    KillTerminalResponse,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

logger = logging.getLogger(__name__)


class LiuruyanACPClient:
    """实现 acp.Client Protocol。

    把外部 agent 推给我们的 session/update 累积到 `self.collected_updates`，
    供 service.run_external_agent_turn 一次性消费后总结返给 LLM。
    """

    def __init__(
        self,
        *,
        allow_write: bool = True,
        on_chunk: Optional[Callable[[str, dict], Awaitable[None]]] = None,
    ) -> None:
        self.collected_updates: List[Any] = []
        self.permission_decisions: List[dict] = []  # 留 debug 用
        self.allow_write = allow_write
        # Phase 5.5：把外部 agent 的 session_update 实时推回柳如烟前端 SSE 用
        # signature: async (kind: str, payload: dict) -> None
        # kind ∈ {"text", "thought", "tool_call", "tool_done"}
        self.on_chunk = on_chunk
        self._chunk_seq = 0

    def on_connect(self, conn: acp.Agent) -> None:  # noqa: ARG002
        """ACP SDK 在 connect 时回调（agent 这边的 conn 是 acp.Agent 类型）。"""
        logger.info("LiuruyanACPClient connected to external agent")

    # ============================================================
    # session/update：累积外部 agent 推流的消息
    # ============================================================

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:  # noqa: ARG002
        self.collected_updates.append(update)
        kind = getattr(update, "session_update", "?")
        # 每条都打 info：debug "外部 agent 是不是还活着 / 卡在哪个工具"用
        if hasattr(update, "content") and hasattr(update.content, "text"):
            text_preview = (update.content.text or "")[:80]
            logger.info("[ACP-CLIENT-UPDATE] kind=%s text=%r", kind, text_preview)
        elif kind == "tool_call":
            logger.info(
                "[ACP-CLIENT-UPDATE] kind=tool_call id=%s title=%r tool_kind=%s",
                getattr(update, "tool_call_id", ""),
                getattr(update, "title", ""),
                getattr(update, "kind", ""),
            )
        elif kind == "tool_call_update":
            logger.info(
                "[ACP-CLIENT-UPDATE] kind=tool_call_update id=%s status=%s",
                getattr(update, "tool_call_id", ""),
                getattr(update, "status", ""),
            )
        else:
            logger.info("[ACP-CLIENT-UPDATE] kind=%s", kind)

        # Phase 5.5：实时把 update 翻成柳如烟 chunk 推给前端
        if self.on_chunk is not None:
            try:
                self._chunk_seq += 1
                payload = self._update_to_chunk_payload(update)
                if payload is not None:
                    payload["seq"] = self._chunk_seq
                    await self.on_chunk(payload["chunk_kind"], payload)
            except Exception:
                # 前端 SSE 失败绝不阻塞 ACP 收流
                logger.exception("on_chunk callback raised, swallowing")

    def _update_to_chunk_payload(self, update: Any) -> Optional[dict]:
        """把 ACP session/update 翻成前端卡片消费的扁平字段。

        返 None 表示这条 update 不入卡片（如 plan/agent_thought 默认隐藏）。
        """
        kind = getattr(update, "session_update", None) or ""
        if kind == "agent_message_chunk":
            content = getattr(update, "content", None)
            text = getattr(content, "text", "") if content is not None else ""
            if not text:
                return None
            return {"chunk_kind": "text", "text": text}
        if kind == "agent_thought_chunk":
            content = getattr(update, "content", None)
            text = getattr(content, "text", "") if content is not None else ""
            if not text:
                return None
            return {"chunk_kind": "thought", "text": text}
        if kind == "tool_call":
            return {
                "chunk_kind": "tool_call",
                "tool_call_id": getattr(update, "tool_call_id", ""),
                "title": getattr(update, "title", "") or "",
                "tool_kind": getattr(update, "kind", "other") or "other",
            }
        if kind == "tool_call_update":
            return {
                "chunk_kind": "tool_done",
                "tool_call_id": getattr(update, "tool_call_id", ""),
                "status": getattr(update, "status", "") or "",
            }
        return None  # plan / 其它暂忽略

    # ============================================================
    # request_permission：MVP 自动 allow
    # ============================================================

    async def request_permission(
        self,
        options: List[Any],
        session_id: str,  # noqa: ARG002
        tool_call: Any,
        **_: Any,
    ) -> RequestPermissionResponse:
        title = getattr(tool_call, "title", "") or getattr(tool_call, "tool_call_id", "?")
        # 选第一个 allow* 类的 option；找不到就选第一个
        chosen = None
        for opt in options:
            kind = (getattr(opt, "kind", "") or "").lower()
            if "allow" in kind or kind == "":
                chosen = opt
                break
        if chosen is None and options:
            chosen = options[0]
        if chosen is None:
            raise RequestError(code=-32603, message="No permission options provided")

        option_id = getattr(chosen, "option_id", "allow_once")
        self.permission_decisions.append({"title": title, "option_id": option_id})
        logger.info("external agent permission [%s] auto-decided: %s", title, option_id)
        return RequestPermissionResponse(
            outcome=AllowedOutcome(option_id=option_id, outcome="selected")
        )

    # ============================================================
    # fs 反向能力：直接走本地 IO（外部 agent 来读/写）
    # ============================================================

    async def read_text_file(
        self,
        path: str,
        session_id: str,  # noqa: ARG002
        limit: int | None = None,
        line: int | None = None,
        **_: Any,
    ) -> ReadTextFileResponse:
        try:
            p = Path(path).expanduser().resolve()
            content = p.read_text(encoding="utf-8", errors="replace")
            full_chars = len(content)
            if line is not None or limit is not None:
                lines = content.splitlines(keepends=True)
                start = max(0, (line or 1) - 1)
                end = start + limit if limit else len(lines)
                content = "".join(lines[start:end])
            logger.info(
                "[ACP-CLIENT-FS-READ] path=%s line=%s limit=%s returned=%d/%d chars",
                p,
                line,
                limit,
                len(content),
                full_chars,
            )
            return ReadTextFileResponse(content=content)
        except FileNotFoundError as exc:
            raise RequestError(code=-32602, message=f"File not found: {path}") from exc
        except Exception as exc:
            logger.exception("LiuruyanACPClient.read_text_file failed: %s", path)
            raise RequestError(code=-32603, message=f"Read failed: {exc}") from exc

    async def write_text_file(
        self,
        content: str,
        path: str,
        session_id: str,  # noqa: ARG002
        **_: Any,
    ) -> WriteTextFileResponse | None:
        if not self.allow_write:
            raise RequestError(code=-32601, message="Write disabled in this client instance")
        try:
            p = Path(path).expanduser().resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            logger.info("LiuruyanACPClient wrote %d chars to %s", len(content), p)
            return None
        except Exception as exc:
            logger.exception("LiuruyanACPClient.write_text_file failed: %s", path)
            raise RequestError(code=-32603, message=f"Write failed: {exc}") from exc

    # ============================================================
    # terminal/* 反向：MVP 拒绝（防外部 agent 跑命令到本机）
    # Phase 5.5 接 command_manager
    # ============================================================

    async def create_terminal(self, *args: Any, **kwargs: Any) -> CreateTerminalResponse:  # noqa: ARG002
        raise RequestError(
            code=-32601,
            message="Terminal capability not exposed (Phase 5.5 will add)",
        )

    async def terminal_output(self, *args: Any, **kwargs: Any) -> TerminalOutputResponse:  # noqa: ARG002
        raise RequestError(code=-32601, message="Terminal capability not exposed")

    async def release_terminal(self, *args: Any, **kwargs: Any) -> ReleaseTerminalResponse | None:  # noqa: ARG002
        raise RequestError(code=-32601, message="Terminal capability not exposed")

    async def wait_for_terminal_exit(
        self, *args: Any, **kwargs: Any
    ) -> WaitForTerminalExitResponse:  # noqa: ARG002
        raise RequestError(code=-32601, message="Terminal capability not exposed")

    async def kill_terminal(self, *args: Any, **kwargs: Any) -> KillTerminalResponse | None:  # noqa: ARG002
        raise RequestError(code=-32601, message="Terminal capability not exposed")

    # ============================================================
    # 扩展协议：拒绝
    # ============================================================

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise RequestError(code=-32601, message=f"Unsupported extension method: {method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:  # noqa: ARG002
        logger.debug("LiuruyanACPClient ignored ext notification: %s", method)
