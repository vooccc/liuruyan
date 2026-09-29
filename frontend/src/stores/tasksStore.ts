/**
 * tasksStore — InlineTasksPanel 的 per-session 任务清单缓存
 *
 * 设计：
 * - keyed by session_id；切换 session 时上一个保留在 store（LRU 5 个），
 *   切回来即时显示缓存、再 fresh fetch 校准。
 * - 不维护增量逻辑。后端 tasks_changed 事件只作为 trigger，hook 收到
 *   后调 fetch /tasks?session_id= 全量替换；store.setTasks 是唯一写入口。
 * - 跨组件订阅"某 sid 的 tasks 变了"靠 module-level dispatcher
 *   (subscribeTasksChanged / notifyTasksChanged)，而不是污染 store
 *   schema 加 signal counter。
 *
 * 范式参考：useAppStore.ts dispatch slice（per-session keyed by sid）
 */

import { create } from 'zustand'

export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'failed' | 'stale'

export interface Task {
  id: string
  title: string
  description: string
  status: TaskStatus
  created_at: number
  started_at?: number | null
  updated_at: number
  metadata?: Record<string, any>
  session_id?: string | null
  agent_id?: string | null
}

const LRU_CAPACITY = 5

interface TasksStore {
  tasksBySession: Record<string, Task[]>
  lastFetchAt: Record<string, number>
  /**
   * 某 sid 进入"全部完成"那一刻的 wall-clock 时间戳。phase 由此值 + Date.now()
   * 计算（fading→receipt→gone），从而 InlineTasksPanel 即使因 Portal target
   * 切换被 unmount/remount，重 mount 时仍能算出对应 phase 而非倒回 normal。
   */
  allDoneAt: Record<string, number>
  /** LRU 顺序：访问时移到末尾；写入超出容量时砍头。 */
  accessOrder: string[]
  setTasks: (sid: string, tasks: Task[]) => void
  getTasks: (sid: string) => Task[]
  clearSession: (sid: string) => void
  /** 标记 sid 进入 allDone（已存在则不覆盖时间戳，保持原相位推进） */
  markAllDone: (sid: string) => void
  /** 清掉 allDone 标记（任何任务回到非 completed 时调用） */
  clearAllDone: (sid: string) => void
}

export const useTasksStore = create<TasksStore>((set, get) => ({
  tasksBySession: {},
  lastFetchAt: {},
  allDoneAt: {},
  accessOrder: [],

  setTasks: (sid, tasks) => set((state) => {
    const nextOrder = state.accessOrder.filter((s) => s !== sid)
    nextOrder.push(sid)

    let nextTasks = { ...state.tasksBySession, [sid]: tasks }
    let nextLastFetch = { ...state.lastFetchAt, [sid]: Date.now() }
    let nextAllDoneAt = { ...state.allDoneAt }
    let finalOrder = nextOrder

    // LRU evict：超出容量砍最旧的
    while (finalOrder.length > LRU_CAPACITY) {
      const evict = finalOrder.shift()!
      delete nextTasks[evict]
      delete nextLastFetch[evict]
      delete nextAllDoneAt[evict]
    }

    return {
      tasksBySession: nextTasks,
      lastFetchAt: nextLastFetch,
      allDoneAt: nextAllDoneAt,
      accessOrder: finalOrder,
    }
  }),

  getTasks: (sid) => get().tasksBySession[sid] ?? [],

  clearSession: (sid) => set((state) => {
    const { [sid]: _, ...rest } = state.tasksBySession
    const { [sid]: __, ...restFetch } = state.lastFetchAt
    const { [sid]: ___, ...restAllDone } = state.allDoneAt
    return {
      tasksBySession: rest,
      lastFetchAt: restFetch,
      allDoneAt: restAllDone,
      accessOrder: state.accessOrder.filter((s) => s !== sid),
    }
  }),

  markAllDone: (sid) => set((state) => {
    if (state.allDoneAt[sid] !== undefined) return state  // 已经标记过，不重置 → 保持相位推进
    return { allDoneAt: { ...state.allDoneAt, [sid]: Date.now() } }
  }),

  clearAllDone: (sid) => set((state) => {
    if (state.allDoneAt[sid] === undefined) return state
    const { [sid]: _, ...rest } = state.allDoneAt
    return { allDoneAt: rest }
  }),
}))


// ─────── tasks_changed 跨组件分发（module singleton）─────────

type ChangeListener = () => void
const sessionListeners = new Map<string, Set<ChangeListener>>()

/** 监听某 sid 的 tasks_changed 事件；返回 dispose 函数。 */
export function subscribeTasksChanged(sid: string, listener: ChangeListener): () => void {
  if (!sessionListeners.has(sid)) sessionListeners.set(sid, new Set())
  sessionListeners.get(sid)!.add(listener)
  return () => {
    const set = sessionListeners.get(sid)
    if (!set) return
    set.delete(listener)
    if (set.size === 0) sessionListeners.delete(sid)
  }
}

/**
 * 触发某 sid 的所有 listener。
 *
 * 特殊语义：sid 为空字符串时（"clear all" 等不带 session 的事件）通知所有
 * 已订阅的 sid，避免遗漏 broadcast 场景。
 */
export function notifyTasksChanged(sid: string): void {
  if (!sid) {
    for (const set of sessionListeners.values()) {
      for (const l of set) {
        try { l() } catch (e) { console.warn('[tasksChanged listener]', e) }
      }
    }
    return
  }
  const set = sessionListeners.get(sid)
  if (!set) return
  for (const l of set) {
    try { l() } catch (e) { console.warn('[tasksChanged listener]', e) }
  }
}
