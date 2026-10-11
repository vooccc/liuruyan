// 柳如烟 Sidebar WebviewView Provider。
//
// 跟 WebviewPanel（editor area）的差别：
//   - WebviewView 挂在 Activity Bar / Side Bar 容器里，是「视图」而不是「编辑器面板」
//   - VSCode 自动管理生命周期 + 重启后恢复 + 用户拖到右侧 Auxiliary Bar
//   - HTML / CSP 完全复用 buildWebviewHtml；只是 webview 的 host 容器不同
//
// 用法：extension.ts activate() 时
//   vscode.window.registerWebviewViewProvider('liuruyan.sidebarView', new LiuruyanSidebarViewProvider(ctx, bridge))

import * as vscode from 'vscode'
import { buildWebviewHtml } from './html'
import { log } from '../utils/logger'

export interface SidebarBridgeAccessor {
  /** 返回 null 表示 bridge 还没起来；provider 会显示"等待 bridge"占位 HTML */
  getBridge(): { port: number; csrfToken: string } | null
}

export class LiuruyanSidebarViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = 'liuruyan.sidebarView'

  private _view: vscode.WebviewView | null = null

  constructor(private readonly _bridgeAccessor: SidebarBridgeAccessor) {}

  resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken,
  ): void {
    this._view = webviewView

    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [],
    }

    const bridge = this._bridgeAccessor.getBridge()
    if (!bridge) {
      webviewView.webview.html = this._buildPlaceholderHtml('bridge server 未启动；请稍候或重启 VSCode')
      log('[sidebar-view] resolveWebviewView: bridge not ready, showing placeholder')
      return
    }

    webviewView.webview.html = buildWebviewHtml({
      bridgePort: bridge.port,
      csrfToken: bridge.csrfToken,
    })
    log(`[sidebar-view] resolveWebviewView: iframe → vite dev (bridge port=${bridge.port})`)

    webviewView.onDidDispose(() => {
      log('[sidebar-view] disposed (user closed view container)')
      this._view = null
    })
  }

  /** bridge 启动晚于 view 被解析的场景：bridge ready 后调它刷新 HTML。 */
  public refreshIfPending(): void {
    if (!this._view) return
    const bridge = this._bridgeAccessor.getBridge()
    if (!bridge) return
    this._view.webview.html = buildWebviewHtml({
      bridgePort: bridge.port,
      csrfToken: bridge.csrfToken,
    })
    log('[sidebar-view] refreshed after bridge ready')
  }

  private _buildPlaceholderHtml(message: string): string {
    // 极简占位；不嵌 iframe，等 bridge ready
    const safe = message.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    return `<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<style>
  body { margin: 0; padding: 24px; font-family: system-ui, sans-serif; background: #0d1117; color: #e6edf3; }
  .hint { font-size: 12px; opacity: 0.7; line-height: 1.6; }
</style>
</head>
<body><div class="hint">${safe}</div></body>
</html>`
  }
}
