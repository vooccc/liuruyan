import { useState, useEffect, useRef } from 'react'
import {
  BookOpen, Brain, RefreshCw,
  Save, X, ChevronRight, Eye, BarChart3, Thermometer,
  ChevronDown, Plus, Trash2, MessageSquare, User, FolderKanban
} from 'lucide-react'
import {
  getLongTermMemory,
  updateLongTermMemory,
  getReflections, getTempStats, getMemoryReport,
  saveReflections, deleteReflectionItem,
  listSessionsByAgent, getSession,
  listNamespaces,
  Session, Message,
} from '../api/chat'
import { Agent } from '../api/chat'
import { useAlertDialog, useConfirmDialog } from './ConfirmDialog'
import AgentAvatar from './AgentAvatar'
import { getAvatarSrc } from './avatarUtils'
import UserProfileView from './UserProfileView'
import NamespaceProjectsView from './NamespaceProjectsView'

// v4 (2026-05-22): tab 'today' / 'diaries' 已废除（diary 注入下线）。
// 新增 'user' tab（用户画像与认知）+ 'sessions' tab（会话浏览器）
// 'longterm' / 'reflections' / 'stats' 保持不变。
// v5 (2026-10-24): 新增 'projects' tab（项目档案：命名空间状态卡 + 全局层）。
type Tab = 'user' | 'sessions' | 'projects' | 'longterm' | 'reflections' | 'stats'

interface TempStats {
  total: number; burning: number; warm: number; cold: number; unlabeled: number
}

interface MemoryReport {
  agent_id: string
  files: Record<string, { bytes: number; lines: number; tokens: number; exceeds_limit?: boolean }>
  conversation_history_tokens: number
  conversation_history_messages: number
  total_tokens_estimate: number
}

interface MemoryPanelProps {
  onClose: () => void
  agentId?: string
  agents?: Agent[]
  standalone?: boolean
}

