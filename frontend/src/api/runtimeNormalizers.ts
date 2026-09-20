/**
 * Runtime/route payload 规整器（纯函数层）
 *
 * 从 `chat.ts` 抽出的「把后端 unknown payload → 前端 typed 结构」纯函数，
 * 无网络 / 无 store 副作用，便于单测与复用（P6 chat.ts 拆分第一刀）。
 *
 * 类型用 `import type` 从 `./chat` 引入——type-only import 编译后被擦除，
 * 不产生运行时循环依赖（chat.ts 反过来 value-import 本模块的函数）。
 */
import type {
  RuntimeNoticePayload,
  RuntimeNoticeRefs,
  RouteDiagnostics,
  RouteBudgetState,
  RouteRuntimeSnapshot,
  RouteSessionLoad,
  RouteRecentOutcomes,
  RouteBudgetPressure,
} from './chat'

export function asRuntimeNoticePayload(payload: unknown): RuntimeNoticePayload | null {
  if (!payload || typeof payload !== 'object') return null
  const normalized = payload as RuntimeNoticePayload
  const refs = normalized.refs && typeof normalized.refs === 'object'
    ? normalized.refs
    : {}
  return {
    ...normalized,
    refs: {
      route_id: typeof refs.route_id === 'string'
        ? refs.route_id
        : (typeof (payload as Record<string, unknown>).route_id === 'string'
          ? (payload as Record<string, unknown>).route_id as string
          : undefined),
      worker_id: typeof refs.worker_id === 'string'
        ? refs.worker_id
        : (typeof (payload as Record<string, unknown>).worker_id === 'string'
          ? (payload as Record<string, unknown>).worker_id as string
          : undefined),
      plan_id: typeof refs.plan_id === 'string'
        ? refs.plan_id
        : (typeof (payload as Record<string, unknown>).plan_id === 'string'
          ? (payload as Record<string, unknown>).plan_id as string
          : undefined),
      plan_step_id: typeof refs.plan_step_id === 'string'
        ? refs.plan_step_id
        : (typeof (payload as Record<string, unknown>).plan_step_id === 'string'
          ? (payload as Record<string, unknown>).plan_step_id as string
          : undefined),
      plan_stage: typeof refs.plan_stage === 'number'
        ? refs.plan_stage
        : (typeof (payload as Record<string, unknown>).plan_stage === 'number'
          ? (payload as Record<string, unknown>).plan_stage as number
          : undefined),
    },
    runtime_snapshot: normalizeRouteRuntimeSnapshot((payload as Record<string, unknown>).runtime_snapshot),
    route_diagnostics: normalizeRouteDiagnosticsPayload((payload as Record<string, unknown>).route_diagnostics),
    budget_state: normalizeRouteBudgetState((payload as Record<string, unknown>).budget_state),
    task: typeof (payload as Record<string, unknown>).task === 'string'
      ? (payload as Record<string, unknown>).task as string
      : undefined,
    title: typeof (payload as Record<string, unknown>).title === 'string'
      ? (payload as Record<string, unknown>).title as string
      : undefined,
    strategy: typeof (payload as Record<string, unknown>).strategy === 'string'
      ? (payload as Record<string, unknown>).strategy as RuntimeNoticePayload['strategy']
      : undefined,
    confidence: typeof (payload as Record<string, unknown>).confidence === 'string'
      ? (payload as Record<string, unknown>).confidence as string
      : undefined,
    source: typeof (payload as Record<string, unknown>).source === 'string'
      ? (payload as Record<string, unknown>).source as string
      : undefined,
    task_class: typeof (payload as Record<string, unknown>).task_class === 'string'
      ? (payload as Record<string, unknown>).task_class as string
      : undefined,
    execution_status: typeof (payload as Record<string, unknown>).execution_status === 'string'
      ? (payload as Record<string, unknown>).execution_status as string
      : undefined,
    priority: typeof (payload as Record<string, unknown>).priority === 'number'
      ? (payload as Record<string, unknown>).priority as number
      : undefined,
    timeout: typeof (payload as Record<string, unknown>).timeout === 'number'
      ? (payload as Record<string, unknown>).timeout as number
      : undefined,
    max_steps: typeof (payload as Record<string, unknown>).max_steps === 'number'
      ? (payload as Record<string, unknown>).max_steps as number
      : undefined,
  }
}

export function getRuntimeNoticeRefs(payload: unknown): RuntimeNoticeRefs | null {
  return asRuntimeNoticePayload(payload)?.refs || null
}

export function normalizeRouteDiagnosticsPayload(routeDiagnostics: unknown): RouteDiagnostics | undefined {
  if (!routeDiagnostics || typeof routeDiagnostics !== 'object') return undefined
  const payload = routeDiagnostics as Partial<RouteDiagnostics>
  if (!payload.kind || !payload.rule_id || !payload.title || !payload.summary) return undefined
  return {
    kind: payload.kind,
    rule_id: String(payload.rule_id),
    title: String(payload.title),
    summary: String(payload.summary),
    matched_conditions: Array.isArray(payload.matched_conditions)
      ? payload.matched_conditions.map((condition) => String(condition || ''))
      : [],
    observed: payload.observed && typeof payload.observed === 'object'
      ? payload.observed as Record<string, unknown>
      : undefined,
    suggested_action: typeof payload.suggested_action === 'string'
      ? payload.suggested_action
      : undefined,
  }
}

export function normalizeRouteBudgetState(budgetState: unknown): RouteBudgetState {
  const normalized = budgetState && typeof budgetState === 'object'
    ? budgetState as Record<string, unknown>
    : {}
  return {
    warning_count: Number(normalized.warning_count || 0),
    exhausted_count: Number(normalized.exhausted_count || 0),
    limit_type: String(normalized.limit_type || '').trim(),
    pressure_axes: Array.isArray(normalized.pressure_axes)
      ? normalized.pressure_axes.map((item) => String(item || '')).filter(Boolean)
      : [],
    last_snapshot: normalized.last_snapshot && typeof normalized.last_snapshot === 'object'
      ? normalized.last_snapshot as Record<string, unknown>
      : {},
    last_kind: typeof normalized.last_kind === 'string' ? normalized.last_kind : undefined,
    last_recorded_at: typeof normalized.last_recorded_at === 'string' ? normalized.last_recorded_at : undefined,
  }
}

export function normalizeRouteRuntimeSnapshot(snapshot: unknown): RouteRuntimeSnapshot | undefined {
  if (!snapshot || typeof snapshot !== 'object') return undefined
  const normalized = snapshot as Record<string, unknown>
  return {
    session_load: normalized.session_load && typeof normalized.session_load === 'object'
      ? normalized.session_load as RouteSessionLoad
      : undefined,
    recent_outcomes: normalized.recent_outcomes && typeof normalized.recent_outcomes === 'object'
      ? normalized.recent_outcomes as RouteRecentOutcomes
      : undefined,
    budget_pressure: normalized.budget_pressure && typeof normalized.budget_pressure === 'object'
      ? normalized.budget_pressure as RouteBudgetPressure
      : undefined,
  }
}
