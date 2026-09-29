#!/usr/bin/env node
import http from 'node:http'
import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { mkdir, readFile, writeFile, chmod } from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'

const VERSION = '3.81.0'
const DEFAULT_PORT = Number(process.env.CLINE_BRIDGE_PORT || 8787)
const CALLBACK_PORT_ENV = process.env.CLINE_BRIDGE_CALLBACK_PORT ? Number(process.env.CLINE_BRIDGE_CALLBACK_PORT) : null
const CALLBACK_PORTS = CALLBACK_PORT_ENV
  ? [CALLBACK_PORT_ENV]
  : Array.from({ length: 11 }, (_, i) => 48801 + i)
const API_BASE_URL = (process.env.CLINE_API_BASE_URL || 'https://api.cline.bot').replace(/\/$/, '')
const RUNTIME_DIR = process.env.CLINE_BRIDGE_DIR || path.join(os.homedir(), '.liuruyan', 'cline-model-bridge')
const AUTH_FILE = path.join(RUNTIME_DIR, 'auth.json')
const RECOMMENDED_MODELS_TTL_MS = 60 * 60 * 1000

const fallbackModels = {
  recommended: [
    {
      id: 'google/gemini-3.1-pro-preview',
      name: 'Google Gemini 3.1 Pro Preview',
      description: 'Latest Gemini release with 1m ctx window and strong coding performance',
      tags: ['NEW'],
    },
    {
      id: 'anthropic/claude-sonnet-4.6',
      name: 'Anthropic Claude Sonnet 4.6',
      description: 'Latest Sonnet release with strong coding and agent performance',
      tags: ['NEW'],
    },
    {
      id: 'anthropic/claude-opus-4.6',
      name: 'Anthropic Claude Opus 4.6',
      description: 'Most intelligent model for agents and coding',
      tags: ['BEST'],
    },
    {
      id: 'openai/gpt-5.3-codex',
      name: 'OpenAI GPT-5.3 Codex',
      description: "OpenAI's latest with strong coding abilities",
      tags: ['NEW'],
    },
  ],
  free: [
    {
      id: 'kwaipilot/kat-coder-pro',
      name: 'KwaiKAT Kat Coder Pro',
      description: "KwaiKAT's most advanced agentic coding model in the KAT-Coder series",
      tags: ['FREE'],
    },
    {
      id: 'arcee-ai/trinity-large-preview:free',
      name: 'Arcee AI Trinity Large Preview',
      description: "Arcee AI's advanced large preview model in the Trinity series",
      tags: ['FREE'],
    },
  ],
}

let modelCache = null
let callbackServer = null
let pendingAuth = null

function nowSeconds() {
  return Math.floor(Date.now() / 1000)
}

function redact(value) {
  if (!value || typeof value !== 'string') return ''
  if (value.length <= 12) return '***'
  return `${value.slice(0, 6)}...${value.slice(-4)}`
}

function baseHeaders() {
  return {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    'User-Agent': `Cline/${VERSION}`,
    'X-PLATFORM': process.platform || 'unknown',
    'X-PLATFORM-VERSION': os.release() || 'unknown',
    'X-CLIENT-TYPE': 'vscode',
    'X-CLIENT-VERSION': 'unknown',
    'X-CORE-VERSION': VERSION,
  }
}

function clineChatHeaders(taskId) {
  return {
    ...baseHeaders(),
    'HTTP-Referer': 'https://cline.bot',
    'X-Title': 'Cline',
    'X-Task-ID': taskId || randomUUID(),
    'X-IS-MULTIROOT': 'false',
  }
}

async function ensureRuntimeDir() {
  await mkdir(RUNTIME_DIR, { recursive: true })
}

async function loadAuth() {
  try {
    const raw = await readFile(AUTH_FILE, 'utf8')
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null
    return parsed
  } catch {
    return null
  }
}

async function saveAuth(auth) {
  await ensureRuntimeDir()
  await writeFile(AUTH_FILE, JSON.stringify(auth, null, 2), 'utf8')
  try {
    await chmod(AUTH_FILE, 0o600)
  } catch {
    // Windows chmod is best-effort.
  }
}

