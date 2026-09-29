/**
 * Gateway 子系统的前端 API 封装。
 *
 * 后端实现：backend/gateway/routes.py
 * 设计文档：_docs/GATEWAY.md
 *
 * 注意：admin 端点强制 loopback，所以这里调用方必须从同机访问后端
 * （柳如烟桌面客户端 + Tauri sidecar 默认就是这样）。
 */
import { API_BASE, API_HOST_PORT } from './base'

export interface GatewayConfig {
  enabled: boolean
  host: string
  cors: boolean
  require_token: boolean
  idle_timeout_secs: number
  log_request_body: boolean
}

export interface GatewayModelInfo {
  id: string
  object: 'model'
  owned_by: string
  provider: string
  provider_name: string
  transport: string
  tags: string[]
  context_length: number
  name: string
}

export interface GatewayHealth {
  ok: boolean
  version: string
  enabled: boolean
}

async function http<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init?.headers as Record<string, string> | undefined),
  }
  const body = init?.json !== undefined ? JSON.stringify(init.json) : init?.body
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers, body: body as BodyInit | null | undefined })
  if (!res.ok) {
    let detail: string
    try {
      const err = await res.json()
      detail = typeof err.detail === 'string' ? err.detail
        : err.error?.message ?? err.error?.code ?? JSON.stringify(err)
    } catch {
      detail = await res.text()
    }
    throw new Error(`[${res.status}] ${detail || res.statusText}`)
  }
  if (res.status === 204) return undefined as unknown as T
  return res.json()
}

export async function getGatewayHealth(): Promise<GatewayHealth> {
  return http<GatewayHealth>('/gateway/v1/health')
}

export async function getGatewayConfig(): Promise<GatewayConfig> {
  return http<GatewayConfig>('/gateway/admin/config')
}

export async function updateGatewayConfig(patch: Partial<GatewayConfig>): Promise<GatewayConfig> {
  return http<GatewayConfig>('/gateway/admin/config', { method: 'PUT', json: patch })
}

/**
 * 模型清单——loopback admin 端点，不需要 token。
 * UI 端拉模型清单不再走 /v1/models（避免为了拉清单去碰用户的明文令牌）。
 * 每次都是新请求，不缓存——这样 provider 改了之后展开看到的就是最新。
 */
export async function listAdminModels(
  capability?: string,
): Promise<{ object: 'list'; data: GatewayModelInfo[] }> {
  const qs = capability ? `?capability=${encodeURIComponent(capability)}` : ''
  return http<{ object: 'list'; data: GatewayModelInfo[] }>(`/gateway/admin/models${qs}`)
}

/** 用于在 UI 里展示给用户复制的 endpoint URL（不含 /v1 后缀；客户端会自己拼）。 */
export function gatewayEndpointBase(): string {
  // API_BASE 已经包含 scheme + host:port；append /gateway 即可
  return `${API_BASE}/gateway`
}

/** OpenAI 兼容客户端用的 base url（带 /v1）。 */
export function gatewayOpenAIBaseUrl(): string {
  return `${API_BASE}/gateway/v1`
}

/** Anthropic 原生客户端用的 base url（不带 /v1，客户端自动拼 /v1/messages）。 */
export function gatewayAnthropicBaseUrl(): string {
  return `${API_BASE}/gateway`
}

export const GATEWAY_HOST_PORT = API_HOST_PORT

/* ─── M5: launcher snippets（外部 CLI 启动命令） ─── */

export interface GatewayLauncherSnippet {
  id: string
  name: string
  description: string
  protocol: 'openai_chat' | 'anthropic_messages'
  posix_command: string
  powershell_command: string
  placeholders: string[]
  notes: string[]
  /** true = CLI 命令可由柳如烟一键启动新终端窗口；false = GUI 配置型，仅展示文本 */
  runnable: boolean
}

export interface GatewayLaunchersResponse {
  openai_base_url: string
  anthropic_base_url: string
  launchers: GatewayLauncherSnippet[]
}

export async function getGatewayLaunchers(): Promise<GatewayLaunchersResponse> {
  return http<GatewayLaunchersResponse>('/gateway/admin/launchers')
}

/* ─── M3: alias 映射 + fallback 链 + 健康缓存 ─── */

export interface GatewayAliasEntry {
  candidates: string[]
  description: string
  created_at: string
  updated_at: string
}

export type GatewayAliasMap = Record<string, GatewayAliasEntry>

export interface GatewayHealthState {
  failure_kind: 'auth' | 'rate_limit' | 'server_error' | 'timeout' | 'network'
  failed_at: number
  cooldown_secs: number
  remaining_secs: number
  error_message: string
}

