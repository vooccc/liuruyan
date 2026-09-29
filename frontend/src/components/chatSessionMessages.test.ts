import { describe, expect, it } from 'vitest'
import type { Message, Session } from '../api/chat'
import { buildSessionMessages } from './chatSessionMessages'

function buildSession(messages: Message[]): Session {
  return {
    id: 'session-1',
    title: 'test',
    createdAt: '2026-07-20T10:00:00.000Z',
    updatedAt: '2026-07-21T02:42:00.000Z',
    messageCount: messages.length,
    messages,
  }
}

describe('buildSessionMessages', () => {
  it('唤醒后首次发送时把缺失的 server 历史前缀放回新轮次之前', () => {
    const serverMessages: Message[] = [
      { id: 1, role: 'user', content: '昨天你好', timestamp: '2026-07-20T02:53:00.000Z', client_id: 'old-user' },
      { id: 2, role: 'assistant', content: '早上好。', timestamp: '2026-07-20T02:53:01.000Z', client_id: 'old-assistant' },
      { id: 3, role: 'user', content: '你好', timestamp: '2026-07-21T12:42:00.000Z', client_id: 'new-user' },
      { id: 4, role: 'assistant', content: '晚上好。', timestamp: '2026-07-21T12:42:01.000Z', client_id: 'new-assistant' },
    ]
    const wakeCache: Message[] = [
      { id: 101, role: 'user', content: '你好', timestamp: '2026-07-21T12:42:00.000Z', client_id: 'new-user' },
      { id: 102, role: 'assistant', content: '晚上好。', timestamp: '2026-07-21T12:42:00.000Z', client_id: 'new-assistant' },
    ]

    const result = buildSessionMessages(buildSession(serverMessages), wakeCache)

    expect(result.map((message) => message.content)).toEqual([
      '昨天你好',
      '早上好。',
      '你好',
      '晚上好。',
    ])
    expect(result[2].id).toBe(101)
    expect(result[3].id).toBe(102)
  })

  it('保留未持久化的 partial cache 原顺序，不按 timestamp 全局排序', () => {
    const serverMessages: Message[] = [
      { id: 1, role: 'user', content: '旧问题', timestamp: '2026-07-20T02:53:00.000Z', client_id: 'old-user' },
      { id: 2, role: 'assistant', content: '旧回答', timestamp: '2026-07-20T02:53:01.000Z', client_id: 'old-assistant' },
    ]
    const cache: Message[] = [
      { id: 101, role: 'user', content: '旧问题', timestamp: '2026-07-20T02:53:00.000Z', client_id: 'old-user' },
      { id: 102, role: 'assistant', content: '中断但未落盘', timestamp: '2026-07-19T02:53:00.000Z' },
      { id: 103, role: 'assistant', content: '旧回答', timestamp: '2026-07-20T02:53:01.000Z', client_id: 'old-assistant' },
    ]

    const result = buildSessionMessages(buildSession(serverMessages), cache)

    expect(result.map((message) => message.content)).toEqual([
      '旧问题',
      '中断但未落盘',
      '旧回答',
    ])
  })

  it('按 message_seq 复用稳定 id 并采用 server revision 内容', () => {
    const serverMessages: Message[] = [
      { id: 1, role: 'assistant', content: '修订后', message_seq: 7, history_revision: 9 },
    ]
    const cache: Message[] = [
      { id: 301, role: 'assistant', content: '修订前', message_seq: 7, history_revision: 7 },
    ]

    const result = buildSessionMessages(buildSession(serverMessages), cache)

    expect(result).toHaveLength(1)
    expect(result[0]).toMatchObject({
      id: 301,
      content: '修订后',
      message_seq: 7,
      history_revision: 9,
    })
  })

  it('删除 snapshot 中已不存在的 persisted cache 行', () => {
    const serverMessages: Message[] = [
      { id: 1, role: 'user', content: '保留', message_seq: 1, history_revision: 1 },
    ]
    const cache: Message[] = [
      { id: 401, role: 'user', content: '保留', message_seq: 1, history_revision: 1 },
      { id: 402, role: 'assistant', content: '已删除', message_seq: 2, history_revision: 2 },
    ]

    const result = buildSessionMessages(buildSession(serverMessages), cache)

    expect(result.map((message) => message.content)).toEqual(['保留'])
    expect(result[0].id).toBe(401)
  })

  it('hydration 时保留尚无 message_seq 的 pending 行', () => {
    const serverMessages: Message[] = [
      { id: 1, role: 'user', content: '旧消息', message_seq: 1, history_revision: 1 },
    ]
    const cache: Message[] = [
      { id: 501, role: 'user', content: '旧消息', message_seq: 1, history_revision: 1 },
      { id: 502, role: 'user', content: '正在发送', client_id: 'pending-user' },
      { id: 503, role: 'assistant', content: '部分响应', client_id: 'pending-assistant', isStreaming: true },
    ]

    const result = buildSessionMessages(buildSession(serverMessages), cache)

    expect(result.map((message) => message.content)).toEqual(['旧消息', '正在发送', '部分响应'])
    expect(result.slice(1).map((message) => message.id)).toEqual([502, 503])
  })

  it('按 message_seq 排列完整 authoritative snapshot', () => {
    const result = buildSessionMessages(buildSession([
      { id: 3, role: 'assistant', content: 'third', message_seq: 3, history_revision: 3 },
      { id: 1, role: 'user', content: 'first', message_seq: 1, history_revision: 1 },
      { id: 2, role: 'assistant', content: 'second', message_seq: 2, history_revision: 2 },
    ]))

    expect(result.map((message) => message.message_seq)).toEqual([1, 2, 3])
  })

  it('pending 通过 client_id 确认后保留本地 id 并获得 server metadata', () => {
    const result = buildSessionMessages(buildSession([
      { id: 1, role: 'assistant', content: '完成', client_id: 'cid', message_seq: 9, history_revision: 4 },
    ]), [
      { id: 701, role: 'assistant', content: '', client_id: 'cid', isStreaming: true },
    ])

    expect(result[0]).toMatchObject({
      id: 701,
      content: '完成',
      message_seq: 9,
      history_revision: 4,
    })
  })

  it('相同内容但不同 message_seq 保持为两条 persisted 消息', () => {
    const result = buildSessionMessages(buildSession([
      { id: 1, role: 'assistant', content: 'same', message_seq: 11, history_revision: 5 },
      { id: 2, role: 'assistant', content: 'same', message_seq: 12, history_revision: 6 },
    ]))

    expect(result.map((message) => message.message_seq)).toEqual([11, 12])
  })

  it('legacy snapshot 无 sequence 时保持 API 物理顺序', () => {
    const result = buildSessionMessages(buildSession([
      { id: 2, role: 'assistant', content: 'api-first' },
      { id: 1, role: 'user', content: 'api-second' },
    ]))

    expect(result.map((message) => message.content)).toEqual(['api-first', 'api-second'])
  })

  it('保留内容为空的 active streaming pending placeholder', () => {
    const result = buildSessionMessages(buildSession([]), [
      { id: 801, role: 'assistant', content: '', client_id: 'streaming', isStreaming: true },
    ])

    expect(result).toHaveLength(1)
    expect(result[0]).toMatchObject({ id: 801, isStreaming: true })
  })

  it('线性处理 10000 条 sequenced snapshot', () => {
    const messages = Array.from({ length: 10_000 }, (_, index): Message => ({
      id: index + 1,
      role: index % 2 === 0 ? 'user' : 'assistant',
      content: `message-${index + 1}`,
      message_seq: index + 1,
      history_revision: index + 1,
    }))

    const result = buildSessionMessages(buildSession(messages))

    expect(result).toHaveLength(10_000)
    expect(result[9_999].message_seq).toBe(10_000)
  })
})