async function jsonFetch(url, init = {}) {
  const response = await fetch(url, init)
  const text = await response.text()
  let json = null
  try {
    json = text ? JSON.parse(text) : null
  } catch {
    json = null
  }
  if (!response.ok) {
    const detail = json?.error_description || json?.error || json?.message || text || response.statusText
    throw new Error(`${response.status} ${detail}`)
  }
  return json
}

function tokenNeedsRefresh(auth) {
  if (!auth?.idToken || !auth?.refreshToken) return true
  const expiresAt = Number(auth.expiresAt || 0)
  return expiresAt < nowSeconds() + 5 * 60
}

async function refreshAuth(auth) {
  if (!auth?.refreshToken) throw new Error('No Cline refresh token. Visit /auth/start first.')
  const data = await jsonFetch(`${API_BASE_URL}/api/v1/auth/refresh`, {
    method: 'POST',
    headers: baseHeaders(),
    body: JSON.stringify({
      refreshToken: auth.refreshToken,
      grantType: 'refresh_token',
    }),
  })
  const tokenData = data?.data
  if (!data?.success || !tokenData?.accessToken || !tokenData?.refreshToken) {
    throw new Error('Invalid Cline refresh response')
  }
  const next = {
    ...auth,
    idToken: tokenData.accessToken,
    refreshToken: tokenData.refreshToken || auth.refreshToken,
    expiresAt: Date.parse(tokenData.expiresAt) / 1000,
    userInfo: tokenData.userInfo || auth.userInfo || null,
    provider: 'cline',
    updatedAt: new Date().toISOString(),
  }
  await saveAuth(next)
  return next
}

async function getAuthForRequest() {
  let auth = await loadAuth()
  if (!auth?.idToken) throw new Error('Not authenticated. Open http://127.0.0.1:' + DEFAULT_PORT + '/auth/start')
  if (tokenNeedsRefresh(auth)) auth = await refreshAuth(auth)
  return auth
}

async function exchangeCode(code, provider, callbackUrl) {
  const data = await jsonFetch(`${API_BASE_URL}/api/v1/auth/token`, {
    method: 'POST',
    headers: baseHeaders(),
    body: JSON.stringify({
      grant_type: 'authorization_code',
      code,
      client_type: 'extension',
      redirect_uri: callbackUrl,
      provider: provider || 'workos',
    }),
  })
  const tokenData = data?.data
  if (!data?.success || !tokenData?.accessToken || !tokenData?.refreshToken) {
    throw new Error('Invalid Cline token response')
  }
  const auth = {
    idToken: tokenData.accessToken,
    refreshToken: tokenData.refreshToken,
    expiresAt: Date.parse(tokenData.expiresAt) / 1000,
    userInfo: tokenData.userInfo || null,
    provider: 'cline',
    startedAt: Date.now(),
    updatedAt: new Date().toISOString(),
    apiBaseUrl: API_BASE_URL,
  }
  await saveAuth(auth)
  return auth
}

function decodeMaybeJsonPayload(value) {
  if (!value || typeof value !== 'string') return null
  const candidates = [value]
  try { candidates.push(decodeURIComponent(value)) } catch { /* already decoded */ }
  for (const candidate of candidates) {
    try {
      const decoded = Buffer.from(candidate, 'base64url').toString('utf8')
      const start = decoded.indexOf('{')
      const end = decoded.lastIndexOf('}')
      if (start >= 0 && end > start) {
        return JSON.parse(decoded.slice(start, end + 1))
      }
    } catch {
      // Not a base64url JSON payload.
    }
    try {
      if (candidate.trim().startsWith('{')) return JSON.parse(candidate)
    } catch {
      // Not raw JSON either.
    }
  }
  return null
}

async function saveDirectAuthPayload(payload, provider = 'cline') {
  if (!payload?.accessToken || !payload?.refreshToken) return null
  const auth = {
    idToken: payload.accessToken,
    refreshToken: payload.refreshToken,
    expiresAt: Date.parse(payload.expiresAt) / 1000,
    userInfo: {
      id: payload.clineUserId || payload.userId || payload.email || '',
      email: payload.email || '',
      displayName: payload.name || [payload.firstName, payload.lastName].filter(Boolean).join(' ') || '',
      createdAt: new Date().toISOString(),
      organizations: [],
    },
    provider: provider || 'cline',
    startedAt: Date.now(),
    updatedAt: new Date().toISOString(),
    apiBaseUrl: API_BASE_URL,
  }
  await saveAuth(auth)
  return auth
}

