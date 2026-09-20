/**
 * 前端开发遥测（路径 A 落地）。
 *
 * 拦截 console.error/warn、fetch、XMLHttpRequest、window error、unhandledrejection，
 * 批量上报到 backend POST /debug/frontend_events，落 ~/.liuruyan/frontend_events.jsonl。
 *
 * AI 用配套工具 read_frontend_events 读，让"console 报啥 / 哪个 fetch 4xx / 组件崩了没"
 * 这些前端 bug 排查不再需要用户手动复制粘贴 DevTools 内容。
 *
 * 设计契约：
 * - console.log/info/debug 故意**不**拦截 —— 太吵，没价值
 * - 所有原生方法**透传**，不影响 DevTools 显示和业务行为
 * - 缓冲：env 上限 1000 条，每 2s 或满 50 条 flush 一次，beforeunload 用 sendBeacon
 * - 反馈环防护：上报自身请求绝不计入 network 事件
 * - opt-out：localStorage.LIURUYAN_DISABLE_TELEMETRY = '1' 即关
 *
 * 后端配套：./backend\routes\debug_routes.py
 * 工具配套：./backend\tools\builtin\frontend_events_tool.py
 *
 * 红线：
 * - 任何上报失败必须**静默吞掉**——遥测自身错决不能引发新错（无限递归就 GG）
 * - 不上报 token / cookie / authorization header（裸 URL 已经够诊断 4xx/5xx）
 * - 单个 msg/stack 在前端就截到合理长度，避免单条爆 100KB
 */

import { API_BASE } from '../api/base'

// ─── 类型 ──────────────────────────────────────────────────────────────────

export interface TelemetryEvent {
  type: 'console' | 'network' | 'error' | 'unhandledrejection'
  level: 'DEBUG' | 'INFO' | 'WARN' | 'ERROR'
  ts: number
  msg?: string
  stack?: string
  url?: string
  method?: string
  status?: number
  duration_ms?: number
  source?: string
  lineno?: number
  colno?: number
  name?: string
  page?: string
}

// ─── 常量 ──────────────────────────────────────────────────────────────────

const ENDPOINT = `${API_BASE}/debug/frontend_events`
const BUFFER_MAX = 1000
const BATCH_SIZE = 50
const FLUSH_INTERVAL_MS = 2000
const MAX_MSG_CHARS = 4000
const MAX_STACK_CHARS = 8000
const OPT_OUT_KEY = 'LIURUYAN_DISABLE_TELEMETRY'

// ─── 单例状态 ──────────────────────────────────────────────────────────────

let initialized = false
let buffer: TelemetryEvent[] = []
let flushTimer: ReturnType<typeof setTimeout> | null = null
let flushing = false
let consecutiveFailures = 0

function isOptOut(): boolean {
  try {
    return localStorage.getItem(OPT_OUT_KEY) === '1'
  } catch {
    return false
  }
}

function truncate(s: unknown, max: number): string | undefined {
  if (s === undefined || s === null) return undefined
  const str = typeof s === 'string' ? s : safeStringify(s)
  if (str.length <= max) return str
  return str.slice(0, max) + '…<TRUNC>'
}

function safeStringify(v: unknown): string {
  try {
    if (v instanceof Error) {
      return v.message + (v.stack ? '\n' + v.stack : '')
    }
    return JSON.stringify(v)
  } catch {
    try {
      return String(v)
    } catch {
      return '<unstringifiable>'
    }
  }
}

function nowPage(): string | undefined {
  try {
    return location.pathname + (location.hash || '')
  } catch {
    return undefined
  }
}

// ─── 缓冲与 flush ──────────────────────────────────────────────────────────

function record(ev: TelemetryEvent): void {
  if (buffer.length >= BUFFER_MAX) {
    // 缓冲爆了：丢最旧的一半（不阻塞业务）
    buffer.splice(0, BUFFER_MAX / 2)
  }
  buffer.push(ev)
  if (buffer.length >= BATCH_SIZE) {
    void flushNow()
  } else if (!flushTimer) {
    flushTimer = setTimeout(() => {
      flushTimer = null
      void flushNow()
    }, FLUSH_INTERVAL_MS)
  }
}

