import { useCallback, useRef, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import type { Message as MessageType } from '../api/chat'
import { useAppStore, type ChatSessionMessageMeta } from '../stores/useAppStore'
import { mergeTimelineItems, type ChatTimelineItem, type RitualMarker } from './chatExternalHandle'

/**
 * P1.2（架构重构 2026-05-23）：类型上提到 store；这里 re-export 保持老 import 路径兼容。
 */
export type { ChatSessionMessageMeta }

interface UseChatSessionRuntimeParams {
  onLoadingChange?: (sessionId: string | null, loading: boolean) => void
  setTimelineItems: Dispatch<SetStateAction<ChatTimelineItem[]>>
}

export interface ChatSessionRuntime {
  sessionIdRef: MutableRefObject<string>
  /**
   * P1.1（架构重构 2026-05-23）：sessionLoadingMapRef 已退场。
   * loading 状态迁到 useAppStore.sessions[sid].loading（module-level、跟 Chat
   * lifecycle 解耦）。读用 `useAppStore.getState().sessions[sid]?.loading`，写经
   * setSessionLoading callback（向后兼容签名，内部走 store action）。
   *
   * P1.2（架构重构 2026-05-23）：sessionMessageMetaMapRef 已退场。
   * meta 迁到 useAppStore.sessions[sid].messageMeta；读经 readSessionMessageMeta helper，
   * 写经 writeSessionMessageMeta helper（封装 store action）。
   *
   * P1.3（架构重构 2026-05-23）：sessionAbortMapRef 已退场。
   * AbortController 迁到 module-level `src/lib/streamRegistry.ts`（不可序列化，不进 store）；
   * callsite 调 registerStream / abortStream / removeStream，跨 Chat unmount 持久。
   */
  /**
   * P1.4a（架构重构 2026-05-23）：externalStreamMessageIdsRef 已退场。
   * 迁到 useAppStore.sessions[sid].externalStreamMessageIds[streamId]；chatExternalHandle
   * 4 个 worker handler 改调 readSession/writeSession/removeSession Helpers。
   *
   * P1.4b（架构重构 2026-05-23）：sessionTimelineMarkersMapRef 已退场。
   * markers 迁到 useAppStore.sessions[sid].timelineMarkers；syncTimelineItems /
   * updateSessionTimelineMarkers 直接读写 store。
   */
  setSessionLoading: (sid: string, loading: boolean) => void
  updateSessionMessages: (sid: string, updater: (prev: MessageType[]) => MessageType[]) => void
  syncTimelineItems: (sid: string) => void
  updateSessionTimelineMarkers: (sid: string, updater: (prev: RitualMarker[]) => RitualMarker[]) => void
}

/**
 * Phase 1B-2 阶段 D：sessionMessagesMapRef ref 层已删，store 是 chatMessages 唯一真值。
 * 读用 selector 或 useAppStore.getState()；写经 setSessionChatMessages。
 *
 * 跨模块共用的纯函数读 helper，避免 callsite 散落 useAppStore.getState() 拼接。
 */
export function readSessionChatMessages(sid: string): MessageType[] {
  if (!sid) return []
  return useAppStore.getState().sessions[sid]?.chatMessages ?? []
}

/** 写 helper：把 updater(prev) 的结果写进 store，prev 直接从 store 取。 */
export function writeSessionChatMessages(
  sid: string,
  updater: (prev: MessageType[]) => MessageType[],
) {
  if (!sid) return
  const prev = readSessionChatMessages(sid)
  useAppStore.getState().setSessionChatMessages(sid, updater(prev))
}

/**
 * P1.2（架构重构 2026-05-23）：读 helper —— 取代 sessionMessageMetaMapRef.current[sid]。
 */
export function readSessionMessageMeta(sid: string): ChatSessionMessageMeta | undefined {
  if (!sid) return undefined
  return useAppStore.getState().sessions[sid]?.messageMeta
}

/**
 * P1.2（架构重构 2026-05-23）：写 helper —— 取代 sessionMessageMetaMapRef.current[sid] = ...。
 * 传 null 清除（取代 delete sessionMessageMetaMapRef.current[sid]）。
 */
export function writeSessionMessageMeta(sid: string, meta: ChatSessionMessageMeta | null): void {
  if (!sid) return
  useAppStore.getState().setSessionMessageMeta(sid, meta)
}

/**
 * P1.4a（架构重构 2026-05-23）：读 worker / dispatch 外部流 streamId 对应的 messageId。
 * 取代 `externalStreamMessageIdsRef.current[getExternalStreamKey(sid, streamId)]`。
 */
export function readSessionExternalStreamMessageId(sid: string, streamId: string): number | undefined {
  if (!sid || !streamId) return undefined
  return useAppStore.getState().sessions[sid]?.externalStreamMessageIds?.[streamId]
}

/**
 * P1.4a：写 streamId → messageId 映射。
 * 取代 `externalStreamMessageIdsRef.current[key] = messageId`。
 */
export function writeSessionExternalStreamMessageId(sid: string, streamId: string, messageId: number): void {
  if (!sid || !streamId) return
  useAppStore.getState().setSessionExternalStreamMessageId(sid, streamId, messageId)
}

/**
 * P1.4a：清除 streamId 映射（终态事件后）。
 * 取代 `delete externalStreamMessageIdsRef.current[key]`。
 */
export function removeSessionExternalStreamMessageId(sid: string, streamId: string): void {
  if (!sid || !streamId) return
  useAppStore.getState().removeSessionExternalStreamMessageId(sid, streamId)
}

export function useChatSessionRuntime({
  onLoadingChange,
  setTimelineItems,
}: UseChatSessionRuntimeParams): ChatSessionRuntime {
  const sessionIdRef = useRef<string>('')

  const setSessionLoading = useCallback((sid: string, loading: boolean) => {
    // P1.1（架构重构 2026-05-23）：写 store 而不是组件内 ref，跟 Chat lifecycle 解耦。
    useAppStore.getState().setSessionLoading(sid, loading)
    onLoadingChange?.(sid, loading)
  }, [onLoadingChange])

  const updateSessionMessages = useCallback((sid: string, updater: (prev: MessageType[]) => MessageType[]) => {
    writeSessionChatMessages(sid, updater)
  }, [])

  const syncTimelineItems = useCallback((sid: string) => {
    // P1.4b（架构重构 2026-05-23）：markers 改读 store，不再依赖组件内 ref。
    const nextMessages = readSessionChatMessages(sid)
    const nextMarkers = useAppStore.getState().sessions[sid]?.timelineMarkers || []
    const merged = mergeTimelineItems(nextMessages, nextMarkers)
    if (sessionIdRef.current === sid) {
      setTimelineItems(merged)
    }
  }, [setTimelineItems])

  const updateSessionTimelineMarkers = useCallback((sid: string, updater: (prev: RitualMarker[]) => RitualMarker[]) => {
    // P1.4b：读取 store 现状 → updater → 写回 store。
    const currentCache = useAppStore.getState().sessions[sid]?.timelineMarkers || []
    const nextCache = updater(currentCache)
    useAppStore.getState().setSessionTimelineMarkers(sid, nextCache)
    syncTimelineItems(sid)
  }, [syncTimelineItems])

  return {
    sessionIdRef,
    setSessionLoading,
    updateSessionMessages,
    syncTimelineItems,
    updateSessionTimelineMarkers,
  }
}
