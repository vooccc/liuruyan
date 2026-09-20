"""网络 / 检索 / 观测类工具注册（从 clean_registry.py 拆出）。

web_search / web_extract / session_search / browser_preview / computer_use /
read_logs / read_frontend_events。handler 全部来自各自的 *_tool 模块；
clean_registry 在 import 时调用 register(registry) 完成注册，行为零改动。
"""
from ..registry_validation import _PATH_ALIAS_MAP


def register(registry):
    from ..web_tools import (
        _handle_web_search,
        _handle_web_extract
    )

    # web_search
    registry.register(
        name="web_search",
        description=(
            "Search the web for time-sensitive info (news, prices, docs, current events). "
            "Returns title + URL only — pair with `web_extract` to fetch content.\n\n"
            "STOP RULES — if the result has any of these fields, do NOT retry:\n"
            "- `cache_note`: you already searched this recently\n"
            "- `quality_warning`: backend can't answer this query well\n"
            "- `abuse_warning`: too many searches this session\n"
            "Reformulate with substantially different keywords, or tell the user the info "
            "can't be found. For fuzzy/obscure entities returning no relevant hits in 2 tries, "
            "stop and say it may not exist.\n\n"
            "After using results in your reply, append a 'Sources:' section as markdown "
            "links: [Title](URL).\n\n"
            "include_domains and exclude_domains are mutually exclusive."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询关键词（中英文均可，中文会自动使用中国区索引）"
                },
                "limit": {
                    "type": "integer",
                    "description": "最大返回结果数（1-20，默认5）",
                    "default": 5
                },
                "topic": {
                    "type": "string",
                    "enum": ["general", "news"],
                    "description": "仅 Tavily 生效。'news' 用于时效性事件（专用新闻索引，结果更新）。默认 'general'。",
                    "default": "general"
                },
                "time_range": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year"],
                    "description": "仅 Tavily 生效。按时间范围过滤结果。不传则不过滤。"
                },
                "include_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "仅返回这些域名下的结果（如 ['docs.python.org', 'github.com']）。子域也匹配。用于精准搜索官方文档/指定站点。"
                },
                "exclude_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "排除这些域名的结果（如 ['pinterest.com']）。子域也匹配。用于过滤低质量/SEO垃圾站。"
                }
            },
            "required": ["query"]
        },
        handler=_handle_web_search,
        is_async=True
    )

    # web_extract
    registry.register(
        name="web_extract",
        description=(
            "抓取一个或多个网页 URL，返回其文本/Markdown 内容。"
            "`compress=true`（默认）会用 LLM 压缩内容，减少 80-90% token 用量同时保留关键信息。"
            "中文内容会自动使用中文提示词压缩，避免语义丢失。\n\n"
            "何时使用：\n"
            "✓ 已有 URL 需要获取完整内容（文章/文档/发布说明）\n"
            "✓ 配合 `web_search` 使用：搜索→拿 URL→提取正文\n"
            "✓ 用户贴了链接让你读取\n\n"
            "何时不用：\n"
            "✗ 还不知道 URL——先用 `web_search`\n"
            "✗ GitHub PR/Issue/Release——如果有 `execute_command`，`gh` CLI 给出更干净的结构化数据\n"
            "✗ 读本地文件——用 `read_file`\n\n"
            "每次最多5个 URL。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要提取内容的 URL 列表（最多5个）",
                    "maxItems": 5
                },
                "compress": {
                    "type": "boolean",
                    "description": "使用 LLM 压缩内容（默认 true）。减少 80-90% token 用量同时保留关键信息。中文内容自动用中文提示词。",
                    "default": True
                },
                "offset": {
                    "type": "integer",
                    "description": "每个 URL 内容的字符起点（默认 0）。用上次返回的 'next_offset' 继续翻页。",
                    "default": 0,
                    "minimum": 0
                },
                "limit": {
                    "type": "integer",
                    "description": "每个 URL 内容最大字符数（默认 0 = 不切片）。大于 0 时，返回包含 content_length / has_more / next_offset。",
                    "default": 0,
                    "minimum": 0
                }
            },
            "required": ["urls"]
        },
        handler=_handle_web_extract,
        is_async=True
    )


    # session_search：跨会话搜索历史对话（"那个上次的 bug 怎么修的" 类查询）
    # 设计参考：./backend\tools\builtin\session_search_tool.py
    from ..session_search_tool import _handle_session_search

    registry.register(
        name="session_search",
        description=(
            "Search ACROSS ALL your historical conversations (transcripts) — your 'session archeologist's shovel'. "
            "Hybrid retrieval: BM25 keyword + semantic vector (RRF-fused) when embedding is configured, "
            "BM25-only otherwise. With embedding on, paraphrased/reworded questions also match "
            "(e.g. asking '并发串号怎么修' can recall a transcript that said '上下文变量隔离').\n\n"
            "DIFFERS FROM `recall_memory`:\n"
            "- `recall_memory` searches your CURATED long-term memory (KV store; what you chose to remember)\n"
            "- `session_search` (this tool) searches RAW conversation transcripts (what actually got said)\n"
            "Use this when the user references a past chat: '那个上次的 bug', 'what did we say about X', "
            "'记不记得我们聊过…', etc.\n\n"
            "SCOPE:\n"
            "- 'self' (default): only YOUR own main sessions\n"
            "- 'all': all agents' main sessions + council rooms (cross-role)\n"
            "- 'rooms': only council rooms (multi-role collab history)\n\n"
            "QUERY TIP: self-contained queries with full context work best (vector search rewards rich context). "
            "Need ≥2 char tokens; bare deictic pronouns ('那个'/'it') still weak.\n\n"
            "RESULTS: per-session top snippets (max 2/session by default), ranked by fused BM25+vector score (or BM25+recency when embedding off)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to search. Multiple words combined as OR-with-score-accumulation.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["self", "all", "rooms"],
                    "default": "self",
                    "description": "Which sessions to search.",
                },
                "max_results": {
                    "type": "integer",
                    "default": 5,
                    "description": "Max sessions to return.",
                },
                "max_per_session": {
                    "type": "integer",
                    "default": 2,
                    "description": "Max snippets per session (avoid dumping a whole conversation).",
                },
                "recency_days": {
                    "type": "integer",
                    "default": 7,
                    "description": "Sessions active within this many days get a +2 score bonus.",
                },
                "days_back": {
                    "type": "integer",
                    "description": "Only search sessions active in the last N days (omit = no time filter).",
                },
                "role": {
                    "type": "string",
                    "enum": ["user", "assistant"],
                    "description": "Only match messages from this role (omit = both).",
                },
            },
            "required": ["query"],
        },
        handler=_handle_session_search,
        is_async=True,
    )


    # browser_preview：在 chat 中嵌入 URL 的实时 iframe 预览
    # 设计参考：./backend\tools\builtin\browser_preview_tool.py
    from ..browser_preview_tool import _handle_browser_preview

    registry.register(
        name="browser_preview",
        description=(
            "Embed a live iframe preview of a URL in the chat. "
            "Use this when you've started a local dev server (vite/next/django/streamlit) and want the user "
            "to see the result immediately, or when verifying a frontend fix.\n\n"
            "WHEN TO USE:\n"
            "✓ Just started `npm run dev` / `python -m http.server` etc. — show the user what's running\n"
            "✓ Verified a UI fix — let the user check it in-place without leaving chat\n"
            "✓ User asked 'how does it look' / '最终长什么样'\n\n"
            "WHEN NOT TO USE:\n"
            "✗ Want page **content** (HTML/text) → use web_extract\n"
            "✗ Web search → use web_search\n"
            "✗ User just pasted a link to read → use web_extract\n\n"
            "LIMITATIONS:\n"
            "- Only http/https; localhost dev servers go white if you stop the server\n"
            "- Some sites block iframe embedding via X-Frame-Options/CSP — user will see blank; "
            "fall back to suggesting they open in new tab\n"
            "- Preview is per-process and not persisted across restarts"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL with http(s) scheme. Local dev server e.g. 'http://localhost:5173'.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional friendly display name (e.g. 'My blog homepage'). Defaults to host+path truncated to 60 chars.",
                },
            },
            "required": ["url"],
        },
        handler=_handle_browser_preview,
        is_async=True,
    )


    # computer_use：跨平台屏幕/键鼠控制（M1 = screenshot + list_displays）
    # 协议契约对齐 anthropic 公开规范 computer_20250124；实现在 backend/computer_use/
    # 设计参考：./backend\computer_use\__init__.py
    from ..computer_use_tool import _handle_computer_use
    from backend.computer_use.schema import COMPUTER_USE_TOOL_SCHEMA as _CU_SCHEMA

    registry.register(
        name="computer_use",
        description=(
            "Control the user's screen, mouse, and keyboard. Take screenshots, click, type, "
            "scroll, drag — to operate desktop applications the user can't easily reach via "
            "shell or APIs.\n\n"
            "PROTOCOL: aligned with anthropic computer_20250124 (action enum + coordinate/text "
            "fields). Models trained on Claude's computer-use data can call this directly.\n\n"
            "WHEN TO USE:\n"
            "✓ User asks 'click on the login button' / '帮我打开记事本'\n"
            "✓ Need visual confirmation of a desktop app's state (screenshot before/after)\n"
            "✓ Automating GUI workflows that don't have CLI/API alternatives\n\n"
            "WHEN NOT TO USE:\n"
            "✗ Anything achievable via shell (run_command), HTTP (web_fetch), or files — those "
            "are faster, more reliable, and don't fight with the user's actual mouse\n"
            "✗ Reading text from the screen when the source is a file/page you can read directly\n\n"
            "SAFETY:\n"
            "- Single-instance lock: two sessions cannot control the screen at once\n"
            "- Each action is audited; user can hit STOP at any time from the UI\n"
            "- Always screenshot first to verify the visual state before clicking\n\n"
            "AVAILABLE ACTIONS:\n"
            "- screenshot:  capture current display → returns inline image (preferred FIRST step)\n"
            "- list_displays:  enumerate monitors\n"
            "- left_click / right_click / middle_click / double_click / triple_click: "
            "click at [x, y]; optional 'modifiers' = ['shift', 'ctrl', ...]\n"
            "- mouse_move:  move cursor to [x, y] without clicking\n"
            "- left_click_drag:  drag from current (or 'start_coordinate') to [x, y]\n"
            "- left_mouse_down / left_mouse_up:  for custom drag/select sequences\n"
            "- cursor_position:  read current mouse position\n"
            "- scroll:  needs scroll_direction (up/down/left/right) and scroll_amount (1-100)\n"
            "- key:  press key sequence, e.g. text='ctrl+c' or 'enter'; optional 'repeat'\n"
            "- hold_key:  hold key(s) for 'duration' seconds (capped 10s)\n"
            "- type:  type text; CJK/Unicode auto-routed via clipboard (avoids IME issues)\n"
            "- read_clipboard / write_clipboard:  read or set system clipboard\n"
            "- wait:  sleep 'duration' seconds before next action (capped 10s)\n\n"
            "RECOMMENDED FLOW:\n"
            "1. Always screenshot first to see the actual visual state\n"
            "2. Reason about coordinates from the screenshot (not memory of past layouts)\n"
            "3. After a click/type/scroll, screenshot AGAIN to verify the result before continuing"
        ),
        input_schema=_CU_SCHEMA,
        handler=_handle_computer_use,
        is_async=True,
    )


    # read_logs：tail/head + grep + level 过滤一个日志文件
    # 设计参考：./backend\tools\builtin\read_logs_tool.py
    from ..read_logs_tool import _handle_read_logs

    registry.register(
        name="read_logs",
        description=(
            "Read a log file with smart tail/head + filtering. Use this to inspect logs for self-debugging "
            "(crash traces, slow request investigation, tool call audits) instead of dumping the whole file "
            "via read_file.\n\n"
            "WHEN TO USE:\n"
            "✓ User reports a bug — tail the relevant .log / .jsonl to see the latest stack trace\n"
            "✓ Investigating tool behavior — read tool_stats.jsonl with level=ERROR or grep=<tool_name>\n"
            "✓ Long-running process diagnostics — tail npm/dev-server output written to a file\n"
            "✓ Large log files (> 1MB) — efficient reverse-read, no full load\n\n"
            "WHEN NOT TO USE:\n"
            "✗ Reading source code / config files → use read_file\n"
            "✗ Following a live log stream → use run_command with `tail -f` (this tool is single-shot)\n"
            "✗ Binary log files (.evtx, .pcap) → not supported, will be rejected\n\n"
            "JSONL-AWARE: if file ends with .jsonl, level filter parses each line as JSON and matches the "
            "'level' / 'levelname' / 'lvl' / 'severity' field; plain logs match level tokens in text.\n\n"
            "OUTPUT CAPS: at most 5000 lines / 200KB JSON; truncated=true when exceeded — narrow with grep/level."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Log file path. Absolute used as-is; relative path resolved against "
                        "working_directory → agent_dir → cwd (same as read_file)."
                    ),
                },
                "tail_lines": {
                    "type": "integer",
                    "description": "Number of trailing lines to read (default 200, max 5000). Ignored if head=true.",
                    "minimum": 1,
                    "maximum": 5000,
                },
                "head": {
                    "type": "boolean",
                    "description": "If true, read from the beginning instead of tail. Defaults to false.",
                },
                "grep": {
                    "type": "string",
                    "description": (
                        "Optional Python regex (re.search semantics, case-sensitive). "
                        "Only matching lines returned. Example: 'Traceback|ERROR'."
                    ),
                },
                "level": {
                    "type": "string",
                    "description": (
                        "Optional level filter: ERROR / WARN / INFO / DEBUG (case-insensitive). "
                        "JSONL files: matches the 'level' / 'levelname' / 'lvl' / 'severity' field. "
                        "Plain logs: matches level tokens in text (ERROR/ERR/FATAL for ERROR, etc.)."
                    ),
                    "enum": ["ERROR", "WARN", "WARNING", "INFO", "DEBUG", "error", "warn", "warning", "info", "debug"],
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Cap on returned lines after filtering (default = tail_lines, hard max 5000).",
                    "minimum": 1,
                    "maximum": 5000,
                },
            },
            "required": ["path"],
        },
        handler=_handle_read_logs,
        is_async=True,
        arg_aliases={
            # 路径别名走共享表（新增 LLM 母语写法只需改 registry_validation 一处）
            **_PATH_ALIAS_MAP,
            "filename": "path",
            "log_file": "path",
            "tail": "tail_lines",
            "n": "tail_lines",
            "lines": "tail_lines",
            "pattern": "grep",
            "regex": "grep",
            "query": "grep",
            "severity": "level",
        },
    )


    # read_frontend_events：前端遥测语义化读取（路径 A 配套）
    # 设计参考：./backend\tools\builtin\frontend_events_tool.py
    from ..frontend_events_tool import _handle_read_frontend_events

    registry.register(
        name="read_frontend_events",
        description=(
            "Read recent frontend telemetry events (console errors, fetch/XHR network calls, "
            "uncaught JS errors, unhandled promise rejections) collected by the boot-time devTelemetry "
            "hook. Use this when the user reports a UI bug — instead of asking them to copy-paste "
            "DevTools console / Network panel.\n\n"
            "WHEN TO USE:\n"
            "✓ User says 'something is broken in the UI' / '页面报错了' — read recent ERROR events\n"
            "✓ Investigating a slow / failing API call — filter event_type=network with status_min=400\n"
            "✓ Component crash diagnosis — filter event_type=error or unhandledrejection\n"
            "✓ Verifying a fix landed — re-run user action then read events since=2 min\n\n"
            "WHEN NOT TO USE:\n"
            "✗ Backend log inspection → use read_logs on tool_stats.jsonl / etc.\n"
            "✗ Reading source code → use read_file\n"
            "✗ Triggering UI actions / clicking buttons (this is read-only telemetry; the tool can't "
            "drive the page — for that you'd need a future probe tool)\n\n"
            "EVENT SCHEMA (JSON each):\n"
            "  type: 'console' | 'network' | 'error' | 'unhandledrejection'\n"
            "  level: 'ERROR' | 'WARN' | 'INFO'\n"
            "  ts / server_ts: epoch milliseconds\n"
            "  msg / stack: present for console + error events\n"
            "  url / method / status / duration_ms: present for network events\n"
            "  source / lineno / colno: present for window errors\n"
            "  page: route at the time (location.pathname + hash)\n\n"
            "DEFAULTS: since_min=30, max_events=100. Returns events oldest→newest within the matched window."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "since_min": {
                    "type": "integer",
                    "description": "Look back N minutes from now (default 30, max 1440 = 24h).",
                    "minimum": 1,
                    "maximum": 1440,
                },
                "event_type": {
                    "type": "string",
                    "description": (
                        "Filter by event type. 'console' = console.error/warn; 'network' = fetch/XHR; "
                        "'error' = window 'error' (uncaught JS); 'unhandledrejection' = Promise rejection."
                    ),
                    "enum": ["console", "network", "error", "unhandledrejection", "perf", "custom"],
                },
                "level": {
                    "type": "string",
                    "description": "Filter by severity. ERROR / WARN / INFO / DEBUG (case-insensitive).",
                    "enum": ["ERROR", "WARN", "WARNING", "INFO", "DEBUG", "error", "warn", "warning", "info", "debug"],
                },
                "grep": {
                    "type": "string",
                    "description": (
                        "Optional Python regex (re.search semantics). Searches across msg / stack / url / "
                        "name / source. Example: 'TypeError|Cannot read'."
                    ),
                },
                "status_min": {
                    "type": "integer",
                    "description": "For network events: only include status >= this. Set 400 to see 4xx+5xx only.",
                    "minimum": 0,
                    "maximum": 599,
                },
                "max_events": {
                    "type": "integer",
                    "description": "Max events to return (default 100, max 500).",
                    "minimum": 1,
                    "maximum": 500,
                },
            },
            "required": [],
        },
        handler=_handle_read_frontend_events,
        is_async=True,
        arg_aliases={
            "minutes": "since_min",
            "since_minutes": "since_min",
            "since": "since_min",
            "type": "event_type",
            "kind": "event_type",
            "category": "event_type",
            "severity": "level",
            "pattern": "grep",
            "regex": "grep",
            "query": "grep",
            "min_status": "status_min",
            "limit": "max_events",
            "n": "max_events",
        },
    )
