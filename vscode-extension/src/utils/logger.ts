// 日志输出 —— 复用 VSCode 自带 OutputChannel，命名 `Liuruyan`。
// 用户在编辑器中 View → Output → Liuruyan 能查看插件日志。

import * as vscode from 'vscode'

let _channel: vscode.OutputChannel | null = null

export function getLogger(): vscode.OutputChannel {
  if (!_channel) {
    _channel = vscode.window.createOutputChannel('Liuruyan')
  }
  return _channel
}

export function log(message: string): void {
  getLogger().appendLine(`[${new Date().toISOString()}] ${message}`)
}

export function disposeLogger(): void {
  if (_channel) {
    _channel.dispose()
    _channel = null
  }
}
