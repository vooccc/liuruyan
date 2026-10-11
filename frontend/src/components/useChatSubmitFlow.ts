import { useCallback, useRef, useState, type Dispatch, type FormEvent, type MutableRefObject, type SetStateAction } from 'react'
import type { AskStatesByAskId, AskStateEntry } from './runtimeBlocks'
import type { AskUserAnswer } from './AskUserQuestionCard'
import {
  createSession,
  getSession,
  getRuntimeNoticeRefs,
  sendMessage,
  submitAskAnswer,
  type Agent,
  type Message as MessageType,
  type RuntimeNoticeRefs,
  type Session,
  type RuntimeTaskDetail,
  type RuntimePlan,
  type RuntimePlanStep,
  type RuntimeTask,
  type RuntimeTraceEvent,
} from '../api/chat'
import type { StreamEvent } from '../api/chat'
import type { ChatStreamLikeEvent } from './chatExternalHandle'
import type { ChatInputMode } from './chatInputModes'
import { buildSessionMessages } from './chatSessionMessages'
import {
  CHAT_SLASH_COMMANDS,
  CHAT_UI_TEXT,
  formatChatRequestErrorMessage,
  formatChatSessionTitle,
} from './chatUiText'
import type {
  SchedulerPanoramaStageSnapshot,
  SchedulerPanoramaState,
  SchedulerPanoramaWorkerSnapshot,
} from './schedulerPanorama'
import { readSessionChatMessages, writeSessionChatMessages, readSessionMessageMeta } from './useChatSessionRuntime'
import { registerStream, abortStream, removeStream, hasInFlightStream } from '../lib/streamRegistry'
import {
  getPlanCacheEntry,
  getWorkerCacheEntry,
  setWorkerCacheEntry,
} from '../lib/schedulerPanoramaCache'
import {
  setTimer,
  clearTimer,
  TIMER_NS_ROUTED_POLLER,
  TIMER_NS_ROUTED_PROGRESS_CLEAR,
} from '../lib/timerRegistry'
import { useAppStore } from '../stores/useAppStore'
import { extractSpeakableText } from '../api/tts'
import {
  feedTtsStreaming,
  flushTtsStreaming,
  resetTtsStreaming,
  ensureTtsStreamingConfig,
} from './ttsStreamingSpeaker'
import { getRuntime } from '../runtime'
import { buildIdeContextBlock } from '../utils/ideContext'
import { tryInjectLryWorkflow } from '../utils/lryWorkflowContext'
import { buildUserMessageWithAttachments } from './userAttachmentFooter'
import { finalizeLastThinkingSegment } from './chatStreamMessage'

/**
 * 安全生成 v4 UUID。
 *
 * 背景：crypto.randomUUID() 需要 secure context（HTTPS 或 localhost/127.0.0.1）。
 * 用户访问 localhost 时 OK，但跳上手机/平板走 Tailscale tailnet 以 HTTP 连后端
 * 时，非 Secure Context → crypto.randomUUID === undefined → TypeError。
 *
 * 这里随机 id 只是用来作 client_id 配对用 / assistant 消息，不需要密码学强度，
 * Math.random + RFC 4122 v4 位型填充完全够用（碰撞概率远低于业务量）。
 */
function safeRandomUUID(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  // Fallback: 遵守 RFC 4122 v4 的 8-4-4-4-12 hex 布局 + version=4 / variant=10xx
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16)
  })
}

interface UseChatSubmitFlowParams {
  input: string
  isLoading: boolean
  inputMode: ChatInputMode
  currentSession?: Session | null
  currentAgentId?: string | null
  currentAgent?: Agent | null
  selectedModel?: string | null
  /** 与 selectedModel 配对的 provider id（仅 override 时有值），让后端按
   *  (model, provider) 严格匹配，避免同名 model 跨多 provider 时静默走错家。 */
  submitProvider?: string | null
  inputRef: MutableRefObject<HTMLTextAreaElement | null>
  userScrolledUpRef: MutableRefObject<boolean>
  sessionIdRef: MutableRefObject<string>
  /**
   * P1.3（架构重构 2026-05-23）：sessionAbortMapRef 已退场，迁到 module-level
   * streamRegistry（`src/lib/streamRegistry.ts`）。 callsite 调 registerStream / abortStream /
   * removeStream，跨 Chat unmount 持久。设计参考标准中间件设计
   */
  isSendingRef: MutableRefObject<Record<string, boolean>>
  setInput: Dispatch<SetStateAction<string>>
  setShowCommands: Dispatch<SetStateAction<boolean>>
  setIsLoading: Dispatch<SetStateAction<boolean>>
  setExpandedThinking: Dispatch<SetStateAction<Set<string>>>
  setSessionLoading: (sid: string, loading: boolean) => void
  syncTimelineItems: (sid: string) => void
  applyStreamEventToMessage: (message: MessageType, event: ChatStreamLikeEvent) => MessageType
  onSessionCreated?: (session: Session) => void
  /** 用户从 📎 上传的附件路径 ref（仅包含已上传成功的绝对路径）。
   *  ref 模式：避免 attachments state 变动触发 handleSubmit useCallback 重建。 */
  attachmentsRef?: MutableRefObject<string[]>
  /** 发送被防重复/防抖分支拦下时告知用户。
   *  没有它这些分支只 console.warn，用户看到的是「点了发送什么都没发生」。 */
  onNotice?: (title: string, desc?: string) => void
  /** Goal 模式状态更新回调（当流中接收到 goal_state_update 时实时触发） */
  onGoalUpdate?: (goal: any) => void
  /** 目标状态刷新回调（会话流结束时触发同步） */
  onRefreshGoal?: () => void
  /** v6「修改并重新提问」延迟截断钩子：点消息上的修改按钮只做前端标记，
   *  真正的截断（删该消息之后所有对话）推迟到这里——即所有防抖 / 发送锁 /
   *  空文本 / slash 模板等 bail 分支都已通过、确定要真发这条消息的那一刻。
   *  抛错 → bail（文字送回输入框 + 释放锁），标记保留以便重试。 */
  onBeforeSend?: () => Promise<void>
}

const REQUEST_DEBOUNCE_MS = 800
/** 发送锁被认定为陈旧的时限。
 *  锁在 handleSubmit 顶部获取，registerStream 要等 createSession / IDE 上下文
 *  这些 await 之后才发生，所以「持锁但 streamRegistry 里没有流」在正常情况下
 *  只会持续一瞬；超过这个时限还是这个状态，说明上一轮在 try/finally 之前就异常
 *  退出了，锁不会再有人释放。 */
const SEND_LOCK_STALE_MS = 15000
const ROUTED_PROGRESS_FINAL_HOLD_MS = 1800
const ROUTE_MAX_STEPS = 6
const ACTIVE_WORKER_STATUSES = new Set<RuntimeTask['status']>(['pending', 'queued', 'running'])
const ACTIVE_PLAN_STATUSES = new Set<RuntimePlan['status']>(['pending', 'queued', 'running'])
const ROUTED_SCHEDULER_SUPPRESSED_NOTICE_KINDS = new Set([
  'task_routed',
  'worker_started',
  'worker_retry',
  'worker_waiting_dependencies',
  'worker_dependency_ready',
  'worker_heartbeat',
  'worker_tool_progress',
  'worker_tool_result_progress',
  'worker_result',
  'worker_failed',
  'worker_cancelled',
  'worker_plan_submitted',
  'worker_plan_completed',
  'worker_plan_failed',
])

function countPersistedMessages(messages: MessageType[]) {
  return messages.filter((message) => !message.ephemeral).length
}

/** cache 条目是否已与后端 transcript 对账（buildSessionMessages 合并时回填的
 *  message_seq）。乐观插入的条目和幽灵条目都没有。 */
function hasReconciledMessageSeq(message: Pick<MessageType, 'message_seq'>): boolean {
  return Number.isInteger(message.message_seq) && Number(message.message_seq) > 0
}

function buildRoutedExecutionSummary(
  workers: RuntimeTask[],
  plans: RuntimePlan[],
) {
  const activeWorkers = workers.filter((worker) => ['pending', 'queued', 'running'].includes(worker.status))
  const runningWorkers = activeWorkers.filter((worker) => worker.status === 'running')
  const queuedWorkers = activeWorkers.filter((worker) => worker.status === 'queued' || worker.status === 'pending')
  const activePlans = plans.filter((plan) => ['pending', 'queued', 'running'].includes(plan.status))

  if (activeWorkers.length === 0 && activePlans.length === 0) return null

  const leadWorker = runningWorkers[0] || queuedWorkers[0] || null
  const leadPlan = activePlans[0] || null
  const isRunning = runningWorkers.length > 0
  const headline = [
    isRunning ? '后台任务执行中' : '后台任务排队中',
    `运行中 ${runningWorkers.length}`,
    `排队 ${queuedWorkers.length}`,
    `计划 ${activePlans.length}`
  ].join(' · ')

  let detail = '等待可用的执行槽位'
  if (leadWorker?.plan_step_id) {
    const stageLabel = typeof leadWorker.plan_stage === 'number'
      ? `Stage ${leadWorker.plan_stage + 1}`
      : 'Plan Step'
    detail = `${stageLabel} · ${leadWorker.description}`
  } else if (leadWorker?.description) {
    detail = leadWorker.description
  } else if (leadPlan?.title) {
    detail = `Plan ${leadPlan.title} 正在推进`
  } else if (leadPlan?.plan_id) {
    detail = `Plan ${leadPlan.plan_id} 正在推进`
  }

  return {
    content: `${headline}\n${detail}`,
    executionStatus: isRunning ? 'running' : 'queued',
    refs: {
      worker_id: leadWorker?.task_id || undefined,
      plan_id: leadWorker?.plan_id || leadPlan?.plan_id || undefined,
      plan_step_id: leadWorker?.plan_step_id || undefined,
      plan_stage: typeof leadWorker?.plan_stage === 'number' ? leadWorker.plan_stage : undefined,
    },
  }
}