export default function MemoryPanel({ onClose, agentId: initialAgentId, agents, standalone = false }: MemoryPanelProps) {
  const [activeTab, setActiveTab] = useState<Tab>('sessions')
  const [viewAgentId, setViewAgentId] = useState(initialAgentId || 'default')
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)
  const [longTermContent, setLongTermContent] = useState('')
  // v5 记忆分层：该角色是否已启用项目命名空间。启用后 MEMORY.md 冻结为归档
  // （注入层不读、refiner 不写），「长期记忆」tab 降级为归档查看器。
  const [nsActive, setNsActive] = useState(false)
  // 会话浏览器（v4 取代旧 diaries tab）
  const [sessionList, setSessionList] = useState<Session[]>([])
  const [sessionListLoading, setSessionListLoading] = useState(false)
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null)
  const [selectedSessionMessages, setSelectedSessionMessages] = useState<Message[]>([])
  const [selectedSessionLoading, setSelectedSessionLoading] = useState(false)
  const [editingLT, setEditingLT] = useState(false)
  const [editLTContent, setEditLTContent] = useState('')
  const [saving, setSaving] = useState(false)
  const [reflections, setReflections] = useState('')
  const [editingR, setEditingR] = useState(false)
  const [editRContent, setEditRContent] = useState('')
  const [addingInsight, setAddingInsight] = useState(false)
  const [tempStats, setTempStats] = useState<TempStats | null>(null)
  const [memReport, setMemReport] = useState<MemoryReport | null>(null)
  const [statsTab, setStatsTab] = useState<'temp' | 'report'>('temp')
  const { alert: alertAction, dialog: alertDialog } = useAlertDialog()
  const { confirm, dialog: confirmDialog } = useConfirmDialog()

  // 当 App 侧切换角色时同步
  useEffect(() => {
    if (initialAgentId) setViewAgentId(initialAgentId)
  }, [initialAgentId])

  // 点击外部关闭下拉
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  const currentAgent = agents?.find(a => a.id === viewAgentId)
  const currentAgentName = currentAgent?.name || viewAgentId

  useEffect(() => { loadAll() }, [viewAgentId])

  async function loadAll() {
    try {
      const [lt, ref, sessions, nsList] = await Promise.all([
        getLongTermMemory(viewAgentId),
        getReflections(viewAgentId),
        listSessionsByAgent(viewAgentId),
        listNamespaces(viewAgentId).catch(() => null),
      ])
      setLongTermContent(lt.content)
      setEditLTContent(lt.content)
      setReflections(ref.content)
      setSessionList(sessions)
      setNsActive(Boolean(nsList?.active))
      // 切换角色时重置选中 session 和统计
      setSelectedSessionId(null)
      setSelectedSessionMessages([])
      setTempStats(null)
      setMemReport(null)
    } catch {}
  }

  async function loadStats() {
    try {
      const [ts, rp] = await Promise.all([
        getTempStats(viewAgentId),
        getMemoryReport(viewAgentId),
      ])
      setTempStats(ts)
      setMemReport(rp)
    } catch {}
  }

  async function refreshSessions() {
    setSessionListLoading(true)
    try {
      const sessions = await listSessionsByAgent(viewAgentId)
      setSessionList(sessions)
    } catch {}
    setSessionListLoading(false)
  }

  async function loadSession(sessionId: string) {
    setSelectedSessionId(sessionId)
    setSelectedSessionLoading(true)
    setSelectedSessionMessages([])
    try {
      const session = await getSession(sessionId)
      setSelectedSessionMessages(session.messages || [])
    } catch {
      // 不弹错误，UI 显示空态即可
    }
    setSelectedSessionLoading(false)
  }

  async function saveLongTerm() {
    setSaving(true)
    try {
      await updateLongTermMemory(editLTContent, viewAgentId)
      setLongTermContent(editLTContent)
      setEditingLT(false)
    } catch { await alertAction('保存失败') }
    setSaving(false)
  }

  async function saveReflectionsFn() {
    setSaving(true)
    try {
      await saveReflections(editRContent, viewAgentId)
      setReflections(editRContent)
      setEditingR(false)
    } catch { await alertAction('保存失败') }
    setSaving(false)
  }

  async function deleteReflectionItemFn(index: number) {
    const confirmed = await confirm('确定要删除这条洞察吗？', '删除后不可恢复')
    if (!confirmed) return
    try {
      const result = await deleteReflectionItem(index, viewAgentId)
      // 刷新
      const r = await getReflections(viewAgentId)
      setReflections(r.content)
      setEditRContent(r.content)
      await alertAction(result.message)
    } catch (e: any) {
      await alertAction(`删除失败: ${e.message || e}`)
    }
  }

  function addNewInsight() {
    const now = new Date().toLocaleString('zh-CN')
    const template = `\n\n## [${now}] 新洞察\n\n**关于用户**：在此填写你的观察...\n`
    const newContent = editRContent + template
    setEditRContent(newContent)
    setAddingInsight(true)
    setEditingR(true)
  }

  function handleTabChange(tab: Tab) {
    setActiveTab(tab)
    if (tab === 'stats' && !tempStats) loadStats()
    if (tab === 'reflections') {
      // refresh reflections
      getReflections(viewAgentId).then(r => setReflections(r.content)).catch(() => {})
    }
  }

  const tabs: { key: Tab; label: string; icon: typeof MessageSquare }[] = [
    { key: 'user', label: '用户画像', icon: User },
    { key: 'sessions', label: '会话', icon: MessageSquare },
    { key: 'projects', label: '项目档案', icon: FolderKanban },
    { key: 'longterm', label: nsActive ? '长期记忆·归档' : '长期记忆', icon: Brain },
    { key: 'reflections', label: '反思', icon: Eye },
    { key: 'stats', label: '统计', icon: BarChart3 },
  ]

  function formatBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  return (
    <div className={standalone ? 'memory-window-root' : 'panel-overlay'}>
      <div className={`panel memory-panel ${standalone ? 'standalone' : ''}`}>
        <div className="panel-header" data-tauri-drag-region={standalone ? true : undefined}>
          <h2><BookOpen size={18} /> 记忆浏览器</h2>
          <div className="panel-header-actions" data-tauri-drag-region={false}>
            {/* 角色下拉选择器 */}
            {agents && agents.length > 0 && (
              <div className="memory-agent-selector" ref={dropdownRef}>
                <button
                  className="memory-agent-btn"
                  onClick={() => setDropdownOpen(!dropdownOpen)}
                >
                  <span className="agent-option-avatar memory-agent-current-avatar">
                    <AgentAvatar
                      name={currentAgent?.name}
                      src={getAvatarSrc(currentAgent)}
                      wrapperClassName="agent-option-avatar-wrap"
                      imageClassName="agent-option-avatar-img"
                      plainClassName="agent-option-avatar-fallback"
                    />
                  </span>
                  <span>{currentAgentName}</span>
                  <ChevronDown size={14} className={`chevron-icon ${dropdownOpen ? 'open' : ''}`} />
                </button>
                {dropdownOpen && (
                  <div className="memory-agent-dropdown">
                    {agents.map(agent => (
                      <button
                        key={agent.id}
                        className={`memory-agent-option ${agent.id === viewAgentId ? 'active' : ''}`}
                        onClick={() => {
                          setViewAgentId(agent.id)
                          setDropdownOpen(false)
                          setSelectedSessionId(null)
                          setSelectedSessionMessages([])
                          setEditingLT(false)
                        }}
                      >
                        <span className="agent-option-avatar">
                          <AgentAvatar
                            name={agent.name}
                            src={getAvatarSrc(agent)}
                            wrapperClassName="agent-option-avatar-wrap"
                            imageClassName="agent-option-avatar-img"
                            plainClassName="agent-option-avatar-fallback"
                          />
                        </span>
                        <span className="agent-option-name">{agent.name}</span>
                        {agent.id === viewAgentId && <span className="agent-option-check">✓</span>}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
            <button className="icon-btn" onClick={onClose} title="关闭"><X size={18} /></button>
          </div>
        </div>

        {/* Tab 切换 */}
        <div className="panel-tabs">
          {tabs.map(tab => (
            <button
              key={tab.key}
              className={`panel-tab ${activeTab === tab.key ? 'active' : ''}`}
              onClick={() => handleTabChange(tab.key)}
            >
              <tab.icon size={14} />
              <span>{tab.label}</span>
            </button>
          ))}
        </div>

        {/* 内容区 */}
        <div className="panel-content">
          {activeTab === 'user' && (
            <UserProfileView agentId={viewAgentId} />
          )}

          {activeTab === 'projects' && (
            <NamespaceProjectsView agentId={viewAgentId} />
          )}

          {activeTab === 'sessions' && (
            <div className="memory-section diaries-layout">
              {/* 会话列表 */}
              <div className="diary-list">
                <div className="section-toolbar" style={{ padding: 0, marginBottom: 6 }}>
                  <h4 style={{ margin: 0 }}>会话</h4>
                  <button className="text-btn" onClick={refreshSessions} title="刷新" disabled={sessionListLoading}>
                    <RefreshCw size={12} style={{ display: 'inline', marginRight: 3 }} />
                  </button>
                </div>
                {sessionList.map(s => (
                  <button
                    key={s.id}
                    className={`diary-item ${selectedSessionId === s.id ? 'active' : ''}`}
                    onClick={() => loadSession(s.id)}
                    title={`${s.title} · ${s.messageCount} 条 · ${formatTimeStamp(s.updatedAt)}`}
                  >
                    <ChevronRight size={14} className="chevron" />
                    <div className="session-item-meta" style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 2, overflow: 'hidden' }}>
                      <span style={{ fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%' }}>{s.title || '无标题'}</span>
                      <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                        {s.messageCount} 条 · {formatTimeStamp(s.updatedAt)}
                      </span>
                    </div>
                  </button>
                ))}
                {sessionList.length === 0 && !sessionListLoading && <p className="empty-hint">暂无会话</p>}
                {sessionListLoading && <p className="empty-hint">加载中…</p>}
              </div>

              {/* 会话内容 */}
              <div className="diary-detail">
                {selectedSessionId ? (
                  selectedSessionLoading ? (
                    <div className="empty-state"><MessageSquare size={32} /><p>加载中…</p></div>
                  ) : selectedSessionMessages.length === 0 ? (
                    <div className="empty-state"><MessageSquare size={32} /><p>该会话无消息</p></div>
                  ) : (
                    <div className="session-transcript">
                      {selectedSessionMessages.map((m, idx) => (
                        <SessionTranscriptRow key={idx} message={m} agents={agents} />
                      ))}
                    </div>
                  )
                ) : (
                  <div className="empty-state">
                    <MessageSquare size={32} />
                    <p>选择一个会话查看完整对话</p>
                    <p className="hint">v4: 已废除全 agent 合并日记，改用 per-session transcript</p>
                  </div>
                )}
              </div>
            </div>
          )}

          {activeTab === 'longterm' && (
            <div className="memory-section">
              {nsActive && (
                <div className="memory-archive-banner">
                  🧊 该角色已启用<b>项目记忆分层</b>：此文件已冻结为归档，不再注入对话、不再写入新内容。
                  日常记忆请看「项目档案」与「反思」；此处仅供追溯历史。
                </div>
              )}
              <div className="section-toolbar">
                <h3>长期记忆{nsActive ? '（已归档）' : ''}</h3>
                {!editingLT ? (
                  <button className="text-btn" onClick={() => setEditingLT(true)}>编辑</button>
                ) : (
                  <div className="edit-actions">
                    <button className="icon-btn-sm success" onClick={saveLongTerm} disabled={saving}>
                      <Save size={14} /> {saving ? '...' : '保存'}
                    </button>
                    <button className="icon-btn-sm" onClick={() => { setEditingLT(false); setEditLTContent(longTermContent) }}>
                      取消
                    </button>
                  </div>
                )}
              </div>
              {!editingLT ? (
                longTermContent ? (
                  <pre className="memory-text">{longTermContent}</pre>
                ) : (
                  <div className="empty-state"><Brain size={32} /><p>暂无长期记忆</p></div>
                )
              ) : (
                <textarea
                  className="memory-editor"
                  value={editLTContent}
                  onChange={e => setEditLTContent(e.target.value)}
                  rows={15}
                />
              )}
            </div>
          )}

          {activeTab === 'reflections' && (
            <div className="memory-section">
              <div className="section-toolbar">
                <h3><Eye size={16} style={{ display: 'inline', verticalAlign: '-2px', marginRight: 6 }} />行为洞察</h3>
                <div className="edit-actions" style={{ gap: '6px' }}>
                  {!editingR ? (
                    <>
                      <button className="text-btn" onClick={addNewInsight} title="新增洞察">
                        <Plus size={13} style={{ display: 'inline', marginRight: 3 }} />新增
                      </button>
                      <button className="text-btn" onClick={() => { setEditRContent(reflections); setEditingR(true) }} title="编辑全部">
                        编辑
                      </button>
                      <button className="text-btn" onClick={() => getReflections(viewAgentId).then(r => { setReflections(r.content); setEditRContent(r.content) }).catch(() => {})}>
                        <RefreshCw size={12} style={{ display: 'inline', marginRight: 3 }} />
                      </button>
                    </>
                  ) : (
                    <>
                      <button className="icon-btn-sm success" onClick={saveReflectionsFn} disabled={saving}>
                        <Save size={14} /> {saving ? '...' : '保存'}
                      </button>
                      <button className="icon-btn-sm" onClick={() => { setEditingR(false); setAddingInsight(false); setEditRContent(reflections) }}>
                        取消
                      </button>
                    </>
                  )}
                </div>
              </div>

              {/* 编辑模式：textarea */}
              {editingR ? (
                <textarea
                  className="memory-editor"
                  value={editRContent}
                  onChange={e => setEditRContent(e.target.value)}
                  rows={16}
                  autoFocus={addingInsight}
                />
              ) : reflections && reflections.trim() ? (
                /* 非编辑模式：分段显示，每条带删除按钮 */
                <div className="reflections-list">
                  {reflections.split('\n').reduce<{ lines: string[]; sections: { header: string; body: string; index: number }[] }>((acc, line) => {
                    if (line.startsWith('## ') && (acc.lines.length > 0 || acc.sections.length > 0)) {
                      // 新条目开始，保存前一个
                      const body = acc.lines.join('\n')
                      if (body.trim() || acc.sections.length > 0) {
                        acc.sections.push({ header: '', body, index: acc.sections.length })
                        acc.lines = []
                      }
                      acc.lines.push(line)
                    } else {
                      acc.lines.push(line)
                    }
                    return acc
                  }, { lines: [], sections: [] }).sections.map((sec, idx) => (
                    <div key={idx} className="reflection-item-card">
                      <div className="reflection-item-header">
                        <span className="reflection-item-title">{sec.header || sec.body.split('\n').find(l => l.startsWith('## '))?.slice(3) || `洞察 #${idx + 1}`}</span>
                        <button
                          className="icon-btn-sm danger"
                          onClick={() => deleteReflectionItemFn(idx)}
                          title="删除此洞察"
                        >
                          <Trash2 size={12} />
                        </button>
                      </div>
                      <pre className="reflection-item-body">{sec.body}</pre>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="empty-state">
                  <Eye size={32} />
                  <p>暂无行为洞察</p>
                  <p className="hint">角色在聊天中会自动积累对互动的反思</p>
                  <p className="hint">你也可以手动添加洞察</p>
                </div>
              )}
            </div>
          )}

          {activeTab === 'stats' && (
            <div className="memory-section">
              <div className="section-toolbar">
                <h3><BarChart3 size={16} style={{ display: 'inline', verticalAlign: '-2px', marginRight: 6 }} />记忆统计</h3>
                <button className="text-btn" onClick={loadStats}>
                  <RefreshCw size={12} style={{ display: 'inline', verticalAlign: '-1px', marginRight: 4 }} />刷新
                </button>
              </div>

              {/* 统计子Tab */}
              <div className="stats-subtabs">
                <button
                  className={`stats-subtab ${statsTab === 'temp' ? 'active' : ''}`}
                  onClick={() => setStatsTab('temp')}
                >
                  <Thermometer size={14} /> 温度分布
                </button>
                <button
                  className={`stats-subtab ${statsTab === 'report' ? 'active' : ''}`}
                  onClick={() => setStatsTab('report')}
                >
                  <BarChart3 size={14} /> 容量报告
                </button>
              </div>

              {statsTab === 'temp' && (
                <div className="stats-content">
                  {tempStats ? (
                    <div className="temp-stats-grid">
                      <div className="temp-stat-card burning">
                        <span className="temp-emoji">🔥</span>
                        <div className="temp-info">
                          <span className="temp-label">燃烧</span>
                          <span className="temp-count">{tempStats.burning}</span>
                          <span className="temp-desc">始终加载</span>
                        </div>
                      </div>
                      <div className="temp-stat-card warm">
                        <span className="temp-emoji">🌡️</span>
                        <div className="temp-info">
                          <span className="temp-label">温热</span>
                          <span className="temp-count">{tempStats.warm}</span>
                          <span className="temp-desc">优先加载</span>
                        </div>
                      </div>
                      <div className="temp-stat-card cold">
                        <span className="temp-emoji">❄️</span>
                        <div className="temp-info">
                          <span className="temp-label">冷存</span>
                          <span className="temp-count">{tempStats.cold}</span>
                          <span className="temp-desc">按需加载</span>
                        </div>
                      </div>
                      <div className="temp-stat-card unlabeled">
                        <span className="temp-emoji">📝</span>
                        <div className="temp-info">
                          <span className="temp-label">未标记</span>
                          <span className="temp-count">{tempStats.unlabeled}</span>
                          <span className="temp-desc">默认冷存</span>
                        </div>
                      </div>
                      <div className="temp-summary">
                        总计 <strong>{tempStats.total}</strong> 行记忆
                      </div>
                    </div>
                  ) : (
                    <div className="empty-state"><Thermometer size={32} /><p>加载中...</p></div>
                  )}
                </div>
              )}

              {statsTab === 'report' && (
                <div className="stats-content">
                  {memReport ? (
                    <div className="report-grid">
                      {Object.entries(memReport.files).map(([name, info]: [string, any]) => (
                        <div key={name} className={`report-file-row ${info.exceeds_limit ? 'over-limit' : ''}`}>
                          <span className="report-filename">{name}</span>
                          <div className="report-file-info">
                            <span>{formatBytes(info.bytes)}</span>
                            <span>{info.lines} 行</span>
                            <span>{info.tokens} tok</span>
                            {info.exceeds_limit && <span className="report-warning">⚠️ 超限</span>}
                          </div>
                        </div>
                      ))}
                      <div className="report-summary">
                        <div className="report-summary-row">
                          <span>对话历史</span>
                          <span>{memReport.conversation_history_messages || 0} 条 / {memReport.conversation_history_tokens || 0} tok</span>
                        </div>
                        <div className="report-summary-row total">
                          <span>总估算</span>
                          <span><strong>{memReport.total_tokens_estimate || 0}</strong> tokens</span>
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="empty-state"><BarChart3 size={32} /><p>加载中...</p></div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
      {confirmDialog}
      {alertDialog}
    </div>
  )
}

// ===== v4 (2026-05-22): Session 浏览器辅助 =====

/** 格式化 ISO 时间戳为短形式：今天 → HH:mm；本年 → MM-DD HH:mm；否则 → YYYY-MM-DD。 */
function formatTimeStamp(iso?: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const sameYear = d.getFullYear() === now.getFullYear()
  const pad = (n: number) => String(n).padStart(2, '0')
  if (sameDay) return `${pad(d.getHours())}:${pad(d.getMinutes())}`
  if (sameYear) return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** 从 Message 抽出可读文本：优先用 segments 里的 text/thinking 段拼接，
 *  没有 segments 时退回 content 字段（兼容老纯文本 user 消息）。
 *  跳过 tool_call / tool_result / system_notice（浏览器不展开工具调用细节）。 */
function extractReadableText(message: Message): string {
  const segs = message.segments
  if (Array.isArray(segs) && segs.length > 0) {
    const parts: string[] = []
    for (const seg of segs) {
      if (seg.type === 'text' && typeof seg.content === 'string' && seg.content.trim()) {
        parts.push(seg.content.trim())
      } else if (seg.type === 'thinking' && typeof seg.content === 'string' && seg.content.trim()) {
        parts.push(`💭 ${seg.content.trim()}`)
      } else if (seg.type === 'tool_call') {
        const name = seg.tool_name || seg.name || '?'
        parts.push(`🔧 调用 ${name}`)
      }
    }
    if (parts.length > 0) return parts.join('\n\n')
  }
  return typeof message.content === 'string' ? message.content : ''
}

function getMessageAgentId(message: Message): string | undefined {
  const segs = message.segments
  if (Array.isArray(segs)) {
    for (const seg of segs) {
      if (seg.agent_id) return seg.agent_id
    }
  }
  return undefined
}

interface SessionTranscriptRowProps {
  message: Message
  agents?: Agent[]
}

function SessionTranscriptRow({ message, agents }: SessionTranscriptRowProps) {
  const role = message.role
  const text = extractReadableText(message)
  const agentId = role === 'assistant' ? getMessageAgentId(message) : undefined
  const agentName = role === 'user'
    ? '我'
    : (agents?.find(a => a.id === agentId)?.name || '助手')
  const ts = formatTimeStamp(message.timestamp)
  const isUser = role === 'user'
  return (
    <div className={`session-transcript-row ${isUser ? 'is-user' : 'is-assistant'}`}>
      <div className="session-transcript-meta">
        <span className="session-transcript-author">{agentName}</span>
        {ts && <span className="session-transcript-time">{ts}</span>}
      </div>
      <pre className="session-transcript-text">{text || '（空消息）'}</pre>
    </div>
  )
}
