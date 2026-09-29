// Bridge HTTP server rate limit（B4 —— 防异常 agent 死循环刷 /vscode/*）。
//
// 设计：per-path token bucket。每个 path 各自独立 bucket，防止某一类工具的爆刷把其他
// 工具饿死。bucket 参数：
//
//   RATE_TOKENS_PER_SEC = 10  每秒补 10 个 token
//   BURST_CAPACITY = 30       bucket 容量（初始 = 30，允许合理突发）
//
// 命中上限：429 + Retry-After header + JSON body 告诉 caller 还要等多久。
//
// 这是"防异常"保护，不是细粒度限速。正常用户操作（开 3 个文件、跑 1 条命令）远达不到
// 上限；只有 agent 死循环每秒刷 100+ 次才会触发。
//
// 粒度：req.ip + req.path。多个 webview session 可能共享 IP（都是 127.0.0.1），但
// path 区分就足够隔离不同工具流。
//
// 观测：rateLimitStats() 返回当前所有 bucket 状态，extension.ts 可以暴露给 Output
// panel 或 dev tools。

import type { Request, Response, NextFunction } from 'express'
import { log } from '../utils/logger'

const RATE_TOKENS_PER_SEC = 10
const BURST_CAPACITY = 30
// 定期清理久未访问的 bucket，防长跑进程下内存慢慢膨胀
const GC_INTERVAL_MS = 60_000
const GC_IDLE_THRESHOLD_MS = 300_000 // 5 分钟没动的 bucket 删掉

interface Bucket {
  tokens: number
  lastRefill: number
  // 观测字段
  totalHits: number
  totalReject: number
}

const buckets = new Map<string, Bucket>()
let gcTimer: NodeJS.Timeout | null = null

function ensureGcTimer(): void {
  if (gcTimer) return
  gcTimer = setInterval(() => {
    const now = Date.now()
    let removed = 0
    for (const [key, b] of buckets.entries()) {
      if (now - b.lastRefill > GC_IDLE_THRESHOLD_MS) {
        buckets.delete(key)
        removed += 1
      }
    }
    if (removed > 0) {
      log(`[rate-limit] gc: removed ${removed} idle buckets, now tracking ${buckets.size}`)
    }
  }, GC_INTERVAL_MS)
  // 让 node 退出时不被定时器拖住
  gcTimer.unref?.()
}

/**
 * Express 中间件：按 `${req.ip}:${req.path}` 粒度做 token bucket。
 * 超限返 429 + JSON body。挂在 csrf 之后、/vscode 路由之前。
 */
export function rateLimitMiddleware(req: Request, res: Response, next: NextFunction): void {
  ensureGcTimer()

  // req.ip 对 127.0.0.1 总是返 "::1" 或 "127.0.0.1"（取决于 IPv4/IPv6），够稳
  const key = `${req.ip}:${req.path}`
  const now = Date.now()
  let b = buckets.get(key)
  if (!b) {
    b = { tokens: BURST_CAPACITY, lastRefill: now, totalHits: 0, totalReject: 0 }
    buckets.set(key, b)
  }
  // 补 token
  const elapsedSec = (now - b.lastRefill) / 1000
  b.tokens = Math.min(BURST_CAPACITY, b.tokens + elapsedSec * RATE_TOKENS_PER_SEC)
  b.lastRefill = now

  if (b.tokens < 1) {
    b.totalReject += 1
    // 算 caller 至少要等多久才能再来 1 个 token
    const retryAfterSec = Math.ceil((1 - b.tokens) / RATE_TOKENS_PER_SEC)
    res.setHeader('Retry-After', String(retryAfterSec))
    log(
      `[rate-limit] 429 key=${key} tokens=${b.tokens.toFixed(2)} ` +
      `total_hits=${b.totalHits} total_reject=${b.totalReject}`,
    )
    res.status(429).json({
      ok: false,
      error: 'rate limited',
      reason: `Too many requests to ${req.path}. Limit: ${RATE_TOKENS_PER_SEC}/s (burst ${BURST_CAPACITY}). Retry after ${retryAfterSec}s.`,
      retryAfterSec,
    })
    return
  }

  b.tokens -= 1
  b.totalHits += 1
  next()
}

/**
 * 观测：当前所有 bucket 状态。调用方自行决定是否暴露给用户。
 */
export function rateLimitStats(): {
  totalBuckets: number
  entries: Array<{
    key: string
    tokens: number
    totalHits: number
    totalReject: number
    lastRefillAgoSec: number
  }>
} {
  const now = Date.now()
  return {
    totalBuckets: buckets.size,
    entries: Array.from(buckets.entries()).map(([key, b]) => ({
      key,
      tokens: Number(b.tokens.toFixed(2)),
      totalHits: b.totalHits,
      totalReject: b.totalReject,
      lastRefillAgoSec: Math.round((now - b.lastRefill) / 1000),
    })),
  }
}

/**
 * 测试 / 重置用：清空所有 bucket 并停 gc timer。正常运行不需要调。
 */
export function _resetRateLimitForTests(): void {
  buckets.clear()
  if (gcTimer) {
    clearInterval(gcTimer)
    gcTimer = null
  }
}
