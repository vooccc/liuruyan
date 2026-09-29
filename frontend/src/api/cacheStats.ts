/**
 * Prompt cache 命中统计 API 封装。
 *
 * 对应后端 `@app.get('/stats/prompt_cache')`（backend/main.py）。
 */

import { API_BASE } from './base'

export interface PromptCacheSummarySlot {
  call_count: number
  total_input: number
  total_output: number
  total_cache_read: number
  total_cache_create: number
  hit_ratio: number
  estimated_savings_tokens: number
}

export interface PromptCacheSummary extends PromptCacheSummarySlot {
  /** key = `${agent_id}|${model}` */
  by_agent_model: Record<string, PromptCacheSummarySlot>
}

export interface PromptCacheRow {
  ts: number
  session_id: string
  agent_id: string
  model: string
  input_tokens: number
  output_tokens: number
  cache_read: number
  cache_create: number
}

export interface PromptCacheStatsResponse {
  summary: PromptCacheSummary
  rows?: PromptCacheRow[]
}

/**
 * 拉取 prompt cache 命中统计。
 *
 * @param lastN 取最近多少次 LLM 调用（后端 clamp 到 [1, 5000]，默认 1000）
 * @param includeRows 是否带上原始 rows（调试用，默认只要 summary）
 */
export async function fetchPromptCacheStats(
  lastN: number = 1000,
  includeRows: boolean = false,
): Promise<PromptCacheStatsResponse> {
  const params = new URLSearchParams()
  params.set('last_n', String(lastN))
  if (includeRows) params.set('include_rows', 'true')

  const resp = await fetch(`${API_BASE}/stats/prompt_cache?${params.toString()}`)
  if (!resp.ok) {
    throw new Error(`fetchPromptCacheStats failed: ${resp.status} ${resp.statusText}`)
  }
  return (await resp.json()) as PromptCacheStatsResponse
}
