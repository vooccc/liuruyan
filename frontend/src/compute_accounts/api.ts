/**
 * Pro 算力账号 API 客户端。
 */
import { API_BASE } from '../api/base'
import type {
  AuthUrlResponse,
  ComputeAccountItem,
  ComputeProviderOverview,
  ConnectionTestResult,
  ModelCatalogItem,
} from './types'

const PREFIX = `${API_BASE}/api/compute-accounts`

export async function getComputeOverview(): Promise<ComputeProviderOverview[]> {
  const res = await fetch(`${PREFIX}/overview`)
  if (!res.ok) {
    throw new Error(`获取算力渠道概览失败 (${res.status})`)
  }
  const data = await res.json()
  return data.channels || []
}

export async function getAuthUrl(providerId: string): Promise<AuthUrlResponse> {
  const res = await fetch(`${PREFIX}/${providerId}/auth-url`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  })
  if (!res.ok) {
    const err = await res.text()
    throw new Error(`获取授权链接失败: ${err}`)
  }
  return res.json()
}

export async function manualExchangeToken(
  providerId: string,
  params: { redirect_url?: string; code?: string; state?: string }
): Promise<any> {
  const res = await fetch(`${PREFIX}/${providerId}/manual-exchange`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) {
    const err = await res.text()
    throw new Error(`Token 兑换失败: ${err}`)
  }
  return res.json()
}

export async function listAccounts(providerId: string): Promise<ComputeAccountItem[]> {
  const res = await fetch(`${PREFIX}/${providerId}/accounts`)
  if (!res.ok) {
    throw new Error(`获取账号列表失败 (${res.status})`)
  }
  const data = await res.json()
  return data.accounts || []
}

export async function deleteAccount(providerId: string, accountId: string): Promise<boolean> {
  const res = await fetch(`${PREFIX}/${providerId}/accounts/${accountId}`, {
    method: 'DELETE',
  })
  if (!res.ok) {
    throw new Error(`删除账号失败 (${res.status})`)
  }
  const data = await res.json()
  return data.ok || false
}

export async function setPrimaryAccount(providerId: string, accountId: string): Promise<boolean> {
  const res = await fetch(`${PREFIX}/${providerId}/accounts/${accountId}/primary`, {
    method: 'POST',
  })
  if (!res.ok) return false
  const data = await res.json()
  return !!data.ok
}

export async function toggleAccount(
  providerId: string,
  accountId: string,
  enabled?: boolean
): Promise<{ ok: boolean; enabled: boolean }> {
  const res = await fetch(`${PREFIX}/${providerId}/accounts/${accountId}/toggle`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })
  if (!res.ok) throw new Error(`切换账号入池状态失败 (${res.status})`)
  return res.json()
}

export async function testConnection(providerId: string): Promise<ConnectionTestResult> {
  const res = await fetch(`${PREFIX}/${providerId}/test`, {
    method: 'POST',
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || `测试连接失败 (${res.status})`)
  }
  return res.json()
}

export async function listChannelModels(providerId: string): Promise<ModelCatalogItem[]> {
  const res = await fetch(`${PREFIX}/${providerId}/models`)
  if (!res.ok) {
    throw new Error(`获取模型列表失败 (${res.status})`)
  }
  const data = await res.json()
  return data.models || []
}

export async function getEnabledModels(providerId: string): Promise<string[] | null> {
  const res = await fetch(`${PREFIX}/${providerId}/enabled-models`)
  if (!res.ok) {
    throw new Error(`获取已配置模型列表失败 (${res.status})`)
  }
  const data = await res.json()
  return data.enabled_models ?? null
}

export async function setEnabledModels(providerId: string, modelIds: string[] | null): Promise<void> {
  const res = await fetch(`${PREFIX}/${providerId}/enabled-models`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled_models: modelIds }),
  })
  if (!res.ok) {
    throw new Error(`保存模型配置失败 (${res.status})`)
  }
}

export async function getAccountQuota(
  providerId: string,
  accountId?: string,
  forceRefresh = false
): Promise<import('./types').AccountQuotaResult> {
  const params = new URLSearchParams()
  if (accountId) params.append('account_id', accountId)
  if (forceRefresh) params.append('force_refresh', 'true')
  const queryStr = params.toString() ? `?${params.toString()}` : ''
  const res = await fetch(`${PREFIX}/${providerId}/quota${queryStr}`)
  if (!res.ok) {
    throw new Error(`获取配额失败 (${res.status})`)
  }
  return res.json()
}


