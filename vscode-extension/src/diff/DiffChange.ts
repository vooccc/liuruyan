import * as vscode from 'vscode'

/**
 * Phase 3 v2 inline diff 数据模型层
 *
 * 核心设计：
 * - DiffChange / InsertionChange / DeletionChange / ModificationChange
 * - 每个 change 同时持有 baselineRange（baseline 文本里的固定位置）+ editorRange（编辑器里
 *   的当前位置，用户接受其它 hunk 后会偏移）
 *
 * v1 (phase3-v1-modal-diff) 用整文件 modal + vscode.diff 的 MVP 实现，v2 拆分为细粒度
 * 数据模型 + per-hunk accept/reject。
 */

export type DiffChangeType = 'insertion' | 'deletion' | 'modification'

export type DiffChangeStatus = 'pending' | 'accepted' | 'rejected'

interface DiffChangeBase {
  readonly id: string
  readonly type: DiffChangeType
  readonly uri: vscode.Uri
  readonly baselineRange: vscode.Range
  editorRange: vscode.Range
  status: DiffChangeStatus
  readonly originalText: string
  readonly proposedText: string
}

export interface InsertionChange extends DiffChangeBase {
  readonly type: 'insertion'
  readonly originalText: ''
}

export interface DeletionChange extends DiffChangeBase {
  readonly type: 'deletion'
  readonly proposedText: ''
}

export interface ModificationChange extends DiffChangeBase {
  readonly type: 'modification'
}

export type DiffChange = InsertionChange | DeletionChange | ModificationChange

let _counter = 0
function generateChangeId(): string {
  _counter += 1
  return `dc_${Date.now().toString(36)}_${_counter.toString(36)}`
}

export interface CreateChangeArgs {
  type: DiffChangeType
  uri: vscode.Uri
  baselineRange: vscode.Range
  editorRange: vscode.Range
  originalText: string
  proposedText: string
}

export function createDiffChange(args: CreateChangeArgs): DiffChange {
  const base = {
    id: generateChangeId(),
    uri: args.uri,
    baselineRange: args.baselineRange,
    editorRange: args.editorRange,
    status: 'pending' as DiffChangeStatus,
  }

  switch (args.type) {
    case 'insertion':
      if (args.originalText.length > 0) {
        throw new Error('InsertionChange.originalText must be empty')
      }
      return {
        ...base,
        type: 'insertion',
        originalText: '' as const,
        proposedText: args.proposedText,
      }
    case 'deletion':
      if (args.proposedText.length > 0) {
        throw new Error('DeletionChange.proposedText must be empty')
      }
      return {
        ...base,
        type: 'deletion',
        originalText: args.originalText,
        proposedText: '' as const,
      }
    case 'modification':
      return {
        ...base,
        type: 'modification',
        originalText: args.originalText,
        proposedText: args.proposedText,
      }
  }
}

export function isPending(change: DiffChange): boolean {
  return change.status === 'pending'
}
