// CORS 中间件 —— 让 webview iframe (http://localhost:1420) 跨端口调 bridge server。
//
// 为什么需要：
//   bridge server 在随机端口 listen，frontend 在 vite dev (1420) 跑，两者 origin 不同；
//   且我们带自定义 header `x-liuruyan-csrf`，浏览器必先发 OPTIONS preflight 询问。
//   不返 CORS 头 → fetch 抛 TypeError: Failed to fetch。
//
// 安全边界：
//   - bridge server 只在 127.0.0.1 listen，外部到不了。
//   - 仅放行 origin 是 localhost / 127.0.0.1 任意端口的请求（webview iframe 自己 + 测试用）。
//   - 实际授权仍由后续 csrf 中间件把守，CORS 只解决浏览器 preflight。
//
// 必须挂在 csrf 之前：preflight OPTIONS 不带 csrf header，要先 204 短路掉。

import type { Request, Response, NextFunction } from 'express'
import { CSRF_HEADER } from '../config'

const ALLOWED_ORIGIN_RE = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/

export function corsMiddleware(req: Request, res: Response, next: NextFunction): void {
  const origin = typeof req.headers.origin === 'string' ? req.headers.origin : undefined
  if (origin && ALLOWED_ORIGIN_RE.test(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin)
    res.setHeader('Vary', 'Origin')
    res.setHeader('Access-Control-Allow-Headers', `${CSRF_HEADER}, content-type`)
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    res.setHeader('Access-Control-Max-Age', '600')
  }
  if (req.method === 'OPTIONS') {
    res.status(204).end()
    return
  }
  next()
}
