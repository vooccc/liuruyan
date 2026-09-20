/**
 * 子代理实时事件总线（纯前端，进程内）
 *
 * 背景：后端已通过主 SSE 流把 subagent_* 事件推给前端。sendMessage 解析到这些
 * 事件后调用 emit()，SubAgentCard 用 subscribe() 订阅自己 subagent_id 的更新。
 *
 * 不走 EventSource 的原因：后端已经不再提供 /api/sse/{subagent_id} 端点；
 * 主 SSE 单通道到达即分发，避免多连接和 race condition。
 */

type SubAgentEvent = {
  subagent_id?: string
  type?: string      // 'system_notice'
  kind?: string      // 'subagent_thinking' / 'subagent_tool_call' / ...
  payload?: any
  event_id?: number
  [key: string]: any
}

type Listener = (event: SubAgentEvent) => void

/**
 * 单个 subagent 历史事件上限。封顶防内存泄漏；超过后只保留尾部最新条目。
 * 200 条够覆盖典型 18 工具 + 间隙 thinking 事件，用户重 mount 卡片仍能复原全貌。
 */
const HISTORY_PER_AGENT_LIMIT = 200
/**
 * 全局保留多少个 subagent 的历史。每多 1 个就驱逐最早的。
 * 30 个够覆盖一次会话内所有 subagent 启动，旧会话切换不会撑大内存。
 */
const HISTORY_AGENT_LIMIT = 30

class SubAgentLiveBus {
  private listeners: Map<string, Set<Listener>> = new Map()
  /**
   * 历史事件缓存：按 subagent_id 分桶。
   *
   * 解决问题：SubAgentCard 用 useState 持有 task 状态，组件 unmount/remount
   * （消息列表 re-render / virtualize / 切会话回切）时 useState 重置成初始 'running'
   * 占位，再 subscribe bus 也拿不到已发完的事件 → 用户看到"卡片内容没了"。
   *
   * 修法：emit 时把事件追加到 history；subscribe 时立刻 replay 给新订阅者。
   * 新订阅者按时间顺序消费历史事件，重建完整 task 状态，跟实时事件无缝衔接。
   *
   * 用 Map 而非 Object 是为了保留插入顺序，方便 LRU 驱逐最早的 subagent。
   */
  private history: Map<string, SubAgentEvent[]> = new Map()

  /** 发布一条 subagent 事件；内部按 payload.subagent_id 路由到订阅者 */
  emit(event: SubAgentEvent): void {
    const subagentId: string | undefined =
      event.payload?.subagent_id || event.subagent_id
    if (!subagentId) return

    // 写入历史缓存（先写再分发，订阅者抛错不影响历史）
    let bucket = this.history.get(subagentId)
    if (!bucket) {
      bucket = []
      this.history.set(subagentId, bucket)
      // 全局 LRU：超过 agent 数上限时丢最早的
      if (this.history.size > HISTORY_AGENT_LIMIT) {
        const oldestKey = this.history.keys().next().value
        if (oldestKey && oldestKey !== subagentId) {
          this.history.delete(oldestKey)
        }
      }
    }
    bucket.push(event)
    if (bucket.length > HISTORY_PER_AGENT_LIMIT) {
      bucket.splice(0, bucket.length - HISTORY_PER_AGENT_LIMIT)
    }

    const set = this.listeners.get(subagentId)
    if (!set) return
    for (const fn of set) {
      try {
        fn(event)
      } catch (e) {
        console.warn('[subagentLiveBus] listener error', e)
      }
    }
  }

  /**
   * 订阅指定 subagent_id 的事件。订阅时立刻同步 replay 历史事件给 fn，
   * 然后才注册到实时分发列表——这样订阅者拿到的是「历史 + 实时」的完整流，
   * 重 mount 不丢状态。
   *
   * 返回取消订阅函数。
   */
  subscribe(subagentId: string, fn: Listener): () => void {
    // 1) 先 replay 历史（同步派发，调用方能在当前 tick 把状态重建出来）
    const bucket = this.history.get(subagentId)
    if (bucket && bucket.length > 0) {
      for (const ev of bucket) {
        try {
          fn(ev)
        } catch (e) {
          console.warn('[subagentLiveBus] replay listener error', e)
        }
      }
    }

    // 2) 注册到实时分发
    let set = this.listeners.get(subagentId)
    if (!set) {
      set = new Set()
      this.listeners.set(subagentId, set)
    }
    set.add(fn)
    return () => {
      const s = this.listeners.get(subagentId)
      if (s) {
        s.delete(fn)
        if (s.size === 0) this.listeners.delete(subagentId)
      }
    }
  }
}

export const subagentLiveBus = new SubAgentLiveBus()
export type { SubAgentEvent }
