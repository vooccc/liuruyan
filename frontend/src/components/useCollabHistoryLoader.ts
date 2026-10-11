import { useEffect } from 'react'
import { type Agent, getSession, type ContentSegment } from '../api/chat'
import { parseStoredRuntimeMessageContent } from './runtimeMessageContent'
import { extractTextFromSegments } from './chatStreamMessage'
import type { CollabBubble } from './CollabMessageItem'
import { COLLAB_UI_TEXT } from './collabUiText'
import { useCollabStreamStore } from '../stores/collabStreamStore'

interface UseCollabHistoryLoaderParams {
  existingSessionId?: string
  /** 当前 bucket key（由 CollabChat 计算），决定历史写到哪个桶 */
  sessionKey: string
  agents: Agent[]
  createBubbleId: (prefix: string) => string
  setBubbles: (key: string, bubbles: CollabBubble[]) => void
  setLoadingHistory: (key: string, value: boolean) => void
  buildTextSegment: (content: string, agentId?: string) => ContentSegment
  normalizeSegments: (segments: ContentSegment[] | undefined, fallbackText: string, agentId?: string) => ContentSegment[]
  /** 2026-09-30：编辑/删除消息后由 CollabChat 自增，强制重拉历史（配合先 setBubbles([])） */
  reloadTick?: number
}

export function useCollabHistoryLoader({
  existingSessionId,
  sessionKey,
  agents,
  createBubbleId,
  setBubbles,
  setLoadingHistory,
  buildTextSegment,
  normalizeSegments,
  reloadTick = 0,
}: UseCollabHistoryLoaderParams) {
  useEffect(() => {
    if (!existingSessionId) return

    // 关键防 clobber：bucket 里已有 bubbles（streaming 中 / 上次留下的）就不要重拉历史。
    //
    // 背景：CollabChat 单实例长挂载（流式中切走能继续在后台跑）。用户切回原群时，
    // sessionKey/agents 等 deps 变化会让这条 effect 再触发一次。如果照常拉取历史
    // 然后 setBubbles 全量覆盖，后端那时往往还没把流式内容持久化，覆盖结果是
    // bubbles 突然变回"切走前那一刻"或更早的快照，等流结束本地 update 才把
    // 最终内容拼回来——用户看到的就是"切回来一片空，过会儿突然冒结果"。
    //
    // 规则：bucket 已有 bubbles → 信任内存里的 live 状态，跳过加载。
    //       想强制刷新历史的话调用方需要先 removeBucket。
    const existingBucket = useCollabStreamStore.getState().buckets[sessionKey]
    if (existingBucket && existingBucket.bubbles.length > 0) {
      return
    }

    let cancelled = false
    // 闭包捕获本次 effect 的 sessionKey：进行中用户可能切到别的群，外部 sessionKey
    // 已经变；本次 fetch 写回的目标必须仍是触发这次 effect 的那个 key。
    const capturedKey = sessionKey
    setLoadingHistory(capturedKey, true)

    getSession(existingSessionId)
      .then((session) => {
        if (cancelled || !session?.messages) return

        const historyBubbles: CollabBubble[] = []
        const lines = session.messages as Array<{ role: string; content: string; segments?: ContentSegment[] }>
        for (let msgIndex = 0; msgIndex < lines.length; msgIndex += 1) {
          const message = lines[msgIndex]
          if (message.role === 'user') {
            historyBubbles.push({
              id: createBubbleId('history-user'),
              agentId: '__user__',
              agentName: COLLAB_UI_TEXT.userName,
              avatar: COLLAB_UI_TEXT.userAvatar,
              content: message.content,
              isStreaming: false,
              segments: [buildTextSegment(message.content, '__user__')],
              chainExpanded: false,
              // 会话内的可见行下标：编辑/删除操作按它定位后端消息（2026-09-30）
              sessionMessageIndex: msgIndex,
            })
            continue
          }

          if (message.role !== 'assistant') continue

          const parsed = message.segments && message.segments.length > 0
            ? {
                text: extractTextFromSegments(message.segments),
                segments: message.segments,
                agentId: message.segments.find((segment) => segment.agent_id)?.agent_id,
              }
            : parseStoredRuntimeMessageContent(message.content)

          const agentId = parsed.agentId || message.segments?.find((segment) => segment.agent_id)?.agent_id || '__assistant__'
          const text = parsed.text || message.content || ''
          const segments = normalizeSegments(parsed.segments, text, agentId)
          const agent = agents.find((item) => item.id === agentId)

          historyBubbles.push({
            id: createBubbleId('history-agent'),
            agentId,
            agentName: agent?.name || COLLAB_UI_TEXT.agentName,
            avatar: agent?.avatar || COLLAB_UI_TEXT.agentAvatar,
            avatarUrl: agent?.avatar_url,
            content: text,
            isStreaming: false,
            segments,
            chainExpanded: false,
            // 同一轮所有角色气泡共享这条助手消息的行下标（删除=删整轮发言）
            sessionMessageIndex: msgIndex,
          })
        }

        if (!cancelled) setBubbles(capturedKey, historyBubbles)
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoadingHistory(capturedKey, false)
      })

    return () => {
      cancelled = true
    }
  }, [existingSessionId, sessionKey, agents, createBubbleId, setBubbles, setLoadingHistory, buildTextSegment, normalizeSegments, reloadTick])
}