async function flushNow(): Promise<void> {
  if (flushing || buffer.length === 0) return
  flushing = true
  const batch = buffer.splice(0, BATCH_SIZE)
  try {
    // 用 _origFetch 避免被自家 wrap 二次拦截
    const res = await _origFetch.call(window, ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events: batch }),
      // 重要：自家上报请求不能再计入 fetch 拦截器（marker 头）
      // 实际靠 wrap 内部的 url match 判定，header 只是冗余标识
    })
    if (!res.ok) {
      consecutiveFailures++
      // 429 / 5xx：丢弃这批避免无限堆积
      if (res.status === 429 && consecutiveFailures < 3) {
        // 把 batch 塞回缓冲头部，等下个周期
        buffer.unshift(...batch)
      }
    } else {
      consecutiveFailures = 0
    }
  } catch {
    consecutiveFailures++
    // 网络挂了、CORS 等：直接丢，遥测自身决不能阻塞
  } finally {
    flushing = false
  }
}

function flushOnUnload(): void {
  if (buffer.length === 0) return
  try {
    const blob = new Blob([JSON.stringify({ events: buffer.splice(0) })], {
      type: 'application/json',
    })
    navigator.sendBeacon(ENDPOINT, blob)
  } catch {
    /* ignore */
  }
}

// ─── 原生方法引用（在 wrap 之前抓） ────────────────────────────────────────

const _origFetch = window.fetch.bind(window)
const _origConsoleError = console.error.bind(console)
const _origConsoleWarn = console.warn.bind(console)
const _origXhrOpen = XMLHttpRequest.prototype.open
const _origXhrSend = XMLHttpRequest.prototype.send

// ─── console wrap ──────────────────────────────────────────────────────────

function wrapConsole(): void {
  console.error = function (...args: unknown[]) {
    try {
      record({
        type: 'console',
        level: 'ERROR',
        ts: Date.now(),
        msg: truncate(args.map(safeStringify).join(' '), MAX_MSG_CHARS),
        page: nowPage(),
      })
    } catch {
      /* never throw from telemetry */
    }
    _origConsoleError(...args)
  }
  console.warn = function (...args: unknown[]) {
    try {
      record({
        type: 'console',
        level: 'WARN',
        ts: Date.now(),
        msg: truncate(args.map(safeStringify).join(' '), MAX_MSG_CHARS),
        page: nowPage(),
      })
    } catch {
      /* never throw from telemetry */
    }
    _origConsoleWarn(...args)
  }
}

// ─── fetch wrap ────────────────────────────────────────────────────────────

function wrapFetch(): void {
  window.fetch = async function (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.toString()
          : input.url
    // 反馈环防护：上报自身请求不计入 telemetry
    if (url.endsWith('/debug/frontend_events')) {
      return _origFetch(input, init)
    }
    const method = (init?.method ?? (typeof input === 'object' && 'method' in input ? input.method : 'GET')) || 'GET'
    const start = performance.now()
    try {
      const res = await _origFetch(input, init)
      const dur = Math.round(performance.now() - start)
      const lvl: TelemetryEvent['level'] = res.status >= 500 ? 'ERROR' : res.status >= 400 ? 'WARN' : 'INFO'
      try {
        record({
          type: 'network',
          level: lvl,
          ts: Date.now(),
          method: method.toUpperCase(),
          url: truncate(url, 1500),
          status: res.status,
          duration_ms: dur,
          page: nowPage(),
        })
      } catch {
        /* ignore */
      }
      return res
    } catch (err) {
      const dur = Math.round(performance.now() - start)
      try {
        record({
          type: 'network',
          level: 'ERROR',
          ts: Date.now(),
          method: method.toUpperCase(),
          url: truncate(url, 1500),
          status: 0,
          duration_ms: dur,
          msg: truncate(safeStringify(err), MAX_MSG_CHARS),
          page: nowPage(),
        })
      } catch {
        /* ignore */
      }
      throw err
    }
  }
}

// ─── XHR wrap ──────────────────────────────────────────────────────────────

interface _XhrMeta {
  method: string
  url: string
  start: number
}

