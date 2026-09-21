/**
 * ACP 外部 agent 状态 API。
 *
 * 设计：API key 配置走「模型服务」面板，这里只读状态——所以只有 GET。
 */
import { API_BASE } from './base'

export type ResolvedVia = 'env' | 'provider' | 'none'

export interface AcpCompatibleProvider {
  id: string
  name: string
  transport: string
  base_url: string
  has_key: boolean
  is_default: boolean
}

export interface AcpAgentStatus {
  agent: string
  description: string
  command: string
  env_required: string[]
  /** EXTERNAL_AGENTS 里硬编码的默认 provider id（不受用户覆盖影响） */
  provider_id: string | null
  /** 算上用户覆盖后真正会用到的 provider id */
  effective_provider_id: string | null
  /** 该 agent 协议兼容的所有 provider，下拉框直接渲染这个 */
  compatible_providers: AcpCompatibleProvider[]
  available: boolean
  missing_env: string[]
  resolved_via: ResolvedVia
}

export interface AcpAgentsListResponse {
  agents: AcpAgentStatus[]
}

export async function getAcpAgentsStatus(): Promise<AcpAgentsListResponse> {
  const res = await fetch(`${API_BASE}/acp/external_agents`)
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`GET /acp/external_agents failed: ${res.status} ${text}`)
  }
  return await res.json()
}

/** 取消正在运行的 ACP delegate。
 *  幂等：已结束 / 不存在的 delegate_id 也返 200，cancelled=false；
 *  真正发出 cancel 信号返 cancelled=true。 */
export async function cancelAcpDelegate(delegateId: string): Promise<{ delegate_id: string; cancelled: boolean }> {
  const res = await fetch(`${API_BASE}/acp/delegate/${encodeURIComponent(delegateId)}/cancel`, {
    method: 'POST',
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`POST /acp/delegate/${delegateId}/cancel failed: ${res.status} ${text}`)
  }
  return await res.json()
}

/** 保存"agent 借哪个 provider 的 key"的用户偏好。
 *  providerId = null / '' → 清除偏好，回退到内置默认。 */
export async function setAcpAgentProvider(agent: string, providerId: string | null): Promise<AcpAgentStatus> {
  const res = await fetch(`${API_BASE}/acp/external_agents/${encodeURIComponent(agent)}/provider`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider_id: providerId }),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`POST /acp/external_agents/${agent}/provider failed: ${res.status} ${text}`)
  }
  return await res.json()
}
