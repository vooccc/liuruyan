// WebviewPanel 创建 + 生命周期管理。
//
// 单例策略：同时只允许一个柳如烟 panel；再次执行 openPanel 命令时把已有 panel 拉到前台。

import * as vscode from 'vscode'
import { buildWebviewHtml } from './html'
import { log } from '../utils/logger'

export interface PanelOptions {
  bridgePort: number
  csrfToken: string
}

let _panel: vscode.WebviewPanel | null = null

export function openWebviewPanel(context: vscode.ExtensionContext, opts: PanelOptions): vscode.WebviewPanel {
  if (_panel) {
    _panel.reveal(_panel.viewColumn ?? vscode.ViewColumn.Beside)
    log('[panel] reveal existing panel')
    return _panel
  }

  const panel = vscode.window.createWebviewPanel(
    'liuruyan',
    '柳如烟',
    vscode.ViewColumn.Beside,
    {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [],
    },
  )

  panel.webview.html = buildWebviewHtml({
    bridgePort: opts.bridgePort,
    csrfToken: opts.csrfToken,
  })

  panel.onDidDispose(() => {
    log('[panel] disposed')
    _panel = null
  })

  _panel = panel
  context.subscriptions.push(panel)
  log(`[panel] created, iframe → http://127.0.0.1:1420/?csrf=<…>&bridgePort=${opts.bridgePort}`)
  return panel
}
