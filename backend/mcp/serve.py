"""把柳如烟自己的工具反向暴露为 MCP server（M3.3）。

用途：让外部 MCP 客户端（Claude Desktop / Cursor / 独立终端客户端 / 其它服务…）
直接接进来，使用柳如烟的 read_file / search 等工具。

启动方式：
    python -m backend.mcp.serve [--allow=name1,name2 ...] [--block-mcp]

默认在 stdio 上跑 FastMCP（适合外部客户端用 stdio_client 接入）。
也可以加 --http 跑 streamable_http transport。

★ 安全默认：
  - 默认 block_mcp=True：不暴露 mcp__* 工具（避免外部客户端通过我们级联调
    我们注册的远程 MCP，造成无限循环 / 凭据扩散）
  - 不传 ContextVar session_id / agent_id（外部调用没这个上下文）

★ 不集成进 FastAPI：故意分进程跑——免得外部客户端崩了拖累主 backend。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logger = logging.getLogger(__name__)


def _select_tools(
    allowed_tools: set[str] | None,
    block_mcp_prefix: bool,
) -> list[tuple[str, dict[str, Any]]]:
    from backend.tools.builtin.clean_registry import get_tool_registry

    registry = get_tool_registry()
    out: list[tuple[str, dict[str, Any]]] = []
    for name, tool in registry.tools.items():
        if block_mcp_prefix and name.startswith("mcp__"):
            continue
        if allowed_tools is not None and name not in allowed_tools:
            continue
        out.append((name, tool))
    return out


def _build_server(
    allowed_tools: set[str] | None,
    block_mcp_prefix: bool,
) -> Server:
    """用 lowlevel Server——可以直接接管 list_tools/call_tool 用 CleanToolRegistry 自己的 schema。"""
    from backend.tools.builtin.clean_registry import get_tool_registry

    server: Server = Server("liuruyan")
    exposed = _select_tools(allowed_tools, block_mcp_prefix)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name=name,
                description=(tool.get("description") or "")[:1024],
                inputSchema=tool.get("input_schema") or {"type": "object", "properties": {}},
            )
            for name, tool in exposed
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        registry = get_tool_registry()
        if name not in {n for n, _ in exposed}:
            return [TextContent(type="text", text=f"Error: tool '{name}' not exposed by liuruyan")]
        try:
            # 不传 session_id / agent_id —— 外部 caller 没柳如烟的会话上下文
            result = await registry.execute(name, arguments or {})
        except Exception as e:  # noqa: BLE001
            return [TextContent(type="text", text=f"Error: {e}")]
        if not isinstance(result, str):
            try:
                result = json.dumps(result, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                result = str(result)
        return [TextContent(type="text", text=result)]

    logger.info("[MCP-SERVE] exposing %s tools", len(exposed))
    return server


async def _amain(args: argparse.Namespace) -> None:
    allowed = (
        set(s.strip() for s in args.allow.split(",") if s.strip()) if args.allow else None
    )
    server = _build_server(allowed_tools=allowed, block_mcp_prefix=not args.expose_mcp)

    # stdio：read_stream / write_stream 来自 stdin/stdout
    async with stdio_server() as (read, write):
        await server.run(
            read,
            write,
            server.create_initialization_options(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="liuruyan-mcp-serve")
    parser.add_argument(
        "--allow",
        type=str,
        default=None,
        help="逗号分隔的工具白名单；不传 = 所有非 mcp__ 工具",
    )
    parser.add_argument(
        "--expose-mcp",
        action="store_true",
        help="也把已注册的 mcp__* 工具暴露出去（默认禁；当心级联循环）",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="跑 streamable_http 而不是 stdio",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="打开 DEBUG 日志（写到 stderr，不污染 stdio 协议流）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
