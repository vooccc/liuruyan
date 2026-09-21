/**
 * 工具 API。
 *
 * 单一事实源是后端 backend.tools.builtin.clean_registry.registry。
 * 这里只是镜像视图 + 白名单浅编辑客户端，不缓存、不再加工。
 */
import { API_BASE } from './base'

export type ToolSource = 'builtin' | 'mcp' | 'ide'

export interface ToolInventoryItem {
  name: string
  description: string
  input_schema: Record<string, unknown>
  source: ToolSource
  concurrency_class: string
  description_chars: number
  approx_tokens: number
  /** 哪些 agent 显式白名单包含此工具（未配置白名单的 agent 不出现） */
  allowed_in_agents: string[]
  /** 当前 description 是否被 dev override 覆盖（仅 builtin 有意义） */
  is_overridden: boolean
  /** 被覆盖前的源码原始 description；未覆盖过为 null */
  baseline_description: string | null
  /** 是否允许 UI 编辑 description（仅 builtin = true） */
  can_edit_description: boolean
}

export interface AgentInventoryItem {
  id: string
  name: string
  avatar?: string | null
  avatar_url?: string | null
  /** null = 未配置白名单 = 全开 */
  allowed_tools: string[] | null
  scope: 'all' | 'whitelist'
  tool_count: number
  description_chars: number
  approx_tokens: number
}

export interface DevModeState {
  enabled: boolean
  via_env: boolean
  via_runtime_toggle: boolean
  can_toggle_off: boolean
}

export interface ToolsInventoryResponse {
  tools: ToolInventoryItem[]
  agents: AgentInventoryItem[]
  totals: { builtin: number; ide: number; mcp: number; total: number }
  dev_mode: boolean
  dev_mode_state: DevModeState
}

export async function getToolsInventory(): Promise<ToolsInventoryResponse> {
  const res = await fetch(`${API_BASE}/tools/inventory`)
  if (!res.ok) {
    throw new Error(`GET /tools/inventory failed: ${res.status} ${res.statusText}`)
  }
  return (await res.json()) as ToolsInventoryResponse
}

/**
 * 更新 agent 白名单（写 meta.json["tools"]）。
 * `tools=null` → 清除白名单，让 agent 全开。
 */
export async function setAgentAllowedTools(
  agentId: string,
  tools: string[] | null,
): Promise<{ status: string; agent_id: string; allowed_tools: string[] | null }> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/allowed_tools`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tools }),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`PATCH allowed_tools failed: ${res.status} ${text}`)
  }
  return await res.json()
}

/**
 * 切换运行时 dev mode（点击手势触发）。env 已开时 toggle off 无效。
 */
export async function toggleDevMode(enabled: boolean): Promise<DevModeState & { status: string }> {
  const res = await fetch(`${API_BASE}/dev/toggle`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`POST /dev/toggle failed: ${res.status} ${text}`)
  }
  return await res.json()
}

/**
 * 覆盖 / 重置内置工具的 description（仅 dev mode；普通模式后端返 403）。
 * `description=null` → 清除覆盖恢复源码原版。
 */
export async function setToolDescription(
  toolName: string,
  description: string | null,
): Promise<{
  status: string
  name: string
  description: string
  overridden: boolean
}> {
  const res = await fetch(
    `${API_BASE}/tools/inventory/${encodeURIComponent(toolName)}/description`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ description }),
    },
  )
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`PUT description failed: ${res.status} ${text}`)
  }
  return await res.json()
}
