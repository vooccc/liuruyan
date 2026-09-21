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

  it('协作事件不含 content/error/tool_call_started，应返回 false', () => {
    expect(isCollabRuntimeEventType('content')).toBe(false)
    expect(isCollabRuntimeEventType('error')).toBe(false)
    expect(isCollabRuntimeEventType('tool_call_started')).toBe(false)
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
      'content', 'thinking', 'tool_call_started', 'tool_call', 'tool_result', 'system_notice', 'error',
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
})
