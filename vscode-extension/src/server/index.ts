// 插件内置 HTTP server（极薄 IDE 能力代理）。
//
// 当前端点（阶段 2 第一轮）：
//   GET /_health                     — 健康检查（不需要 csrf）
//   GET /vscode/_ping                — csrf 自检端点
//   GET /vscode/active-editor        — 拿当前编辑器 + 内容 + 选中
//   GET /vscode/selection            — 只拿选中文本（轻量）
//   GET /vscode/workspace-folders    — workspace 根列表
//
// 阶段 2 第二轮还会加：insert-snippet / open-file / diagnostics
// 阶段 2 第三轮加：/vscode/sse（IDE → 前端推送）
//
// 设计契约（docs/ide_integration_principles.md）：
//   铁律 2：随机端口
//   铁律 3：CSRF 强校验，挂在 /vscode/* 前缀
//   铁律 4：每个能力一个显式端点，不做通用 RPC
//   铁律 5：端点只做 IDE 原生能力桥接，不做业务逻辑
//   铁律 7：IDE → 前端推送用 SSE（阶段 2 第三轮涉及）

import express from 'express'
import * as http from 'http'
import { corsMiddleware } from './cors'
import { createCsrfMiddleware } from './csrf'
import { rateLimitMiddleware, rateLimitStats } from './rate-limit'
import { registerIdeRoutes } from './ide-routes'
import { findFreePort } from '../utils/port'
import { log } from '../utils/logger'

export interface BridgeServer {
  port: number
  csrfToken: string
  close(): Promise<void>
}

export async function startBridgeServer(csrfToken: string): Promise<BridgeServer> {
  const app = express()
  app.disable('x-powered-by')
  app.use(express.json({ limit: '4mb' }))

  // CORS 必须在 csrf 之前：preflight OPTIONS 不带 csrf header，要先 204 短路掉。
  app.use(corsMiddleware)

  // ---- 无需 csrf 的端点 ----
  app.get('/_health', (_req, res) => {
    res.json({
      ok: true,
      service: 'liuruyan-vscode-bridge',
      phase: 1,
      csrfRequiredFor: ['/vscode/*'],
    })
  })

  // ---- /vscode/* 桥接端点（必须 csrf + 过 rate limit）----
  const csrf = createCsrfMiddleware(csrfToken)
  app.use('/vscode', csrf)
  // rate limit 紧跟 csrf 后：非法请求已被 csrf 挡掉（不吃 token），合法请求再过限速
  app.use('/vscode', rateLimitMiddleware)

  // 保留 _ping 作为「csrf 链路通不通」的自检端点（T7 烟雾测试依赖它）
  app.get('/vscode/_ping', (_req, res) => {
    res.json({ ok: true, message: 'bridge alive (csrf passed)', timestamp: new Date().toISOString() })
  })

  // 观测端点：/vscode/_rate-limit-stats 返当前所有 bucket 状态（调试用）
  app.get('/vscode/_rate-limit-stats', (_req, res) => {
    res.json({ ok: true, ...rateLimitStats() })
  })

  // 实际 IDE 桥接能力端点（阶段 2 第一轮：active-editor / selection / workspace-folders）
  registerIdeRoutes(app)

  // 默认 404（明确响应，方便定位）
  app.use((_req, res) => {
    res.status(404).json({ error: 'Not Found', hint: 'See /_health for available endpoints' })
  })

  const port = await findFreePort()

  return new Promise<BridgeServer>((resolve, reject) => {
    const server = http.createServer(app)
    server.on('error', reject)
    server.listen(port, '127.0.0.1', () => {
      log(`[bridge] HTTP server listening on http://127.0.0.1:${port}`)
      log(`[bridge] csrfToken (first 8 chars): ${csrfToken.slice(0, 8)}...`)
      resolve({
        port,
        csrfToken,
        close: () =>
          new Promise<void>((r) => {
            server.close(() => {
              log(`[bridge] closed http://127.0.0.1:${port}`)
              r()
            })
          }),
      })
    })
  })
}
