import { describe, expect, it } from 'vitest'
import { nextReplayCursor, parseSseFrame } from './useDispatchEventConsumer'

describe('dispatch SSE reconnect cursor', () => {
  it('解析事件基础字段与 payload', () => {
    const parsed = parseSseFrame(JSON.stringify({
      type: 'session_token_usage',
      event_id: 9,
      event_seq: 9,
      payload: { total: 1200 },
      session_id: 'sid',
    }))

    expect(parsed).toMatchObject({
      type: 'session_token_usage',
      event_id: 9,
      payload: { total: 1200 },
    })
  })

  it('拒绝 replay/live overlap 中不大于 cursor 的重复事件', () => {
    const event = parseSseFrame(JSON.stringify({ type: 'message_added', event_id: 12 }))!

    expect(nextReplayCursor(12, event)).toEqual({
      accept: false,
      cursor: 12,
      fullSnapshot: false,
    })
    expect(nextReplayCursor(13, event).accept).toBe(false)
  })

  it('接受更大的 event ID 并单调推进 cursor', () => {
    const event = parseSseFrame(JSON.stringify({ type: 'message_added', event_id: 13 }))!

    expect(nextReplayCursor(12, event)).toEqual({
      accept: true,
      cursor: 13,
      fullSnapshot: false,
    })
  })

  it('full_snapshot 控制帧允许切换到服务端新 event domain', () => {
    const control = parseSseFrame(JSON.stringify({
      type: 'replay_control',
      resume_mode: 'full_snapshot',
      latest_event_id: 256,
    }))!

    expect(nextReplayCursor(900, control)).toEqual({
      accept: false,
      cursor: 256,
      fullSnapshot: true,
    })
  })

  it('tail 控制帧不改变 cursor 也不进入业务 reducer', () => {
    const control = parseSseFrame(JSON.stringify({
      type: 'replay_control',
      resume_mode: 'tail',
      latest_event_id: 15,
    }))!

    expect(nextReplayCursor(12, control)).toEqual({
      accept: false,
      cursor: 12,
      fullSnapshot: false,
    })
  })
})
