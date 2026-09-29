/**
 * Per-session SSE 长连接消费者。
 *
 * 职责（按事件类型分发）：
 * - session_token_usage → store.sessions[sid].tokenUsage
 * - backend_modified → 全局 backend toast
 * - message_added → 调用方刷新当前 session 消息
 *
 * 复用同一条 EventSource 长连接（per-session 一条，符合多并发硬约束）。
 */

import { useEffect } from 'react'
import { useAppStore, type BackendToast, type TokenUsageState } from '../stores/useAppStore'
import { API_BASE } from '../api/base'

// EventSource 默认事件 type 是 'message'；sse_manager 用 encode_sse_event 给每个
// 事件设 `event: <type>` 行，浏览器会按 type 路由到 addEventListener。
// 所以要为每种已知 type 注册 handler。
const KNOWN_EVENT_TYPES = [
  'message',
  'replay_control',
  'system_notice',
  'agent_tick_heartbeat',
  'agent_tick_speak',
  // Token 累计消耗（每次 LLM 调用累加后由 SessionManager.record_token_usage 发出）
  'session_token_usage',
  // 后端文件被角色写入（自进化护网 D：toast 通知 + diff 摘要）
  'backend_modified',
  'message_added',
  // 会话标题自动生成完成
  'session_title_updated',
]

export interface ParsedEvent {
  type: string
  kind?: string
  refs?: {
    route_id?: string
    worker_id?: string
    plan_id?: string
    plan_step_id?: string
    plan_stage?: number
  }
  payload?: Record<string, unknown>
  event_id?: number
  raw: Record<string, unknown>
}

function pickTopLevelRefs(obj: Record<string, unknown>): ParsedEvent['refs'] | undefined {
  // 裸事件（thinking / tool_call / tool_result）refs 在顶层而非 payload.refs，
  // 由 backend runtime_protocol.build_runtime_transport_event 注入。
  const wid = obj.worker_id
  const rid = obj.route_id
  const pid = obj.plan_id
  if (typeof wid !== 'string' && typeof rid !== 'string' && typeof pid !== 'string') return undefined
  return {
    worker_id: typeof wid === 'string' ? wid : undefined,
    route_id: typeof rid === 'string' ? rid : undefined,
    plan_id: typeof pid === 'string' ? pid : undefined,
    plan_step_id: typeof obj.plan_step_id === 'string' ? obj.plan_step_id : undefined,
    plan_stage: typeof obj.plan_stage === 'number' ? obj.plan_stage : undefined,
  }
}

export function parseSseFrame(data: string): ParsedEvent | null {
  try {
    const parsed = JSON.parse(data)
    if (!parsed || typeof parsed !== 'object') return null
    const obj = parsed as Record<string, unknown>
    // refs 提取顺序：obj.refs（少见）→ payload.refs（system_notice 标准位）
    // → 顶层各字段（裸事件标准位）
    const explicitRefs = (obj.refs || (obj.payload as Record<string, unknown>)?.refs) as ParsedEvent['refs'] | undefined
    const refs = explicitRefs && typeof explicitRefs === 'object' ? explicitRefs : pickTopLevelRefs(obj)
    return {
      type: String(obj.type || ''),
      kind: typeof obj.kind === 'string' ? obj.kind : undefined,
      refs,
      payload: typeof obj.payload === 'object' && obj.payload !== null ? (obj.payload as Record<string, unknown>) : undefined,
      event_id: typeof obj.event_id === 'number' ? obj.event_id : undefined,
      raw: obj,
    }
  } catch {
    return null
  }
}

export function nextReplayCursor(
  currentCursor: number,
  event: ParsedEvent,
): { accept: boolean; cursor: number; fullSnapshot: boolean } {
  if (event.type === 'replay_control') {
    if (event.raw.resume_mode !== 'full_snapshot') {
      return { accept: false, cursor: currentCursor, fullSnapshot: false }
    }
    const latestEventId = Number(event.raw.latest_event_id)
    return {
      accept: false,
      cursor: Number.isFinite(latestEventId) ? latestEventId : 0,
      fullSnapshot: true,
    }
  }
  if (event.event_id === undefined) {
    return { accept: true, cursor: currentCursor, fullSnapshot: false }
  }
  if (event.event_id <= currentCursor) {
    return { accept: false, cursor: currentCursor, fullSnapshot: false }
  }
  return { accept: true, cursor: event.event_id, fullSnapshot: false }
}

