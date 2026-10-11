import type {
  CollabChatEvent,
  ContentSegment,
  Message as MessageType,
} from '../api/chat'
import { isCollabRuntimeEventType } from '../api/chat'
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

export interface CollabToolChip {
  name: string
  done: boolean
}

/**
 * 内联工具摘要（P1 2026-09-29）：气泡正文下方常驻的小 chips 数据源。
 * 让"角色刚才动了手"不展开链路也可见；status 按 tool_use_id 配对 tool_result。
 */
export function summarizeCollabTools(segments: ContentSegment[]): CollabToolChip[] {
  const chips: CollabToolChip[] = []
  for (const segment of segments) {
    if (segment.type !== 'tool_call') continue
    const id = segment.tool_use_id || ''
    const done = segments.some((candidate) => (
      candidate.type === 'tool_result' && (candidate.tool_use_id || '') === id
    ))
    chips.push({
      name: segment.tool_name || segment.toolCall?.name || segment.name || '工具',
      done,
    })
  }
  return chips
}

export function normalizeCollabStreamEvent(event: CollabChatEvent): ChatStreamLikeEvent | null {
  // 守卫与入口（CollabChat handleCollabEvent）共用同一张 COLLAB_RUNTIME_EVENT_TYPES
  // 注册表，两层天然一致，不会再出现"入口放行、这里不认识"的漂移。
  if (!isCollabRuntimeEventType(event.type)) return null

  if (event.type === 'agent_content') {
    return { ...event, type: 'content' }
  }

  return event as ChatStreamLikeEvent
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
