import * as vscode from 'vscode'

import { ChangeRegistry } from '../ChangeRegistry'
import { DiffChange } from '../DiffChange'
import { calculateDiff } from './DiffCalculator'

/**
 * Phase 3 v2 Step 4 - DiffStateService
 *
 * session lifecycle 管理。一个 session = 用户对单个文件的一次 LLM 编辑预览。
 *
 * 流程：
 * 1. backend POST /vscode/apply-edit → endpoint 调 startSession(uri, baseline, proposed)
 *    - 把 buffer 替换成 proposedText（A 风格：用户立刻看到改后版本）
 *    - calculateDiff → 注册 changes 到 ChangeRegistry
 *    - 设 setContext('liuruyan.diff.active', true)
 *    - 返回 SessionId
 * 2. 用户在 IDE 里 accept/reject hunks（每次操作 ChangeResolutionService 处理）
 * 3. 当所有 changes 都 status != pending → 自动 emit onResolved
 *    - endpoint 收到 emit 后给 backend 回填 ok=true + summary
 * 4. 用户也可以主动 abortSession（reject all + 还原 baseline）
 *
 * **崩溃恢复**（v2.1 #1 已实现）：
 * - attachStorage(memento) 注入 context.globalState
 * - startSession 调 _persistSession 写 baseline 到 globalState
 * - _finalizeSession / abortSession 调 _clearPersistedSession 删 backup
 * - 模块外提供 recoverStaleSessions(memento) 在 activate 时检测 + 提示用户
 */

const PERSIST_KEY = 'liuruyan.diff.staleSessions'

interface PersistedSession {
  sessionId: string
  uriString: string
  baselineText: string
  proposedText: string
  startedAt: number
}

export interface SessionInfo {
  id: string
  uri: vscode.Uri
  baselineText: string
  proposedText: string
  startedAt: number
}

export interface ResolvedSummary {
  sessionId: string
  uri: vscode.Uri
  accepted: number
  rejected: number
  totalChanges: number
  status: 'completed' | 'aborted'
}

export class DiffStateService implements vscode.Disposable {
  private readonly _sessions = new Map<string, SessionInfo>()
  private readonly _resolveEmitter = new vscode.EventEmitter<ResolvedSummary>()
  private readonly _disposables: vscode.Disposable[] = []

  readonly onResolved: vscode.Event<ResolvedSummary> = this._resolveEmitter.event

  private _storage: vscode.Memento | null = null

  constructor(private readonly registry: ChangeRegistry) {
    this._disposables.push(
      registry.onDidChange(ev => {
        if (ev.reason === 'status') this._maybeAutoResolve(ev.uri)
      }),
    )
  }

  /**
   * 注入持久化存储。init.ts 在 activate 时传入 context.globalState。
   * 不调用本方法时 startSession 不持久化（向后兼容 / 测试场景）。
   */
  attachStorage(storage: vscode.Memento): void {
    this._storage = storage
  }

  /**
   * 开启 diff session：替换 buffer 为 proposedText + 注册 changes。
   * 返回 SessionId 或 null（计算后没有任何 changes，无需开 session）。
   */
  async startSession(args: {
    uri: vscode.Uri
    baselineText: string
    proposedText: string
  }): Promise<{ sessionId: string; changes: DiffChange[] } | null> {
    const changes = calculateDiff({
      uri: args.uri,
      baselineText: args.baselineText,
      proposedText: args.proposedText,
    })
    if (changes.length === 0) return null

    // A 风格：先把 buffer 替换为 proposedText，用户打开就看到改后版本
    const replaced = await this._replaceFullBuffer(args.uri, args.proposedText)
    if (!replaced) {
      throw new Error('DiffStateService.startSession: failed to replace buffer with proposedText')
    }

    for (const change of changes) {
      this.registry.register(change)
    }

    const sessionId = generateSessionId()
    const sessionInfo: SessionInfo = {
      id: sessionId,
      uri: args.uri,
      baselineText: args.baselineText,
      proposedText: args.proposedText,
      startedAt: Date.now(),
    }
    this._sessions.set(sessionId, sessionInfo)
    // 崩溃恢复：把 baseline 备份到 globalState，重启时可还原
    await this._persistSession(sessionInfo)
    await vscode.commands.executeCommand('setContext', 'liuruyan.diff.active', true)
    return { sessionId, changes }
  }

