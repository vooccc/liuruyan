/**
 * Embedding 长期记忆 API 客户端（对应 backend/routes/embedding_routes.py）。
 *
 * 后端路由：
 *   GET  /embedding/config       拉当前配置 + 可选 provider 列表 + 推荐模型映射
 *   PUT  /embedding/config       partial 更新（自动失效进程内 client/index 缓存）
 *   POST /embedding/test         临时配置测试连通性（不落盘）
 *   GET  /embedding/stats        各 bucket 索引大小统计
 *   POST /embedding/reindex      全量重算（V1：同步阻塞；V2 改 SSE 进度）
 *
 * 设计参考：./frontend\src\api\voice.ts（同协议风格）
 */
import { API_BASE } from './base'

/* ============================================================================
   类型
   ============================================================================ */

export interface EmbeddingConfig {
  enabled: boolean
  provider: string
  model: string
  base_url: string
  api_key: string
  dim: number
  timeout: number
  cache_size: number
  updated_at: number
  /** 后端返回的脱敏字段：仅指示是否已配 key，不传明文 */
  has_api_key: boolean
}

export interface EmbeddingRecommended {
  model: string
  dim: number
}

export interface EmbeddingEligibleProvider {
  id: string
  name: string
  base_url: string
  has_api_key: boolean
  is_aggregator: boolean
  /** 该 provider 推荐的 embedding 模型（如有），UI 默认勾选 */
  recommended: EmbeddingRecommended | null
}

export interface EmbeddingConfigResponse {
  config: EmbeddingConfig
  eligible_providers: EmbeddingEligibleProvider[]
  recommended_models: Record<string, EmbeddingRecommended>
}

export interface EmbeddingConfigUpdate {
  enabled?: boolean
  provider?: string
  model?: string
  base_url?: string
  api_key?: string
  dim?: number
  timeout?: number
  cache_size?: number
}

export interface EmbeddingTestRequest {
  model?: string
  base_url?: string
  api_key?: string
  dim?: number
  timeout?: number
}

export interface EmbeddingTestResult {
  ok: boolean
  /** 服务端实际返的向量维度 */
  dim: number
  /** 配置里声明的维度 */
  expected_dim: number
  elapsed_ms: number
  error: string | null
}

export interface EmbeddingBucketStats {
  scope: string
  agent_id: string | null
  dir: string
  dim: number
  provider: string
  model: string
  count: number
  updated_at: number
}

export interface EmbeddingStatsResponse {
  enabled: boolean
  current_model: string
  current_dim: number
  buckets: EmbeddingBucketStats[]
  total_vectors: number
}

export interface EmbeddingReindexRequest {
  scope: 'private' | 'shared'
  agent_id: string | null
}

export interface EmbeddingReindexResult {
  scope: string
  agent_id: string | null
  processed: number
  succeeded: number
  failed: number
  elapsed_ms: number
  kv_path: string
  bucket_dir: string
}

/* ============================================================================
   API
   ============================================================================ */

export async function getEmbeddingConfig(): Promise<EmbeddingConfigResponse> {
  const res = await fetch(`${API_BASE}/embedding/config`)
  if (!res.ok) throw new Error(`[${res.status}] failed to load embedding config`)
  return res.json()
}

export async function updateEmbeddingConfig(
  body: EmbeddingConfigUpdate,
): Promise<{ status: string; config: EmbeddingConfig }> {
  const res = await fetch(`${API_BASE}/embedding/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`[${res.status}] update embedding config failed: ${txt}`)
  }
  return res.json()
}

export async function testEmbedding(body: EmbeddingTestRequest): Promise<EmbeddingTestResult> {
  const res = await fetch(`${API_BASE}/embedding/test`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`[${res.status}] test embedding failed: ${txt}`)
  }
  return res.json()
}

export async function getEmbeddingStats(): Promise<EmbeddingStatsResponse> {
  const res = await fetch(`${API_BASE}/embedding/stats`)
  if (!res.ok) throw new Error(`[${res.status}] failed to load embedding stats`)
  return res.json()
}

export async function reindexEmbeddingBucket(
  body: EmbeddingReindexRequest,
): Promise<EmbeddingReindexResult> {
  const res = await fetch(`${API_BASE}/embedding/reindex`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`[${res.status}] reindex failed: ${txt}`)
  }
  return res.json()
}

/* ============================================================================
   Streaming reindex（NDJSON）
   ============================================================================
   每条记录后端 yield 一行 JSON，前端用 fetch + ReadableStream 逐行解析。
   用法：
       for await (const ev of reindexEmbeddingBucketStream(req, signal)) {
         if (ev.event === 'progress') updateUI(ev.processed / ev.total)
         else if (ev.event === 'done') summarize(ev)
         else if (ev.event === 'error') showError(ev.error)
       }
   传 AbortSignal 可中断（关掉浏览器 tab / 用户取消按钮）。
   ============================================================================ */

export type EmbeddingReindexStreamEvent =
  | {
      event: 'start'
      total: number
      scope: 'private' | 'shared'
      agent_id: string | null
      kv_path: string
      bucket_dir: string
    }
  | {
      event: 'progress'
      processed: number
      succeeded: number
      failed: number
      current_id: string
      current_title: string
      ok: boolean
      error: string | null
    }
  | {
      event: 'done'
      processed: number
      succeeded: number
      failed: number
      elapsed_ms: number
    }
  | {
      event: 'error'
      stage: string
      error: string
    }

/** NDJSON 流式 reindex async generator。失败 / 中断都会让 generator 自然结束。 */
export async function* reindexEmbeddingBucketStream(
  body: EmbeddingReindexRequest,
  signal?: AbortSignal,
): AsyncGenerator<EmbeddingReindexStreamEvent> {
  const res = await fetch(`${API_BASE}/embedding/reindex/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok || !res.body) {
    const txt = await res.text().catch(() => '')
    throw new Error(`[${res.status}] reindex stream failed: ${txt || 'no body'}`)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // 按 \n 切行；最后一段不带 \n 留在 buffer 等下次 chunk 拼接
      let idx: number
      while ((idx = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, idx).trim()
        buffer = buffer.slice(idx + 1)
        if (!line) continue
        try {
          yield JSON.parse(line) as EmbeddingReindexStreamEvent
        } catch {
          // 单行 JSON 解析失败不终止整个流；下一行可能正常
          // eslint-disable-next-line no-console
          console.warn('[embedding] failed to parse NDJSON line:', line)
        }
      }
    }
    // flush 收尾：最后一行可能没 \n 结尾
    const tail = buffer.trim()
    if (tail) {
      try {
        yield JSON.parse(tail) as EmbeddingReindexStreamEvent
      } catch {
        // 收尾解析失败忽略
      }
    }
  } finally {
    try {
      reader.releaseLock()
    } catch {
      /* ignore */
    }
  }
}