/**
 * 处理 session_token_usage 事件：payload 是后端累加后的总值，前端覆盖式写入
 * （切勿做累加，否则断线续传会重复加）。
 */
export function reduceTokenUsageEvent(sid: string, event: ParsedEvent) {
  if (event.type !== 'session_token_usage') return
  const p = (event.payload || {}) as Record<string, unknown>
  // 上下文构成（估算）：后端 build_messages 生成，随 usage 事件同步到达。
  // 缺失（旧后端 / 会话首轮前）保持 undefined，徽章只渲染账单区。
  const cb = p.context_breakdown as Record<string, unknown> | null | undefined
  const usage: TokenUsageState = {
    input: Number(p.input) || 0,
    output: Number(p.output) || 0,
    cacheRead: Number(p.cache_read) || 0,
    cacheCreate: Number(p.cache_create) || 0,
    total: Number(p.total) || 0,
    calls: Number(p.calls) || 0,
    lastCallAt: typeof p.last_call_at === 'string' ? p.last_call_at : undefined,
    contextBreakdown: cb
      ? {
          system: Number(cb.system) || 0,
          extra: Number(cb.extra) || 0,
          messages: Number(cb.messages) || 0,
          tools: Number(cb.tools) || 0,
          total: Number(cb.total) || 0,
          model: typeof cb.model === 'string' && cb.model ? cb.model : undefined,
          contextLength: Number(cb.context_length) || undefined,
          compactThreshold: Number(cb.compact_threshold) || undefined,
        }
      : undefined,
  }
  useAppStore.getState().setSessionTokenUsage(sid, usage)
}

/**
 * backend_modified：角色写改了 backend 文件（write_file/patch/edit_file/notebook_edit）。
 *
 * 字段在事件根（不在 payload 里）——backup.emit_backend_modified_sse 直接把字典塞 send_event。
 * Toast 全局队列（不挂 session），UI 层从 store.backendToasts 渲染。
 */
