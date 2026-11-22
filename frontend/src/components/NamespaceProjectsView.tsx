import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  FolderKanban, RefreshCw, Save, Plus, Trash2, Globe,
  RotateCcw, Sparkles, Archive, ChevronDown, ChevronRight,
} from 'lucide-react'
import {
  listNamespaces, getNamespace, updateNamespaceCard,
  createNamespace, deleteNamespace, rebuildNamespaceCard,
  listArchivedNamespaces, restoreArchivedNamespace,
  getGlobalMemory, updateGlobalMemory,
  listSessionsByAgent, setSessionProject,
  type ProjectNamespace, type Session, type TimelineEntry, type ArchivedNamespaceBackup, type NamespaceTopic,
} from '../api/chat'
import { useAlertDialog, useConfirmDialog } from './ConfirmDialog'
import NamespaceMetaBar from './projects/NamespaceMetaBar'
import NamespaceSessionLinker from './projects/NamespaceSessionLinker'
import NamespaceTopicsPanel from './projects/NamespaceTopicsPanel'
import NamespaceTimelineView from './projects/NamespaceTimelineView'

interface NamespaceProjectsViewProps {
  agentId: string
  currentSessionId?: string
  onSessionsChanged?: () => Promise<void> | void
}

type Selection = { kind: 'global' } | { kind: 'project'; nsId: string } | null
type StatusFilter = 'active' | 'archived'

const TIMELINE_PAGE_SIZE = 30

/**
 * 项目档案主视图（v5.1 模块化）：
 * - 对话输入框保持完全无感，此面板承担项目感知、纠偏与管理闭环；
 * - 左列：全局层 + 活跃/归档项目列表 + 误删备份恢复；
 * - 右列：当前会话状态条 + 元信息编辑 + 双向会话关联 + 状态卡编辑/重算 + 时间线分页检索。
 */
