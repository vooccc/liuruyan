import { useEffect, useMemo, useRef, useState } from 'react'
import { Brain, MessageSquareText, Loader2, X, FileText } from 'lucide-react'
import type { ContentSegment, EditMessagePayload, Message as MessageType } from '../api/chat'
import { SendGlyph } from './conversationUi'
import ImageLightbox from './ImageLightbox'
import {
  parseUserMessageAttachments,
  buildUserMessageWithAttachments,
  getUploadedFileUrl,
  type UserMessageAttachment,
} from './userAttachmentFooter'

interface InplaceMessageEditorProps {
  /** 待编辑的消息（user 或 assistant；其他 role 不应进入此组件） */
  message: MessageType
  /** 保存回调；payload 已组装完成。组件不关心 API 调用本身，只负责构造 payload。 */
  onSave: (payload: EditMessagePayload) => Promise<void> | void
  /** v6.1 仅 user 消息传入：「发送」= 保存并截断此条之后的对话重发
   *  （原「修改并重新提问」按钮语义）。父级在成功后关闭编辑器；抛错则
   *  编辑器保持打开并显示错误。assistant 消息不传（无重发语义）。 */
  onSend?: (text: string) => Promise<void> | void
  /** 取消编辑回调 */
  onCancel: () => void
}

interface EditableSlot {
  /** 在 message.segments 中的索引（仅前端展示用，不发后端） */
  segmentIndex: number
  /** 对应原 assistant content[] 的 block 下标（发后端用） */
  blockIndex: number
  type: 'text' | 'thinking'
  /** 当前编辑内容 */
  content: string
}

/**
 * v4 (2026-05-22) L2 聊天编辑 — 内联编辑器
 *
 * 设计：
 * - user 消息（content 是字符串）：单 textarea，保存调
 *   `editSessionMessage(sid, idx, { content })`。
 * - assistant 消息（segments 携带 block_index 字段，由 GET /sessions/{id}
 *   注入）：为每个 text / thinking segment 提供独立 textarea，保存时构造
 *   `block_patches[]`。tool_call / tool_result / system_notice 段**不可编辑**
 *   （UI 上不出现 textarea，但保留只读预览说明"该轮也涉及工具调用，编辑只
 *   影响文字部分"）。
 * - 没有任何可编辑字段时显示提示并禁用保存。
 */
