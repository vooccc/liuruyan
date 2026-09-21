import * as vscode from 'vscode'

import { ChangeRegistry } from '../ChangeRegistry'
import { DiffChange } from '../DiffChange'

/**
 * Phase 3 v2 Step 4 - ChangeResolutionService
 *
 * 处理用户对 DiffChange 的 accept / reject 操作。
 *
 * A 风格语义（关键不变量）：
 * - editor buffer 一开始就是 proposedText
 * - **accept**: 啥都不做（buffer 已是结果）→ 仅 mark status='accepted'
 *   → registry 'status' event → VisualizationService 清掉这个 change 的装饰、CodeLens 消失
 *   → buffer 不变 → 后续 changes 的 editorRange 不受影响
 * - **reject**: 把 editor buffer 在 editorRange 这段 replace 为 originalText
 *   → buffer 行数变化（lineDelta）→ 必须 shift 后续 pending changes 的 editorRange
 *   → mark status='rejected'
 *
 * **offset shift 是 v2 最容易踩坑的地方**。本实现采用**精确 lineDelta 增量调整**：
 * lineDelta = countLines(originalText) - countLines(proposedText)
 * 然后 registry.shiftEditorRanges(uri, anchor.line, lineDelta) 把 anchor 之后的
 * 所有 pending changes 行号偏移。
 *
 * acceptAll / rejectAll：
 * - acceptAll: 直接对所有 pending 调 setStatus('accepted')，buffer 不动
 * - rejectAll: 按 **逆序** 对每个 pending 调 reject（保证已处理的 change 不影响
 *   未处理 change 的 editorRange）
 */
export class ChangeResolutionService {
  constructor(private readonly registry: ChangeRegistry) {}

  async acceptHunk(changeId: string): Promise<boolean> {
    const change = this.registry.getById(changeId)
    if (!change) return false
    if (change.status !== 'pending') return false
    return this.registry.setStatus(changeId, 'accepted')
  }

  async rejectHunk(changeId: string): Promise<boolean> {
    const change = this.registry.getById(changeId)
    if (!change) return false
    if (change.status !== 'pending') return false

    const success = await this._applyRejectToBuffer(change)
    if (!success) return false

    const lineDelta = countLines(change.originalText) - countLines(change.proposedText)
    if (lineDelta !== 0) {
      const anchorLine = change.editorRange.end.line
      this.registry.shiftEditorRanges(change.uri, anchorLine, lineDelta)
    }
    this.registry.setStatus(changeId, 'rejected')
    return true
  }

  async acceptAll(uri: vscode.Uri): Promise<number> {
    const pending = this.registry.getPendingByUri(uri)
    let count = 0
    for (const change of pending) {
      if (this.registry.setStatus(change.id, 'accepted')) count += 1
    }
    return count
  }

  async rejectAll(uri: vscode.Uri): Promise<number> {
    // 按 editorRange.start.line 倒序处理：先 reject 末尾的 hunk，shift 不影响前面
    const pending = [...this.registry.getPendingByUri(uri)].sort((a, b) =>
      b.editorRange.start.line - a.editorRange.start.line
    )
    let count = 0
    for (const change of pending) {
      const ok = await this.rejectHunk(change.id)
      if (ok) count += 1
    }
    return count
  }

  /**
   * 把 buffer 在 editorRange 替换为 originalText。返回是否成功 apply。
   * editorRange 可能在文件末尾（行号 >= document.lineCount），需要 clamp。
   */
  private async _applyRejectToBuffer(change: DiffChange): Promise<boolean> {
    const edit = new vscode.WorkspaceEdit()
    const document = await this._openDocument(change.uri)
    if (!document) return false
    const safeRange = clampRange(document, change.editorRange)
    edit.replace(change.uri, safeRange, change.originalText)
    return await vscode.workspace.applyEdit(edit)
  }

  private async _openDocument(uri: vscode.Uri): Promise<vscode.TextDocument | undefined> {
    const opened = vscode.workspace.textDocuments.find(d => d.uri.toString() === uri.toString())
    if (opened) return opened
    try {
      return await vscode.workspace.openTextDocument(uri)
    } catch {
      return undefined
    }
  }
}

function countLines(text: string): number {
  if (text.length === 0) return 0
  const matches = text.match(/\n/g)
  const newlineCount = matches ? matches.length : 0
  return text.endsWith('\n') ? newlineCount : newlineCount + 1
}

function clampRange(document: vscode.TextDocument, range: vscode.Range): vscode.Range {
  const lastLine = document.lineCount - 1
  const lastChar = document.lineAt(lastLine).text.length

  const start = clampPosition(range.start, lastLine, lastChar)
  const end = clampPosition(range.end, lastLine, lastChar)
  return new vscode.Range(start, end)
}

function clampPosition(pos: vscode.Position, lastLine: number, lastChar: number): vscode.Position {
  if (pos.line < 0) return new vscode.Position(0, 0)
  if (pos.line > lastLine) return new vscode.Position(lastLine, lastChar)
  if (pos.line === lastLine && pos.character > lastChar) {
    return new vscode.Position(lastLine, lastChar)
  }
  if (pos.character < 0) return new vscode.Position(pos.line, 0)
  return pos
}

let _instance: ChangeResolutionService | null = null

export function getChangeResolutionService(registry: ChangeRegistry): ChangeResolutionService {
  if (!_instance) {
    _instance = new ChangeResolutionService(registry)
  }
  return _instance
}

export function resetChangeResolutionServiceForTesting(): void {
  _instance = null
}
