import * as vscode from 'vscode'

import { ChangeRegistry } from '../ChangeRegistry'
import { DiffChange } from '../DiffChange'

/**
 * Phase 3 v2 Step 4 - ChangeNavigationService
 *
 * next/prev change 跳转。Step 6 注册到 F8 / Shift+F8 快捷键（when: liuruyan.diff.active）。
 */
export class ChangeNavigationService {
  constructor(private readonly registry: ChangeRegistry) {}

  /**
   * 跳转到下一个 pending change 并把光标定到 editorRange.start。
   * 没有可跳目标时返回 false。
   */
  async gotoNext(): Promise<boolean> {
    const editor = vscode.window.activeTextEditor
    if (!editor) return false
    const next = this._findNext(editor.document.uri, editor.selection.active)
    if (!next) return false
    return this._jumpTo(editor, next)
  }

  async gotoPrev(): Promise<boolean> {
    const editor = vscode.window.activeTextEditor
    if (!editor) return false
    const prev = this._findPrev(editor.document.uri, editor.selection.active)
    if (!prev) return false
    return this._jumpTo(editor, prev)
  }

  hasNext(uri: vscode.Uri, cursor: vscode.Position): boolean {
    return this._findNext(uri, cursor) !== undefined
  }

  hasPrev(uri: vscode.Uri, cursor: vscode.Position): boolean {
    return this._findPrev(uri, cursor) !== undefined
  }

  private _findNext(uri: vscode.Uri, cursor: vscode.Position): DiffChange | undefined {
    const sorted = this._pendingSorted(uri)
    for (const change of sorted) {
      if (change.editorRange.start.line > cursor.line) return change
    }
    // wrap around: 没有更靠后的 → 跳到第一个（可选，让用户感觉有循环）
    return sorted[0]
  }

  private _findPrev(uri: vscode.Uri, cursor: vscode.Position): DiffChange | undefined {
    const sorted = this._pendingSorted(uri)
    for (let i = sorted.length - 1; i >= 0; i -= 1) {
      const change = sorted[i]
      if (change.editorRange.start.line < cursor.line) return change
    }
    return sorted[sorted.length - 1]
  }

  private _pendingSorted(uri: vscode.Uri): DiffChange[] {
    return [...this.registry.getPendingByUri(uri)].sort((a, b) =>
      a.editorRange.start.line - b.editorRange.start.line
    )
  }

  private async _jumpTo(editor: vscode.TextEditor, change: DiffChange): Promise<boolean> {
    const target = change.editorRange.start
    editor.selection = new vscode.Selection(target, target)
    editor.revealRange(change.editorRange, vscode.TextEditorRevealType.InCenterIfOutsideViewport)
    return true
  }
}

let _instance: ChangeNavigationService | null = null

export function getChangeNavigationService(registry: ChangeRegistry): ChangeNavigationService {
  if (!_instance) {
    _instance = new ChangeNavigationService(registry)
  }
  return _instance
}

export function resetChangeNavigationServiceForTesting(): void {
  _instance = null
}
