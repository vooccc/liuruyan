import { diffLines } from 'diff'
import type { Change as JsDiffChange } from 'diff'
import * as vscode from 'vscode'

import { createDiffChange, DiffChange } from '../DiffChange'

/**
 * Phase 3 v2 Step 2 - DiffCalculator
 *
 * 把 (baselineText, proposedText) 两段文本转成 DiffChange[]，给 ChangeRegistry 喂数据。
 *
 * 语义（A 风格 / Cascade UX）：
 * - editor buffer 一开始就是 proposedText（用户看到的就是 LLM 改后版本）
 * - editorRange = hunk 在 **proposedText** 里的 line-based 范围
 * - baselineRange = hunk 在 **baselineText** 里的 line-based 范围
 * - InsertionChange.baselineRange 零长度（baseline 里这段不存在）
 * - DeletionChange.editorRange 零长度（proposed 里这段不存在）
 * - ModificationChange 两端都有内容
 *
 * jsdiff diffLines 默认输出顺序：common → removed → added → common，
 * 同位置的 removed 紧邻 added 会被识别为 modification。
 */

export interface CalculateDiffArgs {
  uri: vscode.Uri
  baselineText: string
  proposedText: string
}

export function calculateDiff(args: CalculateDiffArgs): DiffChange[] {
  const hunks = diffLines(args.baselineText, args.proposedText)
  const result: DiffChange[] = []

  let baselineLine = 0
  let editorLine = 0
  let i = 0

  while (i < hunks.length) {
    const hunk = hunks[i]
    const count = hunk.count ?? 0

    if (!hunk.added && !hunk.removed) {
      baselineLine += count
      editorLine += count
      i += 1
      continue
    }

    if (hunk.removed) {
      const next = hunks[i + 1]
      if (next && next.added) {
        result.push(buildModification(args.uri, hunk, next, baselineLine, editorLine))
        baselineLine += count
        editorLine += next.count ?? 0
        i += 2
        continue
      }
      result.push(buildDeletion(args.uri, hunk, baselineLine, editorLine))
      baselineLine += count
      i += 1
      continue
    }

    // added 单独出现（前面不是 removed，否则上面已合并）
    result.push(buildInsertion(args.uri, hunk, baselineLine, editorLine))
    editorLine += count
    i += 1
  }

  return result
}

function rangeForLines(startLine: number, lineCount: number): vscode.Range {
  return new vscode.Range(
    new vscode.Position(startLine, 0),
    new vscode.Position(startLine + lineCount, 0),
  )
}

function zeroRangeAt(line: number): vscode.Range {
  const pos = new vscode.Position(line, 0)
  return new vscode.Range(pos, pos)
}

function buildInsertion(
  uri: vscode.Uri,
  hunk: JsDiffChange,
  baselineLine: number,
  editorLine: number,
): DiffChange {
  const count = hunk.count ?? 0
  return createDiffChange({
    type: 'insertion',
    uri,
    baselineRange: zeroRangeAt(baselineLine),
    editorRange: rangeForLines(editorLine, count),
    originalText: '',
    proposedText: hunk.value,
  })
}

function buildDeletion(
  uri: vscode.Uri,
  hunk: JsDiffChange,
  baselineLine: number,
  editorLine: number,
): DiffChange {
  const count = hunk.count ?? 0
  return createDiffChange({
    type: 'deletion',
    uri,
    baselineRange: rangeForLines(baselineLine, count),
    editorRange: zeroRangeAt(editorLine),
    originalText: hunk.value,
    proposedText: '',
  })
}

function buildModification(
  uri: vscode.Uri,
  removed: JsDiffChange,
  added: JsDiffChange,
  baselineLine: number,
  editorLine: number,
): DiffChange {
  return createDiffChange({
    type: 'modification',
    uri,
    baselineRange: rangeForLines(baselineLine, removed.count ?? 0),
    editorRange: rangeForLines(editorLine, added.count ?? 0),
    originalText: removed.value,
    proposedText: added.value,
  })
}
