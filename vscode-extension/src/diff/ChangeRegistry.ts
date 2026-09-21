import * as vscode from 'vscode'

import { DiffChange, isPending } from './DiffChange'

/**
 * Phase 3 v2 inline diff 注册表
 *
 * 参考 _research/codeium/decompiled/com/codeium/intellij/diff/ChangeRegistry.java
 *
 * 设计要点：
 * 1. **以 uri 为 key 分片**：codeium 是 per-document 实例，我们用 Map<uri-string, DiffChange[]>
 *    的全局单例，便于 CodeLens provider / Visualization 等订阅一个事件源。
 * 2. **changes 数组按 editorRange.start 排序**：register 时插入到正确位置以保证有序，
 *    便于 next/prev 跳转和按位置二分查找。
 * 3. **不删 change 状态变更**：reject 时只把 status='rejected'，不从数组里移除（保留历史
 *    供 UI 显示），由 clear/clearByUri 显式清理。但 v2.0 简化：reject 后直接 unregister
 *    （UI 不需要看历史）。
 * 4. **EventEmitter 通知变更**：CodeLens provider / Visualization service 订阅
 *    `onDidChangeChanges` 事件来 refresh 渲染。
 */

export interface RegistryChangeEvent {
  uri: vscode.Uri
  reason: 'register' | 'unregister' | 'status' | 'rangeShift' | 'clear'
}

export class ChangeRegistry implements vscode.Disposable {
  private readonly _byUri = new Map<string, DiffChange[]>()
  private readonly _byId = new Map<string, DiffChange>()
  private readonly _emitter = new vscode.EventEmitter<RegistryChangeEvent>()

  readonly onDidChange: vscode.Event<RegistryChangeEvent> = this._emitter.event

  register(change: DiffChange): void {
    if (this._byId.has(change.id)) {
      throw new Error(`ChangeRegistry: duplicate change id ${change.id}`)
    }
    const key = change.uri.toString()
    const list = this._byUri.get(key) ?? []
    const insertAt = findInsertionIndex(list, change.editorRange.start)
    list.splice(insertAt, 0, change)
    this._byUri.set(key, list)
    this._byId.set(change.id, change)
    this._emitter.fire({ uri: change.uri, reason: 'register' })
  }

  unregister(id: string): boolean {
    const change = this._byId.get(id)
    if (!change) return false
    const key = change.uri.toString()
    const list = this._byUri.get(key)
    if (list) {
      const idx = list.findIndex(c => c.id === id)
      if (idx >= 0) list.splice(idx, 1)
      if (list.length === 0) this._byUri.delete(key)
    }
    this._byId.delete(id)
    this._emitter.fire({ uri: change.uri, reason: 'unregister' })
    return true
  }

  getById(id: string): DiffChange | undefined {
    return this._byId.get(id)
  }

  getByUri(uri: vscode.Uri): readonly DiffChange[] {
    return this._byUri.get(uri.toString()) ?? []
  }

  getPendingByUri(uri: vscode.Uri): DiffChange[] {
    return this.getByUri(uri).filter(isPending)
  }

  /**
   * 按编辑器位置二分查找：返回 editorRange 包含 position 的所有 changes
   * （可能多个 change 共享同一行，比如插入紧挨修改）
   */
  getAtPosition(uri: vscode.Uri, position: vscode.Position): DiffChange[] {
    const list = this._byUri.get(uri.toString()) ?? []
    const result: DiffChange[] = []
    for (const change of list) {
      if (change.editorRange.contains(position)) {
        result.push(change)
      }
    }
    return result
  }

  /**
   * 列出所有有 changes 的 uri（供 UI 显示 pending count / 跨文件视图）
   */
  getUrisWithChanges(): vscode.Uri[] {
    const uris: vscode.Uri[] = []
    for (const [key, list] of this._byUri.entries()) {
      if (list.length > 0) uris.push(vscode.Uri.parse(key))
    }
    return uris
  }

  /**
   * 标记 change 状态。返回是否有更新。
   * 注意：本方法不会自动调整后续 changes 的 editorRange 偏移；调用方需要在落盘后手动 shift。
   */
  setStatus(id: string, status: DiffChange['status']): boolean {
    const change = this._byId.get(id)
    if (!change) return false
    if (change.status === status) return false
    change.status = status
    this._emitter.fire({ uri: change.uri, reason: 'status' })
    return true
  }

  /**
   * 在指定 uri 上对所有起始行 >= afterLine 的 changes 进行编辑器范围偏移。
   * accept 一个 hunk 后会改变文件行数，此方法用于同步剩余 pending changes 的 editorRange。
   */
  shiftEditorRanges(uri: vscode.Uri, afterLine: number, lineDelta: number): void {
    if (lineDelta === 0) return
    const list = this._byUri.get(uri.toString())
    if (!list) return
    let shifted = false
    for (const change of list) {
      if (change.editorRange.start.line < afterLine) continue
      const start = change.editorRange.start.translate(lineDelta, 0)
      const end = change.editorRange.end.translate(lineDelta, 0)
      change.editorRange = new vscode.Range(start, end)
      shifted = true
    }
    if (shifted) this._emitter.fire({ uri, reason: 'rangeShift' })
  }

  clearByUri(uri: vscode.Uri): void {
    const key = uri.toString()
    const list = this._byUri.get(key)
    if (!list) return
    for (const change of list) this._byId.delete(change.id)
    this._byUri.delete(key)
    this._emitter.fire({ uri, reason: 'clear' })
  }

  clear(): void {
    this._byUri.clear()
    this._byId.clear()
    this._emitter.fire({ uri: vscode.Uri.parse('liuruyan:all'), reason: 'clear' })
  }

  size(): number {
    return this._byId.size
  }

  dispose(): void {
    this._byUri.clear()
    this._byId.clear()
    this._emitter.dispose()
  }
}

function findInsertionIndex(list: readonly DiffChange[], position: vscode.Position): number {
  let lo = 0
  let hi = list.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    const midStart = list[mid].editorRange.start
    if (positionBefore(midStart, position)) {
      lo = mid + 1
    } else {
      hi = mid
    }
  }
  return lo
}

function positionBefore(a: vscode.Position, b: vscode.Position): boolean {
  if (a.line !== b.line) return a.line < b.line
  return a.character < b.character
}

let _instance: ChangeRegistry | null = null

export function getChangeRegistry(): ChangeRegistry {
  if (!_instance) {
    _instance = new ChangeRegistry()
  }
  return _instance
}

/**
 * 仅在测试或 reload 场景使用：丢弃当前单例（外部需自行处理已派发事件订阅者的清理）。
 */
export function resetChangeRegistryForTesting(): void {
  if (_instance) {
    _instance.dispose()
    _instance = null
  }
}
