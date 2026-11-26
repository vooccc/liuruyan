import type { Dispatch, MutableRefObject, SetStateAction } from 'react'
import type { ContentSegment, Message as MessageType } from '../api/chat'
import type { StreamEvent } from '../api/chat'
import {
  readSessionExternalStreamMessageId,
  writeSessionExternalStreamMessageId,
  removeSessionExternalStreamMessageId,
} from './useChatSessionRuntime'

export interface ChatStreamLikeEvent extends StreamEvent {
  agent_id?: string
}

export interface ChatExternalMessageInput {
  role: 'user' | 'assistant'
  content: string
  timestamp?: string
  segments?: ContentSegment[]
}

export interface ChatExternalStreamStartInput {
  sessionId?: string
  streamId: string
  agentId: string
  content?: string
  timestamp?: string
}

export interface ChatExternalStreamEventInput {
  sessionId?: string
  streamId: string
  event: ChatStreamLikeEvent
}

export interface ChatExternalStreamFinishInput {
  sessionId?: string
  streamId: string
  content?: string
  timestamp?: string
  segments?: ContentSegment[]
}

export interface ChatExternalStreamFailInput {
  sessionId?: string
  streamId: string
  agentId?: string
  error?: string
}

export type RitualMarkerPhase = 'consult_start' | 'consult_end' | 'consult_error'

export interface RitualMarker {
  id: string
  kind: 'ritual_marker'
  sessionId: string
  phase: RitualMarkerPhase
  agentId?: string
  agentName: string
  content: string
  timestamp: string
  streamId: string
}

export type ChatTimelineItem = MessageType | RitualMarker

export interface ChatRitualMarkerInput {
  sessionId?: string
  streamId: string
  phase: RitualMarkerPhase
  agentId?: string
  agentName: string
  content: string
  timestamp?: string
}

export interface ChatHandle {
  appendExternalMessage: (message: ChatExternalMessageInput) => void
  insertRitualMarker: (input: ChatRitualMarkerInput) => void
  startExternalStream: (input: ChatExternalStreamStartInput) => void
  pushExternalStreamEvent: (input: ChatExternalStreamEventInput) => void
  finishExternalStream: (input: ChatExternalStreamFinishInput) => void
  failExternalStream: (input: ChatExternalStreamFailInput) => void
}

export function isRitualMarker(item: ChatTimelineItem): item is RitualMarker {
  return (item as RitualMarker).kind === 'ritual_marker'
}

export function mergeTimelineItems(messages: MessageType[], markers: RitualMarker[]): ChatTimelineItem[] {
  if (markers.length === 0) return [...messages]

  // messages 保留 append 顺序（即时间序），markers 按 timestamp 插入对应位置。
  const sortedMarkers = [...markers].sort((a, b) => (
    new Date(a.timestamp || 0).getTime() - new Date(b.timestamp || 0).getTime()
  ))

  const result: ChatTimelineItem[] = []
  let mIdx = 0
  for (const message of messages) {
    const messageTime = new Date(message.timestamp || 0).getTime()
    while (mIdx < sortedMarkers.length) {
      const markerTime = new Date(sortedMarkers[mIdx].timestamp || 0).getTime()
      if (markerTime <= messageTime) {
        result.push(sortedMarkers[mIdx])
        mIdx += 1
      } else {
        break
      }
    }
    result.push(message)
  }
  while (mIdx < sortedMarkers.length) {
    result.push(sortedMarkers[mIdx])
    mIdx += 1
  }
  return result
}

function buildNormalizedExternalMessage(message: ChatExternalMessageInput): MessageType {
  const normalizedSegments = message.segments && message.segments.length > 0
    ? message.segments
    : [{ id: 0, type: 'text' as const, content: message.content }]

  return {
    id: Date.now(),
    role: message.role,
    content: message.content,
    timestamp: message.timestamp || new Date().toISOString(),
    segments: normalizedSegments,
  }
}