export async function listGatewayAliases(): Promise<GatewayAliasMap> {
  const data = await http<{ aliases: GatewayAliasMap }>('/gateway/admin/aliases')
  return data.aliases
}

export async function createGatewayAlias(
  name: string,
  candidates: string[],
  description: string = '',
): Promise<GatewayAliasEntry & { name: string }> {
  return http('/gateway/admin/aliases', {
    method: 'POST',
    json: { name, candidates, description },
  })
}

export async function updateGatewayAlias(
  name: string,
  candidates: string[],
  description: string = '',
): Promise<GatewayAliasEntry & { name: string }> {
  return http(`/gateway/admin/aliases/${encodeURIComponent(name)}`, {
    method: 'PUT',
    json: { candidates, description },
  })
}

export async function deleteGatewayAlias(name: string): Promise<{ ok: boolean; deleted: string }> {
  return http(`/gateway/admin/aliases/${encodeURIComponent(name)}`, { method: 'DELETE' })
}

export async function getGatewayUnhealthyProviders(): Promise<Record<string, GatewayHealthState>> {
  const data = await http<{ unhealthy_providers: Record<string, GatewayHealthState> }>(
    '/gateway/admin/health',
  )
  return data.unhealthy_providers
}

export async function clearGatewayHealthBlacklist(): Promise<{ ok: boolean }> {
  return http('/gateway/admin/health/clear', { method: 'POST' })
}

/* ─── M4-C: 用量统计 ─── */

export type GatewayUsageGroupBy =
  | 'provider'
  | 'model'
  | 'token'
  | 'day'
  | 'endpoint'
  | 'client_ip'

export interface GatewayUsageBucket {
  key: string
  calls: number
  errors: number
  error_rate: number
  stream_calls: number
  nonstream_calls: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  fallback_jumps_in: number
  fallback_jumps_out: number
  p50_latency_ms: number | null
  p95_latency_ms: number | null
  first_seen_ts: string
  last_seen_ts: string
}

export interface GatewayUsage {
  range: { from: string; to: string }
  group_by: GatewayUsageGroupBy
  total_calls: number
  total_errors: number
  error_rate: number
  total_prompt_tokens: number
  total_completion_tokens: number
  total_tokens: number
  buckets: GatewayUsageBucket[]
}

export interface GatewayFallbackJumpEvent {
  ts: string
  endpoint: string
  model_requested: string
  from_provider: string
  from_status: number | null
  from_result: string | null
  to_provider: string
  attempts_count: number
  stream: boolean
}

export async function getGatewayUsage(opts: {
  groupBy?: GatewayUsageGroupBy
  start?: string
  end?: string
  topN?: number
} = {}): Promise<GatewayUsage> {
  const params = new URLSearchParams()
  params.set('group_by', opts.groupBy || 'provider')
  if (opts.start) params.set('start', opts.start)
  if (opts.end) params.set('end', opts.end)
  if (opts.topN) params.set('top_n', String(opts.topN))
  return http<GatewayUsage>(`/gateway/admin/usage?${params.toString()}`)
}

export async function getGatewayRecentFallbackJumps(
  limit: number = 20,
): Promise<GatewayFallbackJumpEvent[]> {
  const data = await http<{ jumps: GatewayFallbackJumpEvent[] }>(
    `/gateway/admin/usage/fallback-jumps?limit=${limit}`,
  )
  return data.jumps
}

/* ─── M5: 一键 CLI 接入 ─── */

export type GatewayInstallerState =
  | 'not_installed'
  | 'installed_pointing_to_us'
  | 'installed_pointing_elsewhere'
  | 'external_modified'
  | 'error'

export interface GatewayInstallerInfo {
  id: string
  name: string
  description: string
  protocol: 'openai_chat' | 'anthropic_messages'
  target_path: string
}

export interface GatewayInstallerStatus {
  state: GatewayInstallerState
  detail: string
  target_path: string
  target_exists: boolean
  last_install_at: string | null
  backup_count: number
}

export interface GatewayInstallerEntry {
  info: GatewayInstallerInfo
  status: GatewayInstallerStatus
}

export interface GatewayInstallerPreview {
  id: string
  action: 'install' | 'uninstall'
  target_path: string
  target_exists: boolean
  will_create: boolean
  will_modify: boolean
  will_backup_to: string | null
  external_modified: boolean
  diff_summary: string
}

export interface GatewayInstallerActionResult {
  ok: boolean
  message: string
  target_path: string | null
  backup_path?: string | null
  restored_from?: string | null
}

export async function listGatewayInstallers(): Promise<GatewayInstallerEntry[]> {
  const data = await http<{ installers: GatewayInstallerEntry[] }>('/gateway/admin/installers')
  return data.installers
}