export default function InplaceMessageEditor({
  message,
  onSave,
  onSend,
  onCancel,
}: InplaceMessageEditorProps) {
  const isUserStringMessage = useMemo(() => (
    message.role === 'user' && typeof message.content === 'string'
  ), [message.role, message.content])

  const initialParsed = useMemo(() => {
    if (!isUserStringMessage) return { cleanedText: '', attachments: [] }
    return parseUserMessageAttachments(String(message.content || ''))
  }, [isUserStringMessage, message.content])

  // 初始化 user 文本（去除附件 footer 后的干净文本，后续编辑都在本地）
  const [userText, setUserText] = useState<string>(() => initialParsed.cleanedText)

  // 初始化附件列表（图片/文件），在编辑框内原生渲染为卡片
  const [userAttachments, setUserAttachments] = useState<UserMessageAttachment[]>(() => initialParsed.attachments)

  // 正在全屏预览的大图
  const [previewingAtt, setPreviewingAtt] = useState<UserMessageAttachment | null>(null)

  // 拼装用户完整消息（纯文本 + 附件 footer 规范）
  const getFullUserContent = () => {
    const trimmed = userText.trim()
    const paths = userAttachments.map(a => a.path)
    return buildUserMessageWithAttachments(trimmed, paths)
  }

  // 初始化 assistant 各 segment 槽位
  const [slots, setSlots] = useState<EditableSlot[]>(() => {
    if (message.role !== 'assistant') return []
    const segs = message.segments || []
    const result: EditableSlot[] = []
    segs.forEach((seg: ContentSegment, idx: number) => {
      if (seg.type !== 'text' && seg.type !== 'thinking') return
      // block_index 由 GET /sessions/{id} 注入；无此字段说明老数据或异常
      // 路径，跳过编辑（不构造 patch，避免错位）
      if (typeof seg.block_index !== 'number') return
      result.push({
        segmentIndex: idx,
        blockIndex: seg.block_index,
        type: seg.type,
        content: String(seg.content || ''),
      })
    })
    return result
  })

  const hasToolSegments = useMemo(() => (
    (message.segments || []).some(seg => seg.type === 'tool_call' || seg.type === 'tool_result')
  ), [message.segments])

  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 自动聚焦第一个可编辑 textarea
  const firstTextareaRef = useRef<HTMLTextAreaElement | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (firstTextareaRef.current) {
      firstTextareaRef.current.focus()
      // 光标移到末尾
      const len = firstTextareaRef.current.value.length
      try { firstTextareaRef.current.setSelectionRange(len, len) } catch { /* ignore */ }
    }
  }, [])

  // 点击外部自动取消（同时排除在看大图 lightbox 状态）
  useEffect(() => {
    const handleDocumentMouseDown = (e: MouseEvent) => {
      // 若当前正在全屏预览附件大图，不要因点击 lightbox 背景而关掉编辑器
      if (previewingAtt) return

      const targetNode = e.target as Node | null
      if (containerRef.current && targetNode && !containerRef.current.contains(targetNode)) {
        onCancel()
      }
    }

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (!previewingAtt) {
          e.preventDefault()
          onCancel()
        }
      }
    }

    document.addEventListener('mousedown', handleDocumentMouseDown, true)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handleDocumentMouseDown, true)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [onCancel, previewingAtt])

  const isDirty = useMemo(() => {
    if (isUserStringMessage) {
      const orig = String(message.content || '')
      return getFullUserContent() !== orig
    }
    if (message.role === 'assistant') {
      const segs = message.segments || []
      return slots.some(s => {
        const orig = String(segs[s.segmentIndex]?.content || '')
        return s.content !== orig
      })
    }
    return false
  }, [isUserStringMessage, userText, userAttachments, message.content, message.segments, message.role, slots])

  const canSave = !saving && isDirty && (
    isUserStringMessage || slots.length > 0
  )

  // v6.1「发送」（仅 user 消息）：只要有文字或有附件即可发送
  const canSend = !saving && isUserStringMessage && Boolean(userText.trim() || userAttachments.length > 0)

  async function handleSend() {
    setError(null)
    if (!canSend || !onSend) return
    setSaving(true)
    try {
      const full = getFullUserContent()
      await onSend(full)
    } catch (e: any) {
      setError(e?.message || '发送失败')
    } finally {
      setSaving(false)
    }
  }

  async function handleSave() {
    setError(null)
    if (!canSave) return
    setSaving(true)
    try {
      let payload: EditMessagePayload
      if (isUserStringMessage) {
        payload = { content: getFullUserContent() }
      } else if (message.role === 'assistant') {
        const segs = message.segments || []
        // 只发生了变化的 slot 才需要 patch（减少后端工作 + 让 fields_changed 准确）
        const patches = slots
          .filter(s => s.content !== String(segs[s.segmentIndex]?.content || ''))
          .map(s => ({
            block_index: s.blockIndex,
            type: s.type,
            content: s.content,
          }))
        payload = { block_patches: patches }
      } else {
        throw new Error(`不支持编辑 role=${message.role} 的消息`)
      }
      await onSave(payload)
    } catch (e: any) {
      setError(e?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  // Ctrl/Cmd+Enter：user 消息 = 发送（重发语义），assistant = 保存。
  // Escape：取消编辑退出
  function handleEditorKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Escape') {
      e.preventDefault()
      onCancel()
      return
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault()
      if (isUserStringMessage && onSend) void handleSend()
      else void handleSave()
    }
  }

  // 不支持的消息形态
  if (!isUserStringMessage && message.role !== 'assistant') {
    return (
      <div className="inplace-editor inplace-editor-error">
        <span>该消息不支持原地编辑</span>
        <button className="inplace-btn inplace-btn-ghost" onClick={onCancel}>关闭</button>
      </div>
    )
  }

  if (message.role === 'assistant' && slots.length === 0) {
    return (
      <div className="inplace-editor inplace-editor-error">
        <span>该 assistant 消息没有可编辑的文本段（可能只含工具调用，或为旧版数据缺少 block_index）。</span>
        <button className="inplace-btn inplace-btn-ghost" onClick={onCancel}>关闭</button>
      </div>
    )
  }

  return (
    <div ref={containerRef} className="inplace-editor">
      {/* 编辑区：一体化无内框设计 */}
      <div className="inplace-editor-body">
        {isUserStringMessage ? (
          <div className="inplace-editor-user-flow">
            {userAttachments.length > 0 && (
              <div className="inplace-editor-attachments">
                {userAttachments.map((att, i) => (
                  <div key={`${att.path}-${i}`} className="inplace-attachment-item">
                    {att.isImage ? (
                      <div
                        className="inplace-attachment-thumb"
                        title={`${att.name}（点击放大预览）`}
                        onClick={() => setPreviewingAtt(att)}
                      >
                        <img src={getUploadedFileUrl(att.path)} alt={att.name} loading="lazy" />
                      </div>
                    ) : (
                      <div className="inplace-attachment-file" title={att.path}>
                        <FileText size={14} />
                        <span className="inplace-attachment-name">{att.name}</span>
                      </div>
                    )}
                    <button
                      type="button"
                      className="inplace-attachment-remove"
                      onClick={(e) => {
                        e.stopPropagation()
                        setUserAttachments(prev => prev.filter((_, idx) => idx !== i))
                      }}
                      title="移除附件"
                      aria-label="移除附件"
                    >
                      <X size={11} />
                    </button>
                  </div>
                ))}
              </div>
            )}
            <textarea
              ref={firstTextareaRef}
              className="inplace-editor-textarea"
              value={userText}
              onChange={e => setUserText(e.target.value)}
              onKeyDown={handleEditorKeyDown}
              rows={Math.min(14, Math.max(3, userText.split('\n').length + 1))}
              placeholder="输入消息内容..."
            />
          </div>
        ) : (
          slots.map((slot, i) => (
            <div key={`${slot.segmentIndex}-${slot.blockIndex}`} className="inplace-editor-field">
              {slots.length > 1 && (
                <label className="inplace-editor-label">
                  {slot.type === 'thinking' ? <Brain size={12} /> : <MessageSquareText size={12} />}
                  {slot.type === 'thinking' ? `思考过程 #${slot.blockIndex}` : `正文回复 #${slot.blockIndex}`}
                </label>
              )}
              <textarea
                ref={i === 0 ? firstTextareaRef : undefined}
                className={`inplace-editor-textarea ${slot.type === 'thinking' ? 'is-thinking' : ''}`}
                value={slot.content}
                onChange={e => {
                  const newContent = e.target.value
                  setSlots(prev => prev.map((s, idx) => idx === i ? { ...s, content: newContent } : s))
                }}
                onKeyDown={handleEditorKeyDown}
                rows={Math.min(12, Math.max(3, slot.content.split('\n').length + 1))}
                placeholder={slot.type === 'thinking' ? '思考内容...' : '回复内容...'}
              />
            </div>
          ))
        )}
      </div>

      {hasToolSegments && message.role === 'assistant' && (
        <div className="inplace-editor-notice">
          ⚠️ 该轮包含工具调用，工具部分不可编辑。若需修改工具执行，建议使用“回滚到此处”。
        </div>
      )}

      {error && (
        <div className="inplace-editor-error-banner">{error}</div>
      )}

      {/* 底部操作条：极简三按钮（取消 / 保存 / 发送） */}
      <div className="inplace-editor-footer">
        <div className="inplace-editor-actions">
          <button
            type="button"
            className="inplace-btn inplace-btn-ghost"
            onClick={onCancel}
            disabled={saving}
          >
            取消
          </button>
          <button
            type="button"
            className={`inplace-btn inplace-btn-secondary ${isDirty ? 'is-dirty' : ''}`}
            onClick={handleSave}
            disabled={!canSave}
            title={!isDirty ? '内容无变化' : (saving ? '保存中...' : '只保存文字，不重发、不影响之后对话')}
          >
            {saving ? <Loader2 size={13} className="spin" /> : null}
            <span>{saving ? '保存中...' : '保存'}</span>
          </button>
          {isUserStringMessage && onSend && (
            <button
              type="button"
              className="inplace-btn inplace-btn-primary"
              onClick={handleSend}
              disabled={!canSend}
              title={saving
                ? '发送中...'
                : '替换此条之后的所有对话并重新向AI提问'}
              aria-label="发送"
            >
              {saving ? <Loader2 size={13} className="spin" /> : <SendGlyph />}
              <span>发送</span>
            </button>
          )}
        </div>
      </div>

      {previewingAtt && (
        <ImageLightbox
          src={getUploadedFileUrl(previewingAtt.path)}
          alt={previewingAtt.name}
          caption={previewingAtt.name}
          onClose={() => setPreviewingAtt(null)}
        />
      )}
    </div>
  )
}
