// 柳如烟 VSCode 插件入口。
//
// 职责：
//   - activate 时启动 bridge HTTP server（随机端口 + CSRF）
//   - 注册命令 `Liuruyan: Open Panel`，命令触发时打开 WebviewPanel
//   - deactivate 时清理 server
//
// 真正的 IDE 桥接端点（active-editor / selection / diagnostics 等）将在 T10 加在
// server/index.ts 的 /vscode/* 路径下。
//
// 详见 _docs/vscode_extension_plan.md §12 的 TODO 清单。

import * as vscode from 'vscode'
import { startBridgeServer, type BridgeServer } from './server'
import { generateCsrfToken } from './config'
import { openWebviewPanel } from './webview/panel'
import { LiuruyanSidebarViewProvider } from './webview/sidebar-view'
import { initEditorTracking } from './editor-state'
import { initTerminals } from './terminals'
import { initDiffSubsystem } from './diff/init'
import { log } from './utils/logger'

let _bridge: BridgeServer | null = null

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  log(`[activate] starting bridge server...`)
  // 缓存最近活跃的 text editor，避免 webview focus 后 activeTextEditor 变 undefined
  initEditorTracking(context)
  // Terminal lifecycle 订阅：onDidCloseTerminal 时清理 id→Terminal map
  initTerminals(context)
  // Phase 3 v2 inline diff 子系统：注册 CodeLensProvider + 7 个命令
  initDiffSubsystem(context)

  // 先注册 sidebar provider（不依赖 bridge），bridge 启动晚也能 refreshIfPending
  const sidebarProvider = new LiuruyanSidebarViewProvider({
    getBridge: () => (_bridge ? { port: _bridge.port, csrfToken: _bridge.csrfToken } : null),
  })
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(
      LiuruyanSidebarViewProvider.viewType,
      sidebarProvider,
      // 故意 NOT 开 retainContextWhenHidden：实测它跟 VSCode webview service worker 注册
      // 有偶发冲突（"Could not register service worker: InvalidStateError"），用户反复 reload
      // 会卡死。代价是切走再回来 iframe 会重新 load（但 vite dev 很快，且 chat 状态由后端
      // session 持久化，不会丢对话）。
    ),
  )

  try {
    const csrfToken = generateCsrfToken()
    _bridge = await startBridgeServer(csrfToken)
    log(`[activate] bridge ready on http://127.0.0.1:${_bridge.port}`)
    // bridge 起来后通知 sidebar provider 刷新（如果 view 已经被用户打开过显示了占位 HTML）
    sidebarProvider.refreshIfPending()
  } catch (err) {
    log(`[activate] bridge server failed: ${err instanceof Error ? err.message : String(err)}`)
    void vscode.window.showErrorMessage(`柳如烟 bridge server 启动失败：${err instanceof Error ? err.message : String(err)}`)
  }

  context.subscriptions.push(
    // 原有：editor area 大 panel 模式（保留给重度用户）
    vscode.commands.registerCommand('liuruyan.openPanel', () => {
      if (!_bridge) {
        void vscode.window.showErrorMessage('柳如烟 bridge server 未启动；请重启 IDE 或查看 Output → Liuruyan')
        return
      }
      openWebviewPanel(context, {
        bridgePort: _bridge.port,
        csrfToken: _bridge.csrfToken,
      })
    }),
    // 新增：sidebar 模式入口（Ctrl+Alt+L / editor 右上角图标 / 命令面板都走这个）
    // VSCode 自动生成的 `<viewId>.focus` 命令可以聚焦 view，无需我们手写 reveal 逻辑
    vscode.commands.registerCommand('liuruyan.focusSidebar', async () => {
      await vscode.commands.executeCommand(`${LiuruyanSidebarViewProvider.viewType}.focus`)
    }),
  )
}

export async function deactivate(): Promise<void> {
  log(`[deactivate] cleaning up...`)
  if (_bridge) {
    await _bridge.close()
    _bridge = null
  }
}