export default function NamespaceProjectsView({
  agentId,
  currentSessionId,
  onSessionsChanged,
}: NamespaceProjectsViewProps) {
  const [namespaces, setNamespaces] = useState<ProjectNamespace[]>([])
  const [modeActive, setModeActive] = useState(false)
  const [loading, setLoading] = useState(false)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('active')
  const [selection, setSelection] = useState<Selection>(null)

  // 右侧状态卡 / 全局层
  const [detailCard, setDetailCard] = useState('')
  const [editing, setEditing] = useState(false)
  const [editContent, setEditContent] = useState('')
  const [saving, setSaving] = useState(false)
  const [rebuilding, setRebuilding] = useState(false)

  // 时间线分页与检索 & 树叉列表
  const [topics, setTopics] = useState<NamespaceTopic[]>([])
  const [timeline, setTimeline] = useState<TimelineEntry[]>([])
  const [timelineTotal, setTimelineTotal] = useState(0)
  const [timelineLoading, setTimelineLoading] = useState(false)
  const [timelineQuery, setTimelineQuery] = useState('')

  // 新建项目表单
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newAliases, setNewAliases] = useState('')
  const [newWorkspace, setNewWorkspace] = useState('')

  // 全量会话（用于当前会话状态条 & 双向关联）
  const [sessions, setSessions] = useState<Session[]>([])
  const [bannerBusy, setBannerBusy] = useState(false)

  // _archived/ 备份恢复区
  const [archivedBackups, setArchivedBackups] = useState<ArchivedNamespaceBackup[]>([])
  const [showBackups, setShowBackups] = useState(false)
  const [restoringFolder, setRestoringFolder] = useState<string | null>(null)

  const { alert: alertAction, dialog: alertDialog } = useAlertDialog()
  const { confirm, dialog: confirmDialog } = useConfirmDialog()

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [result, backups] = await Promise.all([
        listNamespaces(agentId),
        listArchivedNamespaces(agentId).catch(() => ({ archived: [] })),
      ])
      setNamespaces(result.namespaces)
      setModeActive(result.active)
      setArchivedBackups(backups.archived || [])
    } catch {
      setNamespaces([])
      setArchivedBackups([])
    }
    setLoading(false)
  }, [agentId])

  useEffect(() => { void refresh() }, [refresh])

  const refreshSessions = useCallback(async () => {
    try {
      const list = await listSessionsByAgent(agentId)
      setSessions(list)
    } catch {
      setSessions([])
    }
  }, [agentId])

  useEffect(() => {
    if (modeActive) void refreshSessions()
  }, [modeActive, refreshSessions])

  const notifySessionsChanged = useCallback(async () => {
    await refreshSessions()
    if (onSessionsChanged) await onSessionsChanged()
  }, [refreshSessions, onSessionsChanged])

  const loadProjectDetail = useCallback(
    async (nsId: string, q: string, offset = 0, append = false) => {
      setTimelineLoading(true)
      try {
        const res = await getNamespace(nsId, agentId, {
          timelineLimit: TIMELINE_PAGE_SIZE,
          timelineOffset: offset,
          q: q.trim() || undefined,
          newestFirst: true,
        })
        if (!append) {
          setDetailCard(res.card)
          setEditContent(res.card)
          setTopics(res.topics || [])
          setTimeline(res.timeline || [])
        } else {
          setTimeline(prev => [...prev, ...(res.timeline || [])])
        }
        setTimelineTotal(res.timeline_total ?? (res.timeline || []).length)
      } catch {
        if (!append) {
          setDetailCard('')
          setTopics([])
          setTimeline([])
          setTimelineTotal(0)
        }
      }
      setTimelineLoading(false)
    },
    [agentId],
  )

  // 选中项切换时加载详情
  useEffect(() => {
    setEditing(false)
    setTimelineQuery('')
    if (!selection) {
      setDetailCard('')
      setTopics([])
      setTimeline([])
      setTimelineTotal(0)
      return
    }
    if (selection.kind === 'global') {
      setTopics([])
      getGlobalMemory(agentId)
        .then(r => { setDetailCard(r.content); setEditContent(r.content) })
        .catch(() => setDetailCard(''))
      return
    }
    void loadProjectDetail(selection.nsId, '', 0, false)
  }, [selection, agentId, loadProjectDetail])

  // 时间线搜索变化时重新加载第一页
  const selectedProjectId = selection?.kind === 'project' ? selection.nsId : null
  useEffect(() => {
    if (!selectedProjectId) return
    const timer = setTimeout(() => {
      void loadProjectDetail(selectedProjectId, timelineQuery, 0, false)
    }, 200)
    return () => clearTimeout(timer)
  }, [selectedProjectId, timelineQuery, loadProjectDetail])

  const selectedProject = useMemo(
    () => (selectedProjectId ? namespaces.find(ns => ns.id === selectedProjectId) : undefined),
    [namespaces, selectedProjectId],
  )

  const activeProjects = useMemo(
    () => namespaces.filter(ns => ns.status !== 'archived'),
    [namespaces],
  )
  const archivedProjects = useMemo(
    () => namespaces.filter(ns => ns.status === 'archived'),
    [namespaces],
  )
  const visibleProjects = statusFilter === 'active' ? activeProjects : archivedProjects

  const currentSession = useMemo(
    () => (currentSessionId ? sessions.find(s => s.id === currentSessionId) : undefined),
    [sessions, currentSessionId],
  )

  async function saveCard() {
    if (!selection) return
    setSaving(true)
    try {
      if (selection.kind === 'global') {
        await updateGlobalMemory(editContent, agentId)
      } else {
        const res = await updateNamespaceCard(selection.nsId, editContent, agentId)
        if (res.namespace) {
          setNamespaces(prev => prev.map(n => (n.id === res.namespace!.id ? res.namespace! : n)))
        }
      }
      setDetailCard(editContent)
      setEditing(false)
    } catch {
      await alertAction('保存失败')
    }
    setSaving(false)
  }

  async function handleRebuildCard() {
    if (!selectedProject) return
    const ok = await confirm(
      `从时间线重算「${selectedProject.name}」状态卡？`,
      '将依据 timeline.jsonl 中的全部历史事实重新整理当前状态卡，并保留现有「红线与禁忌」章节。',
    )
    if (!ok) return
    setRebuilding(true)
    try {
      const res = await rebuildNamespaceCard(selectedProject.id, { agentId, useLlm: true })
      setDetailCard(res.card)
      setEditContent(res.card)
      setEditing(false)
      if (res.namespace) {
        setNamespaces(prev => prev.map(n => (n.id === res.namespace!.id ? res.namespace! : n)))
      }
    } catch (e) {
      await alertAction(`重算失败: ${(e as Error).message || e}`)
    }
    setRebuilding(false)
  }

  async function handleCreate() {
    const name = newName.trim()
    if (!name) return
    const aliases = newAliases
      .split(/[,，、\s]+/)
      .map(s => s.trim())
      .filter(Boolean)
    setSaving(true)
    try {
      const res = await createNamespace(name, {
        agentId,
        aliases,
        workspacePath: newWorkspace.trim() || undefined,
      })
      setNewName('')
      setNewAliases('')
      setNewWorkspace('')
      setCreating(false)
      setStatusFilter('active')
      await refresh()
      setModeActive(true)
      if (res.namespace?.id) {
        setSelection({ kind: 'project', nsId: res.namespace.id })
      }
    } catch (e) {
      await alertAction(`创建失败: ${(e as Error).message || e}`)
    }
    setSaving(false)
  }

  async function handleDelete(ns: ProjectNamespace) {
    const confirmed = await confirm(
      `注销项目「${ns.name}」？`,
      '状态卡与 timeline 会移入 _archived/ 目录留底（可在左下角恢复）；如果只是暂时不用，建议点右上角「归档」。',
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

  async function handleRestoreBackup(folderName: string) {
    setRestoringFolder(folderName)
    try {
      const res = await restoreArchivedNamespace(folderName, agentId)
      await refresh()
      setStatusFilter('active')
      if (res.namespace?.id) {
        setSelection({ kind: 'project', nsId: res.namespace.id })
      }
    } catch (e) {
      await alertAction(`恢复失败: ${(e as Error).message || e}`)
    }
    setRestoringFolder(null)
  }

  async function handleCurrentBannerBind(targetNs: string | null) {
    if (!currentSession) return
    setBannerBusy(true)
    try {
      await setSessionProject(currentSession.id, targetNs, agentId)
      await notifySessionsChanged()
    } catch (e) {
      await alertAction(`切换当前会话项目失败: ${(e as Error).message || e}`)
    }
    setBannerBusy(false)
  }

  function handleMetaUpdated(updated: ProjectNamespace) {
    setNamespaces(prev => prev.map(n => (n.id === updated.id ? updated : n)))
    if (updated.status === 'archived' && statusFilter === 'active') {
      setStatusFilter('archived')
    } else if (updated.status !== 'archived' && statusFilter === 'archived') {
      setStatusFilter('active')
    }
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
            <button
              className="text-btn"
              onClick={() => { setCreating(v => !v); setNewName(''); setNewAliases(''); setNewWorkspace('') }}
              title="新建项目"
            >
              <Plus size={12} style={{ display: 'inline', marginRight: 3 }} />
            </button>
          </div>
        </div>

        {/* 活跃 / 已归档过滤 */}
        {namespaces.length > 0 && (
          <div className="ns-filter-tabs">
            <button
              className={`ns-filter-tab ${statusFilter === 'active' ? 'active' : ''}`}
              onClick={() => setStatusFilter('active')}
            >
              活跃 ({activeProjects.length})
            </button>
            <button
              className={`ns-filter-tab ${statusFilter === 'archived' ? 'active' : ''}`}
              onClick={() => setStatusFilter('archived')}
            >
              已归档 ({archivedProjects.length})
            </button>
          </div>
        )}

        {!modeActive && namespaces.length === 0 && (
          <p className="hint" style={{ padding: '0 6px' }}>
            尚未启用项目记忆。创建首个项目，或运行
            <code> scripts/migrate_memory_namespaces.py </code>
            迁移存量 MEMORY.md。
          </p>
        )}

        {creating && (
          <div className="ns-create-form">
            <input
              className="namespace-new-input"
              value={newName}
              placeholder="项目名，如：中微智码维权"
              onChange={e => setNewName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') void handleCreate() }}
              autoFocus
            />
            <input
              className="namespace-new-input"
              value={newAliases}
              placeholder="口语别名（逗号分隔，如：中微, 回购案）"
              onChange={e => setNewAliases(e.target.value)}
            />
            <input
              className="namespace-new-input"
              value={newWorkspace}
              placeholder="关联工作区目录（可选，如 ."
              onChange={e => setNewWorkspace(e.target.value)}
            />
            <div className="ns-create-actions">
              <button className="icon-btn-sm" onClick={() => setCreating(false)}>取消</button>
              <button
                className="icon-btn-sm success"
                onClick={() => void handleCreate()}
                disabled={saving || !newName.trim()}
              >
                创建
              </button>
            </div>
          </div>
        )}

        {/* 全局层入口 */}
        <button
          className={`diary-item ${selection?.kind === 'global' ? 'active' : ''}`}
          onClick={() => setSelection({ kind: 'global' })}
          title="关系记忆、跨项目习惯（常驻注入）"
        >
          <Globe size={14} className="chevron" />
          <span>全局记忆（关系层）</span>
        </button>

        {/* 项目条目 */}
        {visibleProjects.map(ns => (
          <div key={ns.id} className="diary-item-wrapper">
            <button
              className={`diary-item ${selection?.kind === 'project' && selection.nsId === ns.id ? 'active' : ''}`}
              onClick={() => setSelection({ kind: 'project', nsId: ns.id })}
              title={ns.summary || ns.name}
            >
              <FolderKanban size={14} className="chevron" />
              <div className="ns-list-item-row">
                <span className="ns-list-item-name">{ns.name}</span>
                {ns.aliases?.length > 0 && (
                  <span className="ns-list-item-meta">{ns.aliases.slice(0, 2).join(' · ')}</span>
                )}
              </div>
            </button>
            <button
              className="diary-delete-btn"
              onClick={() => void handleDelete(ns)}
              title={`注销项目「${ns.name}」`}
            >
              <Trash2 size={12} />
            </button>
          </div>
        ))}

        {visibleProjects.length === 0 && modeActive && (
          <p className="empty-hint">
            {statusFilter === 'active' ? '暂无活跃项目' : '暂无已归档项目'}
          </p>
        )}

        {/* _archived/ 误删备份恢复 */}
        {archivedBackups.length > 0 && (
          <div className="ns-archived-box">
            <button
              className="ns-archived-toggle"
              onClick={() => setShowBackups(v => !v)}
            >
              {showBackups ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              <Archive size={12} />
              <span>已注销留底 ({archivedBackups.length})</span>
            </button>
            {showBackups && (
              <div className="ns-archived-list">
                {archivedBackups.map(b => (
                  <div key={b.folder_name} className="ns-archived-row">
                    <div className="ns-archived-info">
                      <span className="ns-archived-name" title={b.name}>{b.name}</span>
                      <span className="ns-archived-sub">
                        {b.timeline_count} 条时间线{b.archived_at ? ` · ${b.archived_at}` : ''}
                      </span>
                    </div>
                    <button
                      className="icon-btn-sm"
                      disabled={restoringFolder === b.folder_name}
                      onClick={() => void handleRestoreBackup(b.folder_name)}
                      title="恢复此项目"
                    >
                      <RotateCcw size={11} /> 恢复
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 右列：当前会话感知条 + 详情 */}
      <div className="diary-detail">
        {modeActive && currentSession && (
          <div className="ns-current-banner">
            <div className="ns-current-banner-left">
              <Sparkles size={13} className="ns-current-icon" />
              <span className="ns-current-badge">当前会话</span>
              <span className="ns-current-title" title={currentSession.title || currentSession.id}>
                {currentSession.title || '无标题会话'}
              </span>
            </div>
            <select
              className="ns-current-banner-select"
              value={currentSession.project_namespace || ''}
              disabled={bannerBusy}
              onChange={e => void handleCurrentBannerBind(e.target.value || null)}
              title="切换当前对话会话的项目归属"
            >
              <option value="">未绑定（自动识别中）</option>
              {activeProjects.map(p => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </div>
        )}

        {selection ? (
          <>
            {/* 项目元信息编辑栏（仅项目态） */}
            {selectedProject && (
              <NamespaceMetaBar
                project={selectedProject}
                agentId={agentId}
                onUpdated={handleMetaUpdated}
                onError={msg => void alertAction(msg)}
              />
            )}

            {/* 双向会话关联区（仅项目态） */}
            {selectedProject && (
              <NamespaceSessionLinker
                project={selectedProject}
                allProjects={namespaces}
                sessions={sessions}
                agentId={agentId}
                currentSessionId={currentSessionId}
                onChanged={notifySessionsChanged}
                onError={msg => void alertAction(msg)}
              />
            )}

            {/* 状态卡 / 全局层工具栏 */}
            <div className="section-toolbar">
              <h3>
                {selection.kind === 'global'
                  ? '全局记忆（关系与跨项目习惯）'
                  : '当前状态卡（FACTS.md）'}
              </h3>
              <div className="edit-actions" style={{ gap: 6 }}>
                {selection.kind === 'project' && !editing && (
                  <button
                    className="text-btn"
                    onClick={() => void handleRebuildCard()}
                    disabled={rebuilding}
                    title="从 timeline.jsonl 全量事实重新整理状态卡"
                  >
                    <RotateCcw size={12} style={{ display: 'inline', marginRight: 3 }} />
                    {rebuilding ? '重算中...' : '从时间线重算'}
                  </button>
                )}
                {!editing ? (
                  <button className="text-btn" onClick={() => setEditing(true)}>编辑</button>
                ) : (
                  <>
                    <button className="icon-btn-sm success" onClick={() => void saveCard()} disabled={saving}>
                      <Save size={14} /> {saving ? '...' : '保存'}
                    </button>
                    <button className="icon-btn-sm" onClick={() => { setEditing(false); setEditContent(detailCard) }}>
                      取消
                    </button>
                  </>
                )}
              </div>
            </div>

            {!editing ? (
              detailCard ? (
                <pre className="memory-text">{detailCard}</pre>
              ) : (
                <div className="empty-state">
                  <FolderKanban size={32} />
                  <p>{selection.kind === 'global' ? '全局记忆为空' : '状态卡尚未沉淀内容'}</p>
                  <p className="hint">随着对话推进，提炼器会自动合并最新状态；也可以点击右上角手动编辑</p>
                </div>
              )
            ) : (
              <textarea
                className="memory-editor"
                value={editContent}
                onChange={e => setEditContent(e.target.value)}
                rows={14}
              />
            )}

            {/* 专题树叉（仅项目态） */}
            {selection.kind === 'project' && selectedProjectId && (
              <NamespaceTopicsPanel
                nsId={selectedProjectId}
                agentId={agentId}
                topics={topics}
                onTopicsChanged={setTopics}
                onError={msg => void alertAction(msg)}
              />
            )}

            {/* 时间线溯源（仅项目态） */}
            {selection.kind === 'project' && (
              <NamespaceTimelineView
                entries={timeline}
                total={timelineTotal}
                loading={timelineLoading}
                searchQuery={timelineQuery}
                onSearchChange={setTimelineQuery}
                onLoadMore={() => {
                  if (selectedProjectId) {
                    void loadProjectDetail(selectedProjectId, timelineQuery, timeline.length, true)
                  }
                }}
              />
            )}
          </>
        ) : (
          <div className="empty-state">
            <FolderKanban size={32} />
            <p>选择一个项目查看或编辑状态卡</p>
            <p className="hint">对话输入框保持无感自动识别；可在此查看、纠偏或管理项目档案</p>
          </div>
        )}
      </div>
      {confirmDialog}
      {alertDialog}
    </div>
  )
}
