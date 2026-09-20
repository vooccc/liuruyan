import * as vscode from 'vscode'

import { getChangeRegistry } from '../diff/ChangeRegistry'
import { calculateDiff } from '../diff/services/DiffCalculator'
import { getDiffStateService, ResolvedSummary } from '../diff/services/DiffStateService'
import { log } from '../utils/logger'

// v2.1 #4 大文件降级阈值：超过 N 个 hunks 自动 fallback v1 modal
// （CodeLens 一次性渲染 100+ lens 在 vscode 旧版本会卡，安全起见降级）
const MAX_INLINE_HUNKS = Number(process.env.LIURUYAN_INLINE_DIFF_MAX_HUNKS ?? 100)

/**
 * Phase 3 v2 Step 5 - inline diff endpoint handler
 *
 * `/vscode/apply-edit` 的 v2 路径。流程：
 * 1. 解析 uri / fsPath，读取 baselineText（不存在的文件 → 空字符串）
 * 2. 把目标文件 showTextDocument 让用户能看到（A 风格：buffer 立刻显示 proposed）
 * 3. DiffStateService.startSession(uri, baselineText, proposedText)
 *    - 服务内部：replaceFullBuffer(proposed) + calculateDiff + register changes
 *      + setContext liuruyan.diff.active=true
 * 4. await DiffStateService.onResolved 等用户处理完所有 hunks
 *    - 用户通过 CodeLens 点 Accept/Reject 单 hunk 或菜单 acceptAll/rejectAll
 *    - 每次 status 变更 registry 触发 _maybeAutoResolve；全部 resolved 时 finalize
 *      + saveBuffer 落盘 + clearByUri + setContext liuruyan.diff.active=false
 *      + emit onResolved(summary)
 * 5. 超时（默认 600s = 10 分钟）→ abortSession（还原 baseline）→ 报错给 backend
 *
 * 兼容性：
 * - 协议字段跟 v1 完全兼容（applied / uri / fsPath / reason），新增 v2 字段
 *   sessionId / accepted / rejected / total / status，便于 backend 给 LLM 报告细节
 * - 通过 LIURUYAN_INLINE_DIFF=0 env var 走 v1 path（在 ide-routes.ts 入口分支判断）
 */

export interface ApplyEditV2Body {
  uri?: unknown
  fsPath?: unknown
  newContent?: unknown
  description?: unknown
}

export interface ApplyEditV2Response {
  ok: boolean
  applied?: boolean
  uri?: string
  fsPath?: string
  reason?: string
  sessionId?: string
  accepted?: number
  rejected?: number
  total?: number
  status?: 'completed' | 'aborted' | 'no-op'
  error?: string
  /**
   * v2.1 #4 大文件降级 sentinel：handler 检测到 hunks > MAX_INLINE_HUNKS 时返
   * { ok: true, fallbackToV1: true, ... }，endpoint 看到这个 flag 跳出 v2 分支
   * 走 v1 modal 路径（不真正落盘 v2 的内容）。
   */
  fallbackToV1?: boolean
}

const DEFAULT_TIMEOUT_MS = 600_000 // 10 minutes

