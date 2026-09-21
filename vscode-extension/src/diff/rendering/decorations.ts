import * as vscode from 'vscode'

/**
 * Phase 3 v2 Step 3 - decoration type 定义
 *
 * A 风格语义下的视觉规则：
 * - editor buffer 已经是 proposedText（用户看到 LLM 改后版本）
 * - InsertionChange 的 editorRange 是新增行，绿色背景高亮
 * - ModificationChange 的 editorRange 是修改后的新内容，跟 InsertionChange 同色
 * - DeletionChange 的 editorRange 是零长度（baseline 那段在 proposed 里没了），
 *   用 before-contentText 在该位置渲染单行占位 "⊖ N lines removed"，hover 看原内容
 *
 * minimap 通过 overviewRulerColor 同步标记，便于用户快速定位 pending changes。
 *
 * accepted/rejected 状态的 change 不再渲染（CodeLensProvider 也不再 emit lens）。
 */

/**
 * 新增行的高亮装饰：整行浅绿背景 + 右侧 minimap 标记
 */
export const insertionDecoration = vscode.window.createTextEditorDecorationType({
  backgroundColor: new vscode.ThemeColor('diffEditor.insertedLineBackground'),
  isWholeLine: true,
  overviewRulerColor: new vscode.ThemeColor('diffEditor.insertedLineBackground'),
  overviewRulerLane: vscode.OverviewRulerLane.Right,
})

/**
 * 修改后的新内容高亮：复用 insertion 同色（用户视角"新内容"语义一致）
 */
export const modificationDecoration = vscode.window.createTextEditorDecorationType({
  backgroundColor: new vscode.ThemeColor('diffEditor.insertedLineBackground'),
  isWholeLine: true,
  overviewRulerColor: new vscode.ThemeColor('diffEditor.insertedLineBackground'),
  overviewRulerLane: vscode.OverviewRulerLane.Right,
})

/**
 * 删除占位装饰：在 zero-length range 那一行的开头渲染 "⊖ N lines removed"，
 * hover message 显示完整原内容。用 before-contentText 是因为 baseline 那段
 * 在 proposed buffer 里完全不存在，没法用 backgroundColor 标记。
 *
 * 注意：单条 deletionPlaceholderDecoration 实例对所有 deletion 共用，但每个
 * deletion 的占位文本不同（lines removed 数）。VSCode decoration API 不支持
 * 同一 decorationType 不同 contentText，所以 VisualizationService 会按
 * 每个 deletion 创建独立的 throwaway decorationType。
 *
 * 此处导出的是 "通用样式" 工厂，VisualizationService 调用 createDeletionDecorationType()
 * 为每个 deletion 实例化。
 */
export interface DeletionDecorationOptions {
  removedLineCount: number
  hoverContent: string
}

export function createDeletionDecorationType(
  options: DeletionDecorationOptions,
): vscode.TextEditorDecorationType {
  return vscode.window.createTextEditorDecorationType({
    before: {
      contentText: `⊖ ${options.removedLineCount} line${options.removedLineCount === 1 ? '' : 's'} removed (hover to see)`,
      color: new vscode.ThemeColor('descriptionForeground'),
      fontStyle: 'italic',
      margin: '0 0 0 0',
    },
    overviewRulerColor: new vscode.ThemeColor('diffEditor.removedLineBackground'),
    overviewRulerLane: vscode.OverviewRulerLane.Right,
  })
}

/**
 * 把 originalText 转成 markdown hover 内容（保留代码格式 + 截断防过长）
 */
export function buildDeletionHoverMarkdown(originalText: string): vscode.MarkdownString {
  const MAX_PREVIEW = 4000
  const truncated = originalText.length > MAX_PREVIEW
    ? originalText.slice(0, MAX_PREVIEW) + '\n... (truncated)'
    : originalText
  const md = new vscode.MarkdownString()
  md.appendMarkdown('**Removed by 柳如烟:**\n\n')
  md.appendCodeblock(truncated, 'plaintext')
  md.isTrusted = false
  return md
}
