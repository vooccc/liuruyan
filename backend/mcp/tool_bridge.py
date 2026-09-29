"""把 MCP server 的工具注册到柳如烟 CleanToolRegistry。

零侵入设计：不修改 `clean_registry.py` 本体，靠 `registry.register(name=..., handler=...)`
把每个 MCP tool 注册成一个 async handler 闭包；handler 内部从 ContextVar 拿 session_id，
转调到对应 StatefulMCPClient.call_tool()。

工具命名遵守 `mcp__<server>__<tool>` 规范（在 stateful_client.qualify_tool_name 实现）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.tools.builtin.clean_registry import registry as global_tool_registry

from .controller import get_controller
from .stateful_client import StatefulClientError
from .types import MCPToolInfo

logger = logging.getLogger(__name__)


def _format_call_result(result: Any) -> str:
    """把 mcp.CallToolResult 转成 LLM 可读字符串。

    mcp 返回 `{ content: [TextContent | ImageContent | ...], isError: bool, structuredContent? }`。
    M1 简单处理：拼接所有 TextContent，错误时前缀 'Error:'，便于 dispatch 主路径识别失败。
    """
    is_error = bool(getattr(result, "isError", False))
    content_blocks = getattr(result, "content", []) or []
    structured = getattr(result, "structuredContent", None)

    parts: list[str] = []
    for block in content_blocks:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(getattr(block, "text", "") or "")
        elif block_type == "image":
            parts.append(f"[image:{getattr(block, 'mimeType', 'unknown')}]")
        elif block_type == "resource":
            res = getattr(block, "resource", None)
            uri = getattr(res, "uri", "") if res else ""
            parts.append(f"[resource:{uri}]")
        else:
            # 兜底：转 dict 序列化
            try:
                if hasattr(block, "model_dump"):
                    parts.append(json.dumps(block.model_dump(), ensure_ascii=False))
                else:
                    parts.append(str(block))
            except Exception:  # noqa: BLE001
                parts.append(repr(block))

    text = "\n".join(p for p in parts if p)
    if structured is not None and not text:
        try:
            text = json.dumps(structured, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            text = str(structured)
    if is_error:
        return f"Error: {text or 'unknown MCP tool error'}"
    return text or "(empty)"


def _make_handler(server_id: str, tool_name: str):
    """工厂：为指定 (server_id, tool_name) 生成一个 async handler 闭包。

    handler 接受 **kwargs（CleanToolRegistry 在工具签名包含 var_kwargs 时会塞 context 进来），
    从中提取真正的 tool arguments；session_id / agent_id 等 context 字段忽略
    （MCP 工具自己不需要这些柳如烟内部 id）。
    """
    # context 注入字段（参考 clean_registry._dispatch 的 context 来源）
    _context_keys = {
        "agent_id",
        "session_id",
        "llm_client",
        "json_repair_layers",
        "trace_id",
    }

    async def handler(**kwargs: Any) -> str:
        # 从 kwargs 剥离 context；剩余视为真正的 tool arguments
        arguments = {k: v for k, v in kwargs.items() if k not in _context_keys}

        client = get_controller().get_client(server_id)
        if client is None:
            return f"Error: MCP server '{server_id}' not registered"
        try:
            result = await client.call_tool(tool_name, arguments)
        except StatefulClientError as e:
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001
            logger.exception("[MCP-BRIDGE] %s/%s call failed", server_id, tool_name)
            return f"Error: {type(e).__name__}: {e}"
        return _format_call_result(result)

    handler.__name__ = f"mcp_handler__{server_id}__{tool_name}"
    return handler


# ── per-server 注册簿（unregister 时清理用）──────────────────────────────
_registered_by_server: dict[str, list[str]] = {}


def register_server_tools(tools: list[MCPToolInfo]) -> None:
    """把一批 MCP tools 注入 CleanToolRegistry。同名重复注册会覆盖（重启场景）。"""
    if not tools:
        return
    by_server: dict[str, list[str]] = {}
    for t in tools:
        handler = _make_handler(t.server_id, t.name)
        # MCP 工具默认 read 并发类（绝大多数是查询）。
        # 用户可在 server config 里覆盖为 write/exclusive。
        cfg = get_controller().get_config(t.server_id)
        cls = (cfg.concurrency_class if cfg else "read")
        global_tool_registry.register(
            name=t.qualified_name,
            description=t.description or f"MCP tool from server '{t.server_id}'",
            input_schema=t.input_schema or {"type": "object", "properties": {}, "additionalProperties": True},
            handler=handler,
            is_async=True,
            concurrency_class=cls,
        )
        by_server.setdefault(t.server_id, []).append(t.qualified_name)
        logger.info("[MCP-BRIDGE] registered tool %s", t.qualified_name)
    # 合并入注册簿（覆盖该 server 的旧 entry）
    for sid, names in by_server.items():
        old = _registered_by_server.get(sid, [])
        # 先删旧的，再写新的
        for n in old:
            if n not in names:
                global_tool_registry.tools.pop(n, None)
        _registered_by_server[sid] = names


def unregister_server_tools(server_id: str) -> None:
    """server stop 时调；删掉该 server 注册过的所有工具。"""
    names = _registered_by_server.pop(server_id, [])
    for n in names:
        global_tool_registry.tools.pop(n, None)
        logger.info("[MCP-BRIDGE] unregistered tool %s", n)


def installed_qualified_names(server_id: str) -> list[str]:
    return list(_registered_by_server.get(server_id, []))


def attach_to_controller() -> None:
    """注入 controller —— 在 main.py lifespan 启动钩子里调一次。"""
    ctrl = get_controller()
    ctrl.set_tool_bridge(register_server_tools, unregister_server_tools)
    logger.info("[MCP-BRIDGE] attached to controller")


__all__ = [
    "register_server_tools",
    "unregister_server_tools",
    "installed_qualified_names",
    "attach_to_controller",
]