  /**
   * 用户主动 abort：reject 所有 pending → 整文件还原 baseline → 落盘。
   */
  async abortSession(sessionId: string): Promise<ResolvedSummary | null> {
    const session = this._sessions.get(sessionId)
    if (!session) return null

    // 直接整文件还原 baseline 而非逐 hunk reject（更快、避免 offset shift）
    const ok = await this._replaceFullBuffer(session.uri, session.baselineText)
    if (!ok) {
      throw new Error(`abortSession: failed to revert ${session.uri.toString()} to baseline`)
    }

    const pending = this.registry.getPendingByUri(session.uri)
    for (const change of pending) {
      this.registry.setStatus(change.id, 'rejected')
    }
    return this._finalizeSession(sessionId, 'aborted')
  }

  getSession(sessionId: string): SessionInfo | undefined {
    return this._sessions.get(sessionId)
  }

  getActiveSessionForUri(uri: vscode.Uri): SessionInfo | undefined {
    const target = uri.toString()
    for (const s of this._sessions.values()) {
      if (s.uri.toString() === target) return s
    }
    return undefined
  }

  hasActiveSessions(): boolean {
    return this._sessions.size > 0
  }

  dispose(): void {
    this._sessions.clear()
    this._resolveEmitter.dispose()
    for (const d of this._disposables) d.dispose()
    this._disposables.length = 0
  }

  /**
   * 检测某个 uri 上的所有 changes 是否都已 resolved → emit onResolved
   */
  private _maybeAutoResolve(uri: vscode.Uri): void {
    const all = this.registry.getByUri(uri)
    if (all.length === 0) return
    const stillPending = all.some(c => c.status === 'pending')
    if (stillPending) return

    const session = this.getActiveSessionForUri(uri)
    if (!session) return
    void this._finalizeSession(session.id, 'completed')
  }

  private async _finalizeSession(
    sessionId: string,
    status: 'completed' | 'aborted',
  ): Promise<ResolvedSummary | null> {
    const session = this._sessions.get(sessionId)
    if (!session) return null

    const all = this.registry.getByUri(session.uri)
    const accepted = all.filter(c => c.status === 'accepted').length
    const rejected = all.filter(c => c.status === 'rejected').length

    // 落盘：保存 buffer 到磁盘
    await this._saveBuffer(session.uri)

    // 清理 registry + session + 持久化备份
    this.registry.clearByUri(session.uri)
    this._sessions.delete(sessionId)
    await this._clearPersistedSession(sessionId)
    if (this._sessions.size === 0) {
      await vscode.commands.executeCommand('setContext', 'liuruyan.diff.active', false)
    }

    const summary: ResolvedSummary = {
      sessionId,
      uri: session.uri,
      accepted,
      rejected,
      totalChanges: all.length,
      status,
    }
    this._resolveEmitter.fire(summary)
    return summary
  }

  private async _replaceFullBuffer(uri: vscode.Uri, content: string): Promise<boolean> {
    let document: vscode.TextDocument
    try {
      document = await vscode.workspace.openTextDocument(uri)
    } catch {
      return false
    }
    const fullRange = new vscode.Range(
      new vscode.Position(0, 0),
      document.lineAt(document.lineCount - 1).range.end,
    )
    const edit = new vscode.WorkspaceEdit()
    edit.replace(uri, fullRange, content)
    return await vscode.workspace.applyEdit(edit)
  }

