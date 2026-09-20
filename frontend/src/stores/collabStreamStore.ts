/**
 * collabStreamStore — 议事厅协作流的 per-session 状态桶
 *
 * 设计动机：
 *   CollabChat 是单实例长挂载（App.tsx 切到伙伴视图时也不能 unmount，
 *   否则 fetch reader 被 GC，流断）。但单实例 + 多群并发的语义冲突——
 *   把所有可变态（bubbles / isChatting / hasSession / bubbleSeq / thinkingState
 *   / loadingHistory）塞进组件 useState，切群时新群和旧群会互相覆盖。
 *
 *   解法：用 zustand store 按 sessionKey 分桶。CollabChat 切群只是切 selector
 *   的 key，已开过的群 bucket 仍在 store 里活着；上一群的 SSE 流（在
 *   useCollabSubmitFlow 里仍持有 setBubbles 等闭包）会继续往它自己的 bucket 写，
 *   不会污染新群。切回时 selector 重新读那个 bucket，进度全在。
 *
 * AbortController / collapseTimers 不放 store：它们不是"状态快照"语义，
 *   是带副作用的资源，独立用 module-level Map 持有，按 sessionKey 收集和释放。
 *
 * sessionKey 选择：existingSessionId 为主，新建场景未拿到 session_id 时
 *   退化用 `scene::${scene.id}` 临时 key，session_info 事件到达后由调用方
 *   显式 rekey（迁移 bucket）。
 */
import { create } from 'zustand'
import { immer } from 'zustand/middleware/immer'
import type { CollabBubble } from '../components/CollabMessageItem'

export interface CollabBucket {
  bubbles: CollabBubble[]
  isChatting: boolean
  hasSession: boolean
  /** bubble id 单调递增序列，避免重复 key */
  bubbleSeq: number
  /** agentId → 当前是否仍在 thinking（用于自动收链路 Hook） */
  thinkingState: Record<string, boolean>
  loadingHistory: boolean
}

const emptyBucket = (): CollabBucket => ({
  bubbles: [],
  isChatting: false,
  hasSession: false,
  bubbleSeq: 0,
  thinkingState: {},
  loadingHistory: false,
})

interface CollabStreamState {
  buckets: Record<string, CollabBucket>
}

interface CollabStreamActions {
  ensureBucket: (key: string) => void
  setBubbles: (key: string, bubbles: CollabBubble[]) => void
  appendBubble: (key: string, bubble: CollabBubble) => void
  updateBubbles: (key: string, updater: (bubbles: CollabBubble[]) => CollabBubble[]) => void
  setIsChatting: (key: string, value: boolean) => void
  setHasSession: (key: string, value: boolean) => void
  /** 取下一个序列号；side-effect 写入 store */
  nextBubbleSeq: (key: string) => number
  setThinking: (key: string, agentId: string, value: boolean) => void
  setLoadingHistory: (key: string, value: boolean) => void
  /** 把 fromKey 的 bucket 内容迁到 toKey（新建场景拿到 session_id 后用） */
  rekeyBucket: (fromKey: string, toKey: string) => void
  removeBucket: (key: string) => void
}

export type CollabStreamStore = CollabStreamState & CollabStreamActions