export function reduceBackendModifiedEvent(parsed: ParsedEvent) {
  const r = parsed.raw
  const path = typeof r.path === 'string' ? r.path : ''
  if (!path) return
  const toast: BackendToast = {
    id: `bw_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    ts: Date.now(),
    agentId: typeof r.agent_id === 'string' ? r.agent_id : undefined,
    toolName: typeof r.tool_name === 'string' ? r.tool_name : 'write',
    path,
    filename: typeof r.filename === 'string' ? r.filename : path.split(/[\\/]/).pop() || path,
    charsBefore: Number(r.chars_before) || 0,
    charsAfter: Number(r.chars_after) || 0,
    linesBefore: Number(r.lines_before) || 0,
    linesAfter: Number(r.lines_after) || 0,
    created: Boolean(r.created),
    backupPath: typeof r.backup_path === 'string' ? r.backup_path : undefined,
  }
  useAppStore.getState().pushBackendToast(toast)
}

/**
 * session_title_updated：会话标题自动生成完成。
 * 派发全局 window 事件供 Sidebar 及相关面板即时更新标题状态。
 */
export function reduceSessionTitleUpdatedEvent(sid: string, event: ParsedEvent) {
  const p = (event.payload || {}) as Record<string, unknown>
  const title = (p.title as string) || (event.raw?.title as string) || ''
  const sessionId = (p.session_id as string) || (event.raw?.session_id as string) || sid
  if (!title || !sessionId) return
  window.dispatchEvent(
    new CustomEvent('session_title_updated', {
      detail: { session_id: sessionId, title },
    }),
  )
}

/**
 * 为 sid 建立 SSE 长连接 + 增量维护 session 事件状态。
 *
 * sid=null/empty 时不建连接；sid 切换时旧连接关闭、新连接开启。
 *
 * 断线续传：cursor（last_event_id）从 useAppStore.sessions[sid].lastEventId 读 / 写。
 * 首次连接 cursor 默认 0（对齐 sse_manager 契约：last_event_id=0 → 回放
 * 整个 ring buffer），URL 始终带 `?last_event_id=N`——浏览器原生 EventSource
 * 不支持自定义 header，主动 `new EventSource` 没法走 Last-Event-ID header，
 * 必须靠 query param 让 backend 找到游标。
 *
 * P1.7（架构重构 2026-05-23）：原本用 `useRef<Map<sid, cursor>>` 持有 cursor。
 * 但 store 早就有 `sessions[sid].lastEventId` 字段（注释明写"用于断线续传"）+ 配套
 * action `setSessionLastEventId`——是重复造轮子。迁到 store 后跟 Chat lifecycle 解耦，
 * 即使 Chat unmount/remount 也能从原游标续传，不会重放 backend ring buffer。
 */
export function useDispatchEventConsumer(
  sid: string | null | undefined,
  options: {
    onMessageAdded?: (sid: string, event: ParsedEvent) => void
    onFullSnapshot?: (sid: string, event: ParsedEvent) => void | Promise<void>
  } = {},
) {
  useEffect(() => {
    const targetSid = (sid || '').trim()
    if (!targetSid) return

    let eventSource: EventSource | null = null
    let cancelled = false

    const open = () => {
      if (cancelled) return
      // P1.7：从 store 读 lastEventId 作为 cursor 起点。
      const cursor = useAppStore.getState().sessions[targetSid]?.lastEventId ?? 0
      // 用 API_BASE 拼绝对 URL：dev 下后端是 127.0.0.1:8001，相对路径会
      // 走到 vite dev server (1420) 拿 404。所有前端 API 都是这个套路。
      const url = `${API_BASE}/sessions/${encodeURIComponent(targetSid)}/events?last_event_id=${cursor}`
      eventSource = new EventSource(url)
      const handler = async (e: MessageEvent) => {
        const parsed = parseSseFrame(e.data)
        if (!parsed) return
        const currentCursor = useAppStore.getState().sessions[targetSid]?.lastEventId ?? 0
        const cursorDecision = nextReplayCursor(currentCursor, parsed)
        if (cursorDecision.fullSnapshot) {
          try {
            await options.onFullSnapshot?.(targetSid, parsed)
            useAppStore.getState().resetSessionLastEventId(targetSid, cursorDecision.cursor)
          } catch (err) {
            console.warn('[SessionSSE] full snapshot recovery failed', err)
          }
        } else if (cursorDecision.cursor !== currentCursor) {
          // P1.7：游标写回 store（持久化跨 Chat unmount）。
          useAppStore.getState().setSessionLastEventId(targetSid, cursorDecision.cursor)
        }
        if (!cursorDecision.accept) return
        try {
          // 按事件 type 分发（一条连接、多种 reducer）
          if (parsed.type === 'session_token_usage') {
            reduceTokenUsageEvent(targetSid, parsed)
          } else if (parsed.type === 'message_added') {
            options.onMessageAdded?.(targetSid, parsed)
          } else if (parsed.type === 'backend_modified') {
            reduceBackendModifiedEvent(parsed)
          } else if (parsed.type === 'session_title_updated') {
            reduceSessionTitleUpdatedEvent(targetSid, parsed)
          }
        } catch (err) {
          console.warn('[SessionSSE] reduce error', err)
        }
      }
      KNOWN_EVENT_TYPES.forEach((t) => eventSource!.addEventListener(t, handler))
      eventSource.onerror = () => {
        // EventSource 浏览器原生有自动重连；这里不主动 close（除非 cancelled）
        if (cancelled && eventSource) {
          eventSource.close()
        }
      }
    }

    open()

    return () => {
      cancelled = true
      if (eventSource) {
        eventSource.close()
        eventSource = null
      }
    }
  }, [sid, options.onFullSnapshot, options.onMessageAdded])
}