  /**
   * 把 session 备份到 globalState 用于崩溃恢复。失败不抛错（持久化是 best-effort）。
   */
  private async _persistSession(session: SessionInfo): Promise<void> {
    if (!this._storage) return
    try {
      const existing = (this._storage.get<PersistedSession[]>(PERSIST_KEY) ?? [])
        .filter(p => p.sessionId !== session.id)
      existing.push({
        sessionId: session.id,
        uriString: session.uri.toString(),
        baselineText: session.baselineText,
        proposedText: session.proposedText,
        startedAt: session.startedAt,
      })
      await this._storage.update(PERSIST_KEY, existing)
    } catch {
      // best-effort
    }
  }

  private async _clearPersistedSession(sessionId: string): Promise<void> {
    if (!this._storage) return
    try {
      const existing = this._storage.get<PersistedSession[]>(PERSIST_KEY) ?? []
      const filtered = existing.filter(p => p.sessionId !== sessionId)
      if (filtered.length === existing.length) return
      await this._storage.update(PERSIST_KEY, filtered.length > 0 ? filtered : undefined)
    } catch {
      // best-effort
    }
  }

  private async _saveBuffer(uri: vscode.Uri): Promise<boolean> {
    const document = vscode.workspace.textDocuments.find(
      d => d.uri.toString() === uri.toString(),
    )
    if (!document) return false
    if (!document.isDirty) return true
    return await document.save()
  }
}

function generateSessionId(): string {
  return `dsession_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`
}

let _instance: DiffStateService | null = null

export function getDiffStateService(registry: ChangeRegistry): DiffStateService {
  if (!_instance) {
    _instance = new DiffStateService(registry)
  }
  return _instance
}

export function resetDiffStateServiceForTesting(): void {
  if (_instance) {
    _instance.dispose()
    _instance = null
  }
}

/**
 * 在 extension activate 时调用一次：检测 globalState 里是否有上次未完成的 diff session
 * （IDE 崩溃 / 强行关闭 / 重启），逐个提示用户「还原 baseline」/「保留当前 buffer」/「忽略」。
 *
 * 不依赖 DiffStateService 单例：直接读 globalState + 用 vscode.workspace.fs 写文件。
 * 因为单例在 activate 极早期还没拿到 storage，重要的是清理旧 sessions 不让它们污染新会话。
 */
export async function recoverStaleSessions(storage: vscode.Memento): Promise<void> {
  const stale = storage.get<PersistedSession[]>(PERSIST_KEY) ?? []
  if (stale.length === 0) return

  for (const session of stale) {
    let uri: vscode.Uri
    try {
      uri = vscode.Uri.parse(session.uriString)
    } catch {
      continue
    }
    const fileName = uri.fsPath.split(/[\\/]/).pop() ?? uri.toString()
    const ageMin = Math.floor((Date.now() - session.startedAt) / 60_000)
    const choice = await vscode.window.showWarningMessage(
      `柳如烟: 发现 ${ageMin} 分钟前未完成的 diff session（${fileName}）。 ` +
        `当前文件可能含未 review 的改动。`,
      { modal: false },
      'Restore Baseline',
      'Keep Current',
      'Ignore',
    )
    if (choice === 'Restore Baseline') {
      try {
        const edit = new vscode.WorkspaceEdit()
        const doc = await vscode.workspace.openTextDocument(uri)
        const fullRange = new vscode.Range(
          new vscode.Position(0, 0),
          doc.lineAt(doc.lineCount - 1).range.end,
        )
        edit.replace(uri, fullRange, session.baselineText)
        await vscode.workspace.applyEdit(edit)
        await doc.save()
      } catch {
        // 文件可能被删除 / 权限问题，best-effort
      }
    }
    // Keep Current / Ignore 都是不动文件
  }

  // 清理 globalState：所有 stale sessions 都已处理完
  await storage.update(PERSIST_KEY, undefined)
}
