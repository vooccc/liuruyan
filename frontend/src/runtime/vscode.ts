/**
 * VSCode runtime —— 通过插件 bridge HTTP server 间接调 VSCode Extension API。
 *
 * 激活条件：webview iframe 加载 URL 带 `?csrf=xxx&bridgePort=xxx`，由插件 webview/html.ts 注入。
 *
 * 分层职责：
 *   - window : 全 no-op（VSCode 宿主窗口不该被 webview 控制；最小化/最大化由 IDE 管）
 *   - dialog : 阶段 2 第一轮尚未实现，throw；阶段 2 后期走 vscode.window.showSaveDialog 桥接
 *   - fs     : 同 dialog，等 vscode.workspace.fs 端点加完再接
 *   - ide    : 调 /vscode/* 端点，阶段 2 第一轮覆盖 active-editor / selection
 *
 * CSRF：每个 fetch 都注入 x-liuruyan-csrf header；插件 server 端挂了
 * `app.use('/vscode', csrf)` 校验，少 header 或错 token 都返 403。
 *
 * 配套设计：docs/vscode_extension_plan.md §6.2 + docs/ide_integration_principles.md 铁律 3
 */

import type {
  Runtime,
  RuntimeWindow,
  RuntimeDialog,
  RuntimeFs,
  RuntimeShell,
  RuntimeIde,
  DiagnosticItem,
} from './types'

const CSRF_HEADER = 'x-liuruyan-csrf'

interface BridgeConfig {
  csrf: string
  bridgePort: string
  base: string
}

function readBridgeConfig(): BridgeConfig {
  const sp = new URLSearchParams(window.location.search)
  const csrf = sp.get('csrf')
  const bridgePort = sp.get('bridgePort')
  if (!csrf || !bridgePort) {
    throw new Error(
      'VSCode runtime requires ?csrf=...&bridgePort=... in URL query (injected by extension webview)',
    )
  }
  // 用 localhost（不是 127.0.0.1）匹配 vite 默认 IPv6 listen 行为；
  // 插件 server 在 extension.ts 里是 '127.0.0.1' listen（Node net 会同时绑 v4/v6），
  // 但为求一致走 localhost 让浏览器/webview 按系统偏好解析。
  const base = `http://localhost:${bridgePort}`
  return { csrf, bridgePort, base }
}

let _config: BridgeConfig | null = null

function getConfig(): BridgeConfig {
  if (!_config) _config = readBridgeConfig()
  return _config
}

async function callBridge<T>(path: string, init: RequestInit = {}): Promise<T> {
  const cfg = getConfig()
  const headers = new Headers(init.headers ?? {})
  headers.set(CSRF_HEADER, cfg.csrf)
  if (init.body && !headers.has('content-type')) {
    headers.set('content-type', 'application/json')
  }
  const res = await fetch(`${cfg.base}${path}`, { ...init, headers })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`bridge ${path} → ${res.status}: ${body.slice(0, 200)}`)
  }
  return res.json() as Promise<T>
}

// ---- 类型别名（跟插件端 ide-routes.ts 的响应 schema 对齐）----
interface ActiveEditorResponse {
  ok: true
  editor: {
    uri: string
    fsPath: string
    fileName: string
    languageId: string
    lineCount: number
    content: string
    selection: { start: number; end: number; text: string } | null
  } | null
}

interface SelectionResponse {
  ok: true
  selection: { uri: string; fsPath: string; text: string } | null
}

interface InsertSnippetResponse {
  ok: true
  applied: boolean
  uri: string
  fsPath: string
  lineCount: number
}

interface OpenFileResponse {
  ok: true
  opened: boolean
  uri: string
  fsPath: string
  lineCount: number
}

interface DiagnosticsResponse {
  ok: true
  diagnostics: DiagnosticItem[]
}

interface ApplyEditResponse {
  ok: true
  applied: boolean
  uri: string
  fsPath: string
  reason: string
  // Phase 3 v2 inline diff 新增字段（v1 modal fallback 不返回）
  sessionId?: string
  accepted?: number
  rejected?: number
  total?: number
  status?: 'completed' | 'aborted' | 'no-op'
}

interface OpenTerminalResponse {
  ok: true
  terminal_id: string
  name: string
  created: boolean
}

interface RunCommandResponse {
  ok: true
  sent: true
  output: string | null
  captured: boolean
  exitCode?: number | null
  reason?: string
  terminal_id: string
}

