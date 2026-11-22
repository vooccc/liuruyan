import { useState, useEffect } from 'react'
import {
  User,
  Pencil,
  Save,
  RotateCcw,
  RefreshCw,
  Sparkles,
  Check,
  History,
  Plus,
  Undo2,
} from 'lucide-react'
import {
  getUserProfile,
  updateUserProfile,
  resetUserProfile,
  getUserProfileHistory,
  rollbackUserProfile,
  evolveUserProfile,
  type UserProfileHistoryItem,
} from '../api/chat'
import { renderChatMarkdown } from './chatMarkdown'
import { useAlertDialog, useConfirmDialog } from './ConfirmDialog'

interface UserProfileViewProps {
  agentId?: string
}

export default function UserProfileView({ agentId }: UserProfileViewProps) {
  const [content, setContent] = useState('')
  const [editContent, setEditContent] = useState('')
  const [isEditing, setIsEditing] = useState(false)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [savedSuccess, setSavedSuccess] = useState(false)

  // 演进历史 & 快速补充偏好
  const [history, setHistory] = useState<UserProfileHistoryItem[]>([])
  const [showHistory, setShowHistory] = useState(false)
  const [showAddHabit, setShowAddHabit] = useState(false)
  const [habitInput, setHabitInput] = useState('')
  const [evolving, setEvolving] = useState(false)

  const { alert: alertAction, dialog: alertDialog } = useAlertDialog()
  const { confirm, dialog: confirmDialog } = useConfirmDialog()

  useEffect(() => {
    loadProfile()
  }, [agentId])

  async function loadProfile() {
    setLoading(true)
    try {
      const [res, histRes] = await Promise.all([
        getUserProfile(agentId),
        getUserProfileHistory(agentId).catch(() => ({ history: [] })),
      ])
      setContent(res.content || '')
      setEditContent(res.content || '')
      setHistory(histRes.history || [])
    } catch {
      setContent('')
      setEditContent('')
      setHistory([])
    } finally {
      setLoading(false)
    }
  }

  async function handleSave() {
    setSaving(true)
    try {
      await updateUserProfile(editContent, agentId)
      setContent(editContent)
      setIsEditing(false)
      setSavedSuccess(true)
      setTimeout(() => setSavedSuccess(false), 2000)
    } catch (err: any) {
      await alertAction(err?.message ? `保存失败：${err.message}` : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  async function handleReset() {
    const ok = await confirm(
      '确定要重置用户画像吗？',
      '重置后将恢复为出厂默认模板，重置前会自动留存快照以便撤销。'
    )
    if (!ok) return

    setSaving(true)
    try {
      const res = await resetUserProfile(agentId)
      setContent(res.content)
      setEditContent(res.content)
      setIsEditing(false)
      const histRes = await getUserProfileHistory(agentId).catch(() => ({ history: [] }))
      setHistory(histRes.history || [])
    } catch (err: any) {
      await alertAction(err?.message ? `重置失败：${err.message}` : '重置失败')
    } finally {
      setSaving(false)
    }
  }

  async function handleEvolveHabit() {
    const text = habitInput.trim()
    if (!text) return
    setEvolving(true)
    try {
      const res = await evolveUserProfile(text, agentId)
      setHabitInput('')
      setShowAddHabit(false)
      if (res.updated) {
        setContent(res.content)
        setEditContent(res.content)
        setSavedSuccess(true)
        setTimeout(() => setSavedSuccess(false), 2000)
        const histRes = await getUserProfileHistory(agentId).catch(() => ({ history: [] }))
        setHistory(histRes.history || [])
      } else {
        await alertAction(
          res.skipped_reason === 'no_change'
            ? '现有画像已包含该偏好，无需重复写入。'
            : `未更新画像（${res.skipped_reason || '模型未返回有效变更'}）`,
        )
      }
    } catch (err: any) {
      await alertAction(err?.message ? `融合失败：${err.message}` : '融合失败')
    } finally {
      setEvolving(false)
    }
  }

  async function handleRollback(item: UserProfileHistoryItem) {
    const ok = await confirm(
      '撤销此次画像变更？',
      `将恢复到「${item.insight}」写入之前的版本。`,
      'warning',
      '撤销',
    )
    if (!ok) return
    setSaving(true)
    try {
      const res = await rollbackUserProfile(item.id, agentId)
      setContent(res.content)
      setEditContent(res.content)
      setIsEditing(false)
      const histRes = await getUserProfileHistory(agentId).catch(() => ({ history: [] }))
      setHistory(histRes.history || [])
    } catch (err: any) {
      await alertAction(err?.message ? `撤销失败：${err.message}` : '撤销失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="memory-section user-profile-section" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* 顶部工具栏 */}
      <div className="section-toolbar" style={{ alignItems: 'flex-start', paddingBottom: 12 }}>
        <div>
          <h3 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: 6 }}>
            <User size={18} style={{ color: 'var(--primary, #3b82f6)' }} />
            <span>用户画像与深度印象</span>
          </h3>
          <p style={{ margin: '4px 0 0 0', fontSize: 12, color: 'var(--text-muted)' }}>
            记录对老板的核心认知、交互偏好、事业全景与处事习惯（USER.md，对话自动增量提纯）
          </p>
        </div>

        <div className="edit-actions" style={{ gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
          {savedSuccess && (
            <span style={{ fontSize: 12, color: '#10b981', display: 'flex', alignItems: 'center', gap: 4 }}>
              <Check size={14} /> 已更新
            </span>
          )}

          {!isEditing ? (
            <>
              <button
                className="text-btn"
                onClick={() => {
                  setShowAddHabit(v => !v)
                  setShowHistory(false)
                }}
                title="补充一条偏好或习惯，智能融入对应章节"
              >
                <Plus size={13} style={{ display: 'inline', marginRight: 3 }} />
                补充习惯
              </button>
              <button
                className="text-btn"
                onClick={() => {
                  setShowHistory(v => !v)
                  setShowAddHabit(false)
                }}
                title="查看画像自动演进与修改历史"
              >
                <History size={13} style={{ display: 'inline', marginRight: 3 }} />
                演进记录{history.length > 0 ? ` (${history.length})` : ''}
              </button>
              <button
                className="text-btn"
                onClick={() => {
                  setEditContent(content)
                  setIsEditing(true)
                  setShowHistory(false)
                  setShowAddHabit(false)
                }}
                title="编辑画像"
              >
                <Pencil size={13} style={{ display: 'inline', marginRight: 4 }} />
                编辑
              </button>
              <button
                className="text-btn"
                onClick={handleReset}
                title="重置为默认模板"
                style={{ opacity: 0.8 }}
              >
                <RotateCcw size={13} style={{ display: 'inline', marginRight: 4 }} />
                重置
              </button>
              <button
                className="text-btn"
                onClick={loadProfile}
                disabled={loading}
                title="刷新"
              >
                <RefreshCw size={12} className={loading ? 'spin' : ''} />
              </button>
            </>
          ) : (
            <>
              <button
                className="icon-btn-sm success"
                onClick={handleSave}
                disabled={saving}
                style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '4px 10px' }}
              >
                <Save size={14} /> {saving ? '保存中…' : '保存'}
              </button>
              <button
                className="icon-btn-sm"
                onClick={() => {
                  setIsEditing(false)
                  setEditContent(content)
                }}
                disabled={saving}
                style={{ padding: '4px 10px' }}
              >
                取消
              </button>
            </>
          )}
        </div>
      </div>

      {/* 快速补充偏好条 */}
      {showAddHabit && !isEditing && (
        <div className="user-profile-habit-bar">
          <input
            className="namespace-new-input"
            value={habitInput}
            onChange={e => setHabitInput(e.target.value)}
            placeholder="输入一条长期偏好或习惯（如：先给结论再给细节；反感冗余标题）"
            onKeyDown={e => {
              if (e.key === 'Enter') void handleEvolveHabit()
              if (e.key === 'Escape') setShowAddHabit(false)
            }}
          />
          <button
            className="ns-btn-xs primary"
            onClick={() => void handleEvolveHabit()}
            disabled={evolving || !habitInput.trim()}
          >
            {evolving ? '融合中…' : '融入画像'}
          </button>
        </div>
      )}

      {/* 演进历史抽屉 */}
      {showHistory && !isEditing && (
        <div className="user-profile-history-panel">
          {history.length === 0 ? (
            <p className="empty-hint" style={{ margin: 0 }}>暂无自动演进记录</p>
          ) : (
            <div className="user-profile-history-list">
              {history.map(item => (
                <div key={item.id} className="user-profile-history-item">
                  <div className="user-profile-history-main">
                    <div className="user-profile-history-top">
                      <span className={`user-profile-kind-pill is-${item.kind || 'auto'}`}>
                        {item.kind === 'manual' ? '手动融合' : item.kind === 'rollback' ? '回滚' : item.kind === 'reset' ? '重置' : '自动提纯'}
                      </span>
                      <span className="user-profile-history-insight">{item.insight}</span>
                      <span className="user-profile-history-ts">{(item.ts || '').replace('T', ' ').slice(0, 16)}</span>
                    </div>
                    {(item.reason || item.session_id) && (
                      <div className="user-profile-history-sub">
                        {item.reason && <span>{item.reason}</span>}
                        {item.session_id && <span> · 会话 {item.session_id.slice(0, 8)}</span>}
                      </div>
                    )}
                  </div>
                  {item.kind !== 'rollback' && (
                    <button
                      className="ns-btn-xs"
                      onClick={() => void handleRollback(item)}
                      disabled={saving}
                      title="恢复到这次修改之前的版本"
                    >
                      <Undo2 size={11} /> 撤销
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 内容区域 */}
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        {loading && !content ? (
          <div className="empty-state">
            <RefreshCw size={28} className="spin" />
            <p>正在读取用户画像…</p>
          </div>
        ) : isEditing ? (
          <div style={{ display: 'flex', flexDirection: 'column', height: '100%', gap: 8 }}>
            <textarea
              className="memory-editor"
              value={editContent}
              onChange={e => setEditContent(e.target.value)}
              placeholder="# USER.md — 关于老板的认知与偏好..."
              style={{
                flex: 1,
                minHeight: 380,
                fontFamily: 'monospace',
                fontSize: 13,
                lineHeight: 1.6,
                padding: '12px 14px',
                borderRadius: 8,
                border: '1px solid var(--border-color)',
                background: 'var(--bg-input, var(--bg-card))',
                color: 'var(--text-main)',
                resize: 'none',
              }}
              onKeyDown={e => {
                if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                  e.preventDefault()
                  handleSave()
                } else if (e.key === 'Escape') {
                  setIsEditing(false)
                  setEditContent(content)
                }
              }}
            />
            <div style={{ fontSize: 12, color: 'var(--text-muted)', display: 'flex', justifyContent: 'space-between' }}>
              <span>支持 Markdown 格式 · 按 Ctrl+Enter 快捷保存，Esc 取消</span>
              <span>{editContent.length} 字符</span>
            </div>
          </div>
        ) : content ? (
          <div
            className="user-profile-render markdown-body"
            style={{
              padding: '16px 20px',
              borderRadius: 8,
              background: 'var(--bg-card, rgba(255,255,255,0.03))',
              border: '1px solid var(--border-color)',
              lineHeight: 1.7,
              fontSize: 14,
            }}
            dangerouslySetInnerHTML={{ __html: renderChatMarkdown(content) }}
          />
        ) : (
          <div className="empty-state">
            <Sparkles size={32} style={{ color: 'var(--text-muted)' }} />
            <p>暂无用户画像记录</p>
            <p className="hint">点击右上角「重置」或「编辑」即可开始记录我对你的了解</p>
          </div>
        )}
      </div>

      {alertDialog}
      {confirmDialog}
    </div>
  )
}
