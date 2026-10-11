/**
 * MCP 子系统前端 API 客户端。
 *
 * 对应后端 `backend/mcp_routes.py`（挂载 /api/mcp/*）。
 * 设计文档：docs/MCP_DESIGN.md
 */

import { API_BASE } from './base'

// ── 类型（跟 backend/mcp/types.py 对齐）────────────────────────────────
export type MCPServerState =
  | 'idle'
  | 'starting'
  | 'running'
  | 'degraded'
  | 'stopping'
  | 'needs_oauth'
  | 'needs_reauth'

export interface MCPServerStatus {
  server_id: string
  state: MCPServerState
  error: string | null
  started_at: number | null
  last_error_at: number | null
  tool_count: number
}

export interface MCPServerConfig {
  command?: string | null
  args: string[]
  env: Record<string, string>
  url?: string | null
  headers: Record<string, string>
  transport?: 'stdio' | 'streamable_http' | 'sse' | null
  enabled: boolean
  timeout: number
  connect_timeout: number
  description: string
  cwd?: string | null
  agents: string[]
  concurrency_class: 'read' | 'write' | 'exclusive'
  sampling: {
    enabled: boolean
    allowed_models: string[]
    max_tokens_cap: number
    max_rpm: number
  }
  elicitation: {
    enabled: boolean
    timeout_seconds: number
  }
}

export interface MCPToolInfo {
  server_id: string
  name: string
  qualified_name: string
  description: string
  input_schema: Record<string, unknown>
}

export interface MCPServerListItem {
  server_id: string
  config: MCPServerConfig
  status: MCPServerStatus
  tools: MCPToolInfo[]
}

export interface MCPCatalogEntry {
  name: string
  title: string
  description: string
  icon: string
  homepage: string
  configuration: Record<string, unknown>
  tags: string[]
  requires_token: boolean
}

// ── 默认 config 构造（前端表单初始化用）──────────────────────────────
export function buildDefaultConfig(partial: Partial<MCPServerConfig> = {}): MCPServerConfig {
  return {
    args: [],
    env: {},
    headers: {},
    enabled: true,
    timeout: 120,
    connect_timeout: 60,
    description: '',
    agents: ['*'],
    concurrency_class: 'read',
    sampling: { enabled: false, allowed_models: [], max_tokens_cap: 4096, max_rpm: 10 },
    elicitation: { enabled: true, timeout_seconds: 60 },
    ...partial,
  }
}

// ── REST 调用 ───────────────────────────────────────────────────────────
async function _json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
  }
  return res.json() as Promise<T>
}

export async function listServers(): Promise<MCPServerListItem[]> {
  const res = await fetch(`${API_BASE}/api/mcp/servers`)
  const data = await _json<{ servers: MCPServerListItem[] }>(res)
  return data.servers
}

export async function getServer(serverId: string): Promise<MCPServerListItem> {
  const res = await fetch(`${API_BASE}/api/mcp/servers/${encodeURIComponent(serverId)}`)
  return _json<MCPServerListItem>(res)
}

export async function addServer(
  serverId: string,
  config: MCPServerConfig,
): Promise<{ server_id: string; status: MCPServerStatus }> {
  const res = await fetch(`${API_BASE}/api/mcp/servers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ server_id: serverId, config }),
  })
  return _json(res)
}

export async function updateServer(
  serverId: string,
  config: MCPServerConfig,
): Promise<{ server_id: string; status: MCPServerStatus }> {
  const res = await fetch(`${API_BASE}/api/mcp/servers/${encodeURIComponent(serverId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  })
  return _json(res)
}

export async function deleteServer(serverId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api/mcp/servers/${encodeURIComponent(serverId)}`, {
    method: 'DELETE',
  })
  await _json(res)
}

export async function toggleServer(
  serverId: string,
  enabled: boolean,
): Promise<{ server_id: string; status: MCPServerStatus }> {
  const res = await fetch(
    `${API_BASE}/api/mcp/servers/${encodeURIComponent(serverId)}/toggle`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    },
  )
  return _json(res)
}

export async function restartServer(
  serverId: string,
): Promise<{ server_id: string; status: MCPServerStatus }> {
  const res = await fetch(
    `${API_BASE}/api/mcp/servers/${encodeURIComponent(serverId)}/restart`,
    { method: 'POST' },
  )
  return _json(res)
}

export async function getRegistry(): Promise<MCPCatalogEntry[]> {
  const res = await fetch(`${API_BASE}/api/mcp/registry`)
  const data = await _json<{ entries: MCPCatalogEntry[] }>(res)
  return data.entries
}

export async function reauthServer(
  serverId: string,
): Promise<{ server_id: string; status: MCPServerStatus }> {
  const res = await fetch(
    `${API_BASE}/api/mcp/servers/${encodeURIComponent(serverId)}/reauth`,
    { method: 'POST' },
  )
  return _json(res)
}

export async function getControlSessionId(): Promise<string> {
  const res = await fetch(`${API_BASE}/api/mcp/control-session-id`)
  const data = await _json<{ session_id: string }>(res)
  return data.session_id
}

export async function respondElicitation(
  requestId: string,
  action: 'accept' | 'decline' | 'cancel',
  content: Record<string, unknown> | null = null,
): Promise<void> {
  const res = await fetch(
    `${API_BASE}/api/mcp/elicitations/${encodeURIComponent(requestId)}/respond`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, content }),
    },
  )
  await _json(res)
}

export async function installMcpb(
  file: File,
  serverIdHint?: string,
): Promise<{ server_id: string; status: MCPServerStatus }> {
  const fd = new FormData()
  fd.append('file', file)
  const url = serverIdHint
    ? `${API_BASE}/api/mcp/install-mcpb?server_id=${encodeURIComponent(serverIdHint)}`
    : `${API_BASE}/api/mcp/install-mcpb`
  const res = await fetch(url, { method: 'POST', body: fd })
  return _json(res)
}

export async function getMarketplaces(): Promise<string[]> {
  const res = await fetch(`${API_BASE}/api/mcp/marketplaces`)
  const data = await _json<{ urls: string[] }>(res)
  return data.urls
}

export async function setMarketplaces(urls: string[]): Promise<string[]> {
  const res = await fetch(`${API_BASE}/api/mcp/marketplaces`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ urls }),
  })
  const data = await _json<{ urls: string[] }>(res)
  return data.urls
}

export async function listAllTools(agentId?: string): Promise<MCPToolInfo[]> {
  const url = agentId
    ? `${API_BASE}/api/mcp/tools?agent_id=${encodeURIComponent(agentId)}`
    : `${API_BASE}/api/mcp/tools`
  const res = await fetch(url)
  const data = await _json<{ tools: MCPToolInfo[] }>(res)
  return data.tools
}
