/**
 * `.lry/` 工作区只读 API 客户端（对应 backend/routes/lry_routes.py）。
 *
 * 9 个 GET 端点：
 *   /api/lry/status                 工作区总览
 *   /api/lry/rules                  rules 元数据列表
 *   /api/lry/rules/{slug}           单个 rule 完整内容
 *   /api/lry/workflows              workflows 元数据列表
 *   /api/lry/workflows/{name}       单个 workflow 完整内容
 *   /api/lry/notes                  PROJECT_NOTES.md 全文
 *   /api/lry/codemap                CODE_MAP.md 全文
 *   /api/lry/memory                 memory 元数据列表（mtime 倒序）
 *   /api/lry/memory/{filename}      单个 memory 完整内容
 *
 * 设计文档：_docs/lry_workspace_design.md
 */
import { API_BASE } from './base'

/* ============================================================================
   类型
   ============================================================================ */

export interface LryFileMeta {
  size: number
  mtime: number
  mtime_iso: string
}

export interface LryStatus {
  exists: boolean
  workspace_root: string | null
  lry_root?: string
  counts?: {
    rules: number
    workflows: number
    memory: number
    /** .lry/private/memory/ 下的记忆条数（Phase 7）。不存在时字段可能缺省。 */
    private_memory?: number
  }
  has_notes?: boolean
  has_codemap?: boolean
  has_readme?: boolean
  /** .lry/.gitignore 是否存在（Phase 7：首次写 private memory 时自动创建） */
  has_gitignore?: boolean
  message?: string
}

export interface LryRuleListItem extends LryFileMeta {
  slug: string
  severity: string
  topic: string
  added?: string
  by?: string
}

export interface LryRuleDetail extends LryFileMeta {
  slug: string
  path: string
  content: string
}

export interface LryWorkflowListItem extends LryFileMeta {
  name: string
  description?: string
  added?: string
  by?: string
}

export interface LryWorkflowDetail extends LryFileMeta {
  name: string
  path: string
  content: string
}

export interface LryNotesResponse extends Partial<LryFileMeta> {
  exists: boolean
  path: string
  content: string
}

export interface LryCodemapResponse extends Partial<LryFileMeta> {
  exists: boolean
  path: string
  content: string
}

export interface LryMemoryListItem extends LryFileMeta {
  filename: string
  kind?: string
  topic?: string
  added?: string
  by?: string
  /** Phase 7：来自 .lry/private/memory/ 的条目必然带此 flag；前端据此加 Lock chip 等标识 */
  private?: boolean
}

export interface LryMemoryDetail extends LryFileMeta {
  filename: string
  path: string
  content: string
  frontmatter: Record<string, string>
  /** Phase 7：从 .lry/private/memory/ 读到时后端带此 flag */
  private?: boolean
}

/* ============================================================================
   通用 fetch helper
   ============================================================================ */

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const err = await res.json()
      if (typeof err.detail === 'string') detail = err.detail
    } catch {
      /* 非 JSON body，保留 HTTP 状态 */
    }
    throw new Error(`[${res.status}] ${path}: ${detail}`)
  }
  return res.json()
}

/* ============================================================================
   API
   ============================================================================ */

export const getLryStatus = (): Promise<LryStatus> => getJson('/api/lry/status')

export const listLryRules = (): Promise<{ items: LryRuleListItem[]; total: number }> =>
  getJson('/api/lry/rules')

export const getLryRule = (slug: string): Promise<LryRuleDetail> =>
  getJson(`/api/lry/rules/${encodeURIComponent(slug)}`)

export const listLryWorkflows = (): Promise<{ items: LryWorkflowListItem[]; total: number }> =>
  getJson('/api/lry/workflows')

export const getLryWorkflow = (name: string): Promise<LryWorkflowDetail> =>
  getJson(`/api/lry/workflows/${encodeURIComponent(name)}`)

export const getLryNotes = (): Promise<LryNotesResponse> => getJson('/api/lry/notes')

export const getLryCodemap = (): Promise<LryCodemapResponse> => getJson('/api/lry/codemap')

/**
 * 列 memory 元数据。
 *
 * `includePrivate=true` 会让后端合并 `.lry/private/memory/` 条目；未指定或 false 时
 * 默认只列公开目录，避免前端误展示敏感内容。
 */
export const listLryMemory = (
  options?: { includePrivate?: boolean },
): Promise<{ items: LryMemoryListItem[]; total: number }> =>
  getJson(
    options?.includePrivate
      ? '/api/lry/memory?include_private=true'
      : '/api/lry/memory',
  )

/**
 * 读单个 memory 文件。`isPrivate=true` 时从 `.lry/private/memory/` 读，路径校验
 * 仍在后端做。
 */
export const getLryMemoryFile = (
  filename: string,
  options?: { isPrivate?: boolean },
): Promise<LryMemoryDetail> =>
  getJson(
    options?.isPrivate
      ? `/api/lry/memory/${encodeURIComponent(filename)}?private=true`
      : `/api/lry/memory/${encodeURIComponent(filename)}`,
  )

/* ============================================================================
   工具：格式化文件大小 / 相对时间
   ============================================================================ */

export function formatBytes(n: number): string {
  if (n < 1024) return `${n}B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`
  return `${(n / 1024 / 1024).toFixed(1)}MB`
}

export function formatRelativeTime(epochSeconds: number): string {
  const now = Date.now() / 1000
  const delta = now - epochSeconds
  if (delta < 60) return '刚刚'
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`
  if (delta < 86400 * 30) return `${Math.floor(delta / 86400)} 天前`
  return new Date(epochSeconds * 1000).toLocaleDateString()
}