// P1.4c-3（架构重构 2026-05-23）：SchedulerPanorama{Worker,Plan}CacheEntry 类型已抖到
// `src/lib/schedulerPanoramaCache.ts`（跟这两个 cache 可跨组件复用，不再是 file-local）。
// 原 file-local interface 已删除；an上面 panoramaCache lib 的 export。

function truncatePanoramaText(value?: string | null, maxLength: number = 120) {
  const normalized = String(value || '').replace(/\s+/g, ' ').trim()
  if (!normalized) return ''
  if (normalized.length <= maxLength) return normalized
  return `${normalized.slice(0, maxLength)}...`
}

function getWorkerUpdateSignature(worker: RuntimeTask) {
  return [
    worker.status,
    worker.updated_at || '',
    worker.trace_event_count || 0,
    worker.notice_event_count || 0,
    worker.result || '',
    worker.error || '',
  ].join('|')
}

function getRuntimeNoticeTitle(kind?: string) {
  switch (kind) {
    case 'llm_upstream_retry': return '模型繁忙，自动重试中'
    case 'llm_provider_error': return '模型服务商错误'
    case 'llm_quota_exhausted': return '账户余额不足'
    case 'llm_auth_failed': return 'API Key 无效'
    case 'llm_rate_limited': return '请求频率超限'
    case 'llm_timeout': return '模型请求超时'
    case 'llm_context_overflow': return '上下文超出窗口'
    case 'llm_schema_error': return '工具定义错误'
    case 'llm_tool_unsupported': return '模型不支持工具'
    case 'llm_upstream_busy': return '模型服务繁忙'
    case 'llm_exception': return '模型调用出错'
    case 'max_tool_rounds': return '达到工具轮次上限'
    case 'repeated_identical_rounds': return '检测到重复回合'
    case 'aggregate_budget_warning': return '运行预算接近上限'
    case 'aggregate_tool_calls_exhausted': return '工具调用预算已耗尽'
    case 'aggregate_tool_output_exhausted': return '工具输出预算已耗尽'
    case 'aggregate_wall_clock_exhausted': return '运行时间预算已耗尽'
    default: return '系统通知'
  }
}

function normalizeRuntimeStatusText(content?: string | null) {
  const normalized = String(content || '').trim()
  if (!normalized) return ''
  const waitingDependencyMatch = normalized.match(/^Waiting on dependencies:\s*(.+?)\.?$/i)
  if (waitingDependencyMatch) return `等待依赖完成：${waitingDependencyMatch[1]}。`
  return normalized
}

function formatTraceStatusMessage(event: RuntimeTraceEvent) {
  if (event.type !== 'worker_status') return ''
  return normalizeRuntimeStatusText(event.message || event.status || '')
}

function getWorkerToolCounters(events: RuntimeTraceEvent[]) {
  return events.reduce((acc, event) => {
    if (event.type === 'tool_call') acc.toolCalls += 1
    if (event.type === 'tool_result') acc.toolResults += 1
    if (event.type === 'thinking') acc.thinkingCount += 1
    return acc
  }, {
    toolCalls: 0,
    toolResults: 0,
    thinkingCount: 0,
  })
}

function extractWorkerWaitingOn(events: RuntimeTraceEvent[]) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event.type !== 'worker_status' || event.status !== 'dependency_waiting') continue
    const payload = event.payload && typeof event.payload === 'object'
      ? event.payload as Record<string, unknown>
      : {}
    const waitingOn = payload.waiting_on
    if (!Array.isArray(waitingOn)) return []
    return waitingOn.map((item) => String(item || '').trim()).filter(Boolean)
  }
  return []
}

function hasWorkerInjectedToParent(events: RuntimeTraceEvent[]) {
  return events.some((event) => (
    event.type === 'worker_status' && event.status === 'injected_to_parent'
  ))
}

function extractLatestWorkerActivity(worker: RuntimeTask, events: RuntimeTraceEvent[]) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event.type === 'tool_result') {
      return `完成工具 ${event.name || '调用'}`
    }
    if (event.type === 'tool_call') {
      return `调用工具 ${event.name || '工具'}`
    }
    if (event.type === 'thinking') {
      return truncatePanoramaText(event.text || '正在思考下一步', 84)
    }
    if (event.type === 'worker_status') {
      const message = formatTraceStatusMessage(event)
      if (message) return truncatePanoramaText(message, 84)
    }
    if (event.type === 'system_notice') {
      return truncatePanoramaText(event.content || getRuntimeNoticeTitle(event.kind), 84)
    }
    if (event.type === 'content' && event.text) {
      return truncatePanoramaText(`生成结果：${event.text}`, 84)
    }
  }

  if (worker.status === 'running') return '任务正在执行中'
  if (worker.status === 'queued' || worker.status === 'pending') return '等待调度或依赖就绪'
  if (worker.status === 'completed') return '任务已完成'
  if (worker.status === 'failed') return truncatePanoramaText(worker.error || '执行失败', 84)
  if (worker.status === 'cancelled') return '任务已取消'
  return ''
}

function buildWorkerPanoramaSnapshot(
  worker: RuntimeTask,
  detail?: RuntimeTaskDetail | null,
): SchedulerPanoramaWorkerSnapshot {
  const events = detail?.trace_events || []
  const counters = getWorkerToolCounters(events)
  const waitingOn = extractWorkerWaitingOn(events)

  return {
    workerId: worker.task_id,
    stepId: worker.plan_step_id || null,
    description: worker.description,
    status: worker.status,
    stage: typeof worker.plan_stage === 'number' ? worker.plan_stage : null,
    dependsOn: worker.depends_on || [],
    waitingOn,
    toolCalls: counters.toolCalls,
    toolResults: counters.toolResults,
    thinkingCount: counters.thinkingCount,
    injected: hasWorkerInjectedToParent(events),
    latestActivity: extractLatestWorkerActivity(worker, events),
    resultPreview: truncatePanoramaText(worker.result || worker.error || '', 96) || null,
    updatedAt: worker.updated_at || null,
  }
}

function buildPlanStepPanoramaSnapshot(
  step: RuntimePlanStep,
  finalMerged: boolean,
  planStatus?: RuntimePlan['status'],
): SchedulerPanoramaWorkerSnapshot {
  let status = step.status || 'pending'
  if (
    (finalMerged || planStatus === 'completed')
    && (status === 'pending' || status === 'queued' || status === 'running')
  ) {
    status = 'completed'
  }

  return {
    workerId: step.worker_id,
    stepId: step.step_id,
    description: step.description,
    status,
    stage: typeof step.stage === 'number' ? step.stage : null,
    dependsOn: step.depends_on || [],
    waitingOn: [],
    toolCalls: 0,
    toolResults: 0,
    thinkingCount: 0,
    injected: false,
    latestActivity: status === 'completed' ? '步骤已完成' : '',
    resultPreview: truncatePanoramaText(step.result || step.error || '', 96) || null,
    updatedAt: step.updated_at || null,
  }
}

function sortByUpdatedAtDesc<T extends { updated_at?: string | null; updatedAt?: string | null }>(items: T[]) {
  return [...items].sort((left, right) => {
    const leftTime = new Date(left.updated_at || left.updatedAt || 0).getTime()
    const rightTime = new Date(right.updated_at || right.updatedAt || 0).getTime()
    return rightTime - leftTime
  })
}

function computePanoramaExecutionStatus(workers: SchedulerPanoramaWorkerSnapshot[]) {
  if (workers.some((worker) => worker.status === 'failed')) return 'failed'
  if (workers.some((worker) => worker.status === 'running')) return 'running'
  if (workers.some((worker) => worker.status === 'queued' || worker.status === 'pending')) return 'queued'
  if (workers.some((worker) => worker.status === 'cancelled')) return 'cancelled'
  if (workers.length > 0 && workers.every((worker) => worker.status === 'completed')) return 'completed'
  return 'queued'
}

function buildPanoramaSummary(
  executionStatus: string,
  totals: SchedulerPanoramaState['totals'],
  awaitingFinalMerge: boolean,
) {
  if (awaitingFinalMerge) {
    return `各步骤已完成，结果正在回流到主对话，已注入 ${totals.injected} 个任务结果`
  }
  if (executionStatus === 'running') {
    return `已分派 ${totals.workers} 个任务，当前并行 ${totals.running} 个，结果回流 ${totals.injected} 个。`
  }
  if (executionStatus === 'queued') {
    return `任务已进入调度队列，待启动 ${totals.queued} 个，阻塞 ${totals.blocked} 个。`
  }
  if (executionStatus === 'failed') {
    return `执行链路出现异常，失败 ${totals.failed} 个，已回流 ${totals.injected} 个。`
  }
  if (executionStatus === 'completed') {
    return `所有任务已完成，累计回流 ${totals.injected} 个结果。`
  }
  return `当前共有 ${totals.workers} 个任务纳入调度。`
}

