"""内置 MCP server 精选目录。

收录覆盖最高频场景的 15 条开源 MCP 服务。新增条目时直接 append；前端 spotlight 自动取这份数据。
图标 URL 优先用公共 CDN（svgl.app）以降低首发包体积，自家图标后续随 frontend 资产分发。
"""

from __future__ import annotations

from ..types import CatalogEntry

BUILTIN_CATALOG: list[CatalogEntry] = [
    CatalogEntry(
        name="filesystem",
        title="Filesystem",
        description="访问本地文件系统：读、写、搜索目录树。仅作用于 args 中显式指定的目录。",
        icon="https://cdn.jsdelivr.net/npm/lucide-static@latest/icons/folder.svg",
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        configuration={
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/directory"],
        },
        tags=["files", "official"],
        requires_token=False,
    ),
    CatalogEntry(
        name="github",
        title="GitHub",
        description="GitHub MCP server：仓库浏览、Issue/PR 管理、代码搜索。",
        icon="https://svgl.app/library/github_light.svg",
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/github",
        configuration={
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "YOUR_TOKEN_HERE"},
        },
        tags=["dev", "official", "popular"],
        requires_token=True,
    ),
    CatalogEntry(
        name="git",
        title="Git",
        description="本地 Git 仓库交互：log / diff / show / blame。",
        icon="https://svgl.app/library/git.svg",
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/git",
        configuration={
            "command": "uvx",
            "args": ["mcp-server-git", "--repository", "/path/to/repo"],
        },
        tags=["dev", "official"],
        requires_token=False,
    ),
    CatalogEntry(
        name="brave-search",
        title="Brave Search",
        description="Brave 搜索 API：网页搜索、本地结果。",
        icon="https://svgl.app/library/brave.svg",
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/brave-search",
        configuration={
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-brave-search"],
            "env": {"BRAVE_API_KEY": "YOUR_API_KEY_HERE"},
        },
        tags=["search", "official"],
        requires_token=True,
    ),
    CatalogEntry(
        name="exa",
        title="Exa",
        description="Exa AI 搜索：面向 AI 的语义网页搜索。",
        icon="https://exa.ai/favicon.ico",
        homepage="https://github.com/exa-labs/exa-mcp-server",
        configuration={
            "command": "npx",
            "args": ["-y", "exa-mcp-server"],
            "env": {"EXA_API_KEY": "YOUR_API_KEY_HERE"},
        },
        tags=["search", "popular"],
        requires_token=True,
    ),
    CatalogEntry(
        name="tavily",
        title="Tavily",
        description="Tavily 搜索 API：实时网页 + 答案合成。",
        icon="https://tavily.com/favicon.ico",
        homepage="https://github.com/tavily-ai/tavily-mcp",
        configuration={
            "command": "npx",
            "args": ["-y", "tavily-mcp"],
            "env": {"TAVILY_API_KEY": "YOUR_API_KEY_HERE"},
        },
        tags=["search"],
        requires_token=True,
    ),
    CatalogEntry(
        name="firecrawl",
        title="Firecrawl",
        description="网页抓取与结构化提取。",
        icon="https://firecrawl.dev/favicon.ico",
        homepage="https://github.com/mendableai/firecrawl-mcp-server",
        configuration={
            "command": "npx",
            "args": ["-y", "firecrawl-mcp"],
            "env": {"FIRECRAWL_API_KEY": "YOUR_API_KEY_HERE"},
        },
        tags=["web", "scrape"],
        requires_token=True,
    ),
    CatalogEntry(
        name="playwright",
        title="Playwright",
        description="浏览器自动化：通过结构化无障碍快照操作网页（无需截图）。",
        icon="https://playwright.dev/img/playwright-logo.svg",
        homepage="https://github.com/microsoft/playwright-mcp",
        configuration={
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest"],
        },
        tags=["browser", "automation", "popular"],
        requires_token=False,
    ),
    CatalogEntry(
        name="notion",
        title="Notion",
        description="Notion 工作区集成：页面、数据库读写。",
        icon="https://svgl.app/library/notion.svg",
        homepage="https://github.com/makenotion/notion-mcp-server",
        configuration={
            "command": "npx",
            "args": ["-y", "@notionhq/notion-mcp-server"],
            "env": {"OPENAPI_MCP_HEADERS": '{"Authorization": "Bearer YOUR_INTEGRATION_TOKEN", "Notion-Version": "2022-06-28"}'},
        },
        tags=["productivity"],
        requires_token=True,
    ),
    CatalogEntry(
        name="slack",
        title="Slack",
        description="Slack 工作区：频道、消息、用户查询。",
        icon="https://svgl.app/library/slack.svg",
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/slack",
        configuration={
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-slack"],
            "env": {
                "SLACK_BOT_TOKEN": "YOUR_BOT_TOKEN",
                "SLACK_TEAM_ID": "YOUR_TEAM_ID",
            },
        },
        tags=["chat", "official"],
        requires_token=True,
    ),
    CatalogEntry(
        name="postgres",
        title="PostgreSQL",
        description="只读 SQL 查询：表结构反射 + safe SELECT。",
        icon="https://svgl.app/library/postgresql.svg",
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/postgres",
        configuration={
            "command": "npx",
            "args": [
                "-y",
                "@modelcontextprotocol/server-postgres",
                "postgresql://USER:PASS@HOST:5432/DB",
            ],
        },
        tags=["database", "official"],
        requires_token=True,
    ),
    CatalogEntry(
        name="sqlite",
        title="SQLite",
        description="本地 SQLite 数据库读写。",
        icon="https://svgl.app/library/sqlite.svg",
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/sqlite",
        configuration={
            "command": "uvx",
            "args": ["mcp-server-sqlite", "--db-path", "/path/to/db.sqlite"],
        },
        tags=["database", "official"],
        requires_token=False,
    ),
    CatalogEntry(
        name="memory",
        title="Memory",
        description="知识图谱式持久记忆：跨会话存取实体关系。",
        icon="https://cdn.jsdelivr.net/npm/lucide-static@latest/icons/brain.svg",
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
        configuration={
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
        },
        tags=["memory", "official"],
        requires_token=False,
    ),
    CatalogEntry(
        name="fetch",
        title="Fetch",
        description="抓取任意 URL 并返回正文（markdown 化）。",
        icon="https://cdn.jsdelivr.net/npm/lucide-static@latest/icons/globe.svg",
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
        configuration={
            "command": "uvx",
            "args": ["mcp-server-fetch"],
        },
        tags=["web", "official"],
        requires_token=False,
    ),
    CatalogEntry(
        name="time",
        title="Time",
        description="时区与时间转换：当前时间、跨时区换算。零配置零 token。",
        icon="https://cdn.jsdelivr.net/npm/lucide-static@latest/icons/clock.svg",
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/time",
        configuration={
            "command": "uvx",
            "args": ["mcp-server-time"],
        },
        tags=["utility", "official"],
        requires_token=False,
    ),
    CatalogEntry(
        name="linear",
        title="Linear",
        description="Linear 项目管理：issue、cycle、team 管理。",
        icon="https://svgl.app/library/linear_light.svg",
        homepage="https://github.com/tacticlaunch/mcp-linear",
        configuration={
            "command": "npx",
            "args": ["-y", "@tacticlaunch/mcp-linear"],
            "env": {"LINEAR_API_KEY": "YOUR_API_KEY_HERE"},
        },
        tags=["productivity"],
        requires_token=True,
    ),
    CatalogEntry(
        name="everything",
        title="Everything (Demo)",
        description="官方 demo server：覆盖全部协议特性，用来调试与回归。",
        icon="https://cdn.jsdelivr.net/npm/lucide-static@latest/icons/boxes.svg",
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/everything",
        configuration={
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-everything"],
        },
        tags=["demo", "official"],
        requires_token=False,
    ),
]


def get_catalog_entry(name: str) -> CatalogEntry | None:
    for entry in BUILTIN_CATALOG:
        if entry.name == name:
            return entry
    return None


__all__ = ["BUILTIN_CATALOG", "get_catalog_entry"]
