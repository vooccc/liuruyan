/**
 * useSessionTasks — 拉一个 session 的任务清单 + 订阅 tasks_changed 实时刷新
 *
 * 数据流：
 *   mount → fetch /tasks?session_id= → setTasks
 *   订阅 subscribeTasksChanged(sid, refetch) → SSE 来事件 → refetch
 *   60s 兜底轮询（SSE 断连保险，比 cron 30s 拉长，tasks 没"按时触发"刚需）
 *   unmount → cleanup（dispose listener + clear interval）
 *
 * 状态：纯 store 驱动，本 hook 不持有 useState，避免双源真值。
 *
 * 范式参考：CronJobsPanel.tsx:128-187（SSE 订阅 + agent_id filter + dedup）
 *           handover §4.2/4.3/4.4 三条 SSE 协议陷阱已在 useGlobalEventStream
 *           侧统一处理（event_id=0 / addEventListener / agent_id），本 hook
 *           只关心 sid 维度的拉取。
 */

import { useEffect, useCallback, useRef } from 'react'
import { API_BASE } from '../api/base'
import { startPolling } from '../lib/pollingLoop'
import { useTasksStore, subscribeTasksChanged, type Task } from '../stores/tasksStore'

const POLL_INTERVAL_MS = 60_000

// 稳定空数组：selector 缺失分支必须返回同一引用，否则 useSyncExternalStore
// 每次渲染都拿到新 [] → 触发新渲染 → 死循环（"getSnapshot should be cached"）。
const EMPTY_TASKS: Task[] = []

interface UseSessionTasksResult {
  tasks: Task[]
  refetch: () => Promise<void>
}

export function useSessionTasks(sid: string | null | undefined): UseSessionTasksResult {
  const tasks = useTasksStore((s) => (sid ? s.tasksBySession[sid] : undefined)) ?? EMPTY_TASKS
  const setTasks = useTasksStore((s) => s.setTasks)

  // 把 setTasks 装进 ref，避免 refetch 闭包频繁重建
  const setTasksRef = useRef(setTasks)
  setTasksRef.current = setTasks

  const refetch = useCallback(async () => {
    if (!sid) return
    try {
      const res = await fetch(`${API_BASE}/tasks?session_id=${encodeURIComponent(sid)}`)
      if (!res.ok) return
      const data = await res.json()
      if (data?.success && Array.isArray(data.tasks)) {
        setTasksRef.current(sid, data.tasks)
      }
    } catch (e) {
      // 静默：60s 兜底会再尝试；SSE 也可能恢复
      console.debug('[useSessionTasks] refetch failed:', e)
    }
  }, [sid])

  useEffect(() => {
    if (!sid) return

    // 初次拉取（即使 store 已有缓存，也校准一次）
    void refetch()

    // 订阅 tasks_changed
    const dispose = subscribeTasksChanged(sid, () => { void refetch() })

    // 60s 兜底轮询（SSE 断连保险，不重叠：上一次没回来就跳过这一 tick）
    const stopPolling = startPolling(refetch, POLL_INTERVAL_MS, { immediate: false })

    return () => {
      dispose()
      stopPolling()
    }
  }, [sid, refetch])

  return { tasks, refetch }
}
