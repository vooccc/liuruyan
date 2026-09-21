// 配置常量 + 一次性 token 生成。
//
// 阶段 1：硬编码 backend 8001 + frontend 1420（vite dev）。阶段 2/3 后再做动态化。

import * as crypto from 'crypto'

/** 柳如烟 backend 监听位置（frontend api/base.ts 同步硬编码） */
export const BACKEND_HOST = '127.0.0.1'
export const BACKEND_PORT = 8001

/** 阶段 1：frontend dev server (`npm run dev` / `npm run tauri dev`) 端口 */
export const FRONTEND_DEV_PORT = 1420

/** webview iframe 跟插件 HTTP server 通信时使用的 header / query 参数名 */
export const CSRF_HEADER = 'x-liuruyan-csrf'
export const CSRF_QUERY = 'csrf'

/** 每次插件 activate 时生成新的 csrf token，进程内有效 */
export function generateCsrfToken(): string {
  return crypto.randomBytes(24).toString('hex')
}