function wrapXHR(): void {
  // 把 method/url 缓在实例上
  XMLHttpRequest.prototype.open = function (
    this: XMLHttpRequest & { __lryMeta?: _XhrMeta },
    method: string,
    url: string | URL,
    ...rest: unknown[]
  ): void {
    try {
      this.__lryMeta = {
        method: (method || 'GET').toUpperCase(),
        url: typeof url === 'string' ? url : url.toString(),
        start: 0,
      }
    } catch {
      /* ignore */
    }
    // @ts-expect-error variadic forwarding
    return _origXhrOpen.call(this, method, url, ...rest)
  } as typeof XMLHttpRequest.prototype.open

  XMLHttpRequest.prototype.send = function (
    this: XMLHttpRequest & { __lryMeta?: _XhrMeta },
    body?: Document | XMLHttpRequestBodyInit | null,
  ): void {
    const meta = this.__lryMeta
    if (meta) {
      meta.start = performance.now()
      // 反馈环：自家上报走 fetch 不走 XHR，但留个保险
      if (!meta.url.endsWith('/debug/frontend_events')) {
        const onDone = () => {
          try {
            const dur = Math.round(performance.now() - meta.start)
            const status = this.status || 0
            const lvl: TelemetryEvent['level'] =
              status >= 500 || status === 0 ? 'ERROR' : status >= 400 ? 'WARN' : 'INFO'
            record({
              type: 'network',
              level: lvl,
              ts: Date.now(),
              method: meta.method,
              url: truncate(meta.url, 1500),
              status,
              duration_ms: dur,
              source: 'xhr',
              page: nowPage(),
            })
          } catch {
            /* ignore */
          }
        }
        this.addEventListener('loadend', onDone, { once: true })
      }
    }
    return _origXhrSend.call(this, body as XMLHttpRequestBodyInit | null)
  } as typeof XMLHttpRequest.prototype.send
}

// ─── 全局 error / unhandledrejection ──────────────────────────────────────

function wrapGlobalErrors(): void {
  window.addEventListener('error', (ev: ErrorEvent) => {
    try {
      record({
        type: 'error',
        level: 'ERROR',
        ts: Date.now(),
        msg: truncate(ev.message || safeStringify(ev.error), MAX_MSG_CHARS),
        stack: truncate(ev.error?.stack, MAX_STACK_CHARS),
        source: ev.filename,
        lineno: ev.lineno,
        colno: ev.colno,
        name: ev.error?.name,
        page: nowPage(),
      })
    } catch {
      /* ignore */
    }
  })

  window.addEventListener('unhandledrejection', (ev: PromiseRejectionEvent) => {
    try {
      const reason = ev.reason
      record({
        type: 'unhandledrejection',
        level: 'ERROR',
        ts: Date.now(),
        msg: truncate(safeStringify(reason), MAX_MSG_CHARS),
        stack: truncate(reason instanceof Error ? reason.stack : undefined, MAX_STACK_CHARS),
        name: reason instanceof Error ? reason.name : undefined,
        page: nowPage(),
      })
    } catch {
      /* ignore */
    }
  })
}

// ─── 入口 ──────────────────────────────────────────────────────────────────

/**
 * 启动遥测。idempotent，重复调用安全。
 * 走 boot 加载（main.tsx 顶部），不需要等后端 ready。
 */
export function initDevTelemetry(): void {
  if (initialized) return
  if (typeof window === 'undefined') return
  if (isOptOut()) return
  initialized = true

  try {
    wrapConsole()
    wrapFetch()
    wrapXHR()
    wrapGlobalErrors()
    window.addEventListener('beforeunload', flushOnUnload)
    // 暴露一个手动 flush 给 console，方便排查
    ;(window as unknown as { __lryFlush?: () => Promise<void> }).__lryFlush = flushNow
  } catch {
    // boot 阶段错都吃，永远不要让 telemetry 阻塞 React mount
  }
}

/** 测试用：清空缓冲 + 重置标记。生产代码不要调。 */
export function _resetTelemetryForTest(): void {
  buffer = []
  if (flushTimer) {
    clearTimeout(flushTimer)
    flushTimer = null
  }
  flushing = false
  consecutiveFailures = 0
  initialized = false
}
