import { useEffect } from 'react'
import type { CollabBubble } from './CollabMessageItem'
import { useCollabStreamStore } from '../stores/collabStreamStore'

interface UseCollabChainAutoCollapseParams {
  /** 当前 bucket key（CollabChat 计算）；thinkingState 按 key 分桶持久化在 store */
  sessionKey: string
  bubbles: CollabBubble[]
  clearAutoCollapseTimer: (bubbleId: string) => void
  scheduleAutoCollapse: (bubbleId: string, delay?: number) => void
  hasDetailedChain: (segments: CollabBubble['segments']) => boolean
}

export function useCollabChainAutoCollapse({
  sessionKey,
  bubbles,
  clearAutoCollapseTimer,
  scheduleAutoCollapse,
  hasDetailedChain,
}: UseCollabChainAutoCollapseParams) {
  useEffect(() => {
    // thinkingState 直接读最新 store 快照——不订阅它本身，避免依赖循环。
    // 写回也走 store action（会触发其它订阅者，但本 hook 自己只 read on next bubble change）。
    const storeState = useCollabStreamStore.getState()
    const bucket = storeState.buckets[sessionKey]
    const thinkingState = bucket?.thinkingState ?? {}
    const setThinking = storeState.setThinking
    const activeIds = new Set<string>()
    const nextThinking: Record<string, boolean> = {}

    for (const bubble of bubbles) {
      activeIds.add(bubble.id)
      // 2026-05-22：之前判断 "lastSegment.type === 'thinking' && isStreaming"，
      // 在 thinking → tool_call 过渡里 lastSegment 立刻变成 tool_call，链路被错误折叠
      // → 工具实时输出（CommandStream / AcpDelegate）用户根本看不到。
      //
      // 改为整个 bubble.isStreaming：流期保持展开 + 清 timer，流式结束才调度折叠。
      // 跟单聊"工具调用块在流期始终可见"的体验一致。
      // 注：store 字段名沿用 thinkingState（避免破坏存量数据），语义已扩展到整 bubble 流期。
      const isStreamActive = bubble.isStreaming
      const wasStreamActive = thinkingState[bubble.id] || false

      if (isStreamActive) {
        clearAutoCollapseTimer(bubble.id)
      } else if (wasStreamActive && bubble.chainExpanded && hasDetailedChain(bubble.segments)) {
        scheduleAutoCollapse(bubble.id)
      }

      nextThinking[bubble.id] = isStreamActive
    }

    // 同步写回 thinkingState：只更新本桶的有效 bubble；老 bubble 自然被淘汰
    Object.keys(thinkingState).forEach((bubbleId) => {
      if (!activeIds.has(bubbleId)) clearAutoCollapseTimer(bubbleId)
    })
    // 一次性 set 整个表（diff 由 immer 处理）
    Object.entries(nextThinking).forEach(([bubbleId, value]) => {
      if (thinkingState[bubbleId] !== value) setThinking(sessionKey, bubbleId, value)
    })
    // 老 key 删除（store 没提供 delete-thinking，给个 false 占位即可——
    // 体积上 thinkingState 只在 bubbles 增长时积累，整桶被清时自然 GC）
  }, [sessionKey, bubbles, clearAutoCollapseTimer, scheduleAutoCollapse, hasDetailedChain])
}