function buildRitualMarker(sessionId: string, input: ChatRitualMarkerInput): RitualMarker {
  return {
    id: `${sessionId}::${input.streamId}::${input.phase}`,
    kind: 'ritual_marker',
    sessionId,
    phase: input.phase,
    agentId: input.agentId,
    agentName: input.agentName,
    content: input.content,
    timestamp: input.timestamp || new Date().toISOString(),
    streamId: input.streamId,
  }
}

/**
 * P1.4a（架构重构 2026-05-23）：getExternalStreamKey 已退场。
 * 原因：externalStreamMessageIds 已从组件 useRef<Record<sid::streamId, number>>
 * 迁到 store.sessions[sid].externalStreamMessageIds[streamId]（嵌套结构），
 * 不再需要拼 sid 前缀。调 readSessionExternalStreamMessageId(sid, streamId) helper 即可。
 */

function createStreamingAssistantMessage(
  messageId: number,
  agentId?: string,
  content = '',
  timestamp?: string,
): MessageType {
  return {
    id: messageId,
    role: 'assistant',
    content,
    isStreaming: true,
    timestamp: timestamp || new Date().toISOString(),
    segments: [{ id: 0, type: 'text', content, agent_id: agentId }],
  }
}

function mergeExternalFinalSegments(
  liveSegments: ContentSegment[] | undefined,
  finalSegments: ContentSegment[],
): ContentSegment[] {
  if (!liveSegments || liveSegments.length === 0) return finalSegments
  return finalSegments.map((segment, index) => {
    if (segment.type !== 'thinking') return segment
    const liveSegment = liveSegments[index]
    if (liveSegment?.type !== 'thinking') return segment
    const firstSeenAt = segment.firstSeenAt ?? liveSegment.firstSeenAt
    return {
      ...segment,
      firstSeenAt,
      completedAt: segment.completedAt ?? liveSegment.completedAt ?? (typeof firstSeenAt === 'number' ? Date.now() : undefined),
    }
  })
}

function finalizeExternalStreamMessage(
  prev: MessageType[],
  messageId: number,
  content: string | undefined,
  timestamp: string | undefined,
  segments: ContentSegment[] | undefined,
  extractTextFromSegments: (segments?: ContentSegment[]) => string,
): MessageType[] {
  const fallbackSegments = segments && segments.length > 0
    ? segments
    : [{ id: 0, type: 'text' as const, content: content || '' }]
  const existingMessage = prev.find((message) => message.id === messageId)
  const normalizedSegments = mergeExternalFinalSegments(existingMessage?.segments, fallbackSegments)
  const finalContent = content ?? extractTextFromSegments(normalizedSegments)

  if (!existingMessage) {
    return [...prev, {
      id: messageId,
      role: 'assistant',
      content: finalContent,
      isStreaming: false,
      timestamp: timestamp || new Date().toISOString(),
      segments: normalizedSegments,
    }]
  }

  return prev.map((message) =>
    message.id === messageId
      ? {
          ...message,
          content: finalContent,
          timestamp: timestamp || message.timestamp,
          segments: normalizedSegments,
          isStreaming: false,
        }
      : message,
  )
}

interface CreateChatExternalHandleParams {
  currentSessionId?: string | null
  sessionIdRef: MutableRefObject<string>
  updateSessionMessages: (sid: string, updater: (prev: MessageType[]) => MessageType[]) => void
  updateSessionTimelineMarkers: (sid: string, updater: (prev: RitualMarker[]) => RitualMarker[]) => void
  syncTimelineItems: (sid: string) => void
  applyStreamEventToMessage: (message: MessageType, event: ChatStreamLikeEvent) => MessageType
  extractTextFromSegments: (segments?: ContentSegment[]) => string
  setExpandedThinking: Dispatch<SetStateAction<Set<string>>>
}