/**
 * Claude Code 三角色 model 分配（ccswitch 风格）。
 * 仅 id === 'claude_code' 时被后端处理；其他 installer 忽略。
 * 值为合法 `<provider:model>` / alias 名时，写到 settings.json 的 ANTHROPIC_DEFAULT_*_MODEL env；
 * 空 / null / 非法字符时跳过该字段，让 Claude Code 用内置默认（需有 gateway alias 兜底）。
 */
export interface ClaudeDefaultModels {
  haiku?: string | null
  sonnet?: string | null
  opus?: string | null
}

/**
 * Claude Code 安装选项。
 *
 * - ``mode``：可选，默认 ``gateway``。``direct`` = 直连服务商：要求 anthropic_messages
 *   协议的 provider，base_url/api_key 直接写进 ~/.claude/settings.json 的 env，
 *   不经柳如烟本地 Gateway 转发。OpenAI 协议上游仍需网关模式做协议互转。
 * - ``directProvider``：直连模式必填。柳如烟『模型管理』里的 provider id。
 *   default_models 的 ``<provider_id>:`` 前缀会被后端剥掉（直连需 vendor 裸 model id）。
 */
export interface ClaudeInstallOptions {
  mode?: 'gateway' | 'direct'
  directProvider?: string
}

/**
 * Codex 安装选项（ccswitch 风格）。
 *
 * - ``defaultModel``：**必填**。填到 ~/.codex/config.toml 的顶层 ``model = "..."``。
 *   网关模式下取柳如烟里一个 alias 名或 ``<provider_id>:<vendor_model>`` 命名空间格式；
 *   直连模式下传 ``<provider_id>:<vendor_model>``，后端会剥前缀写 vendor 裸 id。
 *   不填后端返 400 ``codex_default_model_required``。
 * - ``reasoningEffort``：可选，默认 ``high``。minimal/low/medium/high。
 * - ``mode``：可选，默认 ``gateway``。``direct`` = 直连服务商（把 provider 的
 *   base_url/api_key 直接写进 ~/.codex/，不经柳如烟本地 Gateway 转发）。
 * - ``directProvider``：直连模式必填。柳如烟『模型管理』里的 provider id。
 * - ``wireApi``：已废弃兼容字段。codex 0.153+ 移除了 chat wire 支持
 *   （openai/codex#7782），两种模式均固定 ``responses``，后端会忽略其他值。
 */
export interface CodexInstallOptions {
  defaultModel: string
  reasoningEffort?: 'minimal' | 'low' | 'medium' | 'high'
  mode?: 'gateway' | 'direct'
  directProvider?: string
  wireApi?: 'responses'
}

export async function previewGatewayInstaller(
  id: string,
  options?: {
    defaultModels?: ClaudeDefaultModels
    codex?: CodexInstallOptions
    claude?: ClaudeInstallOptions
  },
): Promise<GatewayInstallerPreview> {
  const body: Record<string, unknown> = {}
  if (options?.defaultModels) body.default_models = options.defaultModels
  if (options?.codex?.defaultModel) body.default_model = options.codex.defaultModel
  if (options?.codex?.reasoningEffort) body.reasoning_effort = options.codex.reasoningEffort
  if (options?.codex?.mode) body.mode = options.codex.mode
  if (options?.codex?.directProvider) body.provider_id = options.codex.directProvider
  if (options?.codex?.wireApi) body.wire_api = options.codex.wireApi
  if (options?.claude?.mode) body.mode = options.claude.mode
  if (options?.claude?.directProvider) body.provider_id = options.claude.directProvider
  return http<GatewayInstallerPreview>(
    `/gateway/admin/installers/${encodeURIComponent(id)}/preview`,
    { method: 'POST', json: body },
  )
}

export async function installGatewayInstaller(
  id: string,
  options?: {
    force?: boolean
    defaultModels?: ClaudeDefaultModels
    codex?: CodexInstallOptions
    claude?: ClaudeInstallOptions
  },
): Promise<GatewayInstallerActionResult> {
  const body: Record<string, unknown> = { force: options?.force ?? false }
  if (options?.defaultModels) body.default_models = options.defaultModels
  if (options?.codex?.defaultModel) body.default_model = options.codex.defaultModel
  if (options?.codex?.reasoningEffort) body.reasoning_effort = options.codex.reasoningEffort
  if (options?.codex?.mode) body.mode = options.codex.mode
  if (options?.codex?.directProvider) body.provider_id = options.codex.directProvider
  if (options?.codex?.wireApi) body.wire_api = options.codex.wireApi
  if (options?.claude?.mode) body.mode = options.claude.mode
  if (options?.claude?.directProvider) body.provider_id = options.claude.directProvider
  return http<GatewayInstallerActionResult>(
    `/gateway/admin/installers/${encodeURIComponent(id)}/install`,
    { method: 'POST', json: body },
  )
}

