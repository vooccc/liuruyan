/**
 * timerRegistry — module-level 单例，按 (namespace, sid) 索引的 setTimeout 注册表。
 *
 * 设计动机（P1.4c-2，2026-05-23 架构重构）：
 *
 *   原本在 `Chat` 组件里：
 *     const routedPollerMapRef = useRef<Record<sid, setTimeout id>>({})
 *     const routedProgressClearTimerMapRef = useRef<Record<sid, setTimeout id>>({})
 *
 *   Chat unmount → 整个 ref Map 蒸发 → 已 schedule 的 setTimeout 句柄丢失：
 *     1) route progress poller 仍在跑（setTimeout 句柄不会因为持有者消失就自动取消）
 *        → 但前端无法再 clearTimeout 它 → 多 cycle 后无主轮询继续打 backend
 *     2) routedProgressClearTimer 同理 → route progress 占位消息延时清除路径仍会执行，
 *        但前端无法 reschedule / cancel
 *
 *   迁到 module-level Map 后：跟 SPA lifecycle 一致，永不蒸发；可正常 reschedule。
 *
 * 设计要点：
 *   - **(namespace, sid)** 复合 key——namespace 区分不同用途（'routed-poller' vs
 *     'routed-progress-clear'），sid 区分会话。同 namespace 下同 sid 重复 set 会
 *     自动 clear 旧句柄（保证至多一个 active timer）。
 *   - setTimer 自动在 callback 执行前从 registry 移除自身（避免泄漏）。
 *   - callback 抛错时不抓——保持跟原 window.setTimeout 一致语义。
 *
 * 不可序列化 → 不进 store。
 * 设计参考 streamRegistry.ts（同样存不可序列化 handle 的 module-level Map）。
 */

const timers = new Map<string, number>()

function makeKey(namespace: string, sid: string): string {
  return `${namespace}::${sid}`
}

/**
 * 注册一个 setTimeout。如果 (namespace, sid) 已有 active timer，先 clear 旧的。
 * callback 执行前会自动从 registry 移除自身。
 */
export function setTimer(
  namespace: string,
  sid: string,
  callback: () => void,
  delayMs: number,
): void {
  if (!namespace || !sid) return
  const key = makeKey(namespace, sid)
  const existing = timers.get(key)
  if (existing !== undefined) {
    window.clearTimeout(existing)
  }
  const id = window.setTimeout(() => {
    timers.delete(key)
    callback()
  }, delayMs)
  timers.set(key, id)
}

/**
 * 查询是否已有 (namespace, sid) 的 active timer。用于"已经在轮询就别重复启动"。
 */
export function hasTimer(namespace: string, sid: string): boolean {
  if (!namespace || !sid) return false
  return timers.has(makeKey(namespace, sid))
}

/**
 * 取消 (namespace, sid) 的 active timer。幂等——没有就 no-op。
 */
export function clearTimer(namespace: string, sid: string): void {
  if (!namespace || !sid) return
  const key = makeKey(namespace, sid)
  const existing = timers.get(key)
  if (existing !== undefined) {
    window.clearTimeout(existing)
    timers.delete(key)
  }
}

/**
 * 取消某个 namespace 下所有 sid 的 timer。极少用——比如 panic recover 时全清。
 */
export function clearAllTimersInNamespace(namespace: string): void {
  if (!namespace) return
  const prefix = `${namespace}::`
  for (const key of timers.keys()) {
    if (key.startsWith(prefix)) {
      const id = timers.get(key)
      if (id !== undefined) window.clearTimeout(id)
      timers.delete(key)
    }
  }
}

// ─── 命名空间常量（callsite 应该用这些，不要散写字面量） ───
export const TIMER_NS_ROUTED_POLLER = 'routed-poller'
export const TIMER_NS_ROUTED_PROGRESS_CLEAR = 'routed-progress-clear'
