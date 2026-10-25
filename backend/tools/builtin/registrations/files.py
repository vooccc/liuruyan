"""文件 / 项目记忆 / 技能包类工具注册（从 clean_registry.py 拆出）。

涵盖：read_file / write_file / patch / grep / edit / multi_edit / git_* /
outline / check_typescript / self_reflect / memory_audit、project-memory /
lry-workspace、agent-memory、skill-pack 等工具。handler 全部来自各自 *_tool /
lry_workspace_tools 模块；clean_registry import 时调用 register(registry)，行为零改动。
"""
from ..registry_core import CONCURRENCY_READ
from ..registry_validation import (
    _PATH_ALIAS_MAP,
    _PATHS_PLURAL_ALIAS_MAP,
    _PATTERN_ALIAS_MAP,
)


def register(registry):
    from ..multi_edit_tool import _handle_multi_edit
    from ..self_reflect_tool import _handle_tool_self_reflect
    from ..git_tool import (
        _handle_git_status,
        _handle_git_log,
        _handle_git_diff,
        _handle_git_blame,
    )
    from ..outline_tool import _handle_outline_file
    from ..typescript_tool import _handle_check_typescript
    from ..agent_memory_tool import (
        _handle_create_memory,
        _handle_recall_memory,
    )
    from ..project_binding_tool import _handle_set_project_binding
    from backend.lry_workspace_tools import (
        _handle_add_project_rule,
        _handle_update_project_notes,
        _handle_save_project_memory,
        _handle_add_project_workflow,
        _handle_archive_lry_file,
        _handle_restore_lry_file,
        _handle_recall_project_memory,
        _handle_get_project_workflow,
        _handle_list_project_rules,
    )
    from ..file_tools import (
        _handle_read_file,
        _handle_write_file,
        _handle_patch,
        _handle_grep,
    )

    # read_file 只接单文件 path（对齐 Claude/Cascade）。批量读靠模型在同一轮发出 N 个
    # read_file 调用（parallel tool calls），dispatch 层本身会并发执行。不再提供 paths 参数。

    registry.register(
        name="read_file",
        description=(
            "Reads a single file from the local filesystem. For multiple files, send parallel read_file "
            "calls in one turn (parallel tool calls).\n\n"
            "Usage:\n"
            "- Absolute paths preferred; relative paths resolve to the project root.\n"
            "- By default reads up to 2000 lines from line 1; use `offset` / `limit` for ranges (1-indexed, `cat -n` format).\n"
            "- Supported file types:\n"
            "  - Text / source code -> `cat -n` lines\n"
            "  - Images (.png / .jpg / .jpeg / .gif / .webp / .bmp) -> base64 inline for vision models "
            "(Claude / GPT-4o / Qwen-VL); max 5 MB\n"
            "  - PDF (.pdf) -> Anthropic native `document` block, pypdf / pdfplumber text, or (scanned "
            "image-only PDFs) page images inlined as base64 blocks for any vision model; see `pages` / `pdf_extractor` params\n"
            "  - Office (.docx / .pptx / .xlsx) -> paragraphs / slides / sheets with tables auto-converted to markdown\n"
            "  - Jupyter notebook (.ipynb) -> cells rendered as markdown / fenced code blocks; outputs truncated\n"
            "- Screenshots: ALWAYS use this tool — multimodal models see images directly.\n"
            "- Binary (.zip / .exe / .doc / .ppt / .xls / ...) and directories return errors "
            "(use `glob_search` to list directories).\n"
            "- Empty files trigger a system reminder."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Single file path (absolute preferred)"},
                "offset": {"type": "integer", "default": 1, "description": "Starting line (1-indexed)"},
                "limit": {"type": "integer", "default": 2000, "description": "Lines per file (default 2000, aligns with Claude / Cascade)"},
                "pages": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "PDF only: 1-indexed page numbers to read (e.g. [1, 5, 8]). "
                        "Empty/absent = full PDF. Out-of-range pages silently skipped. "
                        "On Anthropic native path treated as a focus hint (Claude sees full PDF). "
                        "Ignored for non-PDF files."
                    ),
                },
                "pdf_extractor": {
                    "type": "string",
                    "enum": ["pypdf", "pdfplumber"],
                    "default": "pypdf",
                    "description": (
                        "PDF only: text extractor — 'pypdf' (default, fast) or "
                        "'pdfplumber' (slower, better tables / complex layouts; "
                        "tables converted to markdown). Ignored for non-PDF files."
                    ),
                },
            },
            "required": ["path"],
        },
        handler=_handle_read_file,
        arg_aliases=_PATH_ALIAS_MAP,
    )

    # write_file
    registry.register(
        name="write_file",
        description=(
            "Writes a file to the local filesystem.\n\n"
            "Usage:\n"
            "- This tool will overwrite the existing file if there is one at the provided path.\n"
            "- If this is an existing file, you MUST use the `read_file` tool first to read the file's contents. This tool will fail if you did not read the file first.\n"
            "- Prefer the `edit` tool for modifying existing files — it only sends the diff. Only use this tool to create new files or for complete rewrites.\n"
            "- NEVER create documentation files (*.md) or README files unless explicitly requested by the User.\n"
            "- Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked.\n"
            "- Writes to `backend/**` are blocked by Guardian for cross-agent safety."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to write"
                },
                "content": {
                    "type": "string",
                    "description": "Content to write"
                }
            },
            "required": ["path", "content"]
        },
        handler=_handle_write_file,
        arg_aliases=_PATH_ALIAS_MAP,
    )

    # edit (find-and-replace; 原名 patch，2026-05-11 rename 以对齐 Claude/Cascade 命名)
    registry.register(
        arg_aliases=_PATH_ALIAS_MAP,
        name="edit",
        description=(
            "Performs exact string replacements in files.\n\n"
            "Usage:\n"
            "- You must use your `read_file` tool at least once in the conversation before editing. This tool will error if you attempt an edit without reading the file.\n"
            "- When editing text from `read_file` output, ensure you preserve the exact indentation (tabs/spaces) as it appears AFTER the line number prefix. The line number prefix format is: line number + tab. Everything after that is the actual file content to match. Never include any part of the line number prefix in the `old_string` or `new_string`.\n"
            "- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.\n"
            "- Only use emojis if the user explicitly requests it. Avoid adding emojis to files unless asked.\n"
            "- The edit will FAIL if `old_string` is not unique in the file. Either provide a larger string with more surrounding context to make it unique or use `replace_all` to change every instance of `old_string`.\n"
            "- Use `replace_all` for replacing and renaming strings across the file. This parameter is useful if you want to rename a variable for instance.\n"
            "- Writes to `backend/**` are blocked by Guardian for cross-agent safety."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path"
                },
                "old_string": {
                    "type": "string",
                    "description": "Text to replace"
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text"
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences",
                    "default": False
                }
            },
            "required": ["path", "old_string", "new_string"]
        },
        handler=_handle_patch
    )

    # multi_edit (atomic multi-point edits)
    registry.register(
        name="multi_edit",
        description=(
            "This is a tool for making multiple edits to a single file in one operation. It is built on top of the `edit` tool and allows you to perform multiple find-and-replace operations efficiently. Prefer this tool over the `edit` tool when you need to make multiple edits to the same file.\n\n"
            "Before using this tool:\n"
            "1. Use the `read_file` tool to understand the file's contents and context.\n"
            "2. Verify the path is correct.\n\n"
            "To make multiple file edits, provide the following:\n"
            "1. `path`: The path to the file to modify.\n"
            "2. `edits`: An array of edit operations to perform sequentially, where each edit contains:\n"
            "   - `old_string`: The text to replace (must match the file contents exactly, including all whitespace and indentation).\n"
            "   - `new_string`: The edited text to replace the `old_string`.\n"
            "   - `replace_all`: Replace all occurrences of `old_string`. Optional, defaults to false.\n\n"
            "IMPORTANT:\n"
            "- All edits are applied in sequence, in the order they are provided.\n"
            "- Each edit operates on the result of the previous edit.\n"
            "- All edits must be valid for the operation to succeed — if any edit fails, none will be applied.\n"
            "- This tool is ideal when you need to make several changes to different parts of the same file.\n\n"
            "CRITICAL REQUIREMENTS:\n"
            "- The tool will fail if `old_string` doesn't match the file contents exactly (including whitespace).\n"
            "- The tool will fail if `old_string` and `new_string` are the same.\n"
            "- Since edits are applied in sequence, ensure that earlier edits don't affect the text that later edits are trying to find.\n\n"
            "When making edits:\n"
            "- Ensure all edits result in idiomatic, correct code.\n"
            "- Do not leave the code in a broken state.\n"
            "- Only use emojis if the user explicitly requests it. Avoid adding emojis to files unless asked.\n"
            "- Use `replace_all` for replacing and renaming strings across the file.\n"
            "- Writes to `backend/**` are blocked by Guardian for cross-agent safety."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to modify"},
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "description": "Sequential edits (each operates on the result of the previous)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string", "description": "Text to replace"},
                            "new_string": {"type": "string", "description": "Replacement text"},
                            "replace_all": {
                                "type": "boolean",
                                "description": "Replace all occurrences (default false)",
                                "default": False,
                            },
                        },
                        "required": ["old_string", "new_string"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
        handler=_handle_multi_edit,
        arg_aliases=_PATH_ALIAS_MAP,
    )

    # tool_self_reflect (per-agent stats inspection)
    registry.register(
        name="tool_self_reflect",
        description=(
            "Look at YOUR OWN tool-calling history: which tools you struggle with, "
            "which fail repeatedly, which patterns lead to streak failures.\n\n"
            "WHEN TO USE:\n"
            "✓ Stuck in a retry loop on the same kind of error\n"
            "✓ Before a high-stakes task — verify your habits are healthy\n"
            "✓ Periodic self-check (every 50-100 tool calls)\n"
            "✗ Just curious about overall system stats — that's `/tools/stats/insights` (admin level)\n\n"
            "Returns: {summary, issues, top_tools, top_errors, self_advice}. "
            "self_advice is concise actionable suggestions tailored to your stats. "
            "If `include_global=true`, also returns aggregated stats across ALL agents for comparison."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_global": {
                    "type": "boolean",
                    "description": "Also include global (all-agents) summary for comparison",
                    "default": False,
                },
            },
        },
        handler=_handle_tool_self_reflect,
    )

    # git_status
    registry.register(
        name="git_status",
        description=(
            "Show working tree status: which files are modified / added / deleted / untracked.\n\n"
            "WHEN TO USE:\n"
            "✓ Before committing — confirm what's changed\n"
            "✓ Picking up someone else's work-in-progress repo\n"
            "✓ Verifying that a self-evolution write actually landed on disk\n\n"
            "Returns structured: {branch, modified, added, deleted, untracked, renamed, clean}."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_handle_git_status,
    )

    # git_log
    registry.register(
        name="git_log",
        description=(
            "Show recent commits, optionally filtered by path / author / time.\n\n"
            "WHEN TO USE:\n"
            "✓ 'Why does this code look this way?' — find the commit that introduced it\n"
            "✓ 'When did this regression start?' — bisect by time window\n"
            "✓ 'Who's been touching this file?' — author filter\n\n"
            "Returns: {commits: [{hash, author, email, date, message}], count}. "
            "Use `git_diff` with the returned hash for full change content."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Limit to commits touching this path"},
                "limit": {"type": "integer", "default": 10, "description": "Number of commits (1-200)"},
                "author": {"type": "string", "description": "Filter by author name/email substring"},
                "since": {"type": "string", "description": "Time spec (e.g., '2 weeks ago', '2024-01-01')"},
            },
        },
        handler=_handle_git_log,
        arg_aliases=_PATH_ALIAS_MAP,
    )

    # git_diff
    registry.register(
        name="git_diff",
        description=(
            "Show diff in unified format. Several modes by argument combo:\n"
            "- No refs: working tree vs HEAD (uncommitted changes)\n"
            "- staged=true: staged area vs HEAD\n"
            "- ref_a only: ref_a vs working tree\n"
            "- ref_a + ref_b: ref_a vs ref_b\n\n"
            "Add `path` to limit to a file. Use `stat_only=true` for a summary (just file names + line counts).\n\n"
            "Returns: {diff: '<unified diff text>', empty, truncated, stat_only}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ref_a": {"type": "string", "description": "First ref (commit/branch/tag)"},
                "ref_b": {"type": "string", "description": "Second ref"},
                "path": {"type": "string", "description": "Limit diff to this path"},
                "staged": {"type": "boolean", "default": False, "description": "Diff staged area vs HEAD"},
                "stat_only": {"type": "boolean", "default": False, "description": "Return file-level stats only"},
            },
        },
        handler=_handle_git_diff,
        arg_aliases=_PATH_ALIAS_MAP,
    )

    # git_blame
    registry.register(
        name="git_blame",
        description=(
            "Show per-line last-modifier info for a file: hash + author + email + author_time + summary + content per line. "
            "Use `line_start` (and optional `line_end`) to limit the range; omit both for the whole file.\n\n"
            "WHEN TO USE:\n"
            "- 'Who last touched this line / region?' \n"
            "- 'Which commit introduced this bug?' (find the hash, then git_log/git_diff that hash)\n\n"
            "Returns: {path, count, entries: [{hash, author, author_mail, author_time, summary, line, content}]}."
        ),
        input_schema={
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "File path (relative to repo root)"},
                "line_start": {"type": "integer", "minimum": 1, "description": "First line (1-based, inclusive)"},
                "line_end": {"type": "integer", "minimum": 1, "description": "Last line (1-based, inclusive). Defaults to line_start."},
            },
        },
        handler=_handle_git_blame,
        arg_aliases=_PATH_ALIAS_MAP,
    )

    # outline_file (structural overview without reading whole file)
    registry.register(
        name="outline_file",
        description=(
            "Get a structural outline of one or more files: top-level classes / functions / methods "
            "with line ranges. Cheaper than `read_file` for navigation in large files.\n\n"
            "WHEN TO USE:\n"
            "✓ Big file (>500 lines) — get the map first, then `read_file` only the relevant chunk\n"
            "✓ 'Where is `function foo` defined?' in an unfamiliar file\n"
            "✓ Plan multi-edit on a large file — see all targets before crafting edits\n"
            "✓ Exploring several files at once → pass them all in `paths`, get one batched result\n"
            "✗ Small file (<200 lines) → just `read_file` directly\n\n"
            "BATCHING: pass `paths` as an array to outline N files in one call. Single-file? Pass a one-element array.\n\n"
            "Languages: Python (AST-accurate), JS/TS (regex-based), others (generic regex). "
            "Single-file returns: {path, language, symbols, line_count}. "
            "Batched returns: {outlines: [<single-file result>, ...]}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                    "description": "File paths to outline (1-20 per call)",
                },
                "language": {
                    "type": "string",
                    "description": "Force a parser (python|javascript|typescript|generic). Default: auto by extension.",
                },
            },
            "required": ["paths"],
        },
        handler=_handle_outline_file,
        arg_aliases=_PATHS_PLURAL_ALIAS_MAP,
    )

    # check_typescript (frontend type check via tsc --noEmit)
    registry.register(
        name="check_typescript",
        description=(
            "Run frontend TypeScript type check (tsc --noEmit). Returns structured errors.\n\n"
            "WHEN TO USE:\n"
            "✓ After modifying any `.ts` / `.tsx` in frontend/ — confirm types still align\n"
            "✓ Before claiming a frontend task is done\n"
            "✗ Lint / format issues — that's eslint, not tsc\n\n"
            "Caveats: Whole-project check, takes 5-30s on cold runs (incremental cache cuts later runs). "
            "Use `path` to filter results to a specific file (the check still scans whole project, "
            "but only matching diagnostics return).\n"
            "Returns: {ok, errors: [{file, line, col, code, severity, message}], total, returncode}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Optional: filter errors to this file path"},
                "project": {"type": "string", "description": "tsconfig path (default tsconfig.json)"},
            },
        },
        handler=_handle_check_typescript,
        arg_aliases=_PATH_ALIAS_MAP,
    )

    # create_memory (Cascade-aligned: action enum unifies create/update/delete)
    registry.register(
        name="create_memory",
        description=(
            "Save important context relevant to the USER and their task to a memory database.\n"
            "Examples of context to save:\n"
            "- USER preferences (但跨会话仍成立的\"对人的核心认知\"——称呼/重要偏好/共同经历——应改 USER.md，不在这里)\n"
            "- Explicit USER requests to remember something or otherwise alter your behavior\n"
            "- Important code snippets / patterns\n"
            "- Technical stacks and project structure\n"
            "- Major milestones or features\n"
            "- Hard-won bug fixes (symptom + root cause + fix)\n"
            "- New design patterns and architectural decisions\n"
            "- Any other information you think is important to remember.\n"
            "Before creating a new memory, first call `recall_memory` to see if a related memory "
            "already exists. If found, update it (action='update', id=<existing>) instead of creating a duplicate.\n"
            "Use action='delete' to remove incorrect or stale memories."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "delete"],
                    "description": "The type of action to take. Must be one of 'create', 'update', or 'delete'.",
                },
                "id": {
                    "type": "string",
                    "description": "Id of an existing memory to update or delete. Required for update/delete; leave blank for create.",
                },
                "title": {
                    "type": "string",
                    "description": "Descriptive title for the memory. Required when creating or updating.",
                },
                "content": {
                    "type": "string",
                    "description": "Content of a new or updated memory. Required for create/update; leave blank for delete.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to associate with the memory. Used to filter or retrieve. Use snake_case (e.g. ['bug', 'frontend', 'sse']).",
                },
                "user_triggered": {
                    "type": "boolean",
                    "description": "Set true if the user explicitly asked you to create/modify this memory.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["private", "shared"],
                    "default": "private",
                    "description": (
                        "Memory scope. 'private' (default) = only this role/agent can see; "
                        "'shared' = visible to ALL roles (use for project-wide facts like tech stack, conventions). "
                        "For 'delete', scope is auto-resolved (private first, then shared)."
                    ),
                },
                "temp": {
                    "type": "string",
                    "enum": ["cold", "warm", "burning"],
                    "description": (
                        "Memory temperature (importance / persistence priority). USUALLY OMIT — auto-managed: "
                        "every recall hit increments count; reaching 3 hits → warm, 10+ → burning. "
                        "Set explicitly ONLY for: (1) execute crucial bug-fix root-causes (mark 'burning'), "
                        "(2) deeply emotional moments user wants always remembered (mark 'burning'). "
                        "Burning memories rank ~50% higher in recall. Setting explicitly disables auto-cooling."
                    ),
                },
                "locked": {
                    "type": "boolean",
                    "description": (
                        "If true, this memory never decays automatically (memory_audit won't cool it down). "
                        "Use for permanent facts you must never forget — e.g. user's irreversible preferences, "
                        "critical project invariants. USUALLY OMIT (default false)."
                    ),
                },
            },
            "required": ["action"],
        },
        arg_aliases={"memory_id": "id", "Action": "action", "Id": "id", "Title": "title", "Content": "content", "Tags": "tags", "UserTriggered": "user_triggered"},
        handler=_handle_create_memory,
        is_async=True,
    )

    # recall_memory (search persistent memory; embedding + BM25 hybrid; empty query+tags lists by recency)
    registry.register(
        name="recall_memory",
        description=(
            "Search your persistent memory. Hybrid retrieval (embedding + BM25 + tag) when embedding is configured; "
            "falls back to BM25 (substring + tag) when not. Each result includes a `match_reason` field explaining "
            "why it was matched (vector_sim / bm25_rank / rrf_score / tag_match / scope / temp).\n\n"
            "Provide `query` OR `tags` (or both). Omit both to list most recent memories by ts (audit/cleanup mode).\n\n"
            "QUERY TIP: prefer self-contained queries with full context. Bad: 'that bug'. "
            "Good: 'how to fix the SSE token-not-updating bug'. Vector search rewards rich context; "
            "deictic pronouns ('that'/'it'/'the one') return scattered results.\n\n"
            "SCOPE:\n"
            "- 'private' (default): only your own memories\n"
            "- 'shared': only cross-role shared memories (project facts, conventions)\n"
            "- 'all': both, fused via Reciprocal Rank Fusion\n\n"
            "MODE:\n"
            "- 'auto' (default): use embedding if enabled, else BM25\n"
            "- 'bm25': force literal substring/tag matching (debug)\n"
            "- 'vector': force semantic vector search (returns empty if embedding not configured)"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search. With embedding enabled, semantic neighbors also match (e.g. '事件流不更新' 会命中 'SSE 不生效')."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ALL of these tags must be on the memory.",
                },
                "top_k": {"type": "integer", "default": 8, "description": "Max results (1-50)."},
                "scope": {
                    "type": "string",
                    "enum": ["private", "shared", "all"],
                    "default": "private",
                    "description": "Memory scope to search. Use 'all' for cross-role + private fused recall.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "bm25", "vector"],
                    "default": "auto",
                    "description": "Retrieval mode. 'auto' = hybrid if embedding enabled else BM25.",
                },
            },
        },
        handler=_handle_recall_memory,
        is_async=True,
    )


    # memory_audit (self-evolving memory：检测重复 + 冷记忆 + 温度衰减)
    from ..memory_audit_tool import _handle_memory_audit

    registry.register(
        name="memory_audit",
        description=(
            "Audit your long-term memory system: detect duplicates, cold/stale records, and apply temperature decay. "
            "Self-evolution tool — call periodically (or via cron) to keep memory clean.\n\n"
            "BEHAVIOR:\n"
            "- Default (no `actions`): DRY-RUN — only reports findings, no writes\n"
            "- actions=['decay']: cool down warm/burning records that haven't been recalled in `decay_days` (default 14)\n"
            "- actions=['prune']: DELETE cold records untouched for `prune_days` (default 90, non-locked only)\n"
            "- actions=['dedupe']: merge semantic duplicates (vector_sim ≥ `dedupe_threshold`, default 0.92). "
            "Requires embedding enabled. Keeps the more-recalled / newer / locked one.\n\n"
            "WHEN TO USE:\n"
            "✓ Memory feels noisy or recall returns junk\n"
            "✓ Periodic maintenance (consider cron_create with weekly schedule)\n"
            "✓ Before a major project handoff — clean up before archiving\n\n"
            "WHEN NOT TO USE:\n"
            "✗ Just for a single bad memory — use create_memory(action='delete', id=...) instead\n"
            "✗ Right after enabling embedding — let recall populate metrics first\n\n"
            "SAFETY: All write actions skip `locked: true` records and `temp_user_set` records (user-curated). "
            "Always do a dry-run first, review findings, then re-call with actions to commit changes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["private", "shared", "all"],
                    "default": "private",
                    "description": "Memory scope to audit. 'all' = both private + shared.",
                },
                "actions": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["decay", "dedupe", "prune"]},
                    "default": [],
                    "description": "Empty = dry-run (report only). Subset of {'decay','dedupe','prune'} to apply.",
                },
                "decay_days": {
                    "type": "integer",
                    "default": 14,
                    "description": "Days without recall before warm → cold (or burning → warm).",
                },
                "prune_days": {
                    "type": "integer",
                    "default": 90,
                    "description": "Days without recall before a cold record becomes a prune candidate.",
                },
                "dedupe_threshold": {
                    "type": "number",
                    "default": 0.92,
                    "description": "Vector cosine similarity threshold for duplicate detection (0..1).",
                },
            },
        },
        handler=_handle_memory_audit,
        is_async=True,
    )


    # ============================================================
    # Skill Pack 管理（兼容 Anthropic Claude Code Plugin 协议）
    # 设计文档：docs/skill_marketplace.md
    # 实现：backend/skill_pack_manager.py + backend/tools/builtin/skill_pack_tool.py
    # ============================================================
    from ..skill_pack_tool import (
        _handle_install_skill_pack,
        _handle_list_skill_packs,
        _handle_load_skill,
        _handle_skill_pack_status,
        _handle_uninstall_skill_pack,
        _handle_update_skill_pack,
    )

    # load_skill：Claude Code 渐进披露契约的「激活」入口
    # 之前一直缺这个工具，导致 SkillLoader 注入的索引提示词形同虚设——
    # LLM 看到索引、想触发 SOP 也调不到 load_skill，只能用 read_file/multi_edit 自由发挥。
    registry.register(
        name="load_skill",
        description=(
            "Load the full content of an equipped skill (its SKILL.md) into the current "
            "conversation context. This is step 2 of Claude Code's progressive disclosure "
            "contract: the system prompt only carries the index (name + description); when "
            "you see a skill that matches the user's intent, call this to pull the full SOP.\n\n"
            "WHEN TO USE:\n"
            "✓ User asks something matching a skill's description (e.g. user wants to start "
            "a new webnovel project → load `webnovel-writer__webnovel-init`)\n"
            "✓ Previous skill said 'next step is <skill X>' → load X\n"
            "✗ Don't guess steps from a skill's name alone—load the full SKILL.md first.\n\n"
            "INPUTS:\n"
            "- `name` (required): exact skill name as shown in the equipped skills index, "
            "e.g. 'webnovel-writer__webnovel-init', 'humanizer'.\n\n"
            "Returns: the markdown content of SKILL.md (plus role-local mastery overlay if any). "
            "Follow the SOP inside that document; reload other skills as it instructs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Skill full name from the index, e.g. "
                        "'webnovel-writer__webnovel-init' or 'humanizer'."
                    ),
                },
            },
            "required": ["name"],
        },
        handler=_handle_load_skill,
        concurrency_class=CONCURRENCY_READ,
    )

    registry.register(
        name="install_skill_pack",
        description=(
            "Install a Claude Code Plugin compatible skill pack from GitHub or a git URL into 柳如烟. "
            "After install, the pack's skills are auto-discovered with names prefixed by `<pack>__` "
            "(e.g. `webnovel-writer__webnovel-write`). Each pack gets its own Python venv to avoid "
            "polluting the main env.\n\n"
            "WHEN TO USE:\n"
            "✓ User wants to add a new capability bundle from GitHub (writing system, image gen, etc.)\n"
            "✓ Migrating an existing Claude Code plugin into 柳如烟\n\n"
            "INPUTS:\n"
            "- `source` (required): one of\n"
            "  - github shorthand: `\"owner/repo\"` (e.g. `\"lingfengQAQ/webnovel-writer\"`)\n"
            "  - full URL: `\"https://github.com/owner/repo.git\"`\n"
            "  - dict: `{source:'github', repo:'owner/repo', ref?:'v1.0.0'}` or "
            "`{source:'url', url, ref?}` or `{source:'git-subdir', url, path, ref?}`\n"
            "- `pack_name` (optional): install dir name. Default: read from plugin.json `name` field.\n"
            "- `create_venv` (default true): create a per-pack Python venv at `<pack>/.venv/`.\n"
            "- `install_deps` (default true): pip install requirements.txt into the venv.\n"
            "- `overwrite` (default false): replace existing pack with same name.\n\n"
            "Returns: {success, pack_name, plugin_name, plugin_version, skills_count, "
            "venv_created, deps_installed, messages: [...]}.\n\n"
            "After success, equip the new skills to a role via the existing skill equip API."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source": {
                    "description": (
                        "GitHub shorthand 'owner/repo', full git URL, or structured dict "
                        "({source:'github'|'url'|'git-subdir', ...})."
                    ),
                },
                "pack_name": {
                    "type": "string",
                    "description": "Optional install dir name. Default: read from plugin.json.",
                },
                "create_venv": {"type": "boolean", "default": True},
                "install_deps": {"type": "boolean", "default": True},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["source"],
        },
        handler=_handle_install_skill_pack,
        is_async=True,
    )

    registry.register(
        name="update_skill_pack",
        description=(
            "Update an installed skill pack: `git pull --ff-only` + refresh Python deps in the pack's venv.\n\n"
            "WHEN TO USE:\n"
            "✓ User asks 'check for plugin updates' or names a specific pack to upgrade\n"
            "✓ After upstream repo announces a new version\n\n"
            "Returns: {success, pack_name, before_version, after_version, changed, deps_installed, messages: [...]}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pack_name": {"type": "string", "description": "Installed pack directory name."},
                "install_deps": {"type": "boolean", "default": True},
            },
            "required": ["pack_name"],
        },
        handler=_handle_update_skill_pack,
        is_async=True,
    )

    registry.register(
        name="uninstall_skill_pack",
        description=(
            "Uninstall a skill pack: delete its directory under `skills/`. The pack must be "
            "managed by 柳如烟 (have `.liuruyan-pack.json` or `.claude-plugin/`); otherwise "
            "the operation is refused to avoid deleting user's standalone skills.\n\n"
            "Returns: {success, pack_name, removed?: <path>}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pack_name": {"type": "string"},
            },
            "required": ["pack_name"],
        },
        handler=_handle_uninstall_skill_pack,
        is_async=True,
    )

    registry.register(
        name="list_skill_packs",
        description=(
            "List all installed skill packs (Claude Code Plugin compatible bundles) "
            "with their versions, sources, and management metadata.\n\n"
            "Use this to: see what's installed, find a pack name to update/uninstall, "
            "or check version drift before recommending an update."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_handle_list_skill_packs,
    )

    registry.register(
        name="skill_pack_status",
        description=(
            "Get details of a single installed skill pack: manifest, install metadata, "
            "venv health, and the list of skills it contributes "
            "(each with their fully-qualified `<pack>__<skill>` name)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pack_name": {"type": "string"},
            },
            "required": ["pack_name"],
        },
        handler=_handle_skill_pack_status,
    )


    # ============================================================
    # .lry/ 项目工作区写入 / 检索工具（设计文档 docs/lry_workspace_design.md）
    # ============================================================

    registry.register(
        name="add_project_rule",
        description=(
            "Save a project-specific red-line rule to .lry/rules/<topic>.md. "
            "This rule will be auto-injected into your system prompt for ALL future turns "
            "in this project (cached prefix, very low overhead).\n\n"
            "WHEN TO USE:\n"
            "✓ You discovered a project-specific constraint that future turns MUST respect "
            "(e.g. 'this project must use pnpm not npm', 'do not modify src/legacy/* directly')\n"
            "✓ The rule is short, sharp, absolute (\u22645000 chars)\n"
            "✗ General coding discipline → use the discipline skill, not project rules\n"
            "✗ Personal/role memory → use create_memory instead\n"
            "✗ Project structure / map → use save_project_memory(kind='handover') or write CODE_MAP.md\n\n"
            "Frontmatter (severity / added / by / topic) is added automatically."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "kebab-case slug (a-z, 0-9, '-', '_'); used as filename. e.g. 'build-must-pnpm'.",
                },
                "content": {
                    "type": "string",
                    "description": "Rule body (markdown). Should be short, explain WHY violating leads to bugs.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["critical", "warning", "info"],
                    "default": "warning",
                    "description": "critical = violating breaks build/runtime; warning = degraded behavior; info = preference.",
                },
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": "Replace if rule with same topic already exists.",
                },
            },
            "required": ["topic", "content"],
        },
        handler=_handle_add_project_rule,
    )

    registry.register(
        name="update_project_notes",
        description=(
            "Overwrite (or append to) .lry/PROJECT_NOTES.md with the current project progress.\n\n"
            "WHEN TO USE:\n"
            "✓ You finished a milestone and want to record current stage / next steps\n"
            "✓ You found a temporary memo worth surfacing at next turn's startup\n"
            "✗ Long-term insights → use save_project_memory instead\n"
            "✗ Hard rules → use add_project_rule instead\n\n"
            "NOTES content is auto-injected into system prompt on every turn (dynamic block, ~2K cap).\n"
            "If the resulting NOTES would exceed the cap, the existing NOTES is auto-archived to "
            ".lry/memory/note_notes-archive_<date>.md and the new content takes its place "
            "(set auto_archive=false to error out instead)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Full notes content (markdown). Default mode replaces; set append=true to add.",
                },
                "append": {
                    "type": "boolean",
                    "default": False,
                    "description": "True = append to existing notes; false = replace.",
                },
                "auto_archive": {
                    "type": "boolean",
                    "default": True,
                    "description": "If exceeding cap, auto-archive the OLD notes to memory/ and keep the new content.",
                },
            },
            "required": ["content"],
        },
        handler=_handle_update_project_notes,
    )

    registry.register(
        name="set_project_binding",
        description=(
            "Switch the CURRENT session's project memory context (v5 memory layering).\n\n"
            "WHEN TO USE:\n"
            "- User says this chat should be about a specific project "
            "(e.g. '这个会话聊中微的案子' / 'switch to the legal case')\n"
            "- The auto-binding at session start picked the wrong project and user corrects it\n"
            "- User wants no project memory attached -> project='' (unbind)\n\n"
            "AFTER SWITCHING: only the bound project's state card is injected; "
            "other projects stay isolated.\n"
            "For cross-project references use recall_memory instead of switching."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": (
                        "Target project: registered name or alias (e.g. '中微智码维权', '美股'). "
                        "Empty string / 'none' / '解绑' to unbind."
                    ),
                },
            },
            "required": ["project"],
        },
        handler=_handle_set_project_binding,
    )

    registry.register(
        name="save_project_memory",
        description=(
            "Save a project-level memory entry to .lry/memory/<kind>_<topic>_<date>.md.\n\n"
            "DIFFERENCES vs create_memory:\n"
            "- create_memory → personal/role memory ('I helped user X with Y')\n"
            "- save_project_memory → project-level history ('this project's OAuth was changed in May, root cause was Z')\n\n"
            "WHEN TO USE:\n"
            "✓ Handover note for next agent picking up this project\n"
            "✓ Insight discovered while working (root cause analysis, decision rationale)\n"
            "✓ ADR (architecture decision record)\n\n"
            "Files are cumulative; same topic+date → auto-appended _002 _003 to avoid overwrite.\n\n"
            "PRIVATE MODE (private=true):\n"
            "Writes to .lry/private/memory/ instead, and auto-creates .lry/.gitignore with `private/` rule.\n"
            "Use for sensitive content (API keys shouldn't be here in any case, but also: personal\n"
            "user details, internal URLs, NDA'd decisions). Default stays false — do NOT flip private on\n"
            "unless the content genuinely shouldn't be shared."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "kebab-case slug; used as filename component. e.g. 'oauth-callback-bug'.",
                },
                "content": {
                    "type": "string",
                    "description": "Memory body (markdown).",
                },
                "kind": {
                    "type": "string",
                    "enum": ["handover", "insight", "adr", "note"],
                    "default": "handover",
                    "description": "handover = task continuation; insight = lesson learned; adr = decision; note = misc.",
                },
                "private": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, write to .lry/private/memory/ (git-ignored). Only for genuinely sensitive content.",
                },
            },
            "required": ["topic", "content"],
        },
        handler=_handle_save_project_memory,
    )

    registry.register(
        name="add_project_workflow",
        description=(
            "Save a project-specific SOP / workflow to .lry/workflows/<name>.md.\n\n"
            "DIFFERENCES vs general skills:\n"
            "- skills → reusable across projects ('how to use puppeteer')\n"
            "- workflows → project-specific SOP ('how to deploy THIS project to production')\n\n"
            "WHEN TO USE:\n"
            "✓ You walked through a multi-step procedure that future turns will repeat\n"
            "✓ The procedure is project-specific (commands / paths / configs unique to this project)\n"
            "✗ Generic 'how to debug X' → the existing skills system or workflows/"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "kebab-case slug; used as filename. e.g. 'deploy-prod'.",
                },
                "description": {
                    "type": "string",
                    "description": "One-line summary of when to invoke this workflow.",
                },
                "steps": {
                    "type": "string",
                    "description": "Step-by-step procedure (markdown). Recommended structure: 触发场景 / 步骤 / 验证 / 常见踩雷.",
                },
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": "Replace if workflow with same name already exists.",
                },
            },
            "required": ["name", "description", "steps"],
        },
        handler=_handle_add_project_workflow,
    )

    registry.register(
        name="recall_project_memory",
        description=(
            "Search .lry/memory/*.md by free-text query. Returns ranked snippets.\n\n"
            "WHEN TO USE:\n"
            "✓ Starting a new task in a known project; check what was done before\n"
            "✓ Encountered a familiar problem; look up past root-cause notes\n"
            "✓ Need to understand a past decision (kind='adr')\n\n"
            "DIFFERENCES vs recall_memory:\n"
            "- recall_memory → your personal/role memory (across projects)\n"
            "- recall_project_memory → this project's accumulated knowledge (across roles)\n\n"
            "PRIVATE MODE (include_private=true):\n"
            "Also scans .lry/private/memory/. Matched items are flagged `private: true` — when quoting,\n"
            "summarize abstractly and do NOT dump raw content into user-facing replies unless the user\n"
            "explicitly asked about their private context this turn."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text search (substring match across content). Empty = list recent.",
                },
                "top_k": {
                    "type": "integer",
                    "default": 5,
                    "description": "Max results (1-20).",
                },
                "kind": {
                    "type": "string",
                    "enum": ["handover", "insight", "adr", "note"],
                    "description": "Optional filter by memory kind.",
                },
                "include_private": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, also search .lry/private/memory/. Matched items will carry `private: true`.",
                },
            },
        },
        handler=_handle_recall_project_memory,
    )

    registry.register(
        name="get_project_workflow",
        description=(
            "Fetch the full content of a project workflow at .lry/workflows/<name>.md.\n\n"
            "The list of available workflows (name + one-line description) is already in your "
            "system prompt. When you need the full step-by-step procedure to execute one, call this.\n\n"
            "WHEN TO USE:\n"
            "✓ User asks to perform a task matching a workflow's description\n"
            "✓ You're about to execute a multi-step procedure and want the canonical recipe\n"
            "✗ Don't call if the workflow index is empty (no workflows exist)"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Workflow slug (matches the file at .lry/workflows/<name>.md).",
                },
            },
            "required": ["name"],
        },
        handler=_handle_get_project_workflow,
    )

    registry.register(
        name="list_project_rules",
        description=(
            "List metadata of all .lry/rules/*.md files (slug, file, severity, size).\n\n"
            "The full content of all rules is already in your system prompt; this tool is for "
            "managing them (e.g. 'before I overwrite, list current rules to avoid duplicates')."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_handle_list_project_rules,
    )

    registry.register(
        name="archive_lry_file",
        description=(
            "Archive (soft-delete) a rule / workflow / memory in .lry/, moving it to "
            ".lry/archive/<kind>s/ where it's no longer scanned by recall / system-prompt "
            "injection / list tools.\n\n"
            "USE WHEN:\n"
            "✓ A rule has become obsolete and is wrong-in-context for current work\n"
            "✓ A workflow has been superseded by a better version\n"
            "✓ A memory entry is inaccurate and would mislead future turns\n"
            "✗ You want to keep it but mark as deprecated — instead edit the content and add a note\n"
            "✗ You want to delete forever — archive is soft; manual removal from .lry/archive/ if needed\n\n"
            "BEHAVIOR:\n"
            "- private memory → .lry/archive/private/memory/ (stays hidden from git + API)\n"
            "- same-name archive exists → appended with .archived_<UTC-timestamp>\n"
            "- dry_run=true → returns preview (source + dest paths) without moving\n"
            "- INDEX.md is auto-regenerated after success"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["rule", "workflow", "memory"],
                    "description": "Which kind to archive.",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "For rule/workflow: the slug (e.g. 'build-must-pnpm'). "
                        "For memory: the filename (e.g. 'insight_oauth_2026-05-14.md'; .md optional)."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, return preview without moving the file.",
                },
            },
            "required": ["kind", "name"],
        },
        handler=_handle_archive_lry_file,
    )

    registry.register(
        name="restore_lry_file",
        description=(
            "Restore a previously archived rule / workflow / memory back to its original location "
            "(inverse of archive_lry_file).\n\n"
            "USE WHEN:\n"
            "✓ Accidentally archived something that turned out to still be needed\n"
            "✓ Bringing back an old version on purpose (superseded archive now relevant again)\n\n"
            "BEHAVIOR:\n"
            "- Source must exist under .lry/archive/<kind>s/ (private memory under archive/private/memory/)\n"
            "- If the archived filename has `.archived_<UTC-timestamp>` suffix (collision backup), "
            "the suffix is stripped to recover the original name\n"
            "- **Fails if destination already exists** — safety first, archive the current version before restoring\n"
            "- dry_run=true returns {source, dest} preview without moving\n"
            "- INDEX.md auto-regenerates after success\n"
            "- Private memory restores back to .lry/private/memory/ (isolation preserved)"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["rule", "workflow", "memory"],
                    "description": "Which kind to restore.",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Archive filename (with .md). For rule/workflow you may also pass just the "
                        "slug. If the archive has `.archived_<ts>` suffix (collision backup), "
                        "include it in `name` to target that specific copy."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, return preview without moving.",
                },
            },
            "required": ["kind", "name"],
        },
        handler=_handle_restore_lry_file,
    )


    # grep — 文件内容 / 文件名搜索。命名对齐 Claude Code/Cascade，模型 native 调用。
    # （2026-05-20 从老名 search_files 改过来，无别名兼容 — 任何地方还在调 search_files 都是 bug，直接修）
    registry.register(
        name="grep",
        description=(
            "Search file contents (target='content', default) or file names (target='filename').\n\n"
            "WHEN TO USE:\n"
            "✓ Finding where a function / variable / phrase appears in the codebase (target='content')\n"
            "✓ Finding files by name pattern when exact path is unknown (target='filename')\n"
            "✗ Semantic code search for Python (e.g. 'all callers of X') → use `code_intelligence` with findReferences\n"
            "✗ Listing files by glob pattern only → use `glob_search` (more efficient, no content scan)\n\n"
            "Tips:\n"
            "- Scope with `includes=['*.py']` to avoid scanning the whole tree. Multiple globs allowed; "
            "prefix `!` for negation: `includes=['*.ts', '!**/node_modules/**']`.\n"
            "- `pattern` is regex by default (content mode). Set `fixed_strings=true` for literal match — "
            "use this when searching strings with regex metachars like `console.log(` or `dict['k']`.\n"
            "- Case-insensitive by default; pass `case_sensitive=true` to enforce.\n"
            "- `output_mode='files'` → only return matching file paths (cheap broad-scope scan).\n"
            "  `output_mode='count'` → return per-file match counts. Default `'content'` returns line+snippet.\n"
            "- `context_lines=N` → include N lines of context around each match (content mode only).\n"
            "- By default the project's `.gitignore` is respected and hidden files are skipped (clean results, "
            "no build artifacts / third-party sources). Set `search_ignored=true` to override this — needed when "
            "searching directories like `references/`, `_archive/`, `.lry/` that the project chose to "
            "git-ignore but still contain content you care about. Noise dirs (node_modules, target, dist, .git, "
            "build, __pycache__, etc.) are always excluded regardless.\n"
            "- Engine: ripgrep (PATH or project-bundled `vendor/ripgrep/<arch>/`) → pure-Python fallback. "
            "grep is NOT used."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Search pattern"
                },
                "target": {
                    "type": "string",
                    "enum": ["content", "filename"],
                    "description": "Search target",
                    "default": "content"
                },
                "path": {
                    "type": "string",
                    "description": "Search path",
                    "default": "."
                },
                "includes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Glob patterns to filter files (e.g. ['*.py'] or ['src/**/*.ts', '!**/vendor/**']). "
                        "Prefix `!` for negation. Empty = scan all."
                    )
                },
                "file_glob": {
                    "type": "string",
                    "description": "Legacy single-glob alias of `includes`. Prefer `includes` for new code."
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Case sensitive search",
                    "default": False
                },
                "fixed_strings": {
                    "type": "boolean",
                    "description": "Treat pattern as a literal string instead of regex (content mode).",
                    "default": False
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files", "count"],
                    "description": "content=line+snippet (default); files=only matching file paths; count=per-file count.",
                    "default": "content"
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of context around each match (content mode only). 0 = matches only.",
                    "default": 0,
                    "minimum": 0,
                    "maximum": 20
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination start (0-indexed). Use 'next_offset' from previous response to continue.",
                    "default": 0,
                    "minimum": 0
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results per page (1..500, default 50). Response includes has_more / next_offset for paging.",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 500
                },
                "search_ignored": {
                    "type": "boolean",
                    "description": (
                        "Override project .gitignore and search hidden files. Default false (clean results). "
                        "Set true when you specifically want to search git-ignored content like references/, "
                        "_archive/, .lry/, etc. Noise dirs (node_modules/target/dist/...) stay excluded."
                    ),
                    "default": False
                }
            },
            "required": ["pattern"]
        },
        handler=_handle_grep,
        arg_aliases={**_PATH_ALIAS_MAP, **_PATTERN_ALIAS_MAP},
    )
