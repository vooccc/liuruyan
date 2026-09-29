/**
 * 搜索引擎设置 API 客户端（对应 backend/routes/search_routes.py）。
 *
 * 支持 Devin / Exa / Tavily 多引擎配置与调用优先级排序。
 */
import { API_BASE } from './base'

export interface EngineItemSettings {
  /** UI 配的 key（明文回显，localhost 单机应用可接受）。仅来自环境变量时为空串。 */
  api_key: string
  /** 脱敏展示串，无 key 时为空串。 */
  masked: string
  /** 任一来源（UI / 环境变量）已配置。 */
  configured: boolean
  /** key 只在环境变量 / .env 里配了、UI 没配。 */
  env_fallback: boolean
  /** 引擎启用状态。 */
  enabled: boolean
}

export interface DetectedDevinInfo {
  installed: boolean
  path: string
  ideName: string
  ideVersion: string
  extensionName: string
  extensionVersion: string
}

export interface SearchSettingsResponse {
  engines: {
    devin?: EngineItemSettings
    exa?: EngineItemSettings
    tavily: EngineItemSettings
    [key: string]: EngineItemSettings | undefined
  }
  priority_order: string[]
  detected_devin?: DetectedDevinInfo
  /** 向后兼容老字段 */
  tavily: EngineItemSettings
}

export interface SearchSettingsUpdate {
  tavily_api_key?: string
  exa_api_key?: string
  devin_api_key?: string
  enabled_engines?: Record<string, boolean>
  priority_order?: string[]
}

/** 读取当前搜索引擎配置。 */
export async function getSearchSettings(): Promise<SearchSettingsResponse> {
  const res = await fetch(`${API_BASE}/settings/search`)
  if (!res.ok) {
    throw new Error(`[${res.status}] failed to load search settings`)
  }
  return res.json()
}

/** 保存配置（partial）。后端返新的完整配置。 */
export async function updateSearchSettings(
  body: SearchSettingsUpdate,
): Promise<SearchSettingsResponse> {
  const res = await fetch(`${API_BASE}/settings/search`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const errText = await res.text().catch(() => '')
    throw new Error(`[${res.status}] update search settings failed: ${errText}`)
  }
  return res.json()
}

export interface FetchDevinTokenResponse {
  success: boolean
  token?: string
  masked?: string
  error?: string
}

/** 手动按需读取本机 Devin 已登录的 sessionToken */
export async function fetchLocalDevinToken(): Promise<FetchDevinTokenResponse> {
  const res = await fetch(`${API_BASE}/settings/search/fetch-devin-token`, {
    method: 'POST',
  })
  if (!res.ok) {
    const errText = await res.text().catch(() => '')
    throw new Error(`[${res.status}] fetch local token failed: ${errText}`)
  }
  return res.json()
}