export function createChatExternalHandle({
  currentSessionId,
  sessionIdRef,
  updateSessionMessages,
  updateSessionTimelineMarkers,
  syncTimelineItems,
  applyStreamEventToMessage,
  extractTextFromSegments,
  setExpandedThinking,
}: CreateChatExternalHandleParams): ChatHandle {
  return {
    appendExternalMessage: (message) => {
      const sid = message.role === 'assistant' ? (currentSessionId || '') : (currentSessionId || '')
      if (!sid) return

      updateSessionMessages(sid, (prev) => [...prev, buildNormalizedExternalMessage(message)])
      syncTimelineItems(sid)
    },
    insertRitualMarker: (input) => {
      const sid = input.sessionId || currentSessionId || ''
      if (!sid) return

      const nextMarker = buildRitualMarker(sid, input)
      updateSessionTimelineMarkers(sid, (prev) => {
        if (prev.some((marker) => marker.id === nextMarker.id)) return prev
        return [...prev, nextMarker]
      })
    },
    startExternalStream: ({ sessionId, streamId, agentId, content, timestamp }) => {
      const sid = sessionId || currentSessionId || ''
      if (!sid) return

      // P1.4a：streamId 迁 store，跨 Chat unmount 持久。
      const existingId = readSessionExternalStreamMessageId(sid, streamId)
      const messageId = existingId || (Date.now() + Math.floor(Math.random() * 1000))
      writeSessionExternalStreamMessageId(sid, streamId, messageId)

      updateSessionMessages(sid, (prev) => {
        if (prev.some((message) => message.id === messageId)) return prev
        return [...prev, createStreamingAssistantMessage(messageId, agentId, content || '', timestamp)]
      })
      syncTimelineItems(sid)
    },
    pushExternalStreamEvent: ({ sessionId, streamId, event }) => {
      const sid = sessionId || currentSessionId || ''
      if (!sid) return

      let messageId = readSessionExternalStreamMessageId(sid, streamId)
      if (!messageId) {
        messageId = Date.now() + Math.floor(Math.random() * 1000)
        writeSessionExternalStreamMessageId(sid, streamId, messageId)
        updateSessionMessages(sid, (prev) => [...prev, createStreamingAssistantMessage(messageId!, event.agent_id)])
      }

      updateSessionMessages(sid, (prev) => prev.map((message) =>
        message.id === messageId ? applyStreamEventToMessage({ ...message, isStreaming: true }, event) : message,
      ))
      syncTimelineItems(sid)
    },
    finishExternalStream: ({ sessionId, streamId, content, timestamp, segments }) => {
      const sid = sessionId || currentSessionId || ''
      if (!sid) return

      let messageId = readSessionExternalStreamMessageId(sid, streamId)
      if (!messageId) {
        messageId = Date.now() + Math.floor(Math.random() * 1000)
        writeSessionExternalStreamMessageId(sid, streamId, messageId)
      }

      updateSessionMessages(sid, (prev) =>
        finalizeExternalStreamMessage(prev, messageId!, content, timestamp, segments, extractTextFromSegments),
      )

      removeSessionExternalStreamMessageId(sid, streamId)
      syncTimelineItems(sid)
      if (sessionIdRef.current === sid && messageId) {
        setExpandedThinking((prev) => {
          const next = new Set(prev)
          next.add(`${messageId}-think`)
          return next
        })
      }
    },
    failExternalStream: ({ sessionId, streamId, agentId, error }) => {
      const sid = sessionId || currentSessionId || ''
      if (!sid) return

      let messageId = readSessionExternalStreamMessageId(sid, streamId)
      if (!messageId) {
        messageId = Date.now() + Math.floor(Math.random() * 1000)
        writeSessionExternalStreamMessageId(sid, streamId, messageId)
        updateSessionMessages(sid, (prev) => [...prev, createStreamingAssistantMessage(messageId!, agentId)])
      }

      updateSessionMessages(sid, (prev) => prev.map((message) =>
        message.id === messageId
          ? applyStreamEventToMessage({ ...message, isStreaming: false }, { type: 'error', error, agent_id: agentId })
          : message,
      ))
      removeSessionExternalStreamMessageId(sid, streamId)
      syncTimelineItems(sid)
    },
  }
}