/* ─── 一键启动终端（2026-05-23 新增） ─── */

export interface GatewayLaunchTerminalResult {
  ok: boolean
  message: string
  platform: string
  pid?: number | null
}

/**
 * 在用户机器的**新终端窗口**里直接跑某条 launcher 命令。
 * Win = PowerShell + CREATE_NEW_CONSOLE / macOS = Terminal.app（osascript）/ Linux = 探测 emulator。
 * shell 必须和当前面板的 shellMode 一致；后端按此选 posix_command 还是 powershell_command。
 * 仅 runnable=true 的 launcher 可启动；GUI 配置型会 400。
 *
 * cwd（可选）：新终端的起点工作目录。比如 claude code 启动前选个项目根目录，
 * 这样 claude 就能直接看到你那个项目的文件。后端会校验目录存在性。
 */
export async function launchGatewayLauncherTerminal(
  id: string,
  shell: 'powershell' | 'posix',
  cwd?: string,
): Promise<GatewayLaunchTerminalResult> {
  const body: Record<string, unknown> = { shell }
  if (cwd) body.cwd = cwd
  return http<GatewayLaunchTerminalResult>(
    `/gateway/admin/launchers/${encodeURIComponent(id)}/launch_terminal`,
    { method: 'POST', json: body },
  )
}

export async function uninstallGatewayInstaller(
  id: string,
  force: boolean = false,
): Promise<GatewayInstallerActionResult> {
  return http<GatewayInstallerActionResult>(
    `/gateway/admin/installers/${encodeURIComponent(id)}/uninstall`,
    { method: 'POST', json: { force } },
  )
}

/* ─── M6: 多 token + 配额限流 ─── */

export interface GatewayTokenQuota {
  calls_per_minute: number | null
  calls_per_day: number | null
  total_tokens_per_day: number | null
}

export interface GatewayTokenRuntime {
  calls_last_minute: number
  calls_last_day: number
  tokens_last_day: number
}

export interface GatewayTokenEntry {
  id: string
  name: string
  /** 明文密钥。loopback admin 端点暴露，UI 默认打码显示 + 一键复制。 */
  token: string
  /** 后端衍生的打码串（前 8 + '••••' + 后 4），UI 默认显示用。 */
  preview: string
  created_at: string
  last_used_at: string | null
  disabled: boolean
  allow_models: string[]
  deny_models: string[]
  /** M7：IP 白名单。每条为 IPv4/IPv6 单 IP 或 CIDR；空数组 = 不限制。 */
  allow_ips: string[]
  quota: GatewayTokenQuota
  runtime: GatewayTokenRuntime
}

export interface GatewayTokenCreatePayload {
  name: string
  allow_models?: string[]
  deny_models?: string[]
  /** M7：IP 白名单。后端用 ipaddress 模块校验；任一不合法返 400 invalid_ip_entry。 */
  allow_ips?: string[]
  quota?: Partial<GatewayTokenQuota>
}

export interface GatewayTokenUpdatePayload {
  name?: string
  disabled?: boolean
  allow_models?: string[]
  deny_models?: string[]
  /** M7：传 undefined 不动，传空数组清空，传非空数组替换。 */
  allow_ips?: string[]
  quota?: Partial<GatewayTokenQuota>
}

export async function listGatewayTokens(): Promise<GatewayTokenEntry[]> {
  const data = await http<{ tokens: GatewayTokenEntry[] }>('/gateway/admin/tokens')
  return data.tokens
}

export async function createGatewayToken(
  payload: GatewayTokenCreatePayload,
): Promise<GatewayTokenEntry> {
  return http<GatewayTokenEntry>('/gateway/admin/tokens', { method: 'POST', json: payload })
}

export async function updateGatewayTokenEntry(
  id: string,
  payload: GatewayTokenUpdatePayload,
): Promise<GatewayTokenEntry> {
  return http<GatewayTokenEntry>(
    `/gateway/admin/tokens/${encodeURIComponent(id)}`,
    { method: 'PUT', json: payload },
  )
}

export async function deleteGatewayTokenEntry(id: string): Promise<{ ok: boolean; deleted: string }> {
  return http(`/gateway/admin/tokens/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export async function rotateGatewayTokenEntry(id: string): Promise<GatewayTokenEntry> {
  return http<GatewayTokenEntry>(
    `/gateway/admin/tokens/${encodeURIComponent(id)}/rotate`,
    { method: 'POST', json: {} },
  )
}

export async function getGatewayTokenUsage(
  id: string,
): Promise<{ id: string; quota: GatewayTokenQuota; runtime: GatewayTokenRuntime }> {
  return http(`/gateway/admin/tokens/${encodeURIComponent(id)}/usage`)
}
