import * as vscode from 'vscode'

import { ChangeRegistry } from '../ChangeRegistry'
import { DiffChange, isPending } from '../DiffChange'

/**
 * Phase 3 v2 Step 3 - CodeLensProvider for Accept/Reject UI
 *
 * 在每个 pending DiffChange 的 editorRange.start 行上方渲染两个 CodeLens：
 * - "✓ Accept" → liuruyan.diff.acceptHunk(changeId)
 * - "✗ Reject" → liuruyan.diff.rejectHunk(changeId)
 *
 * 当 ChangeRegistry 变化时 fire _onDidChangeCodeLenses，VSCode 自动重新调用
 * provideCodeLenses 刷新 UI。
 *
 * Step 3 仅交付 provider 类，Step 5/6 在 extension.ts 注册到
 * vscode.languages.registerCodeLensProvider 并实现命令 handler。
 */
export class AcceptRejectCodeLensProvider implements vscode.CodeLensProvider, vscode.Disposable {
  private readonly _emitter = new vscode.EventEmitter<void>()
  private readonly _disposables: vscode.Disposable[] = []

  readonly onDidChangeCodeLenses: vscode.Event<void> = this._emitter.event

  constructor(private readonly registry: ChangeRegistry) {
    this._disposables.push(
      registry.onDidChange(() => {
        this._emitter.fire()
      }),
    )
  }

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    const changes = this.registry.getByUri(document.uri).filter(isPending)
    const lenses: vscode.CodeLens[] = []
    for (const change of changes) {
      lenses.push(...this._buildLensesForChange(change))
    }
    return lenses
  }

  /**
   * VSCode 调用 resolveCodeLens 填充 command 详情时的 hook（本实现已经在
   * provideCodeLenses 阶段就提供了完整 command，所以这里 noop）
   */
  resolveCodeLens(lens: vscode.CodeLens): vscode.CodeLens {
    return lens
  }

  dispose(): void {
    this._emitter.dispose()
    for (const d of this._disposables) d.dispose()
    this._disposables.length = 0
  }

  private _buildLensesForChange(change: DiffChange): vscode.CodeLens[] {
    // CodeLens 渲染在 range 起始行上方。zero-length range 也支持。
    const anchorLine = change.editorRange.start.line
    const anchorRange = new vscode.Range(
      new vscode.Position(anchorLine, 0),
      new vscode.Position(anchorLine, 0),
    )
    const acceptLens = new vscode.CodeLens(anchorRange, {
      title: `$(check) Accept ${describeChange(change)}`,
      command: 'liuruyan.diff.acceptHunk',
      arguments: [change.id],
      tooltip: 'Accept this change (keep the new content as-is)',
    })
    const rejectLens = new vscode.CodeLens(anchorRange, {
      title: '$(x) Reject',
      command: 'liuruyan.diff.rejectHunk',
      arguments: [change.id],
      tooltip: 'Reject this change (revert to the original content)',
    })
    return [acceptLens, rejectLens]
  }
}

function describeChange(change: DiffChange): string {
  switch (change.type) {
    case 'insertion':
      return 'insertion'
    case 'deletion':
      return 'deletion'
    case 'modification':
      return 'change'
  }
}

let _instance: AcceptRejectCodeLensProvider | null = null

export function getAcceptRejectCodeLensProvider(
  registry: ChangeRegistry,
): AcceptRejectCodeLensProvider {
  if (!_instance) {
    _instance = new AcceptRejectCodeLensProvider(registry)
  }
  return _instance
}

export function resetCodeLensProviderForTesting(): void {
  if (_instance) {
    _instance.dispose()
    _instance = null
  }
}