export async function handleApplyEditV2(body: ApplyEditV2Body): Promise<ApplyEditV2Response> {
  if (typeof body.newContent !== 'string') {
    return { ok: false, error: 'body.newContent (string) required' }
  }

  const targetUri = parseTargetUri(body)
  if (!targetUri) {
    return { ok: false, error: 'body.uri or body.fsPath required (and must be valid)' }
  }

  const description = typeof body.description === 'string' ? body.description : ''
  const proposedText = body.newContent

  // 读 baselineText：已存在文件 → 当前内容；不存在 → 空字符串（创建新文件场景）
  let baselineText = ''
  let originalExists = false
  try {
    const doc = await vscode.workspace.openTextDocument(targetUri)
    baselineText = doc.getText()
    originalExists = true
  } catch {
    originalExists = false
  }

  // v2.1 #4 大文件降级预检查：hunks 太多 → 让 endpoint fall through 到 v1 modal
  // 不在这里 createFile（避免 fallback 后又被 v1 重复创建）
  const previewChanges = calculateDiff({
    uri: targetUri,
    baselineText,
    proposedText,
  })
  if (previewChanges.length > MAX_INLINE_HUNKS) {
    log(
      `[apply-edit-v2] hunks=${previewChanges.length} > ${MAX_INLINE_HUNKS}, ` +
      `fallback to v1 modal for ${targetUri.fsPath}`,
    )
    return {
      ok: true,
      fallbackToV1: true,
      uri: targetUri.toString(),
      fsPath: targetUri.fsPath,
      total: previewChanges.length,
      reason:
        `inline diff has ${previewChanges.length} hunks (> ${MAX_INLINE_HUNKS}), ` +
        `falling back to v1 modal for performance`,
    }
  }

  // 不存在的文件需要先创建（DiffStateService.startSession 假设文件可 openTextDocument）
  if (!originalExists) {
    try {
      const edit = new vscode.WorkspaceEdit()
      edit.createFile(targetUri, {
        overwrite: false,
        ignoreIfExists: false,
        contents: new TextEncoder().encode(''),
      })
      const ok = await vscode.workspace.applyEdit(edit)
      if (!ok) {
        return { ok: false, error: 'failed to create new file for diff session' }
      }
    } catch (err) {
      return {
        ok: false,
        error: `failed to create new file: ${err instanceof Error ? err.message : String(err)}`,
      }
    }
  }

  // 让用户看到这个文件（不抢 webview 焦点；如果文件已经是 active editor 这步无害）
  try {
    await vscode.window.showTextDocument(targetUri, { preview: false, preserveFocus: false })
  } catch (err) {
    log(`[apply-edit-v2] showTextDocument failed (continuing): ${err instanceof Error ? err.message : String(err)}`)
  }

  const stateService = getDiffStateService(getChangeRegistry())

  let session
  try {
    session = await stateService.startSession({
      uri: targetUri,
      baselineText,
      proposedText,
    })
  } catch (err) {
    return {
      ok: false,
      error: `startSession failed: ${err instanceof Error ? err.message : String(err)}`,
    }
  }

  if (!session) {
    return {
      ok: true,
      applied: false,
      uri: targetUri.toString(),
      fsPath: targetUri.fsPath,
      status: 'no-op',
      reason: 'no changes detected (proposed === baseline)',
      total: 0,
      accepted: 0,
      rejected: 0,
    }
  }

  log(
    `[apply-edit-v2] session=${session.sessionId} file=${targetUri.fsPath} ` +
    `changes=${session.changes.length} description=${JSON.stringify(description)}`,
  )

  // 提示用户怎么操作（一次性 toast，非 modal 不阻塞）
  void vscode.window.showInformationMessage(
    `柳如烟: ${session.changes.length} 处改动待 review。点行上方 Accept/Reject，或用命令面板批量处理。`,
  )

  // 等待 session resolution（用户处理完 + finalize emit onResolved）或超时
  const summary = await waitForResolution(session.sessionId, DEFAULT_TIMEOUT_MS)

  if (!summary) {
    log(`[apply-edit-v2] session=${session.sessionId} TIMED OUT, aborting`)
    try {
      await stateService.abortSession(session.sessionId)
    } catch (err) {
      log(`[apply-edit-v2] abortSession failed: ${err instanceof Error ? err.message : String(err)}`)
    }
    return {
      ok: false,
      error: `user did not resolve diff within ${Math.floor(DEFAULT_TIMEOUT_MS / 1000)}s (auto-aborted, baseline restored)`,
      sessionId: session.sessionId,
      uri: targetUri.toString(),
      fsPath: targetUri.fsPath,
    }
  }

  log(
    `[apply-edit-v2] session=${session.sessionId} resolved status=${summary.status} ` +
    `accepted=${summary.accepted} rejected=${summary.rejected} total=${summary.totalChanges}`,
  )

  return {
    ok: true,
    applied: summary.accepted > 0,
    uri: summary.uri.toString(),
    fsPath: summary.uri.fsPath,
    sessionId: summary.sessionId,
    accepted: summary.accepted,
    rejected: summary.rejected,
    total: summary.totalChanges,
    status: summary.status,
    reason: summary.status === 'aborted'
      ? 'user aborted session (baseline restored)'
      : `${summary.accepted} accepted / ${summary.rejected} rejected of ${summary.totalChanges} total`,
  }
}

function parseTargetUri(body: ApplyEditV2Body): vscode.Uri | null {
  try {
    if (typeof body.uri === 'string') return vscode.Uri.parse(body.uri)
    if (typeof body.fsPath === 'string') return vscode.Uri.file(body.fsPath)
  } catch {
    return null
  }
  return null
}

async function waitForResolution(
  sessionId: string,
  timeoutMs: number,
): Promise<ResolvedSummary | null> {
  return await new Promise(resolve => {
    const stateService = getDiffStateService(getChangeRegistry())
    const subscription = stateService.onResolved((summary: ResolvedSummary) => {
      if (summary.sessionId !== sessionId) return
      cleanup()
      resolve(summary)
    })
    const timer = setTimeout(() => {
      cleanup()
      resolve(null)
    }, timeoutMs)
    function cleanup(): void {
      subscription.dispose()
      clearTimeout(timer)
    }
  })
}
