// CSRF 校验中间件。
//
// 设计依据：_docs/ide_integration_principles.md 铁律 3「CSRF token 强制校验」。
// 防止用户浏览器里的恶意网页偷偷调 localhost:port/vscode/write-file 之类做坏事。
//
// 接受两种位置传 token：
//   - HTTP header `x-liuruyan-csrf`（常规调用，frontend runtime/vscode.ts 用这条）
//   - URL query `?csrf=xxx`（webview iframe 首次加载用，因为 iframe src 没法注 header）
//
// 不匹配返 403 + 简短 json 错误说明。

import type { Request, Response, NextFunction } from 'express'
import { CSRF_HEADER, CSRF_QUERY } from '../config'

export function createCsrfMiddleware(expectedToken: string) {
  return (req: Request, res: Response, next: NextFunction): void => {
    const headerToken = req.headers[CSRF_HEADER]
    const queryToken = typeof req.query[CSRF_QUERY] === 'string' ? (req.query[CSRF_QUERY] as string) : undefined
    if (headerToken === expectedToken || queryToken === expectedToken) {
      next()
      return
    }
    res.status(403).json({ error: 'CSRF token missing or invalid' })
  }
}