async function consumeAuthCodeOrPayload(code, provider, callbackUrl) {
  const directPayload = decodeMaybeJsonPayload(code)
  const directAuth = await saveDirectAuthPayload(directPayload, provider)
  if (directAuth) return directAuth
  return exchangeCode(code, provider, callbackUrl)
}

function extractAuthParams(raw) {
  if (!raw || typeof raw !== 'string') throw new Error('Missing auth URL or code')
  const trimmed = raw.trim()
  if (!trimmed.includes('://') && !trimmed.includes('?') && !trimmed.includes('&')) {
    return { code: trimmed, provider: 'workos' }
  }
  let parsed
  try {
    parsed = new URL(trimmed)
  } catch {
    parsed = new URL(trimmed, 'http://127.0.0.1/auth')
  }
  const query = new URLSearchParams(parsed.search.slice(1).replace(/\+/g, '%2B'))
  const code = query.get('code') || query.get('idToken') || query.get('refreshToken')
  const provider = query.get('provider') || query.get('auth_provider') || 'workos'
  if (!code) throw new Error('No code/idToken/refreshToken found in pasted URL')
  return { code, provider }
}

function manualAuthHtml(error = '') {
  return `<!doctype html>
<meta charset="utf-8">
<title>Cline Bridge Manual Auth</title>
<body style="font-family: system-ui, sans-serif; max-width: 760px; margin: 40px auto; line-height: 1.5;">
  <h2>Cline Bridge 手动授权</h2>
  <p>如果 Cline 网页提示打开 VS Code，请点取消，复制浏览器地址栏里的 <code>https://app.cline.bot/auth/callback?...code=...</code>，然后回到这个页面点“一键读取剪贴板并保存”。</p>
  ${error ? `<p style="color:#c00">${String(error).replace(/[<>&]/g, '')}</p>` : ''}
  <form id="manualForm" method="post" action="/auth/manual">
    <textarea id="authText" name="auth" rows="8" style="width:100%; box-sizing:border-box; font-family:monospace" placeholder="https://app.cline.bot/auth/callback?...code=... 或 vscode://.../auth?code=... 或直接粘贴 code"></textarea>
    <p>
      <button type="button" id="pasteAndSave">一键读取剪贴板并保存</button>
      <button type="submit">手动保存</button>
    </p>
  </form>
  <p id="msg" style="color:#666"></p>
  <script>
    const text = document.getElementById('authText')
    const form = document.getElementById('manualForm')
    const msg = document.getElementById('msg')
    const params = new URLSearchParams(location.search)
    const fromQuery = params.get('url') || params.get('auth') || params.get('code')
    if (fromQuery) {
      text.value = fromQuery
      form.submit()
    }
    document.getElementById('pasteAndSave').addEventListener('click', async () => {
      try {
        const value = await navigator.clipboard.readText()
        text.value = value
        msg.textContent = '已读取剪贴板，正在保存...'
        form.submit()
      } catch (error) {
        msg.textContent = '浏览器不允许读取剪贴板，请手动粘贴后点“手动保存”。'
      }
    })
  </script>
</body>`
}

async function handleManualAuth(req, res) {
  if (req.method === 'GET') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
    res.end(manualAuthHtml())
    return
  }
  const bodyText = await readRequestBody(req)
  const params = new URLSearchParams(bodyText)
  const raw = params.get('auth') || bodyText
  const { code, provider } = extractAuthParams(raw)
  const callbackUrl = pendingAuth?.callbackUrl || `http://127.0.0.1:${CALLBACK_PORTS[0]}/auth`
  const auth = await consumeAuthCodeOrPayload(code, provider, callbackUrl)
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
  res.end(`<!doctype html><meta charset="utf-8"><body style="font-family:sans-serif"><h2>Cline 登录态已保存</h2><p>token=${redact(auth.idToken)}</p><p>现在可以回到柳如烟配置 provider。</p></body>`)
}

