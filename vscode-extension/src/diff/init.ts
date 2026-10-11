import * as vscode from 'vscode'

import { getChangeRegistry } from './ChangeRegistry'
import { getAcceptRejectCodeLensProvider } from './rendering/AcceptRejectCodeLensProvider'
import { getChangeNavigationService } from './services/ChangeNavigationService'
import { getChangeResolutionService } from './services/ChangeResolutionService'
import { getDiffStateService, recoverStaleSessions } from './services/DiffStateService'
import { getVisualizationService } from './services/VisualizationService'

/**
 * Phase 3 v2 Step 5 - diff 子系统初始化
 *
 * extension.ts activate 时调用一次。建立 DI 链 + 注册 CodeLensProvider + 注册命令 handlers。
 * 所有创建的服务/订阅都挂到 context.subscriptions，dispose 时自动清理。
 *
 * 命令清单（package.json 也得 declare 同名命令，Step 6 落实）：
 * - liuruyan.diff.acceptHunk(changeId)
 * - liuruyan.diff.rejectHunk(changeId)
 * - liuruyan.diff.acceptAll
 * - liuruyan.diff.rejectAll
 * - liuruyan.diff.nextChange
 * - liuruyan.diff.prevChange
 * - liuruyan.diff.abortSession (新增：用户主动放弃当前 diff session)
 */
export function initDiffSubsystem(context: vscode.ExtensionContext): void {
  const registry = getChangeRegistry()
  const visualization = getVisualizationService(registry)
  const codeLensProvider = getAcceptRejectCodeLensProvider(registry)
  const resolution = getChangeResolutionService(registry)
  const navigation = getChangeNavigationService(registry)
  const state = getDiffStateService(registry)
  // v2.1 #1 崩溃恢复：注入 globalState 让 startSession 持久化 baseline
  state.attachStorage(context.globalState)
  // 检测 stale sessions 提示用户还原 (异步, 不阻塞 activate)
  void recoverStaleSessions(context.globalState)

  context.subscriptions.push(
    vscode.languages.registerCodeLensProvider({ scheme: 'file' }, codeLensProvider),
    visualization,
    state,
    codeLensProvider,
    registry,
    // v2.1 #3 保存提醒：用户在 session 中 save 时 toast 提示状态
    // 不阻止 save (VSCode 没有可靠 API 取消 save), 仅让用户清楚当前 buffer 含未 review 改动
    vscode.workspace.onDidSaveTextDocument(doc => {
      const pending = registry.getPendingByUri(doc.uri)
      if (pending.length === 0) return
      const fileName = doc.fileName.split(/[\\/]/).pop() ?? doc.fileName
      void vscode.window.showWarningMessage(
        `柳如烟: 已保存 ${fileName} 但仍有 ${pending.length} 处未 review 的改动。` +
          `继续在 IDE 里 Accept/Reject，或按 Esc 还原 baseline。`,
      )
    }),
  )

  context.subscriptions.push(
    vscode.commands.registerCommand('liuruyan.diff.acceptHunk', async (changeId: unknown) => {
      if (typeof changeId !== 'string') return
      await resolution.acceptHunk(changeId)
    }),
    vscode.commands.registerCommand('liuruyan.diff.rejectHunk', async (changeId: unknown) => {
      if (typeof changeId !== 'string') return
      await resolution.rejectHunk(changeId)
    }),
    vscode.commands.registerCommand('liuruyan.diff.acceptAll', async () => {
      const editor = vscode.window.activeTextEditor
      if (!editor) return
      await resolution.acceptAll(editor.document.uri)
    }),
    vscode.commands.registerCommand('liuruyan.diff.rejectAll', async () => {
      const editor = vscode.window.activeTextEditor
      if (!editor) return
      await resolution.rejectAll(editor.document.uri)
    }),
    vscode.commands.registerCommand('liuruyan.diff.nextChange', async () => {
      await navigation.gotoNext()
    }),
    vscode.commands.registerCommand('liuruyan.diff.prevChange', async () => {
      await navigation.gotoPrev()
    }),
    vscode.commands.registerCommand('liuruyan.diff.abortSession', async () => {
      const editor = vscode.window.activeTextEditor
      if (!editor) return
      const session = state.getActiveSessionForUri(editor.document.uri)
      if (!session) {
        void vscode.window.showInformationMessage('柳如烟: no active diff session for this file.')
        return
      }
      await state.abortSession(session.id)
    }),
  )
}
