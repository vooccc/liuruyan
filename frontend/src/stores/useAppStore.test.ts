import { beforeEach, describe, expect, it } from 'vitest'
import type { Message } from '../api/chat'
import { useAppStore } from './useAppStore'

const snapshot = (content: string, messageSeq: number, historyRevision: number): Message[] => [{
  id: messageSeq,
  role: 'assistant',
  content,
  message_seq: messageSeq,
  history_revision: historyRevision,
}]

describe('hydrateSessionChatMessages', () => {
  beforeEach(() => {
    useAppStore.getState().resetAll()
  })

  it('原子提交 messages 与 metadata', () => {
    useAppStore.getState().hydrateSessionChatMessages('sid', snapshot('new', 3, 2), {
      messageCount: 1,
      historyRevision: 2,
      nextMessageSeq: 4,
    })

    const state = useAppStore.getState().sessions.sid
    expect(state.chatMessages?.[0].content).toBe('new')
    expect(state.messageMeta).toMatchObject({ historyRevision: 2, nextMessageSeq: 4 })
  })

  it('拒绝较旧 revision 的延迟 snapshot', () => {
    useAppStore.getState().hydrateSessionChatMessages('sid', snapshot('new', 3, 3), {
      historyRevision: 3,
      nextMessageSeq: 4,
    })
    useAppStore.getState().hydrateSessionChatMessages('sid', snapshot('stale', 2, 2), {
      historyRevision: 2,
      nextMessageSeq: 3,
    })

    const state = useAppStore.getState().sessions.sid
    expect(state.chatMessages?.[0].content).toBe('new')
    expect(state.messageMeta).toMatchObject({ historyRevision: 3, nextMessageSeq: 4 })
  })

  it('同 revision 下拒绝较旧 nextMessageSeq 的 snapshot', () => {
    useAppStore.getState().hydrateSessionChatMessages('sid', snapshot('new', 4, 3), {
      historyRevision: 3,
      nextMessageSeq: 5,
    })
    useAppStore.getState().hydrateSessionChatMessages('sid', snapshot('stale', 3, 3), {
      historyRevision: 3,
      nextMessageSeq: 4,
    })

    expect(useAppStore.getState().sessions.sid.chatMessages?.[0].content).toBe('new')
  })

  it('普通 cursor 单调推进，full snapshot reset 可切换到更低 event domain', () => {
    useAppStore.getState().hydrateSessionChatMessages('sid', [], {})
    useAppStore.getState().setSessionLastEventId('sid', 900)
    useAppStore.getState().setSessionLastEventId('sid', 500)
    expect(useAppStore.getState().sessions.sid.lastEventId).toBe(900)

    useAppStore.getState().resetSessionLastEventId('sid', 256)
    expect(useAppStore.getState().sessions.sid.lastEventId).toBe(256)
  })
})