function openBrowser(url) {
  const command = process.platform === 'win32' ? 'cmd' : process.platform === 'darwin' ? 'open' : 'xdg-open'
  const args = process.platform === 'win32' ? ['/c', 'start', '', url] : [url]
  const child = spawn(command, args, { detached: true, stdio: 'ignore' })
  child.unref()
}

async function listenCallbackServer(server) {
  let lastError = null
  for (const port of CALLBACK_PORTS) {
    try {
      await new Promise((resolve, reject) => {
        const onError = (error) => {
          server.off('error', onError)
          reject(error)
        }
        server.once('error', onError)
        server.listen(port, '127.0.0.1', () => {
          server.off('error', onError)
          resolve()
        })
      })
      return port
    } catch (error) {
      lastError = error
      if (error?.code !== 'EADDRINUSE') throw error
    }
  }
  throw lastError || new Error('No available callback port')
}

async function resolveAuthUrl(rawAuthUrl) {
  try {
    const response = await fetch(rawAuthUrl, {
      method: 'GET',
      redirect: 'manual',
      headers: baseHeaders(),
    })
    const location = response.headers.get('location')
    if (response.status >= 300 && response.status < 400 && location) {
      return {
        mode: 'redirect',
        status: response.status,
        raw_auth_url: rawAuthUrl,
        final_auth_url: location,
      }
    }
    const data = await response.json().catch(() => null)
    if (data?.redirect_url) {
      return {
        mode: 'json_redirect_url',
        status: response.status,
        raw_auth_url: rawAuthUrl,
        final_auth_url: data.redirect_url,
      }
    }
    return {
      mode: 'raw_fallback',
      status: response.status,
      raw_auth_url: rawAuthUrl,
      final_auth_url: rawAuthUrl,
      response_preview: data ? JSON.stringify(data).slice(0, 500) : null,
    }
  } catch (error) {
    console.warn('[cline-bridge] Could not pre-resolve auth redirect, using authorize URL:', error.message)
    return {
      mode: 'fetch_error_raw_fallback',
      status: 0,
      raw_auth_url: rawAuthUrl,
      final_auth_url: rawAuthUrl,
      error: error.message,
    }
  }
}

async function startAuthFlow(reqUrl, res) {
  if (callbackServer) {
    callbackServer.close()
    callbackServer = null
  }

  let callbackPort = CALLBACK_PORTS[0]
  let callbackUrl = `http://127.0.0.1:${callbackPort}/auth`
  pendingAuth = { callbackUrl, createdAt: Date.now() }

  callbackServer = http.createServer(async (req, callbackRes) => {
    try {
      const url = new URL(req.url || '/', callbackUrl)
      if (url.pathname !== '/auth') {
        callbackRes.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' })
        callbackRes.end('Not found')
        return
      }
      const code = url.searchParams.get('code')
      const provider = url.searchParams.get('provider') || url.searchParams.get('auth_provider') || 'workos'
      const error = url.searchParams.get('error')
      if (error) throw new Error(error)
      if (!code) throw new Error('Missing authorization code')
      const auth = await consumeAuthCodeOrPayload(code, provider, callbackUrl)
      callbackRes.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
      callbackRes.end('<!doctype html><meta charset="utf-8"><title>Cline Bridge</title><body style="font-family:sans-serif"><h2>Cline 登录成功</h2><p>可以关闭这个页面，回到柳如烟继续配置模型。</p></body>')
      console.log(`[cline-bridge] Auth success. token=${redact(auth.idToken)}`)
    } catch (error) {
      callbackRes.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' })
      callbackRes.end(`Cline auth failed: ${error.message}`)
      console.error('[cline-bridge] Auth callback failed:', error.message)
    } finally {
      pendingAuth = null
      if (callbackServer) {
        setTimeout(() => {
          callbackServer?.close()
          callbackServer = null
        }, 1000)
      }
    }
  })

  callbackPort = await listenCallbackServer(callbackServer)
  callbackUrl = `http://127.0.0.1:${callbackPort}/auth`
  pendingAuth = { callbackUrl, createdAt: Date.now() }

  const authUrl = new URL(`${API_BASE_URL}/api/v1/auth/authorize`)
  authUrl.searchParams.set('client_type', 'extension')
  authUrl.searchParams.set('callback_url', callbackUrl)
  authUrl.searchParams.set('redirect_uri', callbackUrl)

  const rawAuthUrl = authUrl.toString()
  const forceRaw = reqUrl.searchParams.get('open') === 'raw'
  const resolved = forceRaw
    ? { mode: 'forced_raw', status: 0, raw_auth_url: rawAuthUrl, final_auth_url: rawAuthUrl }
    : await resolveAuthUrl(rawAuthUrl)

  console.log(`[cline-bridge] auth callback: ${pendingAuth.callbackUrl}`)
  console.log(`[cline-bridge] auth url mode: ${resolved.mode}, status: ${resolved.status}`)
  openBrowser(resolved.final_auth_url)
  sendJson(res, 200, {
    status: 'opened',
    mode: resolved.mode,
    http_status: resolved.status,
    auth_url: resolved.final_auth_url,
    raw_auth_url: resolved.raw_auth_url,
    callback_url: callbackUrl,
    hint: forceRaw ? 'Opened raw Cline authorize URL.' : 'If login page errors, try /auth/start?open=raw',
  })
}

