import { useEffect, useMemo, useRef, useState } from 'react'
import { Save, X, Brain, MessageSquareText } from 'lucide-react'
import type { ContentSegment, EditMessagePayload, Message as MessageType } from '../api/chat'

interface InplaceMessageEditorProps {
  /** 待编辑的消息（user 或 assistant；其他 role 不应进入此组件） */
  message: MessageType
  /** 保存回调；payload 已组装完成。组件不关心 API 调用本身，只负责构造 payload。 */
  onSave: (payload: EditMessagePayload) => Promise<void> | void
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
  onCancel,
}: InplaceMessageEditorProps) {
  const isUserStringMessage = useMemo(() => (
    message.role === 'user' && typeof message.content === 'string'
  ), [message.role, message.content])

  // 初始化 user 文本（拷贝原文，后续编辑都在本地）
  const [userText, setUserText] = useState<string>(() => (
    isUserStringMessage ? String(message.content || '') : ''
  ))

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
  useEffect(() => {
    if (firstTextareaRef.current) {
      firstTextareaRef.current.focus()
      // 光标移到末尾
      const len = firstTextareaRef.current.value.length
      try { firstTextareaRef.current.setSelectionRange(len, len) } catch { /* ignore */ }
    }
  }, [])

  const isDirty = useMemo(() => {
    if (isUserStringMessage) {
      return userText !== String(message.content || '')
    }
    if (message.role === 'assistant') {
      const segs = message.segments || []
      return slots.some(s => {
        const orig = String(segs[s.segmentIndex]?.content || '')
        return s.content !== orig
      })
    }
    return false
  }, [isUserStringMessage, userText, message.content, message.segments, message.role, slots])

  const canSave = !saving && isDirty && (
    isUserStringMessage || slots.length > 0
  )

  async function handleSave() {
    setError(null)
    if (!canSave) return
    setSaving(true)
    try {
      let payload: EditMessagePayload
      if (isUserStringMessage) {
        payload = { content: userText }
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

  // 不支持的消息形态
  if (!isUserStringMessage && message.role !== 'assistant') {
    return (
      <div className="inplace-editor inplace-editor-error">
        <span>该消息不支持原地编辑</span>
        <button className="icon-btn-sm" onClick={onCancel}>关闭</button>
      </div>
    )
  }

  if (message.role === 'assistant' && slots.length === 0) {
    return (
      <div className="inplace-editor inplace-editor-error">
        <span>该 assistant 消息没有可编辑的文本段（可能只含工具调用，或为旧版数据缺少 block_index）。</span>
        <button className="icon-btn-sm" onClick={onCancel}>关闭</button>
      </div>
    )
  }

  return (
    <div className="inplace-editor">
      {isUserStringMessage ? (
        <div className="inplace-editor-field">
          <label className="inplace-editor-label">
            <MessageSquareText size={13} /> 用户消息
          </label>
          <textarea
            ref={firstTextareaRef}
            className="inplace-editor-textarea"
            value={userText}
            onChange={e => setUserText(e.target.value)}
            rows={Math.min(12, Math.max(3, userText.split('\n').length + 1))}
            placeholder="编辑消息内容..."
          />
        </div>
      ) : (
        slots.map((slot, i) => (
          <div key={`${slot.segmentIndex}-${slot.blockIndex}`} className="inplace-editor-field">
            <label className="inplace-editor-label">
              {slot.type === 'thinking' ? <Brain size={13} /> : <MessageSquareText size={13} />}
              {slot.type === 'thinking' ? `思考 #${slot.blockIndex}` : `文本 #${slot.blockIndex}`}
            </label>
            <textarea
              ref={i === 0 ? firstTextareaRef : undefined}
              className={`inplace-editor-textarea ${slot.type === 'thinking' ? 'is-thinking' : ''}`}
              value={slot.content}
              onChange={e => {
                const newContent = e.target.value
                setSlots(prev => prev.map((s, idx) => idx === i ? { ...s, content: newContent } : s))
              }}
              rows={Math.min(12, Math.max(3, slot.content.split('\n').length + 1))}
            />
          </div>
        ))
      )}

      {hasToolSegments && message.role === 'assistant' && (
        <div className="inplace-editor-hint">
          ⚠️ 该轮包含工具调用，工具部分**不可编辑**。要修改工具调用请用"回滚到此处"重发。
        </div>
      )}

      {error && (
        <div className="inplace-editor-error-banner">{error}</div>
      )}

      <div className="inplace-editor-actions">
        <button
          className="icon-btn-sm success"
          onClick={handleSave}
          disabled={!canSave}
          title={!isDirty ? '内容无变化' : (saving ? '保存中...' : '保存（Ctrl+Enter）')}
        >
          <Save size={13} /> {saving ? '保存中...' : '保存'}
        </button>
        <button className="icon-btn-sm" onClick={onCancel} disabled={saving}>
          <X size={13} /> 取消
        </button>
      </div>
    </div>
  )
}
