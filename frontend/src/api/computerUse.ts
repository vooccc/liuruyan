/**
 * Computer Use 控制面 API 客户端（对应 backend/computer_use/routes.py）。
 *
 * 后端路由：
 *   GET  /api/computer_use/policy     读策略 + meta（已知 actions）
 *   PUT  /api/computer_use/policy     改策略（partial）
 *   POST /api/computer_use/release    强制释放锁（"立即停止"按钮）
 *   GET  /api/computer_use/status     实时状态（lock holder + 最近 N 秒 action 数）
 *   GET  /api/computer_use/audit      读最近 N 条审计
 */
import { API_BASE } from './base'

/* ============================================================================
   类型
   ============================================================================ */

export interface ComputerUsePolicy {
  enabled: boolean
  /** null = 允许全部；非空数组 = 白名单 */
  allowed_actions: string[] | null
  blocked_actions: string[]
  max_per_second: number
  max_per_minute: number
  /**
   * M5 视觉闭环：mutating action（click/type/key/scroll/...）执行后自动再截一张，
   * 算 before/after diff 塞回 LLM 上下文。开启后 LLM 能"看到"自己点击的实际效果。
   */
  auto_screenshot_after_mutate: boolean
}

export interface ComputerUsePolicyMeta {
  /** schema 里所有已知 action 名（用于 UI 渲染勾选） */
  all_actions: string[]
  platform: string
  platform_supported: boolean
}

export interface PolicyResponse {
  policy: ComputerUsePolicy
  meta: ComputerUsePolicyMeta
}

export interface LockHolder {
  session_id: string
  pid: number
  acquired_at: number
  held_seconds: number
}

export interface ComputerUseStatus {
  platform: string
  platform_supported: boolean
  policy_enabled: boolean
  lock_holder: LockHolder | null
  activity: {
    actions_last_5s: number
    actions_last_60s: number
    last_action: AuditRecord | null
  }
}

export interface AuditRecord {
  ts: number
  session_id: string
  action: string
  payload?: Record<string, unknown>
  result?: unknown
  duration_ms?: number
  ok: boolean
  error?: string
  error_code?: string
}

/* ============================================================================
   Helpers
   ============================================================================ */

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`)
  }
  return res.json() as Promise<T>
}

/* ============================================================================
   API
   ============================================================================ */

export async function getComputerUsePolicy(): Promise<PolicyResponse> {
  const res = await fetch(`${API_BASE}/api/computer_use/policy`)
  return jsonOrThrow<PolicyResponse>(res)
}

export async function updateComputerUsePolicy(
  patch: Partial<ComputerUsePolicy>,
): Promise<{ ok: boolean; policy: ComputerUsePolicy }> {
  const res = await fetch(`${API_BASE}/api/computer_use/policy`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  return jsonOrThrow(res)
}

export async function releaseComputerUseLock(
  sessionId?: string,
): Promise<{
  ok: boolean
  released: boolean
  reason?: string
  former_holder?: { session_id: string; pid: number }
}> {
  const url = new URL(`${API_BASE}/api/computer_use/release`)
  if (sessionId) url.searchParams.set('session_id', sessionId)
  const res = await fetch(url.toString(), { method: 'POST' })
  return jsonOrThrow(res)
}

export async function getComputerUseStatus(): Promise<ComputerUseStatus> {
  const res = await fetch(`${API_BASE}/api/computer_use/status`)
  return jsonOrThrow<ComputerUseStatus>(res)
}

export async function getComputerUseAudit(
  limit = 50,
  sessionId?: string,
): Promise<{ items: AuditRecord[]; count: number }> {
  const url = new URL(`${API_BASE}/api/computer_use/audit`)
  url.searchParams.set('limit', String(limit))
  if (sessionId) url.searchParams.set('session_id', sessionId)
  const res = await fetch(url.toString())
  return jsonOrThrow(res)
}

/* ============================================================================
   M7 Replay
   ============================================================================ */

export interface ReplayStep {
  index: number
  action: string
  payload: Record<string, unknown>
  original_ts: number
  delay_before_ms: number
}

export interface ReplayPlan {
  session_id: string
  step_count: number
  skipped_count: number
  total_audit_entries: number
  earliest_ts: number | null
  latest_ts: number | null
  duration_estimate_sec: number
  steps: ReplayStep[]
}

export interface ReplayStepResult {
  index: number
  action: string
  ok: boolean
  error: string | null
  duration_ms: number
  skipped_reason: string | null
}

export interface ReplayExecution {
  results: ReplayStepResult[]
  completed: boolean
  aborted_at: number | null
  step_count: number
  executed_count: number
}

export interface ReplayResponse {
  dry_run: boolean
  plan: ReplayPlan
  execution: ReplayExecution | null
}

export interface ReplayRequest {
  session_id?: string
  last_n?: number
  max_steps?: number
  dry_run?: boolean
  speed_multiplier?: number
  abort_on_error?: boolean
  replay_session_id?: string
}

export async function postComputerUseReplay(req: ReplayRequest): Promise<ReplayResponse> {
  const res = await fetch(`${API_BASE}/api/computer_use/replay`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
  return jsonOrThrow<ReplayResponse>(res)
}
