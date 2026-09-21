// "最近活跃文本编辑器" 缓存。
//
// 为什么需要：
//   webview panel 抢 focus 时，vscode.window.activeTextEditor 会变 undefined，
//   导致 /vscode/active-editor 拿不到任何文件 —— 哪怕用户上一秒还在编辑代码。
//   这是 VSCode webview 的硬伤，所有同类插件都得做缓存层。
//
// 策略：
//   - activate 时如果已有 activeTextEditor，先记下当前
//   - 订阅 onDidChangeActiveTextEditor，仅在新 editor 非 undefined 时更新缓存
//     （webview focus 触发 undefined 时不抹掉缓存，这样切回 webview 点按钮还能拿到上次的 editor）
//   - 取值时：先 vscode.window.activeTextEditor → 否则 lastActiveEditor（且 document 未关闭）

import * as vscode from 'vscode'

let lastActiveEditor: vscode.TextEditor | undefined

export function initEditorTracking(context: vscode.ExtensionContext): void {
  if (vscode.window.activeTextEditor) {
    lastActiveEditor = vscode.window.activeTextEditor
  }
  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor((editor) => {
      if (editor) lastActiveEditor = editor
    }),
  )
}

export function getCurrentTextEditor(): vscode.TextEditor | undefined {
  const active = vscode.window.activeTextEditor
  if (active) return active
  if (lastActiveEditor && !lastActiveEditor.document.isClosed) return lastActiveEditor
  return undefined
}
