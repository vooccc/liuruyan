import * as vscode from 'vscode'

import { ChangeRegistry, RegistryChangeEvent } from '../ChangeRegistry'
import { isPending } from '../DiffChange'
import {
  buildDeletionHoverMarkdown,
  createDeletionDecorationType,
  insertionDecoration,
  modificationDecoration,
} from '../rendering/decorations'

/**
 * Phase 3 v2 Step 3 - VisualizationService
 *
 * 监听 ChangeRegistry 变化 + active editor 切换，把 pending changes 渲染到对应 editor。
 *
 * 渲染策略（A 风格）：
 * - InsertionChange.editorRange → insertionDecoration 整行绿色
 * - ModificationChange.editorRange → modificationDecoration 整行绿色
 * - DeletionChange.editorRange (zero-length) → 独立 throwaway decorationType
 *   带 before-contentText "⊖ N lines removed" + hover markdown 显示 originalText
 *
 * accept 后 change.status='accepted' → 不再渲染（保持 buffer 当前内容，无需 IDE 操作）
 * reject 后 change.status='rejected' → 也不再渲染（buffer 已被 ChangeResolutionService
 *   replace 为 originalText）
 *
 * 内存管理：每个 deletion 创建的 throwaway decorationType 必须 dispose，
 * 否则 leak 严重（每次 reload 都会留下旧装饰）。本服务用 _activeDeletionTypes
 * 跟踪所有创建过的，clearForUri / dispose 时统一清理。
 */
export class VisualizationService implements vscode.Disposable {
  private readonly _activeDeletionTypes = new Map<string, vscode.TextEditorDecorationType[]>()
  private readonly _disposables: vscode.Disposable[] = []

  constructor(private readonly registry: ChangeRegistry) {
    this._disposables.push(registry.onDidChange(this._handleRegistryChange))
    this._disposables.push(
      vscode.window.onDidChangeActiveTextEditor(this._handleEditorChange),
    )
    this._disposables.push(
      vscode.window.onDidChangeVisibleTextEditors(this._handleVisibleEditorsChange),
    )
  }

  /**
   * 显式刷新所有 visible editors 的装饰（外部命令在状态变更后调用）
   */
  refreshAll(): void {
    for (const editor of vscode.window.visibleTextEditors) {
      this._renderEditor(editor)
    }
  }

  /**
   * 清除单个 uri 对应的所有装饰（dispose throwaway types + 清掉 visible editor 的高亮）
   */
  clearForUri(uri: vscode.Uri): void {
    const key = uri.toString()
    const types = this._activeDeletionTypes.get(key)
    if (types) {
      for (const t of types) t.dispose()
      this._activeDeletionTypes.delete(key)
    }
    for (const editor of vscode.window.visibleTextEditors) {
      if (editor.document.uri.toString() !== key) continue
      editor.setDecorations(insertionDecoration, [])
      editor.setDecorations(modificationDecoration, [])
    }
  }

  dispose(): void {
    for (const types of this._activeDeletionTypes.values()) {
      for (const t of types) t.dispose()
    }
    this._activeDeletionTypes.clear()
    for (const d of this._disposables) d.dispose()
    this._disposables.length = 0
  }

  private readonly _handleRegistryChange = (ev: RegistryChangeEvent): void => {
    for (const editor of vscode.window.visibleTextEditors) {
      if (editor.document.uri.toString() === ev.uri.toString()) {
        this._renderEditor(editor)
      }
    }
  }

  private readonly _handleEditorChange = (editor: vscode.TextEditor | undefined): void => {
    if (editor) this._renderEditor(editor)
  }

  private readonly _handleVisibleEditorsChange = (editors: readonly vscode.TextEditor[]): void => {
    for (const editor of editors) this._renderEditor(editor)
  }

  private _renderEditor(editor: vscode.TextEditor): void {
    const uri = editor.document.uri
    const changes = this.registry.getByUri(uri)
    const pending = changes.filter(isPending)

    // 1. 整行高亮装饰（insertion + modification 复用 type 实例，set 多 ranges）
    const insertionRanges: vscode.Range[] = []
    const modificationRanges: vscode.Range[] = []
    for (const change of pending) {
      if (change.type === 'insertion') insertionRanges.push(change.editorRange)
      else if (change.type === 'modification') modificationRanges.push(change.editorRange)
    }
    editor.setDecorations(insertionDecoration, insertionRanges)
    editor.setDecorations(modificationDecoration, modificationRanges)

    // 2. 删除占位装饰：每个 DeletionChange 独立 type 实例（contentText 不同）
    //    先 dispose 旧的，再为当前 pending deletions 重建
    const key = uri.toString()
    const oldTypes = this._activeDeletionTypes.get(key) ?? []
    for (const t of oldTypes) t.dispose()
    const newTypes: vscode.TextEditorDecorationType[] = []
    for (const change of pending) {
      if (change.type !== 'deletion') continue
      const removedLineCount = countLines(change.originalText)
      const decoType = createDeletionDecorationType({
        removedLineCount,
        hoverContent: change.originalText,
      })
      const decoOption: vscode.DecorationOptions = {
        range: change.editorRange,
        hoverMessage: buildDeletionHoverMarkdown(change.originalText),
      }
      editor.setDecorations(decoType, [decoOption])
      newTypes.push(decoType)
    }
    this._activeDeletionTypes.set(key, newTypes)
  }
}

function countLines(text: string): number {
  if (text.length === 0) return 0
  const matches = text.match(/\n/g)
  const newlineCount = matches ? matches.length : 0
  // 不以 \n 结尾时最后还有半行
  return text.endsWith('\n') ? newlineCount : newlineCount + 1
}

let _instance: VisualizationService | null = null

export function getVisualizationService(registry: ChangeRegistry): VisualizationService {
  if (!_instance) {
    _instance = new VisualizationService(registry)
  }
  return _instance
}

export function resetVisualizationServiceForTesting(): void {
  if (_instance) {
    _instance.dispose()
    _instance = null
  }
}
