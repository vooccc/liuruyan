/**
 * schedulerPanoramaCache — module-level 单例缓存，给 task / plan detail 加去重层。
 *
 * 设计动机（P1.4c-3，2026-05-23 架构重构）：
 *
 *   原本在 `Chat` 组件里：
 *     const schedulerPanoramaTaskCacheRef = useRef<Record<task_id, ...>>({})
 *     const schedulerPanoramaPlanCacheRef = useRef<Record<plan_id, ...>>({})
 *
 *   两个缓存按 task_id / plan_id 索引（task_id 和 plan_id 都是 backend
 *   分配的全局唯一 id），**不是 per-sid 状态**——同一个任务跨多 session 共享。
 *   useChatSubmitFlow 也强调过：
 *   "schedulerPanorama task / plan cache 是按
 *    task_id / plan_id 的全局缓存，用于避免每
 *    cycle 重 fetch detail。"
 *
 *   既然不是 per-sid，更不该放在 Chat 组件 useRef 里——Chat unmount → 缓存全丢
 *   → polling 重 mount 后第一 cycle 把 backend 所有 task / plan detail 重新拉一次，
 *   带宽 / DB 双爆。module-level Map 跟 SPA lifecycle 一致，缓存永不蒸发。
 *
 * key 设计：
 *   - taskCache: key = `task_id`（task_id 全局唯一）
 *   - planCache:   key = `plan_id` （plan.plan_id 全局唯一）
 *
 * 失效策略：
 *   - taskCache: 按 signature（getWorkerUpdateSignature 派生）失效——signature 变
 *     就视为缓存过期，重新计算 snapshot。
 *   - planCache: 按 updatedAt 失效——只要 backend updatedAt 没变就用 cache。
 *
 * 内存：
 *   - 不主动清理。task / plan 关闭后 entry 仍留着，预期 1 个用户日常跑几百个
 *     task 上限可控。如果未来发现内存膨胀再加 LRU。
 */

// 类型从 useChatSubmitFlow.ts 抽出来共享，作为公共契约 export。
// 用 `import type` 引入（编译期 erase，无运行时循环依赖）。
import type { RuntimePlan } from '../api/chat'
import type { SchedulerPanoramaWorkerSnapshot } from '../components/schedulerPanorama'

export interface SchedulerPanoramaWorkerCacheEntry {
  signature: string
  snapshot: SchedulerPanoramaWorkerSnapshot
}

export interface SchedulerPanoramaPlanCacheEntry {
  updatedAt: string
  detail: RuntimePlan
}

const workerCache = new Map<string, SchedulerPanoramaWorkerCacheEntry>()
const planCache = new Map<string, SchedulerPanoramaPlanCacheEntry>()

/**
 * 取 task_id 对应的任务缓存条目（含 signature + 已构建好的 snapshot）。
 */
export function getWorkerCacheEntry(taskId: string): SchedulerPanoramaWorkerCacheEntry | undefined {
  if (!taskId) return undefined
  return workerCache.get(taskId)
}

/**
 * 写 / 更新任务缓存条目。callsite 负责 signature 计算与 snapshot 构建。
 */
export function setWorkerCacheEntry(taskId: string, entry: SchedulerPanoramaWorkerCacheEntry): void {
  if (!taskId) return
  workerCache.set(taskId, entry)
}

/**
 * 取 plan_id 对应的 plan 缓存条目（含 updatedAt + detail）。
 */
export function getPlanCacheEntry(planId: string): SchedulerPanoramaPlanCacheEntry | undefined {
  if (!planId) return undefined
  return planCache.get(planId)
}

/**
 * 写 / 更新 plan 缓存条目。
 */
export function setPlanCacheEntry(planId: string, entry: SchedulerPanoramaPlanCacheEntry): void {
  if (!planId) return
  planCache.set(planId, entry)
}

/**
 * 测试用：清空所有缓存（生产代码不应调用）。
 */
export function __clearAllPanoramaCaches(): void {
  workerCache.clear()
  planCache.clear()
}
