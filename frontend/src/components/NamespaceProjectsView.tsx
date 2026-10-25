import { useCallback, useEffect, useState } from 'react'
import {
  FolderKanban, RefreshCw, Save, Plus, Trash2, Globe, History, Archive, Link2,
} from 'lucide-react'
import {
  listNamespaces, getNamespace, updateNamespaceCard,
  createNamespace, deleteNamespace,
  getGlobalMemory, updateGlobalMemory,
  listSessionsByAgent, setSessionProject,
  type ProjectNamespace, type Session,
} from '../api/chat'
import { useAlertDialog, useConfirmDialog } from './ConfirmDialog'

interface NamespaceProjectsViewProps {
  agentId: string
}

type Selection = { kind: 'global' } | { kind: 'project'; nsId: string } | null

/**
 * 项目档案视图（v5 记忆分层 · 2026-10-24）。
 *
 * 左列：全局层 + 项目列表；右列：状态卡（可编辑）/ 全局层（可编辑）。
 * 会话绑定区：v5 的确定性手动入口——常规路径是"会话开头自动识别 + 工作区锚定 +
 * 对话里说一声（set_project_binding 工具）"，这里提供可视化兜底。
 * 后端契约见 backend/namespace_manager.py 与 routes/memory_routes.py。
 */
