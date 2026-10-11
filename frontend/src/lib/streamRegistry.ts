/**
 * streamRegistry — module-level 单例，按 sid 索引 in-flight chat 流。
 *
 * 取代之前的 `sessionAbortMapRef`（在 Chat 组件 useRef 里），后者跟 Chat lifecycle
 * 绑定：Chat unmount → ref 蒸发 → 失去对正在跑的 fetch 的控制权 → 停止按钮失效、
 * 重新 mount 后无法 abort 之前的请求。
 *
 * 设计参考 references/provider-models
 * AbortController 不能进 Redux/Zustand store（不可序列化、Immer 也不喜欢），但能放
 * 在 module-level Map 里。组件只调 API，不直接持有 controller。
 *
 * P1.3（架构重构 2026-05-23）落地。
 */

export interface StreamEntry {
  /** 用于 abort 该 stream 的 fetch / generator */
  controller: AbortController
  /** 本轮提交时分配的 assistant message id（前端 closure 捕获用） */
  assistantMessageId: number
  /** 注册时间（ms epoch）。便于诊断"卡了多久"。 */
  startedAt: number
}

// module-level Map — 永生、跨组件 mount/unmount 持久。
const streams = new Map<string, StreamEntry>()

/**
 * 注册 in-flight 流。重复 register 同 sid 会用新 controller 覆盖旧 entry，
 * 但**不会**自动 abort 旧 controller —— 由 callsite 负责（一般 handleStop / 提交前
 * 都会显式 abort）。
 */
export function registerStream(
  sid: string,
  entry: Omit<StreamEntry, 'startedAt'>,
): void {
  if (!sid) return
  streams.set(sid, { ...entry, startedAt: Date.now() })
}

/**
 * 触发 sid 对应 stream 的 abort 并移除注册。
 * @returns 是否真的有 stream 被 abort（无注册时返 false）。
 */
export function abortStream(sid: string, reason?: string): boolean {
  if (!sid) return false
  const entry = streams.get(sid)
  if (!entry) return false
  try {
    entry.controller.abort(reason)
  } catch {
    // controller.abort 不抛，这里只是防御
  }
  streams.delete(sid)
  return true
}

/**
 * 移除注册（不 abort，假定流已自然结束 / [DONE] / error 路径已处理）。
 * onComplete / catch 路径调用。
 */
export function removeStream(sid: string): void {
  if (!sid) return
  streams.delete(sid)
}

/** 取注册项（一般用于诊断 / 取 assistantMessageId）。 */
export function getStream(sid: string): StreamEntry | undefined {
  if (!sid) return undefined
  return streams.get(sid)
}

/** 该 sid 是否有 in-flight 流。 */
export function hasInFlightStream(sid: string): boolean {
  if (!sid) return false
  return streams.has(sid)
}

/**
 * 仅诊断 / 测试用：清空所有注册。生产 callsite 应该走 abortStream / removeStream。
 */
export function _resetStreamRegistryForTest(): void {
  streams.clear()
}
