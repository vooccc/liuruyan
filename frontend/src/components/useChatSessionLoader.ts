import { useEffect, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import { getSession, type Session } from '../api/chat'
import { buildSessionMessages } from './chatSessionMessages'
import type { ChatTimelineItem } from './chatExternalHandle'
import { readSessionChatMessages, readSessionMessageMeta } from './useChatSessionRuntime'
import { useAppStore } from '../stores/useAppStore'

interface UseChatSessionLoaderParams {
  currentSession?: Session | null
  sessionIdRef: MutableRefObject<string>
  setIsLoading: Dispatch<SetStateAction<boolean>>
  setTimelineItems: Dispatch<SetStateAction<ChatTimelineItem[]>>
  syncTimelineItems: (sid: string) => void
}

export function useChatSessionLoader({
  currentSession,
  sessionIdRef,
  setIsLoading,
  setTimelineItems,
  syncTimelineItems,
}: UseChatSessionLoaderParams) {
  useEffect(() => {
    const targetSessionId = currentSession?.id || ''
    sessionIdRef.current = targetSessionId
    // P1.1（架构重构 2026-05-23）：loading 真值改读 store。
    // 之前读 sessionLoadingMapRef（Chat 内 useRef，unmount 就丢），导致 Chat 重 mount 时
    // setIsLoading 永远 false → 停止按钮即使 in-flight 也不出现。改读 store 后跨 unmount 持久。
    setIsLoading(useAppStore.getState().sessions[targetSessionId]?.loading || false)

    if (!targetSessionId) {
      setTimelineItems([])
      return
    }

    // Phase 1B-2 阶段 D：store 是 chatMessages 唯一真值，无 ref 反向同步。
    // P1.2（架构重构 2026-05-23）：meta 也改从 store 读，跨 Chat unmount 持久。
    const cachedMessages = readSessionChatMessages(targetSessionId)
    const cachedMeta = readSessionMessageMeta(targetSessionId)
    // bug 修复（2026-05-23）：cache 里若有 isStreaming=true 的 in-flight assistant message，
    // 一律视为 not-fresh，强制 syncSessionSnapshot 拉一次 backend 真值。
    // 场景：用户在主会话流跑到一半切到议事厅，切回时 currentSession 来自 sidebar 缓存（updatedAt 是
    // 切走前的快照），跟 cacheMeta 完全一致 → cacheIsFresh=true → 跳过 syncSessionSnapshot →
    // 用户看到的还是切走那一刻停滞的 in-flight cache（segments 缺尾巴 + 光标一直闪）。再切走切回
    // 一次才看到完整结果，因为 sidebar 轮询（App.tsx:286 listSessions 15s）把 updatedAt 刷新了。
    // 用 isStreaming 作为「需校准」信号比单纯按 updatedAt 严密：只要本地觉得这条还在流就强制重拉，
    // backend 已写盘的话 buildSessionMessages 用 backend 真值替换，否则保留 in-flight cache 不变。
    const hasInFlightStreaming = cachedMessages.some((message) => message.isStreaming === true)
    const cacheIsFresh = Boolean(
      !hasInFlightStreaming &&
      cachedMessages.length > 0 &&
      cachedMeta &&
      cachedMeta.updatedAt === currentSession?.updatedAt &&
      cachedMeta.messageCount === currentSession?.messageCount &&
      cachedMeta.historyRevision === currentSession?.historyRevision &&
      cachedMeta.nextMessageSeq === currentSession?.nextMessageSeq,
    )

    // 只在 cache 新鲜时立即渲染；否则等 syncSessionSnapshot 拉完再渲染（避免闪现空状态）
    if (cacheIsFresh) {
      syncTimelineItems(targetSessionId)
    }

    let cancelled = false

    const applySessionSnapshot = (session: Session) => {
      if (cancelled || sessionIdRef.current !== targetSessionId) return

      const previousMessages = readSessionChatMessages(targetSessionId)
      const previousMeta = readSessionMessageMeta(targetSessionId)
      const nextMessages = buildSessionMessages(session, previousMessages)

      const previousTail = previousMessages[previousMessages.length - 1]
      const nextTail = nextMessages[nextMessages.length - 1]
      const hasMeaningfulChange = (
        previousMeta?.updatedAt !== session.updatedAt
        || previousMeta?.messageCount !== session.messageCount
        || previousMeta?.historyRevision !== session.historyRevision
        || previousMeta?.nextMessageSeq !== session.nextMessageSeq
        || previousMessages.length !== nextMessages.length
        || previousTail?.id !== nextTail?.id
        || previousTail?.message_seq !== nextTail?.message_seq
        || previousTail?.history_revision !== nextTail?.history_revision
        || previousTail?.timestamp !== nextTail?.timestamp
        || previousTail?.content !== nextTail?.content
      )

      if (!hasMeaningfulChange) return

      useAppStore.getState().hydrateSessionChatMessages(targetSessionId, nextMessages, {
        updatedAt: session.updatedAt,
        messageCount: session.messageCount,
        historyRevision: session.historyRevision,
        nextMessageSeq: session.nextMessageSeq,
      })

      if (!cancelled && sessionIdRef.current === targetSessionId) {
        syncTimelineItems(targetSessionId)
      }
    }

    const syncSessionSnapshot = async () => {
      if (cancelled) return
      try {
        const session = await getSession(targetSessionId)
        if (cancelled || sessionIdRef.current !== targetSessionId) return
        applySessionSnapshot(session)
      } catch (error) {
        console.debug('[useChatSessionLoader] Error in syncSessionSnapshot:', error)
        return
      }
    }

    async function loadMessages() {
      if (cacheIsFresh) return

      // 性能优化：如果 currentSession 已经带有完整 messages 数据（例如侧栏刚拉取的 liveSession），
      // 直接就地水合渲染，免去二次网络请求，实现 0ms 瞬间秒开！
      if (
        currentSession?.id === targetSessionId &&
        Array.isArray(currentSession.messages) &&
        currentSession.messages.length > 0
      ) {
        applySessionSnapshot(currentSession)
        return
      }

      await syncSessionSnapshot()
    }

    loadMessages()

    return () => {
      cancelled = true
    }
  }, [
    currentSession?.id,
    setIsLoading,
    setTimelineItems,
    syncTimelineItems,
  ])
}
