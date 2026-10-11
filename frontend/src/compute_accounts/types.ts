/**
 * 算力账号（Compute Accounts / 插件配置）TypeScript 类型定义。
 */

export type AccountStatus = 'active' | 'cooling' | 'expired'

export interface ComputeAccountItem {
  id: string
  email: string
  name?: string
  picture?: string
  status: AccountStatus
  enabled?: boolean
  created_at?: number
  has_refresh_token?: boolean
}

export interface ComputeProviderOverview {
  id: string
  display_name: string
  sub_title: string
  author: string
  status: 'configured' | 'unconfigured'
  account_count: number
  model_count: number
  icon: string
  can_add: boolean
}

export interface AuthUrlResponse {
  ok: boolean
  auth_url: string
  state: string
  redirect_uri: string
}

export interface ConnectionTestResult {
  ok: boolean
  latency_ms?: number
  email?: string
  model_count?: number
  error?: string
}

export interface ModelCatalogItem {
  id: string
  name: string
  context_window?: number
  vision?: boolean
  tools?: boolean
}

export interface QuotaBucketInfo {
  bucket_id: string
  display_name: string
  window: string // 'weekly' | '5h'
  remaining_fraction: number
  percentage: number
  reset_time: string
  reset_in: string // e.g. '3d 21h'
  reset_formatted: string // e.g. '09/30 10:25'
  description?: string
}

export interface QuotaCategoryInfo {
  five_hour?: QuotaBucketInfo
  weekly?: QuotaBucketInfo
}

export interface AccountQuotaResult {
  ok: boolean
  cached?: boolean
  fetched_at?: number
  rate_limited?: boolean
  warning?: string
  error?: string
  gemini?: QuotaCategoryInfo
  claude?: QuotaCategoryInfo
  groups?: Array<{
    display_name: string
    description?: string
    buckets: QuotaBucketInfo[]
  }>
}

