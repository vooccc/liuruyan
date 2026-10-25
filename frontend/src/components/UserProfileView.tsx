import { useState, useEffect } from 'react'
import {
  User,
  Pencil,
  Save,
  RotateCcw,
  RefreshCw,
  Sparkles,
  Check,
} from 'lucide-react'
import { getUserProfile, updateUserProfile, resetUserProfile } from '../api/chat'
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

  const { alert: alertAction, dialog: alertDialog } = useAlertDialog()
  const { confirm, dialog: confirmDialog } = useConfirmDialog()

  useEffect(() => {
    loadProfile()
  }, [agentId])

  async function loadProfile() {
    setLoading(true)
    try {
      const res = await getUserProfile(agentId)
      setContent(res.content || '')
      setEditContent(res.content || '')
    } catch {
      setContent('')
      setEditContent('')
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
      '重置后将恢复为出厂默认模板，当前的自定义内容将被覆盖。'
    )
    if (!ok) return

    setSaving(true)
    try {
      const res = await resetUserProfile(agentId)
      setContent(res.content)
      setEditContent(res.content)
      setIsEditing(false)
    } catch (err: any) {
      await alertAction(err?.message ? `重置失败：${err.message}` : '重置失败')
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
            记录对老板的核心认知、交互偏好、事业全景与羁绊评价（USER.md）
          </p>
        </div>

        <div className="edit-actions" style={{ gap: 8, alignItems: 'center' }}>
          {savedSuccess && (
            <span style={{ fontSize: 12, color: '#10b981', display: 'flex', alignItems: 'center', gap: 4 }}>
              <Check size={14} /> 已保存
            </span>
          )}

          {!isEditing ? (
            <>
              <button
                className="text-btn"
                onClick={() => {
                  setEditContent(content)
                  setIsEditing(true)
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
                重置模板
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
            <p className="hint">点击右上角「重置模板」或「编辑」即可开始记录我对你的了解</p>
          </div>
        )}
      </div>

      {alertDialog}
      {confirmDialog}
    </div>
  )
}