// ---- RuntimeWindow: 全 no-op ----
const vscodeWindow: RuntimeWindow = {
  async minimize() {
    /* no-op: VSCode 宿主窗口不由 webview 控制 */
  },
  async toggleMaximize() {
    /* no-op */
  },
  async isMaximized() {
    return false
  },
  async close() {
    /* no-op */
  },
  async startDragging() {
    /* no-op */
  },
  async onResized() {
    return () => {
      /* no-op */
    }
  },
}

const notImplemented = (api: string) =>
  new Error(`VSCode runtime: ${api} not implemented yet (see vscode_extension_plan.md §12)`)

const vscodeDialog: RuntimeDialog = {
  async save() {
    throw notImplemented('dialog.save')
  },
  async pickDirectory() {
    // VSCode webview 不能直接调原生对话框；需要走 bridge endpoint 让插件主进程
    // 调 vscode.window.showOpenDialog。短期未实现，调用方应 catch 后 fallback 到手动输入。
    throw notImplemented('dialog.pickDirectory')
  },
}

const vscodeFs: RuntimeFs = {
  async copyFile() {
    throw notImplemented('fs.copyFile')
  },
}

// ---- RuntimeIde: 核心桥接 ----
const vscodeIde: RuntimeIde = {
  async getActiveEditor() {
    const resp = await callBridge<ActiveEditorResponse>('/vscode/active-editor')
    if (!resp.editor) return null
    return {
      uri: resp.editor.uri,
      content: resp.editor.content,
      selection: resp.editor.selection ?? undefined,
    }
  },
  async getSelection() {
    const resp = await callBridge<SelectionResponse>('/vscode/selection')
    if (!resp.selection) return null
    return { uri: resp.selection.uri, text: resp.selection.text }
  },
  async insertSnippet(opts) {
    const resp = await callBridge<InsertSnippetResponse>('/vscode/insert-snippet', {
      method: 'POST',
      body: JSON.stringify(opts),
    })
    return { applied: resp.applied, uri: resp.uri, lineCount: resp.lineCount }
  },
  async openFile(opts) {
    const resp = await callBridge<OpenFileResponse>('/vscode/open-file', {
      method: 'POST',
      body: JSON.stringify(opts),
    })
    return { opened: resp.opened, uri: resp.uri, lineCount: resp.lineCount }
  },
  async getDiagnostics(opts = {}) {
    const sp = new URLSearchParams()
    if (opts.scope === 'all') sp.set('all', '1')
    if (opts.uri) sp.set('uri', opts.uri)
    const qs = sp.toString()
    const path = qs ? `/vscode/diagnostics?${qs}` : '/vscode/diagnostics'
    const resp = await callBridge<DiagnosticsResponse>(path)
    return resp.diagnostics
  },
  async applyEdit(opts) {
    const resp = await callBridge<ApplyEditResponse>('/vscode/apply-edit', {
      method: 'POST',
      body: JSON.stringify(opts),
    })
    return {
      applied: resp.applied,
      uri: resp.uri,
      reason: resp.reason,
      // 透传 v2 新增字段 (v1 路径下全部 undefined)
      sessionId: resp.sessionId,
      accepted: resp.accepted,
      rejected: resp.rejected,
      total: resp.total,
      status: resp.status,
    }
  },
  async openTerminal(opts) {
    const resp = await callBridge<OpenTerminalResponse>('/vscode/open-terminal', {
      method: 'POST',
      body: JSON.stringify(opts),
    })
    return { terminal_id: resp.terminal_id, name: resp.name, created: resp.created }
  },
  async runCommandInTerminal(opts) {
    const resp = await callBridge<RunCommandResponse>('/vscode/run-command-in-terminal', {
      method: 'POST',
      body: JSON.stringify(opts),
    })
    return {
      sent: resp.sent,
      output: resp.output,
      captured: resp.captured,
      exitCode: resp.exitCode,
      reason: resp.reason,
      terminal_id: resp.terminal_id,
    }
  },
}

const vscodeShell: RuntimeShell = {
  async openExternal(url) {
    // VSCode webview 默认会拦截 window.open 弹出"打开外部链接"确认框。
    // 不走 bridge endpoint 是因为：webview 自带的确认 UX 已经够好，不需要绕一圈到插件主进程。
    try {
      window.open(url, '_blank', 'noopener,noreferrer')
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn('[runtime/vscode] window.open failed', err)
    }
  },
}

export async function createVSCodeRuntime(): Promise<Runtime> {
  // 预热：在工厂里提前读一次 config，让 URL 参数缺失的错误尽早抛出（而不是延后到第一次 API 调用时）
  getConfig()
  return {
    kind: 'vscode',
    window: vscodeWindow,
    dialog: vscodeDialog,
    fs: vscodeFs,
    shell: vscodeShell,
    ide: vscodeIde,
  }
}