export default function NamespaceProjectsView({ agentId }: NamespaceProjectsViewProps) {
  const [namespaces, setNamespaces] = useState<ProjectNamespace[]>([])
  const [modeActive, setModeActive] = useState(false)
  const [loading, setLoading] = useState(false)
  const [selection, setSelection] = useState<Selection>(null)

  const [detailCard, setDetailCard] = useState('')
  const [editing, setEditing] = useState(false)
  const [editContent, setEditContent] = useState('')
  const [timeline, setTimeline] = useState<Array<Record<string, unknown>>>([])
  const [showTimeline, setShowTimeline] = useState(false)
  const [saving, setSaving] = useState(false)

  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')

  // 会话绑定区
  const [sessions, setSessions] = useState<Session[]>([])
  const [bindSessionId, setBindSessionId] = useState<string>('')
  const [binding, setBinding] = useState(false)

  const { alert: alertAction, dialog: alertDialog } = useAlertDialog()
  const { confirm, dialog: confirmDialog } = useConfirmDialog()

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const result = await listNamespaces(agentId)
      setNamespaces(result.namespaces)
      setModeActive(result.active)
    } catch {
      setNamespaces([])
    }
    setLoading(false)
  }, [agentId])

  useEffect(() => { void refresh() }, [refresh])

  // 会话列表（绑定区下拉用）：绑定字段随列表回传
  const refreshSessions = useCallback(async () => {
    try {
      const list = await listSessionsByAgent(agentId)
      setSessions(list.slice(0, 30))
      setBindSessionId(prev => prev || list[0]?.id || '')
    } catch {
      setSessions([])
    }
  }, [agentId])

  useEffect(() => { if (modeActive) void refreshSessions() }, [modeActive, refreshSessions])

  // 选中态变化时加载右侧内容
  useEffect(() => {
    setEditing(false)
    setShowTimeline(false)
    if (!selection) { setDetailCard(''); setTimeline([]); return }
    if (selection.kind === 'global') {
      getGlobalMemory(agentId)
        .then(r => { setDetailCard(r.content); setEditContent(r.content) })
        .catch(() => setDetailCard(''))
      return
    }
    getNamespace(selection.nsId, agentId)
      .then(r => {
        setDetailCard(r.card)
        setEditContent(r.card)
        setTimeline(r.timeline || [])
      })
      .catch(() => { setDetailCard(''); setTimeline([]) })
  }, [selection, agentId])

  const selectedProject = selection?.kind === 'project'
    ? namespaces.find(ns => ns.id === selection.nsId)
    : undefined

  async function saveCard() {
    if (!selection) return
    setSaving(true)
    try {
      if (selection.kind === 'global') {
        await updateGlobalMemory(editContent, agentId)
      } else {
        await updateNamespaceCard(selection.nsId, editContent, agentId)
      }
      setDetailCard(editContent)
      setEditing(false)
    } catch { await alertAction('保存失败') }
    setSaving(false)
  }

  async function handleCreate() {
    const name = newName.trim()
    if (!name) return
    setSaving(true)
    try {
      await createNamespace(name, { agentId })
      setNewName('')
      setCreating(false)
      await refresh()
      setModeActive(true)
    } catch (e) {
      await alertAction(`创建失败: ${(e as Error).message || e}`)
    }
    setSaving(false)
  }

  async function handleDelete(ns: ProjectNamespace) {
    const confirmed = await confirm(
      `注销项目「${ns.name}」？`,
      '状态卡与 timeline 会归档到 _archived/ 目录，可手工恢复；会话绑定不会自动解除。',
    )
    if (!confirmed) return
    try {
      await deleteNamespace(ns.id, agentId)
      if (selection?.kind === 'project' && selection.nsId === ns.id) setSelection(null)
      await refresh()
    } catch (e) {
      await alertAction(`注销失败: ${(e as Error).message || e}`)
    }
  }

  async function handleBind(nsId: string | null) {
    if (!bindSessionId) return
    setBinding(true)
    try {
      await setSessionProject(bindSessionId, nsId, agentId)
      await refreshSessions()
    } catch (e) {
      await alertAction(`绑定失败: ${(e as Error).message || e}`)
    }
    setBinding(false)
  }

  function renderTimelineEvent(entry: Record<string, unknown>): string {
    return String(entry.event ?? '（无标题）')
  }

  return (
    <div className="memory-section diaries-layout">
      {/* 左列：项目列表 */}
      <div className="diary-list">
        <div className="section-toolbar" style={{ padding: 0, marginBottom: 6 }}>
          <h4 style={{ margin: 0 }}>项目档案</h4>
          <div style={{ display: 'flex', gap: 4 }}>
            <button className="text-btn" onClick={() => void refresh()} title="刷新" disabled={loading}>
              <RefreshCw size={12} style={{ display: 'inline', marginRight: 3 }} />
            </button>
            <button className="text-btn" onClick={() => { setCreating(v => !v); setNewName('') }} title="新建项目">
              <Plus size={12} style={{ display: 'inline', marginRight: 3 }} />
            </button>
          </div>
        </div>

        {!modeActive && namespaces.length === 0 && (
          <p className="hint" style={{ padding: '0 6px' }}>
            尚未启用项目记忆。创建首个项目，或运行
            <code> scripts/migrate_memory_namespaces.py </code>
            迁移存量 MEMORY.md。
          </p>
        )}

        {creating && (
          <div style={{ padding: '4px 6px 8px', display: 'flex', gap: 4 }}>
            <input
              className="namespace-new-input"
              value={newName}
              placeholder="项目名，如：中微智码维权"
              onChange={e => setNewName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') void handleCreate() }}
              autoFocus
              style={{ flex: 1 }}
            />
            <button className="icon-btn-sm success" onClick={() => void handleCreate()} disabled={saving || !newName.trim()}>
              <Save size={13} />
            </button>
          </div>
        )}

        <button
          className={`diary-item ${selection?.kind === 'global' ? 'active' : ''}`}
          onClick={() => setSelection({ kind: 'global' })}
          title="关系里程碑与跨项目认知（每轮常驻注入，不分项目）"
        >
          <Globe size={14} className="chevron" />
          <div className="session-item-meta" style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 2, overflow: 'hidden' }}>
            <span style={{ fontSize: 13 }}>全局 · 关系层</span>
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>常驻注入</span>
          </div>
        </button>

        {namespaces.map(ns => (
          <button
            key={ns.id}
            className={`diary-item ${selection?.kind === 'project' && selection.nsId === ns.id ? 'active' : ''}`}
            onClick={() => setSelection({ kind: 'project', nsId: ns.id })}
            title={ns.summary || ns.name}
          >
            {ns.status === 'archived'
              ? <Archive size={14} className="chevron" />
              : <FolderKanban size={14} className="chevron" />}
            <div className="session-item-meta" style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 2, overflow: 'hidden' }}>
              <span style={{ fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%' }}>
                {ns.name}
              </span>
              {ns.summary && (
                <span style={{ fontSize: 11, color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%' }}>
                  {ns.summary}
                </span>
              )}
            </div>
          </button>
        ))}
        {namespaces.length === 0 && !loading && (
          <p className="empty-hint" style={{ paddingLeft: 6 }}>暂无项目</p>
        )}
      </div>

      {/* 右列：详情 */}
      <div className="diary-detail">
        {!selection ? (
          <div className="empty-state">
            <FolderKanban size={32} />
            <p>选择一个项目查看状态卡</p>
            <p className="hint">绑定会话后，该会话只加载此项目的记忆（输入框工具栏可选）</p>
          </div>
        ) : (
          <div className="memory-section" style={{ padding: '0 0 0 10px', height: '100%', overflow: 'auto' }}>
            <div className="section-toolbar">
              <h3>
                {selection.kind === 'global' ? (
                  <><Globe size={15} style={{ display: 'inline', verticalAlign: '-2px', marginRight: 6 }} />全局 · 关系层</>
                ) : (
                  <><FolderKanban size={15} style={{ display: 'inline', verticalAlign: '-2px', marginRight: 6 }} />{selectedProject?.name ?? selection.nsId}</>
                )}
              </h3>
              <div className="edit-actions" style={{ gap: 6 }}>
                {selection.kind === 'project' && timeline.length > 0 && (
                  <button className="text-btn" onClick={() => setShowTimeline(v => !v)} title="事件溯源（append-only 事实之源）">
                    <History size={12} style={{ display: 'inline', marginRight: 3, verticalAlign: '-1px' }} />
                    {showTimeline ? '卡片' : `溯源 ${timeline.length}`}
                  </button>
                )}
                {!editing ? (
                  <button className="text-btn" onClick={() => { setEditContent(detailCard); setEditing(true) }}>编辑</button>
                ) : (
                  <>
                    <button className="icon-btn-sm success" onClick={() => void saveCard()} disabled={saving}>
                      <Save size={14} /> {saving ? '...' : '保存'}
                    </button>
                    <button className="icon-btn-sm" onClick={() => setEditing(false)}>取消</button>
                  </>
                )}
                {selection.kind === 'project' && selectedProject && (
                  <button
                    className="icon-btn-sm danger"
                    onClick={() => void handleDelete(selectedProject)}
                    title="注销此项目（数据归档）"
                  >
                    <Trash2 size={12} />
                  </button>
                )}
              </div>
            </div>

            {selection.kind === 'project' && selectedProject?.workspace_path && (
              <p className="hint" style={{ marginTop: 0 }}>
                工作区锚定：{selectedProject.workspace_path}（该目录下的会话自动绑定本项目）
              </p>
            )}

            {modeActive && selection?.kind === 'project' && (
              <div className="namespace-bind-box">
                <span className="namespace-bind-label">
                  <Link2 size={12} style={{ display: 'inline', verticalAlign: '-2px', marginRight: 4 }} />
                  会话绑定（兜底修正；常规为自动识别）
                </span>
                <select
                  className="namespace-new-input"
                  value={bindSessionId}
                  onChange={e => setBindSessionId(e.target.value)}
                >
                  {sessions.map(s => (
                    <option key={s.id} value={s.id}>
                      {s.title || '无标题'}{s.project_namespace ? ` · 已绑:${s.project_namespace}` : ' · 未绑定'}
                    </option>
                  ))}
                </select>
                <div style={{ display: 'flex', gap: 6 }}>
                  <button
                    className="icon-btn-sm success"
                    disabled={binding || !bindSessionId}
                    onClick={() => void handleBind(selection.nsId)}
                    title="把所选会话绑定到当前项目"
                  >
                    <Save size={13} /> 绑定
                  </button>
                  <button
                    className="icon-btn-sm"
                    disabled={binding || !bindSessionId}
                    onClick={() => void handleBind(null)}
                    title="解绑所选会话"
                  >
                    解绑
                  </button>
                </div>
              </div>
            )}

            {showTimeline && selection.kind === 'project' ? (
              <div className="reflections-list">
                {[...timeline].reverse().map((entry, idx) => (
                  <div key={idx} className="reflection-item-card">
                    <div className="reflection-item-header">
                      <span className="reflection-item-title">{renderTimelineEvent(entry)}</span>
                      <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{String(entry.ts ?? '')}</span>
                    </div>
                    {entry.decision ? <pre className="reflection-item-body">{String(entry.decision)}</pre> : null}
                  </div>
                ))}
              </div>
            ) : editing ? (
              <textarea
                className="memory-editor"
                value={editContent}
                onChange={e => setEditContent(e.target.value)}
                rows={16}
                autoFocus
              />
            ) : detailCard ? (
              <pre className="memory-text">{detailCard}</pre>
            ) : (
              <div className="empty-state">
                <FolderKanban size={32} />
                <p>{selection.kind === 'global' ? '暂无全局记忆' : '暂无状态卡内容'}</p>
              </div>
            )}
          </div>
        )}
      </div>

      {confirmDialog}
      {alertDialog}
    </div>
  )
}
