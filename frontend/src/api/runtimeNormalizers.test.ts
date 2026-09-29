import { describe, it, expect } from 'vitest'
import {
  asRuntimeNoticePayload,
  getRuntimeNoticeRefs,
  normalizeRouteDiagnosticsPayload,
  normalizeRouteBudgetState,
  normalizeRouteRuntimeSnapshot,
} from './runtimeNormalizers'

describe('normalizeRouteBudgetState', () => {
  it('非对象输入回退为全默认（计数 0 / 空数组 / 空字符串）', () => {
    expect(normalizeRouteBudgetState(null)).toEqual({
      warning_count: 0,
      exhausted_count: 0,
      limit_type: '',
      pressure_axes: [],
      last_snapshot: {},
      last_kind: undefined,
      last_recorded_at: undefined,
    })
    expect(normalizeRouteBudgetState(undefined).warning_count).toBe(0)
    expect(normalizeRouteBudgetState(42).pressure_axes).toEqual([])
  })

  it('数值字段强转 Number，pressure_axes 过滤空串', () => {
    const out = normalizeRouteBudgetState({
      warning_count: '3',
      exhausted_count: 1,
      limit_type: '  daily ',
      pressure_axes: ['tokens', '', null, 'calls'],
      last_kind: 'warn',
      last_recorded_at: '2026-06-15T00:00:00Z',
    })
    expect(out.warning_count).toBe(3)
    expect(out.exhausted_count).toBe(1)
    expect(out.limit_type).toBe('daily')
    expect(out.pressure_axes).toEqual(['tokens', 'calls'])
    expect(out.last_kind).toBe('warn')
    expect(out.last_recorded_at).toBe('2026-06-15T00:00:00Z')
  })

  it('last_kind/last_recorded_at 非字符串时为 undefined', () => {
    const out = normalizeRouteBudgetState({ last_kind: 5, last_recorded_at: {} })
    expect(out.last_kind).toBeUndefined()
    expect(out.last_recorded_at).toBeUndefined()
  })
})

describe('normalizeRouteRuntimeSnapshot', () => {
  it('非对象输入返回 undefined', () => {
    expect(normalizeRouteRuntimeSnapshot(null)).toBeUndefined()
    expect(normalizeRouteRuntimeSnapshot('x')).toBeUndefined()
  })

  it('只保留对象类型的三个子字段，否则 undefined', () => {
    const out = normalizeRouteRuntimeSnapshot({
      session_load: { a: 1 },
      recent_outcomes: 'nope',
      budget_pressure: { b: 2 },
    })
    expect(out?.session_load).toEqual({ a: 1 })
    expect(out?.recent_outcomes).toBeUndefined()
    expect(out?.budget_pressure).toEqual({ b: 2 })
  })
})

describe('normalizeRouteDiagnosticsPayload', () => {
  it('非对象或缺必填字段返回 undefined', () => {
    expect(normalizeRouteDiagnosticsPayload(null)).toBeUndefined()
    expect(normalizeRouteDiagnosticsPayload({ kind: 'x' })).toBeUndefined()
    expect(
      normalizeRouteDiagnosticsPayload({ kind: 'x', rule_id: 'r', title: 't' }),
    ).toBeUndefined()
  })

  it('齐全字段时强转字符串 + matched_conditions 映射为字符串数组', () => {
    const out = normalizeRouteDiagnosticsPayload({
      kind: 'budget',
      rule_id: 1,
      title: 2,
      summary: 3,
      matched_conditions: ['a', null, 4],
      observed: { x: 1 },
      suggested_action: 'do-it',
    })
    expect(out).toEqual({
      kind: 'budget',
      rule_id: '1',
      title: '2',
      summary: '3',
      matched_conditions: ['a', '', '4'],
      observed: { x: 1 },
      suggested_action: 'do-it',
    })
  })

  it('matched_conditions 非数组 / observed 非对象时给安全默认', () => {
    const out = normalizeRouteDiagnosticsPayload({
      kind: 'k',
      rule_id: 'r',
      title: 't',
      summary: 's',
      matched_conditions: 'nope',
      observed: 'nope',
      suggested_action: 5,
    })
    expect(out?.matched_conditions).toEqual([])
    expect(out?.observed).toBeUndefined()
    expect(out?.suggested_action).toBeUndefined()
  })
})

describe('asRuntimeNoticePayload', () => {
  it('非对象返回 null', () => {
    expect(asRuntimeNoticePayload(null)).toBeNull()
    expect(asRuntimeNoticePayload('x')).toBeNull()
  })

  it('从顶层平铺字段回填 refs（refs 缺失时）', () => {
    const out = asRuntimeNoticePayload({
      route_id: 'r1',
      worker_id: 'w1',
      plan_id: 'p1',
      plan_step_id: 's1',
      plan_stage: 2,
    })
    expect(out?.refs).toEqual({
      route_id: 'r1',
      worker_id: 'w1',
      plan_id: 'p1',
      plan_step_id: 's1',
      plan_stage: 2,
    })
  })

  it('refs 内字段优先于顶层平铺字段', () => {
    const out = asRuntimeNoticePayload({
      refs: { route_id: 'fromRefs' },
      route_id: 'fromTop',
    })
    expect(out?.refs.route_id).toBe('fromRefs')
  })

  it('标量字段按类型过滤（类型不符 → undefined）', () => {
    const out = asRuntimeNoticePayload({
      task: 'build',
      priority: 'high', // 非 number → undefined
      timeout: 30,
    })
    expect(out?.task).toBe('build')
    expect(out?.priority).toBeUndefined()
    expect(out?.timeout).toBe(30)
  })
})

describe('getRuntimeNoticeRefs', () => {
  it('非法 payload 返回 null', () => {
    expect(getRuntimeNoticeRefs(null)).toBeNull()
  })

  it('返回 asRuntimeNoticePayload 的 refs', () => {
    expect(getRuntimeNoticeRefs({ worker_id: 'w9' })).toEqual({
      route_id: undefined,
      worker_id: 'w9',
      plan_id: undefined,
      plan_step_id: undefined,
      plan_stage: undefined,
    })
  })
})
