import { describe, it, expect } from 'vitest'
import {
  STREAM_EVENT_TYPES,
  CONSULT_STREAM_EVENT_TYPES,
  COLLAB_RUNTIME_EVENT_TYPES,
  isRuntimeStreamEventType,
  isCollabRuntimeEventType,
} from './streamEventTypes'

describe('isRuntimeStreamEventType', () => {
  it('对 STREAM_EVENT_TYPES 中的每个值都返回 true', () => {
    for (const t of STREAM_EVENT_TYPES) {
      expect(isRuntimeStreamEventType(t)).toBe(true)
    }
  })

  it('对未知字符串返回 false', () => {
    expect(isRuntimeStreamEventType('agent_content')).toBe(false)
    expect(isRuntimeStreamEventType('consult_start')).toBe(false)
    expect(isRuntimeStreamEventType('')).toBe(false)
    expect(isRuntimeStreamEventType('CONTENT')).toBe(false)
  })

  it('对非字符串输入返回 false', () => {
    expect(isRuntimeStreamEventType(null)).toBe(false)
    expect(isRuntimeStreamEventType(undefined)).toBe(false)
    expect(isRuntimeStreamEventType(0)).toBe(false)
    expect(isRuntimeStreamEventType({ type: 'content' })).toBe(false)
    expect(isRuntimeStreamEventType(['content'])).toBe(false)
  })
})

describe('isCollabRuntimeEventType', () => {
  it('对 COLLAB_RUNTIME_EVENT_TYPES 中的每个值都返回 true', () => {
    for (const t of COLLAB_RUNTIME_EVENT_TYPES) {
      expect(isCollabRuntimeEventType(t)).toBe(true)
    }
  })

  it('单聊专属的 content 不在协作表里（协作侧改名 agent_content）', () => {
    expect(isCollabRuntimeEventType('content')).toBe(false)
    expect(isCollabRuntimeEventType('agent_content')).toBe(true)
  })

  it('collab 外层信封事件不属 runtime 注册表（CollabChat 显式分支处理）', () => {
    expect(isCollabRuntimeEventType('session_info')).toBe(false)
    expect(isCollabRuntimeEventType('agent_start')).toBe(false)
    expect(isCollabRuntimeEventType('agent_end')).toBe(false)
    expect(isCollabRuntimeEventType('agent_error')).toBe(false)
    expect(isCollabRuntimeEventType('scene_update')).toBe(false)
  })

  it('对非字符串输入返回 false', () => {
    expect(isCollabRuntimeEventType(null)).toBe(false)
    expect(isCollabRuntimeEventType(undefined)).toBe(false)
    expect(isCollabRuntimeEventType(42)).toBe(false)
  })
})

describe('事件类型常量基线（防止误删/误改成员）', () => {
  it('STREAM_EVENT_TYPES 成员稳定', () => {
    expect([...STREAM_EVENT_TYPES]).toEqual([
      'content', 'thinking', 'tool_call_started', 'tool_call', 'tool_result', 'tool_result_update', 'system_notice', 'error',
    ])
  })

  it('CONSULT_STREAM_EVENT_TYPES 含 consult_start/consult_end 边界事件', () => {
    expect(CONSULT_STREAM_EVENT_TYPES).toContain('consult_start')
    expect(CONSULT_STREAM_EVENT_TYPES).toContain('consult_end')
  })

  it('COLLAB_RUNTIME_EVENT_TYPES 用 agent_content 而非 content', () => {
    expect(COLLAB_RUNTIME_EVENT_TYPES).toContain('agent_content')
    expect(COLLAB_RUNTIME_EVENT_TYPES).not.toContain('content')
  })

  it('防漂移：STREAM 全集（content 改名 agent_content）必须都在 COLLAB 表里', () => {
    // collab 内层回合复用同一条 transport（runtime_protocol.build_runtime_transport_event），
    // 单聊能收到的事件 collab 都必须放行——2026-09-29 前手抄表漏收
    // tool_result_update / error / tool_call_started，collab 侧静默丢弃。
    for (const t of STREAM_EVENT_TYPES) {
      if (t === 'content') continue // 协作侧正文统一走 agent_content，裸 content 保持排除
      expect(COLLAB_RUNTIME_EVENT_TYPES).toContain(t)
    }
    expect(COLLAB_RUNTIME_EVENT_TYPES).toContain('tool_result_update')
    expect(COLLAB_RUNTIME_EVENT_TYPES).toContain('tool_call_started')
    expect(COLLAB_RUNTIME_EVENT_TYPES).toContain('error')
  })
})
