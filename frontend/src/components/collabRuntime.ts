import type {
  CollabChatEvent,
  ContentSegment,
  Message as MessageType,
} from '../api/chat'
import type { ChatStreamLikeEvent } from './chatExternalHandle'
import { applyStreamEventToMessage } from './chatStreamMessage'
import type { CollabBubble } from './CollabMessageItem'
import { hasDetailedRuntimeChain } from './runtimeDisplay'

export function buildCollabTextSegment(content: string, agentId?: string): ContentSegment {
  return { id: 0, type: 'text', content, agent_id: agentId }
}

export function normalizeCollabSegments(
  segments: ContentSegment[] | undefined,
  fallbackText: string,
  agentId?: string,
): ContentSegment[] {
  const normalized = (segments && segments.length > 0 ? segments : [buildCollabTextSegment(fallbackText, agentId)])
    .filter((segment): segment is ContentSegment => Boolean(segment && typeof segment === 'object'))
    .map((segment, index) => ({
      ...segment,
      id: index,
      agent_id: segment.agent_id || agentId,
    }))

  return normalized.length > 0 ? normalized : [buildCollabTextSegment(fallbackText, agentId)]
}

export function hasCollabDetailedChain(segments: ContentSegment[]) {
  return hasDetailedRuntimeChain(segments)
}

export function normalizeCollabStreamEvent(event: CollabChatEvent): ChatStreamLikeEvent | null {
  if (event.type === 'agent_content') {
    return { ...event, type: 'content' }
  }

  if (['content', 'thinking', 'tool_call', 'tool_result', 'system_notice', 'error'].includes(event.type)) {
    return event as ChatStreamLikeEvent
  }

  return null
}

export function applyEventToCollabBubble(bubble: CollabBubble, event: CollabChatEvent): CollabBubble {
  const normalizedEvent = normalizeCollabStreamEvent(event)
  if (!normalizedEvent) return bubble

  const message: MessageType = {
    id: Date.now(),
    role: 'assistant',
    content: bubble.content,
    isStreaming: bubble.isStreaming,
    segments: normalizeCollabSegments(bubble.segments, bubble.content, bubble.agentId),
  }
  const updated = applyStreamEventToMessage(message, normalizedEvent)
  const nextSegments = normalizeCollabSegments(updated.segments, updated.content, bubble.agentId)

  return {
    ...bubble,
    content: updated.content,
    segments: nextSegments,
    chainExpanded: bubble.chainExpanded || normalizedEvent.type !== 'content',
  }
}
