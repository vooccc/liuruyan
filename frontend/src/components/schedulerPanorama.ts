export interface SchedulerPanoramaWorkerSnapshot {
  workerId: string
  stepId?: string | null
  description: string
  status: string
  stage?: number | null
  dependsOn: string[]
  waitingOn: string[]
  toolCalls: number
  toolResults: number
  thinkingCount: number
  injected: boolean
  latestActivity?: string
  resultPreview?: string | null
  updatedAt?: string | null
  startedAt?: string
}

export interface SchedulerPanoramaStageSnapshot {
  key: string
  label: string
  stage?: number | null
  total: number
  running: number
  queued: number
  completed: number
  failed: number
  cancelled: number
  blocked: number
  injected: number
  workers: SchedulerPanoramaWorkerSnapshot[]
}

export interface SchedulerPanoramaState {
  version: number
  strategy: 'inline' | 'worker' | 'worker_plan'
  title: string
  summary: string
  routeId?: string
  planId?: string
  workerId?: string
  startedAt?: string
  updatedAt?: string
  executionStatus: string
  totals: {
    workers: number
    stages: number
    active: number
    running: number
    queued: number
    completed: number
    failed: number
    cancelled: number
    blocked: number
    injected: number
    thinking: number
    toolCalls: number
    toolResults: number
  }
  milestones: {
    routed: boolean
    planSubmitted: boolean
    workersAssigned: number
    activeWorkers: number
    resultsInjected: number
    finalMerged: boolean
  }
  stages: SchedulerPanoramaStageSnapshot[]
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? value as Record<string, unknown> : null
}

function asString(value: unknown) {
  return typeof value === 'string' ? value : ''
}

function asStringArray(value: unknown) {
  return Array.isArray(value)
    ? value.map((item) => asString(item).trim()).filter(Boolean)
    : []
}

function asNumber(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function normalizeWorkerSnapshot(value: unknown): SchedulerPanoramaWorkerSnapshot | null {
  const record = asRecord(value)
  if (!record) return null

  const workerId = asString(record.workerId).trim()
  if (!workerId) return null

  return {
    workerId,
    stepId: asString(record.stepId).trim() || null,
    description: asString(record.description).trim(),
    status: asString(record.status).trim() || 'pending',
    stage: typeof record.stage === 'number' ? record.stage : null,
    dependsOn: asStringArray(record.dependsOn),
    waitingOn: asStringArray(record.waitingOn),
    toolCalls: asNumber(record.toolCalls),
    toolResults: asNumber(record.toolResults),
    thinkingCount: asNumber(record.thinkingCount),
    injected: Boolean(record.injected),
    latestActivity: asString(record.latestActivity).trim() || undefined,
    resultPreview: asString(record.resultPreview).trim() || null,
    updatedAt: asString(record.updatedAt).trim() || null,
  }
}

function normalizeStageSnapshot(value: unknown): SchedulerPanoramaStageSnapshot | null {
  const record = asRecord(value)
  if (!record) return null

  const key = asString(record.key).trim()
  const label = asString(record.label).trim()
  const workers = Array.isArray(record.workers)
    ? record.workers.map(normalizeWorkerSnapshot).filter((item): item is SchedulerPanoramaWorkerSnapshot => Boolean(item))
    : []

  if (!key || !label) return null

  return {
    key,
    label,
    stage: typeof record.stage === 'number' ? record.stage : null,
    total: asNumber(record.total),
    running: asNumber(record.running),
    queued: asNumber(record.queued),
    completed: asNumber(record.completed),
    failed: asNumber(record.failed),
    cancelled: asNumber(record.cancelled),
    blocked: asNumber(record.blocked),
    injected: asNumber(record.injected),
    workers,
  }
}

export function parseSchedulerPanoramaState(payload: unknown): SchedulerPanoramaState | null {
  const root = asRecord(payload)
  const panorama = asRecord(root?.scheduler_panorama)
  if (!panorama) return null

  const totals = asRecord(panorama.totals)
  const milestones = asRecord(panorama.milestones)
  const stages = Array.isArray(panorama.stages)
    ? panorama.stages.map(normalizeStageSnapshot).filter((item): item is SchedulerPanoramaStageSnapshot => Boolean(item))
    : []

  const strategy = asString(panorama.strategy).trim()
  if (strategy !== 'worker' && strategy !== 'worker_plan' && strategy !== 'inline') return null

  return {
    version: asNumber(panorama.version) || 1,
    strategy,
    title: asString(panorama.title).trim() || '调度执行中',
    summary: asString(panorama.summary).trim(),
    routeId: asString(panorama.routeId).trim() || undefined,
    planId: asString(panorama.planId).trim() || undefined,
    workerId: asString(panorama.workerId).trim() || undefined,
    startedAt: asString(panorama.startedAt).trim() || undefined,
    updatedAt: asString(panorama.updatedAt).trim() || undefined,
    executionStatus: asString(panorama.executionStatus).trim() || 'running',
    totals: {
      workers: asNumber(totals?.workers),
      stages: asNumber(totals?.stages),
      active: asNumber(totals?.active),
      running: asNumber(totals?.running),
      queued: asNumber(totals?.queued),
      completed: asNumber(totals?.completed),
      failed: asNumber(totals?.failed),
      cancelled: asNumber(totals?.cancelled),
      blocked: asNumber(totals?.blocked),
      injected: asNumber(totals?.injected),
      thinking: asNumber(totals?.thinking),
      toolCalls: asNumber(totals?.toolCalls),
      toolResults: asNumber(totals?.toolResults),
    },
    milestones: {
      routed: Boolean(milestones?.routed),
      planSubmitted: Boolean(milestones?.planSubmitted),
      workersAssigned: asNumber(milestones?.workersAssigned),
      activeWorkers: asNumber(milestones?.activeWorkers),
      resultsInjected: asNumber(milestones?.resultsInjected),
      finalMerged: Boolean(milestones?.finalMerged),
    },
    stages,
  }
}