function normalizeModel(raw) {
  if (!raw || typeof raw !== 'object' || typeof raw.id !== 'string') return null
  return {
    id: raw.id,
    object: 'model',
    created: 0,
    owned_by: 'cline',
    name: typeof raw.name === 'string' ? raw.name : raw.id,
    description: typeof raw.description === 'string' ? raw.description : '',
    tags: Array.isArray(raw.tags) ? raw.tags.filter((x) => typeof x === 'string') : [],
  }
}

async function getRecommendedModels() {
  if (modelCache && Date.now() - modelCache.timestamp <= RECOMMENDED_MODELS_TTL_MS) return modelCache.data
  try {
    const data = await jsonFetch(`${API_BASE_URL}/api/v1/ai/cline/recommended-models`, { headers: baseHeaders() })
    const recommended = Array.isArray(data?.recommended) ? data.recommended.map(normalizeModel).filter(Boolean) : []
    const free = Array.isArray(data?.free) ? data.free.map(normalizeModel).filter(Boolean) : []
    const result = recommended.length || free.length ? { recommended, free } : null
    if (result) {
      modelCache = { data: result, timestamp: Date.now() }
      return result
    }
  } catch (error) {
    console.warn('[cline-bridge] Using fallback recommended models:', error.message)
  }
  const result = {
    recommended: fallbackModels.recommended.map(normalizeModel).filter(Boolean),
    free: fallbackModels.free.map(normalizeModel).filter(Boolean),
  }
  modelCache = { data: result, timestamp: Date.now() }
  return result
}

function openAiModelsPayload(models) {
  const seen = new Set()
  const data = []
  const entries = [
    ...models.free.map((model) => ({ model, tier: 'free' })),
    ...models.recommended.map((model) => ({ model, tier: 'recommended' })),
  ]
  for (const { model, tier } of entries) {
    if (!model?.id || seen.has(model.id)) continue
    seen.add(model.id)
    const tags = Array.isArray(model.tags) ? [...model.tags] : []
    if (tier === 'free' && !tags.some((tag) => String(tag).toUpperCase() === 'FREE')) {
      tags.unshift('FREE')
    }
    data.push({
      ...model,
      name: tier === 'free' && !String(model.name || model.id).includes('(free)') ? `${model.name || model.id} (free)` : model.name,
      description: tier === 'free' && !model.description ? 'Free model' : model.description,
      tags,
      tier,
      free: tier === 'free',
    })
  }
  return { object: 'list', data }
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload)
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type, X-Task-ID',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Content-Length': Buffer.byteLength(body),
  })
  res.end(body)
}

function sendText(res, status, text) {
  res.writeHead(status, {
    'Content-Type': 'text/plain; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
  })
  res.end(text)
}

async function readRequestBody(req) {
  const chunks = []
  for await (const chunk of req) chunks.push(chunk)
  return Buffer.concat(chunks).toString('utf8')
}