function isSameSchedulerFlowMessage(
  message: MessageType,
  refs: {
    route_id?: string
    worker_id?: string
    plan_id?: string
    plan_step_id?: string
    plan_stage?: number
  },
) {
  const notice = message.segments?.find((segment) => segment.type === 'system_notice')
  if (!notice) return false
  const noticeRefs = getRuntimeNoticeRefs(notice.payload)
  if (!noticeRefs) return false

  const targetRouteId = String(refs.route_id || '').trim()
  const targetPlanId = String(refs.plan_id || '').trim()
  const targetWorkerId = String(refs.worker_id || '').trim()

  if (targetRouteId && noticeRefs.route_id === targetRouteId) return true
  if (targetPlanId && noticeRefs.plan_id === targetPlanId) return true
  if (targetWorkerId && noticeRefs.worker_id === targetWorkerId) return true
  return false
}

function shouldSuppressMessageForSchedulerFlow(
  message: MessageType,
  refs: {
    route_id?: string
    worker_id?: string
    plan_id?: string
    plan_step_id?: string
    plan_stage?: number
  },
) {
  const notice = message.segments?.find((segment) => segment.type === 'system_notice')
  if (!notice) return false
  if (!isSameSchedulerFlowMessage(message, refs)) return false

  return ROUTED_SCHEDULER_SUPPRESSED_NOTICE_KINDS.has(notice.kind || '')
}

function finalizePanoramaWorkerSnapshots(
  snapshots: SchedulerPanoramaWorkerSnapshot[],
  finalMerged: boolean,
) {
  if (!finalMerged) return snapshots
  return snapshots.map((snapshot) => {
    if (snapshot.status === 'failed' || snapshot.status === 'cancelled') return snapshot
    return {
      ...snapshot,
      status: 'completed',
      waitingOn: [],
      latestActivity: snapshot.injected
        ? (snapshot.latestActivity || '结果已回流到主对话')
        : '主结果已完成回流',
    }
  })
}

