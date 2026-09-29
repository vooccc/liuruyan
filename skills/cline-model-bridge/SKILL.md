---
name: cline-model-bridge
description: "Zero-pollution guide for experimenting with Cline account model access as an external skill. Use when the user asks to borrow Cline's logged-in model channel, investigate Cline free models, or build a removable Cline model bridge without modifying the host project."
allowed-tools:
disable: true
---

# Cline Model Bridge

This skill is a removable, project-independent guide for testing whether a Cline account can be used as a lightweight model channel.

## Goal

Use Cline's account-authenticated OpenAI-compatible model endpoint as an external bridge, without changing the host project's frontend, backend, provider registry, database, or settings.

## Non-goals

- Do not embed the VS Code extension UI.
- Do not run the full Cline ACP agent unless explicitly requested.
- Do not modify the host project's provider list during the first proof of concept.
- Do not store tokens in source-controlled files.
- Do not treat Cline's internal API as a stable public provider.

## Source facts from Cline

Cline's own provider creates an OpenAI SDK client with:

- `baseURL: ${apiBaseUrl}/api/v1`
- `apiKey: clineAccountAuthToken`
- headers including `HTTP-Referer: https://cline.bot`, `X-Title: Cline`, and `X-Task-ID`

Relevant research files:

- `_research/cline-main/src/core/api/providers/cline.ts`
- `_research/cline-main/src/services/auth/AuthService.ts`
- `_research/cline-main/src/services/auth/providers/ClineAuthProvider.ts`
- `_research/cline-main/cli/src/agent/ClineAgent.ts`

## Quick start

Run the local proxy with Node.js 18+:

```powershell
node "./skills\cline-model-bridge\scripts\cline_proxy.mjs"
```

Default endpoints:

```text
Bridge UI/status: http://127.0.0.1:8787
Login:            http://127.0.0.1:8787/auth/start
Health:           http://127.0.0.1:8787/health
Models:           http://127.0.0.1:8787/v1/models
Chat completions: http://127.0.0.1:8787/v1/chat/completions
```

After starting the proxy:

1. Open `http://127.0.0.1:8787/auth/start`.
2. Complete Cline login in the browser.
3. Add a custom provider in the host project:
   - Provider id: `cline-local`
   - Name: `Cline Free`
   - Transport: `openai_chat`
   - Base URL: `http://127.0.0.1:8787/v1`
   - API key: any non-empty placeholder, for example `local`
4. Import or manually add free models from `http://127.0.0.1:8787/v1/models`.
5. Select the model in the chat model picker.

Environment overrides:

```powershell
$env:CLINE_BRIDGE_PORT="8787"
# Optional. By default the proxy uses Cline-compatible callback ports 48801-48811.
# Only override this if all official callback ports are occupied.
# $env:CLINE_BRIDGE_CALLBACK_PORT="48801"
$env:CLINE_BRIDGE_DIR="$env:USERPROFILE\.liuruyan\cline-model-bridge"
$env:CLINE_API_BASE_URL="https://api.cline.bot"
node "./skills\cline-model-bridge\scripts\cline_proxy.mjs"
```

## Implementation

The runnable proxy script is:

```text
skills/cline-model-bridge/scripts/cline_proxy.mjs
```

It implements:

- `GET /auth/start`
- `GET /auth/status`
- `GET /health`
- `GET /v1/models`
- `GET /v1/cline/recommended-models`
- `POST /v1/chat/completions`

It uses the same Cline-source-compatible header shape for normal model calls:

- `Authorization: Bearer workos:<accessToken>`
- `HTTP-Referer: https://cline.bot`
- `X-Title: Cline`
- `X-Task-ID: <uuid>`
- `User-Agent: Cline/<version>`
- `X-PLATFORM`, `X-PLATFORM-VERSION`, `X-CLIENT-TYPE`, `X-CLIENT-VERSION`, `X-CORE-VERSION`, `X-IS-MULTIROOT`

This matches the observed Cline provider behavior closely enough for compatibility testing. Do not add extra spoofing, fingerprint bypass, or anti-fraud evasion logic.

## Recommended architecture

Keep everything in an external sidecar folder or temporary output directory:

```text
host project
  skills/cline-model-bridge/SKILL.md   # this guide only

external runtime, ignored/removable
  /mnt/agent-data/cline-model-bridge/
    auth.json                          # local secret store, never commit
    cline_model_probe.py or .mjs         # probe script
    logs/
```

The bridge should expose either:

1. a tiny local OpenAI-compatible proxy, or
2. a one-shot probe script that verifies login and model calls.

Only after the bridge works should the user decide whether to wire it into the host project.

## Minimal PoC phases

### Phase 1: Read current Cline auth flow

Inspect these Cline files before implementing:

- `src/services/auth/AuthService.ts`
- `src/services/auth/providers/ClineAuthProvider.ts`
- `src/core/api/providers/cline.ts`
- `src/shared/cline/api.ts`
- `src/config.ts`

Confirm:

- `apiBaseUrl`
- auth start endpoint
- token exchange endpoint
- refresh endpoint
- required headers
- callback URL shape

### Phase 2: Build standalone OAuth callback

Implement outside the host project:

- start a localhost callback server
- generate a Cline auth URL with `client_type=extension`
- open the browser
- receive `code` and `provider`
- exchange code for `accessToken` and `refreshToken`
- save tokens to a local private file outside git

Security rules:

- never print full tokens
- never write tokens into `.env.example`, source files, frontend state, or transcripts
- redact tokens in logs

### Phase 3: Probe chat completions

Call Cline's OpenAI-compatible endpoint with:

```text
POST {apiBaseUrl}/api/v1/chat/completions
Authorization: Bearer <token or SDK apiKey equivalent>
HTTP-Referer: https://cline.bot
X-Title: Cline
X-Task-ID: <random task id>
```

Use a tiny prompt first:

```text
Say pong in one word.
```

Validate:

- streaming works
- selected free model responds
- auth refresh works after token expiry
- failure modes are cleanly reported

### Phase 4: Optional local proxy

If the probe works, wrap it as a local OpenAI-compatible proxy:

```text
POST http://127.0.0.1:<port>/v1/chat/completions
```

The proxy should:

- read Cline token from private local storage
- refresh token when needed
- forward OpenAI-compatible requests to Cline
- preserve stream chunks as much as possible
- expose `/health` and `/models`

This keeps the host project untouched. The host can then use the local proxy through an existing custom/OpenAI-compatible provider only if the user asks.

## Removal

To remove this experiment:

1. delete `skills/cline-model-bridge/`
2. delete the external runtime directory used for tokens and logs
3. stop the local sidecar process if running

No host project code should need reverting if the skill was followed correctly.

## When to choose ACP instead

Use Cline ACP sidecar instead of this model bridge if the user wants:

- Cline's full agent behavior
- Cline tool execution
- Cline permission prompts
- Cline diff workflow
- Cline task/session lifecycle

Use this model bridge only when the goal is lightweight model access.

## Risk notes

- Cline's account API may not be a public stable third-party provider.
- Free model eligibility and limits can change.
- Auth headers or callback flow can change.
- This should be treated as an experimental bridge, not a permanent production dependency.
