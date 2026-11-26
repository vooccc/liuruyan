/**
 * timerRegistry — module-level 单例，按 (namespace, sid) 索引的 setTimeout 注册表。
 * 不可序列化 → 不进 store，跟 SPA lifecycle 一致，永不蒸发；可正常 reschedule。
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
