---
name: codegeex-bridge
description: "Run a removable local OpenAI-compatible bridge for CodeGeeX's logged-in extension channel. Use when testing CodeGeeX as a model provider, debugging why CodeGeeX tool calls stop after one sentence, or starting/stopping the local CodeGeeX bridge without polluting the host project."
allowed-tools:
disable: true
---

# CodeGeeX Bridge

This skill keeps the CodeGeeX bridge experiment isolated from the host project.

## What it provides

- Local OpenAI-compatible endpoint: `http://127.0.0.1:18788/v1`
- Model id: `glm-5-1`
- Health endpoint: `http://127.0.0.1:18788/health`
- Debug endpoint: `http://127.0.0.1:18788/debug/last`
- Uses the CodeGeeX extension token service at `http://127.0.0.1:18787/token`

## Start

Double-click:

```text
skills/codegeex-bridge/_启动CodeGeeX桥.bat
```

Or run:

```powershell
./skills\codegeex-bridge\_启动CodeGeeX桥.bat
```

The batch file will:

1. Check whether CodeGeeX token service `18787` is available.
2. Stop any old bridge listening on `18788`.
3. Set `CODEGEEX_DEBUG_LAST_PATH` to `skills/codegeex-bridge/last_debug.json`.
4. Start `skills/codegeex-bridge/scripts/codegeex_bridge.py`.

## Configure in host project

Use a custom OpenAI-compatible provider:

```text
Base URL: http://127.0.0.1:18788/v1
Model:    glm-5-1
API Key:  any non-empty placeholder, e.g. codegeex
```

## Current behavior (2026-05-31)

- **Default route is ASK mode** (`chatCodeSseV3/chat?stream=true`, model `glm-5-1`).
- Tool calls use the `<tool_call>name<tool_sep>{json}</tool_call>` text protocol and are converted to OpenAI `tool_calls`.
- **Agent mode is disabled by default** (`CODEGEEX_FORCE_ASK=1`). Set `CODEGEEX_FORCE_ASK=0` to fall back to the legacy local agent channel.
- Outgoing requests mimic the genuine VSCode extension: axios-style headers (`User-Agent: axios/1.7.3`), no extra `Accept: text/event-stream`, and no legacy `chatGlmTalk/insert` pre-call (`CODEGEEX_SKIP_INSERT=1` by default — inserting the tool prompt there breaks chat).

## Tool-call debugging

If CodeGeeX says “I will read/analyze” and then stops without a tool card, check:

```text
http://127.0.0.1:18788/debug/last
```

Interpretation:

- `has_tools: false` means the host backend did not send tool schemas to this provider.
- `has_tools: true` and empty `parsed_tool_calls` means CodeGeeX did not output a parsable tool call.
- non-empty `parsed_tool_calls` means the bridge returned tool calls; the issue is downstream in the host backend tool loop/parser.

## Files

```text
skills/codegeex-bridge/SKILL.md
skills/codegeex-bridge/_启动CodeGeeX桥.bat
skills/codegeex-bridge/scripts/codegeex_bridge.py
skills/codegeex-bridge/last_debug.json  # generated at runtime, ignored if not committed
```

## Current limitation

This bridge adapts CodeGeeX ask-mode text tool intent into OpenAI-compatible `tool_calls`. End-to-end tool execution depends on the host backend continuing the tool loop after receiving `tool_calls`. With many tools (20+), keep an eye on `parsed_tool_calls` in `/debug/last` — if the model only narrates, the compact tool schema may still be too large for a given prompt.
