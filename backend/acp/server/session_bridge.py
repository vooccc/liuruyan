"""ACP session ↔ 柳如烟 session 的双向映射。

设计要点（@docs/acp_integration_design.md §5.3.2）：
- ACP session_id 和柳如烟 SessionManager.session_id 不是同一个东西，必须做映射
- bridge 是 LiuruyanACPAgent 实例的成员（不是全局单例的实例变量）——
  每个 Zed 实例 spawn 一个 server 子进程 → 一个 agent 实例 → 一个 bridge，天然隔离
- ContextVar 用法见 backend/llm_client.py:_current_session_id_var；本模块不直接动 ContextVar，
  由 prompt_executor 在每次 prompt 入口设置
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class ACPSession:
    """单个 ACP 会话的元数据。"""

    acp_session_id: str
    liuruyan_session_id: str
    cwd: str
    mcp_servers: list = field(default_factory=list)
    # 后续 Phase 会加：mode_id, model_id, config_options 等


class SessionBridge:
    """ACP session ↔ 柳如烟 session 双向 map。

    线程安全性：当前实现假设 ACP server 在单 event loop 内运行（ACP SDK 默认行为），
    所有操作都在主 loop 上不需加锁。如果未来要并行处理多个 Zed 连接，再加 asyncio.Lock。
    """

    def __init__(self) -> None:
        self._acp_to_lry: Dict[str, ACPSession] = {}
        self._lry_to_acp: Dict[str, str] = {}

    def register(
        self,
        *,
        liuruyan_session_id: str,
        cwd: str,
        mcp_servers: Optional[list] = None,
        acp_session_id: Optional[str] = None,
    ) -> ACPSession:
        """注册一对新映射。

        Args:
            liuruyan_session_id: 柳如烟 SessionManager 创建的 session_id
            cwd: ACP client 传过来的工作目录
            mcp_servers: ACP client 传过来的 MCP 服务器列表（透传，未来 Phase 处理）
            acp_session_id: 可选，外部指定 ACP session id（用于 load_session 复活场景）；
                            不传时自动生成
        """
        acp_sid = acp_session_id or f"acp_{uuid.uuid4()}"
        if acp_sid in self._acp_to_lry:
            # load_session 复用同 id 是合法的，但 new_session 同 id 是 bug
            logger.warning("ACP session id %s already registered, overwriting", acp_sid)
        sess = ACPSession(
            acp_session_id=acp_sid,
            liuruyan_session_id=liuruyan_session_id,
            cwd=cwd,
            mcp_servers=mcp_servers or [],
        )
        self._acp_to_lry[acp_sid] = sess
        self._lry_to_acp[liuruyan_session_id] = acp_sid
        logger.info("ACP session registered: %s ↔ %s (cwd=%s)", acp_sid, liuruyan_session_id, cwd)
        return sess

    def get_by_acp(self, acp_session_id: str) -> Optional[ACPSession]:
        return self._acp_to_lry.get(acp_session_id)

    def get_lry_id(self, acp_session_id: str) -> Optional[str]:
        sess = self._acp_to_lry.get(acp_session_id)
        return sess.liuruyan_session_id if sess else None

    def get_acp_id(self, liuruyan_session_id: str) -> Optional[str]:
        return self._lry_to_acp.get(liuruyan_session_id)

    def unregister(self, acp_session_id: str) -> None:
        sess = self._acp_to_lry.pop(acp_session_id, None)
        if sess:
            self._lry_to_acp.pop(sess.liuruyan_session_id, None)
            logger.info("ACP session unregistered: %s", acp_session_id)

    def list_acp_ids(self) -> list[str]:
        return list(self._acp_to_lry.keys())
