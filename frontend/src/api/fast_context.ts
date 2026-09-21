/**
 * Fast Context 代码探索设置 API 客户端（对应 backend/routes/fast_context_routes.py）。
 *
 * 支持：
 * - 模型选择：跟随当前会话模型 ('session') 或 指定独立模型服务商 ('custom')
 * - 自定义模型与服务商配置 (custom_provider, custom_model)
 * - 独立手动的 Devin 本机登录态获取与 Token 配置 (devin_token)
 * - 检索最大轮数配置 (max_turns)
 */
import { API_BASE } from './base'
import type { DetectedDevinInfo } from './search'

export interface FastContextSettingsResponse {
  mode: 'devin' | 'session' | 'custom'
  custom_provider: string
  custom_model: string
  devin_token: string
  masked_token: string
  configured_token: boolean
  max_turns: number
  detected_devin?: DetectedDevinInfo
}

export interface FastContextSettingsUpdate {
  mode?: 'devin' | 'session' | 'custom'
  custom_provider?: string
  custom_model?: string
  devin_token?: string
  max_turns?: number
}

export interface FetchDevinTokenResult {
  success: boolean
  token?: string
  masked?: string
  error?: string
}

/** 读取当前 Fast Context 配置。 */
export async function getFastContextSettings(): Promise<FastContextSettingsResponse> {
  const res = await fetch(`${API_BASE}/settings/fast-context`)
  if (!res.ok) {
    throw new Error(`[${res.status}] failed to load fast context settings`)
  }
  return res.json()
}

/** 保存 Fast Context 配置。 */
export async function updateFastContextSettings(
  body: FastContextSettingsUpdate,
): Promise<FastContextSettingsResponse> {
  const res = await fetch(`${API_BASE}/settings/fast-context`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const errText = await res.text().catch(() => '')
    throw new Error(`[${res.status}] failed to update fast context settings: ${errText}`)
  }
  return res.json()
}

/** 手动按需读取本机 Devin 客户端的已登录 sessionToken。 */
export async function fetchLocalDevinTokenForFastContext(): Promise<FetchDevinTokenResult> {
  const res = await fetch(`${API_BASE}/settings/fast-context/fetch-devin-token`, {
    method: 'POST',
  })
  if (!res.ok) {
    const errText = await res.text().catch(() => '')
    throw new Error(`[${res.status}] failed to fetch local devin token: ${errText}`)
  }
  return res.json()
}
