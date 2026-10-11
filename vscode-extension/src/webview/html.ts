// WebviewPanel 的 HTML 模板生成。
//
// 阶段 1 策略：webview body 只有一个 iframe，指向 vite dev server（127.0.0.1:1420）。
//
// 关键 CSP 配置：
//   - default-src 'none'    — 顶层 HTML 不允许加载任何资源
//   - frame-src http://127.0.0.1:* http://localhost:*  — 但允许 iframe 嵌入本地 dev server
//   - style-src 'unsafe-inline'  — 顶层 <style> 内联布局样式
//
// 见 docs/vscode_extension_plan.md §5 + §6.2。

import { FRONTEND_DEV_PORT } from '../config'

export interface WebviewHtmlOptions {
  bridgePort: number
  csrfToken: string
}

export function buildWebviewHtml(opts: WebviewHtmlOptions): string {
  const params = new URLSearchParams({
    csrf: opts.csrfToken,
    bridgePort: String(opts.bridgePort),
  })
  // 用 `localhost` 而不是 `127.0.0.1`：vite 默认只在 IPv6 ([::1]) 监听，
  // webview 直连 IPv4 会失败（Windows 上典型现象：iframe 空白），改 localhost 让浏览器/webview 解析。
  const frontendUrl = `http://localhost:${FRONTEND_DEV_PORT}/?${params.toString()}`

  // CSP：默认全禁，仅允许本地 dev server iframe + 内联 style
  const csp = [
    `default-src 'none'`,
    `frame-src http://127.0.0.1:* http://localhost:*`,
    `style-src 'unsafe-inline'`,
  ].join('; ')

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <title>柳如烟</title>
  <style>
    html, body { margin: 0; padding: 0; height: 100%; width: 100%; overflow: hidden; background: #0d1117; color: #e6edf3; font-family: system-ui, sans-serif; }
    iframe { width: 100%; height: 100%; border: none; display: block; }
  </style>
</head>
<body>
  <iframe src="${frontendUrl}"></iframe>
</body>
</html>`
}
