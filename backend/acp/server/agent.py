"""LiuruyanACPAgent：实现 acp.Agent Protocol，把柳如烟暴露给 ACP 客户端。

Phase 1 范围：
- initialize：协商协议版本 + 上报能力 + 自我介绍
- authenticate：返空 ok（暂不做认证，依赖环境变量）
- new_session / load_session / list_sessions：建立/恢复 session 映射
- close_session / cancel：清理
- prompt：**echo 实现**——只回流式 "Hello from 柳如烟 ACP (Phase 1 echo, prompt 内容: ...)"
  Phase 2 替换成接 LLMClient.chat 真跑

未实现（后续 Phase）：
- set_session_mode / set_session_model / set_config_option：返 None（表示不支持）
- fork_session / resume_session：返默认 / 报错（暂不支持）
- ext_method / ext_notification：返 -32601

设计参考：
- _research/hermes-agent-main/acp_adapter/server.py（结构）
- _research/QwenPaw-main/src/qwenpaw/agents/acp/server.py（双向映射）
- acp.Agent Protocol 实测签名：see _docs/acp_integration_design.md §4.1
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Optional

import acp
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    CloseSessionResponse,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,
    NewSessionResponse,
    PromptCapabilities,
    PromptResponse,
    ResumeSessionResponse,
    SessionAdditionalDirectoriesCapabilities,
    SessionCapabilities,
    SessionListCapabilities,
    TextContentBlock,
)

from .event_translator import translate_event
from .prompt_executor import run_prompt
from .reverse_calls import install_acp_handler_overrides
from .session_bridge import SessionBridge

logger = logging.getLogger(__name__)


# Phase 2 起接入真实 LLMClient，echo 模板保留作为兜底（LLM 完全沉默时给个回复，避免 Zed 卡住）。
_FALLBACK_EMPTY_RESPONSE = "(LLM returned empty response)"

# ACP server 当前固定用 default 角色；Phase 6+ 加 slash command /model 或角色切换
_ACP_AGENT_ID = os.environ.get("LIURUYAN_ACP_AGENT_ID", "default")


class LiuruyanACPAgent:
    """实现 acp.Agent Protocol 的柳如烟 agent。

    注意：不需要 `class LiuruyanACPAgent(acp.Agent)`，acp.Agent 是 Protocol（鸭子类型）。
    但写了继承也无害（更利于静态检查）。这里**不继承**避免 SDK 版本变动时签名校验报错。
    """

    AGENT_NAME = "liuruyan"
    AGENT_TITLE = "柳如烟"
    # 跟随主项目版本（懒加载，避免 import 失败）
    AGENT_VERSION = "0.1.0-acp-phase1"

    def __init__(self) -> None:
        self._bridge = SessionBridge()
        self._conn: Optional[acp.Client] = None
        # 已知客户端能力（initialize 后填）
        self._client_capabilities: Any = None
        logger.info("LiuruyanACPAgent initialized")

    # ============================================================
    # 连接生命周期（ACP SDK 在握手时自动调用）
    # ============================================================

    def on_connect(self, conn: acp.Client) -> None:
        """ACP SDK 在 listen 开始前调用，传入用来推流的 client conn。"""
        self._conn = conn
        logger.info("ACP client connected; conn=%r", conn)

    # ============================================================
    # initialize / authenticate（必选）
    # ============================================================

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Optional[Implementation] = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        """协议握手。

        ACP 当前 protocol_version = 1（数字）。我们也声明 1。
        capabilities 协商：上报我们支持的能力。Phase 1 阶段除了基础对话外不暴露太多。
        """
        self._client_capabilities = client_capabilities
        logger.info(
            "initialize: client protocol_version=%s, client_info=%s, client_capabilities=%s",
            protocol_version,
            getattr(client_info, "name", "?"),
            client_capabilities,
        )

        # Phase 4：握手完成后挂 ACP 反向能力 wrapper（fs/read fs/write）。
        # wrapper 内部读 ContextVar，conn 没设就走原 handler，所以即使 install 了
        # 也不影响非 ACP 代码路径。幂等。
        try:
            install_acp_handler_overrides()
        except Exception:
            logger.exception("install_acp_handler_overrides failed (continuing with original handlers)")

        return InitializeResponse(
            protocol_version=1,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=False,            # Phase 1 暂不支持（Phase 7 加）
                    audio=False,
                    embedded_context=False,
                ),
                mcp_capabilities=McpCapabilities(
                    http=False,             # 后续 Phase 接入柳如烟 MCP 子系统
                    sse=False,
                ),
                session_capabilities=SessionCapabilities(
                    list=SessionListCapabilities(),
                    additional_directories=SessionAdditionalDirectoriesCapabilities(),
                ),
            ),
            auth_methods=[],  # Phase 1 不要求 authenticate（依赖 env）
            agent_info=Implementation(
                name=self.AGENT_NAME,
                title=self.AGENT_TITLE,
                version=self.AGENT_VERSION,
            ),
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> AuthenticateResponse | None:
        """认证。Phase 1 不要求，返 None 表示 ok。"""
        logger.info("authenticate called with method_id=%s (Phase 1: accept all)", method_id)
        return None

    # ============================================================
    # session 生命周期
    # ============================================================

    async def new_session(
        self,
        cwd: str,
        additional_directories: Optional[list[str]] = None,
        mcp_servers: Optional[list[Any]] = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        """创建新会话。

        Phase 2：接入真实 SessionManager.create_session，agent_id 固定 default。
        失败时（如 default 目录不存在）回退到 mock id，保证 ACP 协议不崩，
        但 prompt() 调时会抛 InvalidSession（Phase 2 用户可感知问题）。
        """
        # 延迟 import，避免 backend.acp 模块在没启动主进程时 import 失败
        from backend.session import get_session_manager

        try:
            session = await get_session_manager().create_session(
                agent_id=_ACP_AGENT_ID,
                title=f"ACP from {cwd}",
            )
            # 打 ACP 来源标记 + cwd：前端侧栏据此把 ACP 会话归到独立分组
            # （主侧栏过滤 metadata.source=='acp'，ACP 分组按 metadata.acp_cwd 聚合）
            session.metadata["source"] = "acp"
            session.metadata["acp_cwd"] = cwd
            session.save_meta()
            lry_sid = session.id
            logger.info("new_session: created real liuruyan session %s", lry_sid)
        except Exception as exc:
            # 兜底：创建失败仍然给个 id 让协议继续走，prompt 阶段才会 raise
            logger.exception("new_session: failed to create liuruyan session: %s", exc)
            lry_sid = f"lry_acp_orphan_{uuid.uuid4()}"

        sess = self._bridge.register(
            liuruyan_session_id=lry_sid,
            cwd=cwd,
            mcp_servers=mcp_servers or [],
        )
        logger.info(
            "new_session: acp=%s, lry=%s, cwd=%s, additional_dirs=%s, mcp_servers_count=%d",
            sess.acp_session_id,
            lry_sid,
            cwd,
            additional_directories,
            len(mcp_servers or []),
        )
        return NewSessionResponse(session_id=sess.acp_session_id)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: Optional[list[str]] = None,
        mcp_servers: Optional[list[Any]] = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        """加载历史会话。Phase 1 仅注册映射，不真复活历史消息。"""
        existing = self._bridge.get_by_acp(session_id)
        if existing is None:
            # 复活路径：客户端用一个我们不知道的 ACP session id 来加载 → 建一条映射
            lry_sid = f"lry_acp_{uuid.uuid4()}"
            self._bridge.register(
                liuruyan_session_id=lry_sid,
                cwd=cwd,
                mcp_servers=mcp_servers or [],
                acp_session_id=session_id,
            )
            logger.info("load_session: created new mapping for unknown session_id=%s", session_id)
        else:
            logger.info("load_session: existing session_id=%s", session_id)
        return LoadSessionResponse()

    async def list_sessions(
        self,
        additional_directories: Optional[list[str]] = None,
        cursor: Optional[str] = None,
        cwd: Optional[str] = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        """列出 session。Phase 1：仅返当前 bridge 内的。"""
        # ListSessionsResponse 字段：sessions: list[SessionInfo], next_cursor: Optional[str]
        # 实际 SessionInfo schema 比较丰富，Phase 1 先返空（cursor=None 表示没下一页）
        return ListSessionsResponse(sessions=[], next_cursor=None)

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> CloseSessionResponse | None:
        """关闭会话。"""
        self._bridge.unregister(session_id)
        return None

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """取消正在运行的 prompt。Phase 1：echo 不会卡，无操作。Phase 2 起需要中断 LLMClient。"""
        logger.info("cancel: session_id=%s (Phase 1: no-op for echo)", session_id)
        return None

    # ============================================================
    # 不支持的可选能力：返 None / 报错（snake_case Method）
    # ============================================================

    async def set_session_mode(
        self, mode_id: str, session_id: str, **kwargs: Any
    ) -> None:
        return None

    async def set_session_model(
        self, model_id: str, session_id: str, **kwargs: Any
    ) -> None:
        return None

    async def set_config_option(
        self, config_id: str, session_id: str, value: Any, **kwargs: Any
    ) -> None:
        return None

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: Optional[list[str]] = None,
        mcp_servers: Optional[list[Any]] = None,
        **kwargs: Any,
    ) -> Any:
        raise RequestError(code=-32601, message="fork_session not yet implemented")

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: Optional[list[str]] = None,
        mcp_servers: Optional[list[Any]] = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        raise RequestError(code=-32601, message="resume_session not yet implemented")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError(code=-32601, message=f"Unsupported extension method: {method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        # 通知不要求响应，仅 log
        logger.debug("ext_notification (ignored): method=%s", method)

    # ============================================================
    # prompt：Phase 1 echo 实现
    # ============================================================

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: Optional[str] = None,
        **kwargs: Any,
    ) -> PromptResponse:
        """处理一轮对话。

        Phase 2：接入 prompt_executor → LLMClient.chat(use_tools=False) → event_translator → session/update。
        Phase 3 会切换到 Agent.chat 含工具循环 + system prompt。
        """
        sess = self._bridge.get_by_acp(session_id)
        if sess is None:
            raise RequestError(
                code=-32602,
                message=f"Unknown session_id: {session_id}",
            )

        if self._conn is None:
            raise RequestError(code=-32603, message="No active client connection")

        if sess.liuruyan_session_id.startswith("lry_acp_orphan_"):
            # new_session 创建失败的兜底 id，prompt 阶段告诉用户原因而不是闷头跑
            raise RequestError(
                code=-32603,
                message="ACP session 未关联到柳如烟 SessionManager（new_session 创建失败，见 server stderr）",
            )

        # 抽取用户消息文本（多模态等 Phase 7 再处理）
        prompt_text = self._extract_text(prompt)
        logger.info(
            "prompt: acp_session=%s, lry_session=%s, text_len=%d",
            session_id,
            sess.liuruyan_session_id,
            len(prompt_text),
        )

        # 跑 LLM，流式翻译
        # Phase 4：把 ACP context 传下去，让反向 wrapper 能找到 conn
        emitted_any = False
        try:
            async for event in run_prompt(
                user_text=prompt_text,
                liuruyan_session_id=sess.liuruyan_session_id,
                agent_id=_ACP_AGENT_ID,
                acp_conn=self._conn,
                acp_session_id=session_id,
                client_capabilities=self._client_capabilities,
            ):
                update = translate_event(event)
                if update is None:
                    continue
                await self._conn.session_update(session_id=session_id, update=update)
                emitted_any = True
        except Exception:
            # 兜底：异常时给客户端一个 message，避免 Zed 卡死
            logger.exception("prompt: LLM call failed")
            try:
                await self._conn.session_update(
                    session_id=session_id,
                    update=acp.update_agent_message_text(
                        "\n\n⚠️ LLM 调用失败，请检查 server stderr 日志。"
                    ),
                )
            except Exception:
                pass
            return PromptResponse(stop_reason="cancelled")

        # LLM 一字未出（极少见，但 Zed 看到没输出会困惑）
        if not emitted_any:
            await self._conn.session_update(
                session_id=session_id,
                update=acp.update_agent_message_text(_FALLBACK_EMPTY_RESPONSE),
            )

        return PromptResponse(stop_reason="end_turn")

    # ============================================================
    # 辅助
    # ============================================================

    @staticmethod
    def _extract_text(prompt_blocks: list[Any]) -> str:
        """从 ACP content blocks 抽取纯文本（非 text 块暂忽略）。"""
        parts: list[str] = []
        for block in prompt_blocks:
            if isinstance(block, TextContentBlock):
                parts.append(block.text)
            elif hasattr(block, "text") and isinstance(block.text, str):
                parts.append(block.text)
        return "\n".join(parts)