async function proxyChatCompletions(req, res) {
  const auth = await getAuthForRequest()
  const bodyText = await readRequestBody(req)
  let body
  try {
    body = bodyText ? JSON.parse(bodyText) : {}
  } catch {
    throw new Error('Invalid JSON request body')
  }

  const taskId = req.headers['x-task-id'] || randomUUID()
  const headers = {
    ...clineChatHeaders(String(taskId)),
    Authorization: `Bearer workos:${auth.idToken}`,
  }

  const upstream = await fetch(`${API_BASE_URL}/api/v1/chat/completions`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  })

  const responseHeaders = {
    'Access-Control-Allow-Origin': '*',
    'Cache-Control': 'no-cache',
  }
  const contentType = upstream.headers.get('content-type')
  if (contentType) responseHeaders['Content-Type'] = contentType
  const requestId = upstream.headers.get('x-request-id') || upstream.headers.get('request-id')
  if (requestId) responseHeaders['X-Cline-Request-ID'] = requestId

  res.writeHead(upstream.status, responseHeaders)
  if (!upstream.body) {
    res.end()
    return
  }
  for await (const chunk of upstream.body) {
    res.write(Buffer.from(chunk))
  }
  res.end()
}

async function handle(req, res) {
  try {
    if (req.method === 'OPTIONS') {
      sendJson(res, 204, {})
      return
    }
    const url = new URL(req.url || '/', `http://127.0.0.1:${DEFAULT_PORT}`)

    if (req.method === 'GET' && url.pathname === '/') {
      sendJson(res, 200, {
        name: 'cline-model-bridge',
        status: 'ok',
        api_base_url: API_BASE_URL,
        runtime_dir: RUNTIME_DIR,
        endpoints: ['/auth/start', '/auth/manual', '/auth/status', '/v1/models', '/v1/chat/completions'],
      })
      return
    }

    if (req.method === 'GET' && url.pathname === '/health') {
      const auth = await loadAuth()
      sendJson(res, 200, { ok: true, authenticated: Boolean(auth?.idToken), expires_at: auth?.expiresAt || null })
      return
    }

    if (req.method === 'GET' && url.pathname === '/auth/status') {
      const auth = await loadAuth()
      sendJson(res, 200, {
        authenticated: Boolean(auth?.idToken),
        token: auth?.idToken ? redact(auth.idToken) : null,
        expires_at: auth?.expiresAt || null,
        user: auth?.userInfo || null,
        pending_auth: Boolean(pendingAuth),
      })
      return
    }

    if ((req.method === 'GET' || req.method === 'POST') && url.pathname === '/auth/manual') {
      await handleManualAuth(req, res)
      return
    }

    if (req.method === 'GET' && url.pathname === '/auth/start') {
      await startAuthFlow(url, res)
      return
    }

    if (req.method === 'GET' && (url.pathname === '/v1/models' || url.pathname === '/models')) {
      const models = await getRecommendedModels()
      sendJson(res, 200, openAiModelsPayload(models))
      return
    }

    if (req.method === 'GET' && url.pathname === '/v1/cline/recommended-models') {
      sendJson(res, 200, await getRecommendedModels())
      return
    }

    if (req.method === 'POST' && url.pathname === '/v1/chat/completions') {
      await proxyChatCompletions(req, res)
      return
    }

    sendText(res, 404, 'Not found')
  } catch (error) {
    console.error('[cline-bridge]', error)
    sendJson(res, 500, { error: { message: error.message || String(error), type: 'cline_bridge_error' } })
  }
}

await ensureRuntimeDir()
const server = http.createServer(handle)
server.listen(DEFAULT_PORT, '127.0.0.1', () => {
  console.log(`[cline-bridge] listening on http://127.0.0.1:${DEFAULT_PORT}`)
  console.log(`[cline-bridge] auth: http://127.0.0.1:${DEFAULT_PORT}/auth/start`)
  console.log(`[cline-bridge] callback ports: ${CALLBACK_PORTS.join(', ')}`)
  console.log(`[cline-bridge] OpenAI-compatible base_url: http://127.0.0.1:${DEFAULT_PORT}/v1`)
  console.log(`[cline-bridge] runtime dir: ${RUNTIME_DIR}`)
})