export const useCollabStreamStore = create<CollabStreamStore>()(
  immer((set, get) => ({
    buckets: {},

    ensureBucket: (key) =>
      set((state) => {
        if (!state.buckets[key]) state.buckets[key] = emptyBucket()
      }),

    setBubbles: (key, bubbles) =>
      set((state) => {
        if (!state.buckets[key]) state.buckets[key] = emptyBucket()
        state.buckets[key].bubbles = bubbles
      }),

    appendBubble: (key, bubble) =>
      set((state) => {
        if (!state.buckets[key]) state.buckets[key] = emptyBucket()
        state.buckets[key].bubbles.push(bubble)
      }),

    updateBubbles: (key, updater) =>
      set((state) => {
        if (!state.buckets[key]) state.buckets[key] = emptyBucket()
        state.buckets[key].bubbles = updater(state.buckets[key].bubbles)
      }),

    setIsChatting: (key, value) =>
      set((state) => {
        if (!state.buckets[key]) state.buckets[key] = emptyBucket()
        state.buckets[key].isChatting = value
      }),

    setHasSession: (key, value) =>
      set((state) => {
        if (!state.buckets[key]) state.buckets[key] = emptyBucket()
        state.buckets[key].hasSession = value
      }),

    nextBubbleSeq: (key) => {
      set((state) => {
        if (!state.buckets[key]) state.buckets[key] = emptyBucket()
        state.buckets[key].bubbleSeq += 1
      })
      return get().buckets[key]?.bubbleSeq ?? 1
    },

    setThinking: (key, agentId, value) =>
      set((state) => {
        if (!state.buckets[key]) state.buckets[key] = emptyBucket()
        state.buckets[key].thinkingState[agentId] = value
      }),

    setLoadingHistory: (key, value) =>
      set((state) => {
        if (!state.buckets[key]) state.buckets[key] = emptyBucket()
        state.buckets[key].loadingHistory = value
      }),

    rekeyBucket: (fromKey, toKey) =>
      set((state) => {
        if (fromKey === toKey) return
        const src = state.buckets[fromKey]
        if (!src) return
        // 目标 key 已存在：保留目标，丢弃源（语义上目标是"权威 sessionId"）
        if (state.buckets[toKey]) {
          delete state.buckets[fromKey]
          return
        }
        state.buckets[toKey] = src
        delete state.buckets[fromKey]
      }),

    removeBucket: (key) =>
      set((state) => {
        delete state.buckets[key]
      }),
  })),
)

// ─────── module-level 资源持有（不进 store） ─────────
//
// AbortController 不可序列化、生命周期是"被 abort 一次就废"的副作用资源；
// collapseTimers 是 setTimeout 句柄，同理。放 store 里只会让 immer 抱怨且没任何收益。
// 用 Map<sessionKey, ...> 持有，调用方按 key 收/放。

const abortControllers = new Map<string, AbortController>()

export function getAbortController(key: string): AbortController | null {
  return abortControllers.get(key) ?? null
}

export function setAbortController(key: string, ctrl: AbortController | null) {
  if (ctrl) abortControllers.set(key, ctrl)
  else abortControllers.delete(key)
}

export function abortStream(key: string) {
  const ctrl = abortControllers.get(key)
  if (ctrl) {
    ctrl.abort()
    abortControllers.delete(key)
  }
}

const collapseTimers = new Map<string, Map<string, ReturnType<typeof setTimeout>>>()

export function getCollapseTimers(key: string): Map<string, ReturnType<typeof setTimeout>> {
  let map = collapseTimers.get(key)
  if (!map) {
    map = new Map()
    collapseTimers.set(key, map)
  }
  return map
}

export function clearAllCollapseTimers(key: string) {
  const map = collapseTimers.get(key)
  if (!map) return
  map.forEach((t) => clearTimeout(t))
  map.clear()
}

/** 同步把一个 key 持有的副作用资源 rekey（abort + timers 跟随 bucket 迁） */
export function rekeyResources(fromKey: string, toKey: string) {
  if (fromKey === toKey) return
  const ctrl = abortControllers.get(fromKey)
  if (ctrl) {
    abortControllers.delete(fromKey)
    if (!abortControllers.has(toKey)) abortControllers.set(toKey, ctrl)
    else ctrl.abort() // 目标已有流，弃旧
  }
  const timers = collapseTimers.get(fromKey)
  if (timers) {
    collapseTimers.delete(fromKey)
    if (!collapseTimers.has(toKey)) collapseTimers.set(toKey, timers)
    else timers.forEach((t) => clearTimeout(t))
  }
}

// dev-only：暴露 store 到 window 方便 DevTools 检查
if (import.meta.env.DEV) {
  ;(window as unknown as { useCollabStreamStore?: typeof useCollabStreamStore }).useCollabStreamStore = useCollabStreamStore
}