export function useChatSubmitFlow({
  input,
  isLoading,
  inputMode: _inputMode,
  currentSession,
  currentAgentId,
  currentAgent,
  selectedModel,
  submitProvider,
  inputRef,
  userScrolledUpRef,
  sessionIdRef,
  isSendingRef,
  setInput,
  setShowCommands,
  setIsLoading,
  setExpandedThinking,
  setSessionLoading,
  syncTimelineItems,
  applyStreamEventToMessage,
  onSessionCreated,
  attachmentsRef,
  onNotice,
  onGoalUpdate,
  onRefreshGoal,
  onBeforeSend,
}: UseChatSubmitFlowParams) {
  // per-session 索引：A session 的节流不应阻塞 B session（用户实测切换会话点发送无反应即此问题）。
  const lastRequestIdRef = useRef<Record<string, string>>({})
  // 发送锁的获取时刻（per sid），用于识别永远不会被释放的陈旧锁。见 SEND_LOCK_STALE_MS。
  const sendLockAtRef = useRef<Record<string, number>>({})
  // AskUserQuestion 提交 override：handleSubmitAskAnswer 写入，handleSubmit 顶部消费
  const submitOverrideTextRef = useRef<string | null>(null)
  const [askStatesByAskId, setAskStatesByAskId] = useState<AskStatesByAskId>({})
  const setAskState = useCallback((askId: string, entry: AskStateEntry) => {
    setAskStatesByAskId(prev => ({ ...prev, [askId]: entry }))
  }, [])
  // P1.4c-1（架构重构 2026-05-23）：routeProgressMessageIdMapRef 已退场，
  // 迁到 useAppStore.sessions[sid].routedProgressMessageId（数据型 per-sid 状态）。
  // 读 / 写 / 删全部调 store action setSessionRoutedProgressMessageId(sid, idOrNull)。
  //
  // P1.4c-2（架构重构 2026-05-23）：routeProgressClearTimerMapRef / routedPollerMapRef 已退场，
  // 迁到 module-level `src/lib/timerRegistry.ts`（setTimeout id 不可序列化，不进 store）。
  // 调 setTimer(ns, sid, cb, ms) / hasTimer(ns, sid) / clearTimer(ns, sid)，ns 用
  // TIMER_NS_ROUTED_POLLER / TIMER_NS_ROUTED_PROGRESS_CLEAR 常量。跨 Chat unmount 持久。
  //
  // P1.4c-3（架构重构 2026-05-23）：schedulerPanorama{Worker,Plan}CacheRef 已退场，
  // 迁到 module-level `src/lib/schedulerPanoramaCache.ts`（按 task_id / plan_id 全局唯一，
  // 不是 per-sid，跨 Chat unmount 持久，避免 remount 后打爆 backend）。

  const refreshSessionState = useCallback(async (sid: string) => {
    const session = await getSession(sid)
    // P1.2（架构重构 2026-05-23）：meta 改走 store helper，不再依赖组件内 ref。
    const previousMeta = readSessionMessageMeta(sid)
    const previousMessages = readSessionChatMessages(sid)
    const nextMeta = {
      updatedAt: session.updatedAt,
      messageCount: session.messageCount,
      historyRevision: session.historyRevision,
      nextMessageSeq: session.nextMessageSeq,
    }
    const previousTail = previousMessages[previousMessages.length - 1]
    const serverMessages = session.messages || []
    const nextTail = serverMessages[serverMessages.length - 1]
    const hasMeaningfulChange = (
      previousMeta?.updatedAt !== nextMeta.updatedAt
      || previousMeta?.messageCount !== nextMeta.messageCount
      || previousMeta?.historyRevision !== nextMeta.historyRevision
      || previousMeta?.nextMessageSeq !== nextMeta.nextMessageSeq
      || previousMessages.length !== serverMessages.length
      || previousTail?.message_seq !== nextTail?.message_seq
      || previousTail?.history_revision !== nextTail?.history_revision
      || previousTail?.timestamp !== nextTail?.timestamp
      || previousTail?.content !== nextTail?.content
    )

    if (!hasMeaningfulChange) {
      return session
    }

    // server 主动收缩消息（truncate / admin 删除）时的孤儿防线（2026-09-28）：
    // 「从未对账（无 message_seq）」的非 ephemeral cache 条目，其对应的
    // transcript 行已被删掉（或该轮停止/出错根本没落盘），永远等不到
    // client_id / signature 配对——buildSessionMessages 会把它们当
    // in-flight/partial 原位保留成幽灵消息（用户编辑重发后看到两条一样的
    // 消息即此根因，b66a3a8 删掉 serverShrunk 空重建分支后暴露）。
    // 收缩时把这些孤儿滤掉再合并；已对账条目仍走原合并路径（seq 规则自然
    // 丢弃服务端已不存在的）。 ephemeral 尾巴不参与计数也不滤。
    // 注：不能退回 b66a3a8 之前「收缩即空缓存重建」的做法——backend 重启
    // 瞬间的半量 / 空 messages 快照会把整屏清空（该提交修的就是这个）。
    // 在途流（streamRegistry 有 entry）时不滤，避免误杀正在生成的乐观消息。
    const nonEphemeralCacheCount = countPersistedMessages(previousMessages)
    const mergeBasis = (!hasInFlightStream(sid) && serverMessages.length < nonEphemeralCacheCount)
      ? previousMessages.filter((m) => m.ephemeral || hasReconciledMessageSeq(m))
      : previousMessages
    const nextMessages = buildSessionMessages(session, mergeBasis)
    useAppStore.getState().hydrateSessionChatMessages(sid, nextMessages, nextMeta)
    syncTimelineItems(sid)
    return session
  }, [syncTimelineItems])

  const clearRoutedProgressMessage = useCallback((sid: string) => {
    // P1.4c-2：routedProgressClearTimer 走 timerRegistry。
    clearTimer(TIMER_NS_ROUTED_PROGRESS_CLEAR, sid)
    // P1.4c-1：读 store、清除走 setSessionRoutedProgressMessageId(sid, null)。
    const messageId = useAppStore.getState().sessions[sid]?.routedProgressMessageId
    if (!messageId) return

    useAppStore.getState().setSessionRoutedProgressMessageId(sid, null)
    writeSessionChatMessages(sid, (messages) => messages.filter((message) => message.id !== messageId))
    syncTimelineItems(sid)
  }, [syncTimelineItems])

  const resetRoutedProgressRuntime = useCallback((sid: string) => {
    // P1.4c-2：两个 timer 都走 timerRegistry，不再撑本地 ref。
    clearTimer(TIMER_NS_ROUTED_PROGRESS_CLEAR, sid)
    clearTimer(TIMER_NS_ROUTED_POLLER, sid)

    // 注意：schedulerPanoramaCache（lib/schedulerPanoramaCache.ts，P1.4c-3 之后）是
    // 按 task_id / plan_id 的全局缓存，用于避免每 cycle 重 fetch detail。
    // 此前在 reset 时清空整个 cache 是 root cause：
    // 任意 session 用户发新消息都会把 *所有* 其他 session 在跑的 route progress
    // 缓存清掉——并行 spawn 时另一会话切回来后看到进度条重置 / tokens / 工具计数
    // 显示丢失。task_id 不会复用，cache 自然随任务生命周期老化即可，无需
    // 在 reset 时主动清。
    clearRoutedProgressMessage(sid)
  }, [clearRoutedProgressMessage])

  const scheduleClearRoutedProgressMessage = useCallback((sid: string, delayMs: number = ROUTED_PROGRESS_FINAL_HOLD_MS) => {
    // P1.4c-2：setTimer 内部会自动 clear 旧句柄，并在 cb 执行前从 registry 移除自身，
    // 代替原本手动的 cancel + setTimeout + delete 三步。
    setTimer(TIMER_NS_ROUTED_PROGRESS_CLEAR, sid, () => {
      clearRoutedProgressMessage(sid)
    }, delayMs)
  }, [clearRoutedProgressMessage])

  const buildSchedulerPanoramaPayload = useCallback(async (
    _sid: string,
    workers: RuntimeTask[],
    plans: RuntimePlan[],
    finalMerged: boolean,
  ) => {
    const legacySummary = buildRoutedExecutionSummary(workers, plans)
    const activeWorkers = workers.filter((worker) => ACTIVE_WORKER_STATUSES.has(worker.status))
    const activePlans = plans.filter((plan) => ACTIVE_PLAN_STATUSES.has(plan.status))
    const relevantPlans = activePlans.length > 0 ? activePlans : sortByUpdatedAtDesc(plans).slice(0, 1)
    const standaloneWorkers = workers.filter((worker) => !worker.plan_id)
    const relevantStandaloneWorkers = standaloneWorkers.some((worker) => ['pending', 'queued', 'running'].includes(worker.status))
      ? standaloneWorkers.filter((worker) => ['pending', 'queued', 'running'].includes(worker.status))
      : sortByUpdatedAtDesc(standaloneWorkers).slice(0, 1)

    if (
      activeWorkers.length === 0
      && activePlans.length === 0
      && relevantPlans.length === 0
      && relevantStandaloneWorkers.length === 0
    ) {
      if (legacySummary) {
        const skeletonPanorama: SchedulerPanoramaState = {
          version: 1,
          strategy: legacySummary.refs.plan_id ? 'worker_plan' : 'worker',
          title: '调度已启动',
          summary: '正在等待任务与计划详情同步...',
          routeId: undefined,
          planId: legacySummary.refs.plan_id,
          workerId: legacySummary.refs.worker_id,
          startedAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
          executionStatus: finalMerged ? 'completed' : legacySummary.executionStatus,
          totals: {
            workers: 0,
            stages: 0,
            active: 0,
            running: 0,
            queued: 0,
            completed: 0,
            failed: 0,
            cancelled: 0,
            blocked: 0,
            injected: finalMerged ? 1 : 0,
            thinking: 0,
            toolCalls: 0,
            toolResults: 0,
          },
          milestones: {
            routed: true,
            planSubmitted: Boolean(legacySummary.refs.plan_id),
            workersAssigned: 0,
            activeWorkers: 0,
            resultsInjected: finalMerged ? 1 : 0,
            finalMerged,
          },
          stages: [],
        }
        return {
          content: `${skeletonPanorama.title}\n${skeletonPanorama.summary}`,
          executionStatus: skeletonPanorama.executionStatus,
          refs: legacySummary.refs,
          panorama: skeletonPanorama,
        }
      }
      return null
    }

    const planDetails = await Promise.all(relevantPlans.map(async (plan) => {
      // P1.4c-3：读 / 写 module-level schedulerPanoramaCache，跨 Chat unmount 持久。
      const cached = getPlanCacheEntry(plan.plan_id)
      const hasFreshCache = cached && cached.updatedAt === (plan.updated_at || '')
      if (hasFreshCache) return cached.detail

      return plan
    }))

    const relevantPlanIds = new Set(planDetails.map((plan) => plan.plan_id))
    const relevantWorkers = [
      ...workers.filter((worker) => worker.plan_id && relevantPlanIds.has(worker.plan_id)),
      ...relevantStandaloneWorkers,
    ]

    const workerDetails = await Promise.all(relevantWorkers.map(async (worker) => {
      const signature = getWorkerUpdateSignature(worker)
      // P1.4c-3：读 / 写 module-level schedulerPanoramaCache。
      const cached = getWorkerCacheEntry(worker.task_id)
      if (cached && cached.signature === signature) {
        return cached.snapshot
      }
      const snapshot = buildWorkerPanoramaSnapshot(worker, null)
      setWorkerCacheEntry(worker.task_id, { signature, snapshot })
      return snapshot
    }))

    const snapshotByWorkerId = new Map(workerDetails.map((item) => [item.workerId, item]))
    const stages: SchedulerPanoramaStageSnapshot[] = []

    planDetails.forEach((plan) => {
      const stageMap = new Map<string, SchedulerPanoramaStageSnapshot>()
      const planWorkers = workers.filter((worker) => worker.plan_id === plan.plan_id)
      const planSteps = (plan.steps && plan.steps.length > 0)
        ? plan.steps
        : planWorkers.map((worker) => ({
            step_id: worker.plan_step_id || worker.task_id,
            worker_id: worker.task_id,
            description: worker.description,
            priority: worker.priority,
            timeout: worker.timeout,
            depends_on: worker.depends_on || [],
            stage: typeof worker.plan_stage === 'number' ? worker.plan_stage : 0,
          }))

      planSteps.forEach((step) => {
        const worker = planWorkers.find((item) => item.task_id === step.worker_id)
        const snapshot = worker
          ? (snapshotByWorkerId.get(worker.task_id) || buildWorkerPanoramaSnapshot(worker, null))
          : buildPlanStepPanoramaSnapshot(step, finalMerged, plan.status)
        const stageKey = `${plan.plan_id}:${step.stage}`
        const existing = stageMap.get(stageKey) || {
          key: stageKey,
          label: `${plan.title || '调度计划'} · Stage ${step.stage + 1}`,
          stage: step.stage,
          total: 0,
          running: 0,
          queued: 0,
          completed: 0,
          failed: 0,
          cancelled: 0,
          blocked: 0,
          injected: 0,
          workers: [],
        }

        existing.total += 1
        if (snapshot.status === 'running') existing.running += 1
        if (snapshot.status === 'queued' || snapshot.status === 'pending') existing.queued += 1
        if (snapshot.status === 'completed') existing.completed += 1
        if (snapshot.status === 'failed') existing.failed += 1
        if (snapshot.status === 'cancelled') existing.cancelled += 1
        if (snapshot.waitingOn.length > 0) existing.blocked += 1
        if (snapshot.injected) existing.injected += 1
        existing.workers.push(snapshot)
        stageMap.set(stageKey, existing)
      })

      stages.push(...Array.from(stageMap.values()).sort((left, right) => (left.stage ?? 0) - (right.stage ?? 0)))
    })

    const standaloneSnapshots = relevantWorkers
      .filter((worker) => !worker.plan_id)
      .map((worker) => snapshotByWorkerId.get(worker.task_id) || buildWorkerPanoramaSnapshot(worker, null))
    if (standaloneSnapshots.length > 0) {
      stages.push({
        key: 'standalone',
        label: '直接分派任务',
        stage: null,
        total: standaloneSnapshots.length,
        running: standaloneSnapshots.filter((item) => item.status === 'running').length,
        queued: standaloneSnapshots.filter((item) => item.status === 'queued' || item.status === 'pending').length,
        completed: standaloneSnapshots.filter((item) => item.status === 'completed').length,
        failed: standaloneSnapshots.filter((item) => item.status === 'failed').length,
        cancelled: standaloneSnapshots.filter((item) => item.status === 'cancelled').length,
        blocked: standaloneSnapshots.filter((item) => item.waitingOn.length > 0).length,
        injected: standaloneSnapshots.filter((item) => item.injected).length,
        workers: standaloneSnapshots,
      })
    }

    const allSnapshots = finalizePanoramaWorkerSnapshots(
      stages.flatMap((stage) => stage.workers),
      finalMerged,
    )
    if (allSnapshots.length === 0) return null
    const finalizedByWorkerId = new Map(allSnapshots.map((item) => [item.workerId, item]))
    stages.forEach((stage) => {
      stage.workers = stage.workers.map((worker) => finalizedByWorkerId.get(worker.workerId) || worker)
      stage.running = stage.workers.filter((item) => item.status === 'running').length
      stage.queued = stage.workers.filter((item) => item.status === 'queued' || item.status === 'pending').length
      stage.completed = stage.workers.filter((item) => item.status === 'completed').length
      stage.failed = stage.workers.filter((item) => item.status === 'failed').length
      stage.cancelled = stage.workers.filter((item) => item.status === 'cancelled').length
      stage.blocked = stage.workers.filter((item) => item.waitingOn.length > 0).length
      stage.injected = stage.workers.filter((item) => item.injected).length
    })

    const totals: SchedulerPanoramaState['totals'] = {
      workers: allSnapshots.length,
      stages: stages.length,
      active: allSnapshots.filter((item) => ['pending', 'queued', 'running'].includes(item.status)).length,
      running: allSnapshots.filter((item) => item.status === 'running').length,
      queued: allSnapshots.filter((item) => item.status === 'queued' || item.status === 'pending').length,
      completed: allSnapshots.filter((item) => item.status === 'completed').length,
      failed: allSnapshots.filter((item) => item.status === 'failed').length,
      cancelled: allSnapshots.filter((item) => item.status === 'cancelled').length,
      blocked: allSnapshots.filter((item) => item.waitingOn.length > 0).length,
      injected: allSnapshots.filter((item) => item.injected).length,
      thinking: allSnapshots.reduce((sum, item) => sum + item.thinkingCount, 0),
      toolCalls: allSnapshots.reduce((sum, item) => sum + item.toolCalls, 0),
      toolResults: allSnapshots.reduce((sum, item) => sum + item.toolResults, 0),
    }
    if (finalMerged && totals.injected === 0) {
      totals.injected = 1
    }

    const baseExecutionStatus = computePanoramaExecutionStatus(allSnapshots)
    const awaitingFinalMerge = !finalMerged
      && totals.active === 0
      && totals.failed === 0
      && totals.cancelled === 0
      && allSnapshots.length > 0
    const executionStatus = finalMerged && !allSnapshots.some((item) => item.status === 'failed')
      ? 'completed'
      : awaitingFinalMerge
        ? 'running'
        : baseExecutionStatus
    const leadPlan = sortByUpdatedAtDesc(relevantPlans)[0] || null
    const leadWorker = sortByUpdatedAtDesc(relevantWorkers)[0] || null
    const title = leadPlan?.title || leadWorker?.description || '调度执行中'
    const startedAt = [
      ...relevantPlans.map((plan) => plan.created_at),
      ...relevantWorkers.map((worker) => worker.started_at || worker.updated_at || ''),
    ].filter(Boolean).sort()[0]
    const updatedAt = sortByUpdatedAtDesc([
      ...relevantPlans.map((plan) => ({ updatedAt: plan.updated_at })),
      ...relevantWorkers.map((worker) => ({ updatedAt: worker.updated_at })),
    ])[0]?.updatedAt || new Date().toISOString()

    const panorama: SchedulerPanoramaState = {
      version: 1,
      strategy: leadPlan ? 'worker_plan' : 'worker',
      title,
      summary: buildPanoramaSummary(executionStatus, totals, awaitingFinalMerge),
      routeId: undefined,
      planId: leadPlan?.plan_id,
      workerId: leadWorker?.task_id,
      startedAt,
      updatedAt,
      executionStatus,
      totals,
      milestones: {
        routed: true,
        planSubmitted: Boolean(leadPlan),
        workersAssigned: totals.workers,
        activeWorkers: finalMerged ? 0 : totals.active,
        resultsInjected: totals.injected,
        finalMerged,
      },
      stages,
    }

    return {
      content: `${panorama.title}\n${panorama.summary}`,
      executionStatus: legacySummary?.executionStatus || executionStatus,
      refs: {
        route_id: leadPlan?.route_id || leadWorker?.route_id || undefined,
        worker_id: leadWorker?.task_id || legacySummary?.refs.worker_id || undefined,
        plan_id: leadPlan?.plan_id || leadWorker?.plan_id || legacySummary?.refs.plan_id || undefined,
        plan_step_id: leadWorker?.plan_step_id || legacySummary?.refs.plan_step_id || undefined,
        plan_stage: typeof leadWorker?.plan_stage === 'number'
          ? leadWorker.plan_stage
          : legacySummary?.refs.plan_stage,
      },
      panorama,
    }
  }, [])

  const upsertRoutedProgressMessage = useCallback(async (
    sid: string,
    workers: RuntimeTask[],
    plans: RuntimePlan[],
    finalMerged: boolean,
  ) => {
    const summary = await buildSchedulerPanoramaPayload(sid, workers, plans, finalMerged)
    if (!summary) {
      // 注意：刻意不调 clearRoutedProgressMessage(sid)。
      // buildSchedulerPanoramaPayload 返回 null 的语义是"暂时构造不出 panorama"——
      // 通常发生在 route-history/runtime 数据暂时为空（backend race）或 plans/workers 形态意外，
      // 不该把已显示的 route progress 清掉。原版每次返回 null 都 clear，配合 polling 每 3s
      // 一 cycle 形成"切走切回 route progress 看不到"——任意一次 cycle race 进 null 分支
      // route progress 就没了，且因为 polling 持续跑，下次拿到 worker 数据也不会自动恢复
      // （ID 已 delete）。
      // route progress 的真正生命周期终点统一交给 resetRoutedProgressRuntime（用户在该
      // session 发新消息时清）。polling 拿不到 panorama 数据时不动 route progress。
      return
    }

    const existingMessages = readSessionChatMessages(sid)
    // P1.4c-1：读 store 现存 messageId，没有就生一个负数（区别于正常 message）并写回 store。
    const messageId = useAppStore.getState().sessions[sid]?.routedProgressMessageId || (-Date.now() - Math.floor(Math.random() * 1000))
    useAppStore.getState().setSessionRoutedProgressMessageId(sid, messageId)
    const currentMessage = existingMessages.find((message) => message.id === messageId)
    const nextMessage: MessageType = {
      id: messageId,
      role: 'assistant',
      content: '',
      timestamp: currentMessage?.timestamp || new Date().toISOString(),
      ephemeral: true,
      segments: [
        {
          id: 0,
          type: 'system_notice',
          kind: 'scheduler_panorama',
          content: summary.content,
          payload: {
            execution_status: summary.executionStatus,
            refs: summary.refs,
            scheduler_panorama: summary.panorama,
          },
        },
      ],
    }

    const applyUpdate = (messages: MessageType[]) => {
      const filtered = messages.filter((message) => {
        if (message.id === messageId) return true
        return !shouldSuppressMessageForSchedulerFlow(message, summary.refs)
      })

      if (filtered.some((message) => message.id === messageId)) {
        return filtered.map((message) => (message.id === messageId ? nextMessage : message))
      }
      return [...filtered, nextMessage]
    }

    useAppStore.getState().setSessionChatMessages(sid, applyUpdate(existingMessages))
    syncTimelineItems(sid)
  }, [buildSchedulerPanoramaPayload, clearRoutedProgressMessage, syncTimelineItems])

  const startRoutedTaskPolling = useCallback((
    sid: string,
    baselineMessageCount: number = 0,
    routedRefs?: RuntimeNoticeRefs | null,
    requestStartedAt?: string | null,
  ) => {
    void sid
    void baselineMessageCount
    void routedRefs
    void requestStartedAt
    return undefined
  }, [])

  void scheduleClearRoutedProgressMessage
  void upsertRoutedProgressMessage
  void startRoutedTaskPolling

  const handleStop = useCallback(() => {
    const sid = currentSession?.id || ''
    // P1.3（架构重构 2026-05-23）：调 streamRegistry.abort——abort 同时从 registry 清掉。
    // 即使 Chat 中间 unmount过，registry 上仍有 entry，停止按钮付原。
    abortStream(sid, 'user_stop')
    // M2-Streaming：用户主动停 = 中断流 + 清流式 speaker 状态 + 停所有播放
    resetTtsStreaming(sid)
    setSessionLoading(sid, false)
    setIsLoading(false)
    // P1.4c-1：读 store + 清除。
    const routedProgressMessageId = useAppStore.getState().sessions[sid]?.routedProgressMessageId
    useAppStore.getState().setSessionRoutedProgressMessageId(sid, null)

    const stopUpdater = (prev: MessageType[]) => prev
      .map((message) => (
        message.isStreaming ? { ...message, isStreaming: false } : message
      ))
      .filter((message) => message.id !== routedProgressMessageId)

    writeSessionChatMessages(sid, stopUpdater)
    syncTimelineItems(sid)
  }, [
    currentSession?.id,
    setIsLoading,
    setSessionLoading,
    syncTimelineItems,
  ])

  const handleSubmit = useCallback(async (event?: FormEvent, messageOverride?: string) => {
    if (event) event.preventDefault()

    // 附件 snapshot：必须在 handleSubmit 顶部同步抓一份独立拷贝。
    //
    // bug 复现（2026-05-12 用户报"点角色卡片→选图问图里是啥→LLM 说没看到图"）：
    //   Chat.tsx 的 wrap `handleSubmit` 调本函数后立刻 `clearAttachments()`，
    //   `setAttachments([])` 会触发 useChatAttachments 内 useEffect 把
    //   `attachmentsRef.current` 重置为 []。
    //   "无 activeSessionId 首条消息"路径要 `await createSession(...)`——这个
    //   真异步会把控制权交还 event loop，clearAttachments 的 re-render +
    //   useEffect 先跑完；等 createSession 返回，原代码读 ref 已是空数组，
    //   后端收到的 chat body 不含 attachments，LLM 完全感知不到有图。
    //   "已有 session" 路径同步执行到 `await sendMessage(...)` 前才 await，
    //   参数表达式同步求值，clear 来不及跑，所以不出 bug。
    //
    // 抓快照后续读 snapshot 不读 ref，clear 何时跑都不影响。
    const attachmentSnapshot: string[] = attachmentsRef?.current
      ? [...attachmentsRef.current]
      : []

    // session 还没创建时（首条消息），用占位 key '__no_session__'。
    // 创建成功后通过 sessionIdRef 接续，这里仅决定节流/锁的索引粒度。
    const submitSid = currentSession?.id || sessionIdRef.current || '__no_session__'

    const now = Date.now()
    const lastTs = lastRequestIdRef.current[submitSid]
    if (lastTs && (now - parseInt(lastTs, 10)) < REQUEST_DEBOUNCE_MS) {
      console.warn(`[Chat] Request throttled (sid=${submitSid}): ${now - parseInt(lastTs, 10)}ms < ${REQUEST_DEBOUNCE_MS}ms`)
      return
    }
    lastRequestIdRef.current[submitSid] = String(now)

    if (isSendingRef.current[submitSid]) {
      const lockedAt = sendLockAtRef.current[submitSid] || 0
      const heldMs = now - lockedAt
      // 真有 in-flight 流 → 是正常的「上一条还在跑」，明确告诉用户去点停止。
      if (hasInFlightStream(submitSid)) {
        console.warn(`[Chat] handleSubmit blocked by isSendingRef (sid=${submitSid}, in-flight stream)`)
        onNotice?.('上一条还在生成中', '想改内容请先点停止，再重新发送')
        return
      }
      // 没有流却还持锁：只可能是上一轮在拿到锁之后、进 try/finally 之前异常退出。
      // 锁没人释放 → 该会话此后所有发送都在这里静默返回（用户感知为「界面正常但发不出去」）。
      if (heldMs < SEND_LOCK_STALE_MS) {
        console.warn(`[Chat] handleSubmit blocked by isSendingRef (sid=${submitSid}, held=${heldMs}ms)`)
        return
      }
      console.warn(`[Chat] stale send lock cleared (sid=${submitSid}, held=${heldMs}ms)`)
    }
    isSendingRef.current[submitSid] = true
    sendLockAtRef.current[submitSid] = now
    const releaseLock = () => {
      isSendingRef.current[submitSid] = false
      delete sendLockAtRef.current[submitSid]
    }

    // 优先消费入参 messageOverride 或 AskUserQuestion 的 override 文本（由 handleSubmitAskAnswer 写入）
    const overrideText = messageOverride ?? submitOverrideTextRef.current
    submitOverrideTextRef.current = null
    let rawText = (overrideText ?? input).trim()
    if (!rawText) {
      releaseLock()
      return
    }

    // 🎯 Goal 模式自动代理：如果当前处于 goal 模式且用户输入的不是以 / 开头的命令，
    // 自动作为 /goal 目标派发，省去手动键入 /goal
    if (_inputMode === 'goal' && !rawText.startsWith('/')) {
      rawText = `/goal ${rawText}`
    }

    // 预载 TTS 配置
    void ensureTtsStreamingConfig()

    if (isLoading) {
      handleStop()
      releaseLock()
      return
    }

    if (rawText.startsWith('/')) {
      const command = CHAT_SLASH_COMMANDS.find((item) => rawText === item.command || rawText.startsWith(item.command + ' '))
      // /goal 为可直接执行的系统命令，不拦截为纯模板填充，直接进入发送流让后端解析执行
      if (command && !rawText.startsWith('/goal')) {
        const argsText = rawText.slice(command.command.length).trim()
        setInput(command.prompt + ' ' + argsText)
        inputRef.current?.focus()
        releaseLock()
        return
      }
    }

    // LRY-1: `.lry/workflows/<slug>.md` slash 触发。
    // 用户输入首行 `/add-new-tool …` → 从后端取对应 workflow markdown，
    // 包进 `<details>` 折叠块注入到 user message 头部。fetch 失败（404 / 网络）静默降级：
    // `/xxx` 按原样作为普通 user 文本发送，不阻断。这里在 CHAT_SLASH_COMMANDS 之后，
    // 所以内置 slash 模板优先，跟历史行为兼容。
    let workflowBlock = ''
    let messageBody = rawText
    try {
      const injection = await tryInjectLryWorkflow(rawText)
      if (injection) {
        workflowBlock = injection.block
        // 如果剥完首行 body 为空，保留 rawText 语义，让 LLM 至少能看到 workflow +
        // "用户没写具体任务" 的事实（workflow 里通常会引导"请描述你的目标"）。
        messageBody = injection.rest || ''
      }
    } catch (err) {
      console.warn('[Chat] tryInjectLryWorkflow failed, sending as plain text:', err)
    }

    // C-1: VSCode webview 运行时 + 用户未关闭 toggle 时，发送前拼一块 IDE 上下文。
    // 拿不到 / 出错 都返空串 → 不会拼，不阻断发送。
    // 拼后的文本作为 user message 全程使用（前端 cache + 后端 transcript 一致），
    // 渲染靠 markdown <details> 天然折叠。
    let ideContextBlock = ''
    try {
      const runtime = await getRuntime()
      ideContextBlock = await buildIdeContextBlock(runtime)
    } catch (err) {
      console.warn('[Chat] buildIdeContextBlock failed, sending without IDE context:', err)
    }
    // 顺序：workflow 块（行为框架）→ IDE 上下文（环境提示）→ 用户剩余文本。
    // 三部分全空极端情况不发生：rawText 判空已在本函数顶部拦截。
    const text = `${workflowBlock}${ideContextBlock}${messageBody}`

    // 如果不是外部 override 文本发送，则清空用户当前输入框
    if (!overrideText) {
      setInput('')
    }
    setShowCommands(false)
    userScrolledUpRef.current = false
    // 输入框已经清空，后面任何一条防重复/建会话失败分支提前 return 都必须把文字
    // 送回去 —— 否则用户的输入既没发出去也没了（override 文本不属于输入框，不回填）。
    const restoreInput = () => {
      if (overrideText === null) setInput(input)
    }
    const bail = (title: string, desc?: string) => {
      restoreInput()
      releaseLock()
      onNotice?.(title, desc)
    }

    const effectiveAgentId = currentAgentId || 'default'

    let sessionId = currentSession?.id
    if (!sessionId) {
      try {
        const agentName = currentAgent?.name || CHAT_UI_TEXT.defaultAssistantName
        const title = formatChatSessionTitle(effectiveAgentId, agentName)

        const session = await createSession(title, effectiveAgentId)
        if (session.agent_id && session.agent_id !== effectiveAgentId) {
          console.warn(`[Chat] Session agent_id mismatch! Expected=${effectiveAgentId}, Got=${session.agent_id}`)
        }
        sessionId = session.id
        sessionIdRef.current = session.id
        onSessionCreated?.(session)
      } catch (error) {
        console.error('Failed to create session:', error)
        bail('新建会话失败', '后端没响应，消息已送回输入框，可直接重试')
        return
      }
    }

    const userMessageId = Date.now()
    const assistantMessageId = userMessageId + 1

    // v6「修改并重新提问」延迟截断：走到这里说明防抖 / 发送锁 / 空文本 /
    // slash 模板 / 建会话等所有 bail 分支都已通过，这条消息确定要发 ——
    // 才真正执行被标记消息之后的截断。onBeforeSend 抛错 → bail（文字送回
    // 输入框 + 释放锁），标记保留，用户可重试。必须在读 cache 之前 await，
    // 让下面 readSessionChatMessages 拿到的是截断后的消息视图。
    if (onBeforeSend) {
      try {
        await onBeforeSend()
      } catch (error) {
        console.error('[Chat] onBeforeSend (edit-mark truncation) failed:', error)
        bail('修改失败', error instanceof Error ? error.message : '截断会话失败，文字已送回输入框')
        return
      }
    }

    // 端到端 client_id：本轮 user / assistant 各发一个 UUID，写进 cache + 传 backend，
    // backend 持久化进 transcript + 响应带回，buildSessionMessages 用它一对一精确匹配。
    // 注：http Tailscale 跨设备访问是非 secure context，crypto.randomUUID 不存在，
    // 用 safeRandomUUID() 带 fallback（见文件顶部定义）。
    const userClientId = safeRandomUUID()
    const assistantClientId = safeRandomUUID()

    const sid = sessionId || ''
    resetRoutedProgressRuntime(sid)
    const currentMessages = readSessionChatMessages(sid)
    const baselinePersistedMessageCount = countPersistedMessages(currentMessages)
    const lastUserMessage = [...currentMessages].reverse().find((message) => message.role === 'user')

    if (lastUserMessage && lastUserMessage.content === text && lastUserMessage.timestamp) {
      const lastTime = new Date(lastUserMessage.timestamp).getTime()
      if (Date.now() - lastTime < 2000) {
        console.warn(`[Chat] Duplicate content blocked (within 2s): "${rawText.substring(0, 30)}..."`)
        bail('刚刚已发过相同内容', '2 秒内的重复发送被拦下，文字已送回输入框')
        return
      }
    }

    const userTimestamp = new Date().toISOString()
    const nextMessages = (() => {
      const currentLast = currentMessages[currentMessages.length - 1]
      if (currentLast?.role === 'user' && currentLast.content === text) {
        // 只把「刚入 cache」的同文本当成重复点击。不卡时间的话，「修改并重新提问」
        // （截断后原文原样回到输入框）会被永久判为重复 —— 发送静默丢弃，用户
        // 看到的就是「点了重发之后发不出去」。无 timestamp 时保守处理（当作重复）。
        const lastTs = currentLast.timestamp ? new Date(currentLast.timestamp).getTime() : 0
        if (!lastTs || Date.now() - lastTs < 2000) {
          console.warn('[Chat] Cache duplicate blocked')
          return null
        }
      }

      // 乐观渲染的 user message content 提前拼上 attachment footer，
      // 让 ChatMessageItem 的 parseUserMessageAttachments 能立刻拿到附件路径
      // → 用户气泡发完就显图，不等 backend 持久化 + session 刷新那一拍。
      // 注意：text 本身不能拼 footer——它是发往 /chat 的 wire body，backend
      // 自己会再拼一次，重复就 footer×2。所以这里只用于 cache content。
      const displayContent = buildUserMessageWithAttachments(text, attachmentSnapshot)
      return [
        ...currentMessages,
        {
          id: userMessageId,
          role: 'user' as const,
          content: displayContent,
          timestamp: userTimestamp,
          client_id: userClientId,
          segments: [{ id: 0, type: 'text' as const, content: displayContent }],
        },
        {
          id: assistantMessageId,
          role: 'assistant' as const,
          content: '',
          isStreaming: true,
          timestamp: userTimestamp,
          client_id: assistantClientId,
          agent_id: currentSession?.agent_id || effectiveAgentId,
          segments: undefined,
        },
      ]
    })()

    if (!nextMessages) {
      bail('这条刚刚发过', '文字已送回输入框，改点内容再发或稍等一下重试')
      return
    }

    // 流式开始前不预 push 任何占位 segment：backend 持久化时会丢掉所有空段，
    // 只要 cache 多一个空段，buildMessageSignature 就漂移，in-flight assistant 永远
    // 匹配不上 backend，每轮发消息都把 cache 那条接到末尾累积（"发 N 条回 N 条"）。
    // segments 保持 undefined，由 applyStreamEventToMessage 在第一个 chunk 到达时构造；
    // 空 thinking 段本来就被 runtimeBlocks 过滤掉（mergedContent.trim() 为空 return null），
    // 所以删除占位 0 UX 退化。Plan 模式 nextMessages 里已带真实 thinking 段，自然保留。
    useAppStore.getState().setSessionChatMessages(sid, nextMessages)
    syncTimelineItems(sid)
    setIsLoading(true)

    const controller = new AbortController()
    // P1.3（架构重构 2026-05-23）：注册到 module-level streamRegistry，跨 Chat unmount 持久。
    // 同时抓上 assistantMessageId 以便后续诊断 / 可能的多 session 并发识别。
    registerStream(sid, { controller, assistantMessageId })
    setSessionLoading(sid, true)

    // ── P3 抛光（2026-05-16）：SSE 事件 rAF micro-batch ──────────────────
    // 5 个 tool_call 同帧到达时原本要触发 5 次 writeSessionChatMessages + 5 次
    // syncTimelineItems → store subscribers 重渲染 5 次（React 18 自动 batching
    // 在 fetch reader 的 await chunk 边界会断）。改成 rAF 合并：16ms 窗口内累积
    // events → 一次 prev.map + 内层依次 reduce 应用。SSE 总线 publishStreamEvent
    // 仍然每事件同步发，其他订阅者不受影响；只合并 chat store 写入侧。
    //
    // ── P4 平滑流（2026-05-16）：smooth streaming / typewriter 节流 ───────
    // 上面 rAF batch 保证一帧一次 setState，但**一帧涌入 50 字符仍会一蹦上屏**，
    // 视觉不稳（用户报告"输出文字太快有点不稳定的感觉"）。
    // 借鉴 Vercel AI SDK smoothStream + opencode-dev typewriter：
    //   content/thinking 文本按 CHARS_PER_FRAME 切片匀速播报，剩余字符留下一帧；
    //   结构化事件（tool_call/result/notice）原样发出**不消耗预算**，且必须等
    //   前面 text/thinking buffer 播完才出——否则工具卡会跳出来但文字还在流，
    //   顺序错乱看着比不平滑更糟。
    // queue FIFO 自然保证顺序：head 是 text 时按 budget 切片，剩余留 head；
    //   head 是结构化事件时原样移出。
    // CHARS_PER_FRAME = 12 → 60fps × 12 ≈ 720 字/秒，比模型生成速度（120-400 字/秒）
    //   快、不积压，但单帧最多 12 字、人眼能感知到"流"的连续性。
    // 正文（content）+ 思考（thinking）：都走"句子边界 chunked"——累 buffer，遇句子
    // 边界（。！？!?\n 等）整块 flush；强切阈值兜底过长无标点段落；静默超时兜底
    // LLM 不带标点收尾。整块入 queue 走 'event' kind 不受字符 budget 限制 → 一帧
    // 跳变出现，对齐 Cascade "一句一句啪啪出"的节奏。
    // 用户反馈"字符匀速像机关枪收不住"——所以正文也走块状，只是参数比思考紧
    // （强切 50 vs 60，超时 180 vs 250），节奏稍快但仍是块状跳变。
    type StreamQueueItem = { kind: 'event'; raw: ChatStreamLikeEvent }
    interface SmoothConfig {
      type: 'content' | 'thinking'
      flushChars: number
      flushTimeoutMs: number
      // 2026-05-17：最小 flush 间隔节流，限制块状跳变最大频率。
      // 模型突发输出时（一帧涌入大量字符）原本会连续多帧密集 flush 让用户视觉
      // "跟不上"；加节流后高速输出被限流到稳定节奏（CONTENT 5次/秒、THINKING 4次/秒），
      // 慢速输出仍由 flushTimeoutMs 兜底（不影响）。
      // force=true 路径（结构化事件 boundary）bypass 此节流保证顺序。
      minIntervalMs: number
      boundaryRe: RegExp
    }
    interface SmoothBuffer {
      config: SmoothConfig
      text: string
      agentId?: string
      timeoutId: number | null
      lastFlushAt: number    // 0 表示尚未 flush 过；用于 minIntervalMs 节流判定
    }
    // 2026-05-17：迭代到 v2（小块 + 中等节奏）——v3 大块慢节奏（60 字 / 350ms）
    // 用户反馈"过于慢"；回到小块 + minIntervalMs 节流方案，让突发输出被限流到
    // 4-5 次/秒稳定节奏，慢速输出仍由 timeout 兜底。
    const CONTENT_CONFIG: SmoothConfig = {
      type: 'content',
      flushChars: 30,            // 小块单次跳变量小，视觉柔和
      flushTimeoutMs: 220,       // 无标点段落兜底
      minIntervalMs: 200,        // 5 次/秒上限，限制突发块状跳变
      // 正文：仅强句子边界（不含 ；; 因正文里 ; 多用在代码/列表场景）
      boundaryRe: /[。！？!?\n]/,
    }
    const THINKING_CONFIG: SmoothConfig = {
      type: 'thinking',
      flushChars: 35,            // 思考块也小
      flushTimeoutMs: 300,       // 思考节奏稍慢易读
      minIntervalMs: 250,        // 4 次/秒上限，比正文略慢符合"思考"语义
      boundaryRe: /[。！？；!?;\n]/,
    }
    let streamQueue: StreamQueueItem[] = []
    let flushScheduled = false
    const contentBuf: SmoothBuffer = { config: CONTENT_CONFIG, text: '', timeoutId: null, lastFlushAt: 0 }
    const thinkingBuf: SmoothBuffer = { config: THINKING_CONFIG, text: '', timeoutId: null, lastFlushAt: 0 }

    const cancelBufTimeout = (buf: SmoothBuffer) => {
      if (buf.timeoutId !== null) {
        clearTimeout(buf.timeoutId)
        buf.timeoutId = null
      }
    }

    // 把 buf 中"已成块"的部分推入 queue。force=true 全推（无视 minIntervalMs）。
    // 返回是否实际推入了 chunk。
    const flushSmoothBufferToQueue = (buf: SmoothBuffer, force: boolean): boolean => {
      if (!buf.text) return false
      // 2026-05-17：minIntervalMs 节流——非 force 路径下，距上次 flush 不足
      // 此间隔则不 flush（buf 继续累，靠 timeout 兜底或下次输入时再次尝试）。
      // 解决"模型突发输出 → 多帧密集块状跳变 → 视觉跟不上"。
      // force=true（结构化事件强制）bypass 节流保证顺序。
      if (!force && buf.lastFlushAt > 0) {
        const elapsedMs = Date.now() - buf.lastFlushAt
        if (elapsedMs < buf.config.minIntervalMs) return false
      }
      let cutAt = -1
      if (!force) {
        // 找最后一个句子边界，把边界前所有内容作为一块
        for (let i = buf.text.length - 1; i >= 0; i--) {
          if (buf.config.boundaryRe.test(buf.text[i])) {
            cutAt = i + 1
            break
          }
        }
        // 没句子边界但 ≥ flushChars 字 → 强切（避免长段一直不出）
        if (cutAt < 0) {
          if (buf.text.length < buf.config.flushChars) return false
          cutAt = buf.text.length
        }
      } else {
        cutAt = buf.text.length
      }
      const chunk = buf.text.slice(0, cutAt)
      buf.text = buf.text.slice(cutAt)
      streamQueue.push({
        kind: 'event',
        raw: { type: buf.config.type, content: chunk, agent_id: buf.agentId } as ChatStreamLikeEvent,
      })
      buf.lastFlushAt = Date.now()
      return true
    }

    const scheduleFlushIfNeeded = () => {
      if (!flushScheduled) {
        flushScheduled = true
        requestAnimationFrame(flushPendingStreamEvents)
      }
    }

    const handleSmoothEvent = (buf: SmoothBuffer, ev: ChatStreamLikeEvent) => {
      buf.text += ev.content || ''
      buf.agentId = ev.agent_id
      const flushed = flushSmoothBufferToQueue(buf, false)
      if (flushed) {
        cancelBufTimeout(buf)
      } else {
        // 没凑齐 → 启动/重启超时兜底
        cancelBufTimeout(buf)
        buf.timeoutId = window.setTimeout(() => {
          buf.timeoutId = null
          if (flushSmoothBufferToQueue(buf, true)) scheduleFlushIfNeeded()
        }, buf.config.flushTimeoutMs)
      }
    }

    const enqueueStreamEvent = (ev: ChatStreamLikeEvent) => {
      if (ev.type === 'content') {
        // 正文先 flush thinking buffer 保顺序（"思考完了再说正文"）
        cancelBufTimeout(thinkingBuf)
        flushSmoothBufferToQueue(thinkingBuf, true)
        handleSmoothEvent(contentBuf, ev)
        return
      }
      if (ev.type === 'thinking') {
        handleSmoothEvent(thinkingBuf, ev)
        return
      }
      // 结构化事件（tool_call/result/notice/error/...）：必须等所有 buffer 清空才能出，
      // 否则会出现"工具卡跳出来但前面文字还没收尾"的顺序错乱。
      cancelBufTimeout(thinkingBuf)
      cancelBufTimeout(contentBuf)
      flushSmoothBufferToQueue(thinkingBuf, true)
      flushSmoothBufferToQueue(contentBuf, true)
      streamQueue.push({ kind: 'event', raw: ev })
    }

    const applyEventsToMessage = (events: ChatStreamLikeEvent[]) => {
      if (events.length === 0) return
      let updatedMessage: MessageType | null = null
      writeSessionChatMessages(sid, (prev) => prev.map((message) => {
        if (message.id !== assistantMessageId) return message
        let next: MessageType = { ...message, isStreaming: true }
        for (const ev of events) {
          next = applyStreamEventToMessage(next, ev)
        }
        updatedMessage = next
        return next
      }))
      syncTimelineItems(sid)
      // M2-Streaming：每帧把"当前消息累积的可朗读文本"喂给流式 speaker，
      // 完整句子立刻被切出去合成，无需等整段完成。
      // 仅当 sessionIdRef 仍是 sid 时才 feed——切走会话不该继续给老消息合成。
      if (updatedMessage && sessionIdRef.current === sid) {
        try {
          const fullText = extractSpeakableText(updatedMessage)
          if (fullText) {
            feedTtsStreaming({
              sessionId: sid,
              messageId: assistantMessageId,
              fullText,
              agentId: effectiveAgentId || undefined,
            })
          }
        } catch (e) {
          console.warn('[chat] TTS streaming feed failed:', e)
        }
      }
    }

    // queue 现在只装 'event' kind（content/thinking 都在 enqueue 时已成块入队，
    // 不再需要按 budget 切片），所以一帧消费全部即可。rAF batch 仍然保留：同一帧
    // 涌入多个块（一次 fetch chunk 拆出 5 个事件）合并成一次 setState。
    const flushPendingStreamEvents = () => {
      flushScheduled = false
      if (streamQueue.length === 0) return
      const events = streamQueue.map((item) => item.raw)
      streamQueue = []
      applyEventsToMessage(events)
    }

    // 终态（complete/abort/error）：force flush 两个 buffer + 一次性 drain queue，
    // 否则末尾未成句的字符 + 终态 tool_result 会丢失。
    const drainAllPendingStreamEvents = () => {
      flushScheduled = false
      cancelBufTimeout(thinkingBuf)
      cancelBufTimeout(contentBuf)
      flushSmoothBufferToQueue(thinkingBuf, true)
      flushSmoothBufferToQueue(contentBuf, true)
      if (streamQueue.length === 0) return
      const events = streamQueue.map((item) => item.raw)
      streamQueue = []
      applyEventsToMessage(events)
    }

    try {
      await sendMessage(
        text,
        (streamEvent: StreamEvent) => {
          if ((streamEvent as any).type === 'goal_state_update') {
            const g = (streamEvent as any).payload
            if (g) {
              onGoalUpdate?.(g)
            }
            return
          }
          enqueueStreamEvent(streamEvent)
          if (!flushScheduled) {
            flushScheduled = true
            requestAnimationFrame(flushPendingStreamEvents)
          }
        },
        () => {
          // 终态前必须 drain（全量一次出），否则末尾几十字 + tool_result 会被
          // 节流在 buffer 里丢失——P4 后 flushPending 只出 CHARS_PER_FRAME 个字，
          // 原来的一句 flushPending() 收不干净。
          drainAllPendingStreamEvents()
          // P1.3：正常 [DONE] 路径，从 registry 移除（不 abort，流已自然结束）。
          removeStream(sid)
          setSessionLoading(sid, false)

          const cacheUpdater = (prev: MessageType[]) => prev.map((message) =>
            message.id === assistantMessageId
              ? finalizeLastThinkingSegment({ ...message, isStreaming: false })
              : message,
          )

          writeSessionChatMessages(sid, cacheUpdater)

          syncTimelineItems(sid)
          if (sessionIdRef.current === sid) {
            syncTimelineItems(sid)
            setIsLoading(false)
            setExpandedThinking((prev) => {
              const next = new Set(prev)
              next.add(`${assistantMessageId}-think`)
              return next
            })

            // M2-Streaming：流式 speaker 在 applyEventsToMessage 里逐句已喂出去合成；
            // 这里只 flush 尾巴（最后一句没标点收尾时）。
            try {
              flushTtsStreaming({
                sessionId: sid,
                messageId: assistantMessageId,
                agentId: effectiveAgentId || undefined,
              })
            } catch (e) {
              console.warn('[chat] TTS flush failed:', e)
            }
          }

          void refreshSessionState(sid).catch(() => null)
          onRefreshGoal?.()
          void baselinePersistedMessageCount  // 保留变量未用警告
          void userTimestamp
        },
        sessionId || undefined,
        selectedModel || undefined,
        submitProvider || undefined,
        effectiveAgentId,
        controller.signal,
        'inline',
        ROUTE_MAX_STEPS,
        { userClientId, assistantClientId },
        attachmentSnapshot,
      )
    } catch (error: any) {
      // P3 micro-batch + P4 节流：错误/abort 前 drain 保证已收事件不丢
      drainAllPendingStreamEvents()
      // M2-Streaming：abort/error 都立刻停朗读 + 清状态
      // 防止半截消息留 cursor 状态污染下一次重发
      resetTtsStreaming(sid)
      if (error.name === 'AbortError') {
        // P1.3：AbortError 路径一般是 handleStop 触发的，abortStream 已经在那里 remove 过。
        // 这里幂等再打一次，防止额外路径（外部 abort signal）漏清。
        removeStream(sid)
        setSessionLoading(sid, false)
        // P1.4c-1：读 store + 清除。
        const routedProgressMessageId = useAppStore.getState().sessions[sid]?.routedProgressMessageId
        useAppStore.getState().setSessionRoutedProgressMessageId(sid, null)

        const stopUpdater = (prev: MessageType[]) => prev
          .map((message) => (
            message.isStreaming ? finalizeLastThinkingSegment({ ...message, isStreaming: false }) : message
          ))
          .filter((message) => message.id !== routedProgressMessageId)

        writeSessionChatMessages(sid, stopUpdater)
        if (sessionIdRef.current === sid) {
          setIsLoading(false)
          syncTimelineItems(sid)
        }
        return
      }

      const errorMessage = error?.message || String(error)
      console.error('Chat error:', errorMessage)
      // P1.4c-1：读 store + 清除。
      const routedProgressMessageId = useAppStore.getState().sessions[sid]?.routedProgressMessageId
      useAppStore.getState().setSessionRoutedProgressMessageId(sid, null)

      const errorUpdater = (prev: MessageType[]) => prev
        .map((message) => (
          message.id === assistantMessageId
            ? finalizeLastThinkingSegment({ ...message, content: formatChatRequestErrorMessage(errorMessage), isStreaming: false })
            : message
        ))
        .filter((message) => message.id !== routedProgressMessageId)

      writeSessionChatMessages(sid, errorUpdater)
      if (sessionIdRef.current === sid) {
        syncTimelineItems(sid)
        setIsLoading(false)
      }
    } finally {
      releaseLock()
    }
  }, [
    applyStreamEventToMessage,
    clearRoutedProgressMessage,
    currentAgent?.name,
    currentAgentId,
    currentSession?.id,
    handleStop,
    input,
    inputRef,
    isLoading,
    isSendingRef,
    onSessionCreated,
    resetRoutedProgressRuntime,
    selectedModel,
    submitProvider,
    sessionIdRef,
    setExpandedThinking,
    setInput,
    setIsLoading,
    setSessionLoading,
    setShowCommands,
    syncTimelineItems,
    userScrolledUpRef,
    onRefreshGoal,
    onBeforeSend,
  ])

  const handleSubmitAskAnswer = useCallback(async (
    askId: string,
    toolUseId: string | undefined,
    answers: AskUserAnswer[],
  ) => {
    const sid = currentSession?.id || sessionIdRef.current
    if (!sid) {
      setAskState(askId, { state: 'failed', error: '会话未就绪，无法提交回答' })
      return
    }
    setAskState(askId, { state: 'submitting', error: null })
    // v2（2026-09-29，交互式阻塞语义）：提问后工具循环在后端挂起等待，
    // 提交直接 POST /chat/ask/answer 把答案投递回去 → 答案作为该 tool_call 的
    // tool_result 同轮回流，模型立即继续（不再走"答案当新消息"路径，也就
    // 不存在与在途流的发送锁冲突 / 假"已回答" / override 文本残留问题）。
    try {
      await submitAskAnswer({
        session_id: sid,
        ask_id: askId,
        tool_use_id: toolUseId ?? null,
        answers,
      })
      setAskState(askId, { state: 'submitted', error: null })
    } catch (err: any) {
      // 404 = pending ask 已不存在（页面刷新 / 后端重启 / 等待超时）。
      // 降级走旧路径：答案作为普通 user message 回流，模型从历史里接上。
      if (err?.status === 404) {
        submitOverrideTextRef.current =
          '[用户对 ask_user_question 的回答]\n\n' + JSON.stringify({
            kind: 'ask_user_answer' as const,
            ask_id: askId,
            tool_use_id: toolUseId ?? null,
            answers,
          }, null, 2)
        try {
          await handleSubmit()
          setAskState(askId, { state: 'submitted', error: null })
        } catch (err2: any) {
          setAskState(askId, {
            state: 'failed',
            error: String(err2?.message || err2 || '提交失败'),
          })
        }
        return
      }
      setAskState(askId, {
        state: 'failed',
        error: String(err?.message || err || '提交失败'),
      })
    }
  }, [currentSession?.id, handleSubmit, setAskState])

  return {
    handleStop,
    handleSubmit,
    askStatesByAskId,
    handleSubmitAskAnswer,
    refreshSessionState,
  }
}
