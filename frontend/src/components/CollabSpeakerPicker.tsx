import { useEffect, useRef } from 'react'
import { ChevronUp, ChevronDown, X, Users, Plus } from 'lucide-react'
import type { Agent } from '../api/chat'
import AgentAvatar from './AgentAvatar'
import { getAvatarFallback, getAvatarSrc } from './avatarUtils'

interface CollabSpeakerPickerProps {
  /** 当前场景的所有参与者 id（候选池） */
  participants: string[]
  /** 全角色目录（用来 lookup name/avatar） */
  agents: Agent[]
  /** 当前已指定的发言序列（粘性） */
  value: string[]
  /** 序列变化回调 */
  onChange: (next: string[]) => void
  /** 关闭 popover（外部状态由调用方持） */
  onClose: () => void
  /** popover 锚点（按钮 DOM 节点）；用于点外部关闭时排除自己 */
  anchorEl: HTMLElement | null
}

/**
 * 「指定发言」popover：候选区点击追加，序列区可重排/移除/重复。
 *
 * 设计要点：
 * - 序列允许重复（同一角色可多次出现，如 a→b→a 收尾），通过候选区重复点击实现。
 * - 重排用 ◀/▶ 按钮（不用 HTML5 DnD：触屏体验差 + 实现复杂度不必要）。
 * - 序列区显示 ① ② ③ 编号，让用户直观看到顺序。
 * - 空序列时给文案"全员按默认顺序发言"，避免空状态焦虑。
 */
export default function CollabSpeakerPicker({
  participants,
  agents,
  value,
  onChange,
  onClose,
  anchorEl,
}: CollabSpeakerPickerProps) {
  const popoverRef = useRef<HTMLDivElement>(null)

  // ── 点外部关闭 ──
  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null
      if (!target) return
      if (popoverRef.current?.contains(target)) return
      if (anchorEl?.contains(target)) return
      onClose()
    }
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('pointerdown', handlePointerDown)
    window.addEventListener('keydown', handleEscape)
    return () => {
      window.removeEventListener('pointerdown', handlePointerDown)
      window.removeEventListener('keydown', handleEscape)
    }
  }, [anchorEl, onClose])

  const lookup = (id: string) => agents.find((a) => a.id === id)

  const append = (id: string) => onChange([...value, id])
  const removeAt = (idx: number) => onChange(value.filter((_, i) => i !== idx))
  const moveUp = (idx: number) => {
    if (idx <= 0) return
    const next = [...value]
    ;[next[idx - 1], next[idx]] = [next[idx], next[idx - 1]]
    onChange(next)
  }
  const moveDown = (idx: number) => {
    if (idx >= value.length - 1) return
    const next = [...value]
    ;[next[idx], next[idx + 1]] = [next[idx + 1], next[idx]]
    onChange(next)
  }
  const clear = () => onChange([])

  return (
    <div ref={popoverRef} className="collab-speaker-popover" role="dialog" aria-label="指定发言顺序">
      <header className="collab-speaker-popover-header">
        <Users size={14} />
        <span className="collab-speaker-popover-title">指定发言顺序</span>
        <button
          type="button"
          className="collab-speaker-popover-close"
          onClick={onClose}
          aria-label="关闭"
        >
          <X size={14} />
        </button>
      </header>

      {/* 序列区 */}
      <section className="collab-speaker-section">
        <div className="collab-speaker-section-label">
          <span>发言序列</span>
          {value.length > 0 && (
            <button type="button" className="collab-speaker-clear-btn" onClick={clear}>
              清空
            </button>
          )}
        </div>
        {value.length === 0 ? (
          <div className="collab-speaker-empty-hint">
            未指定 · 全员按默认顺序发言
          </div>
        ) : (
          <ol className="collab-speaker-sequence">
            {value.map((id, idx) => {
              const agent = lookup(id)
              const name = agent?.name || id
              return (
                <li key={`${id}-${idx}`} className="collab-speaker-chip">
                  <span className="collab-speaker-chip-index">{idx + 1}</span>
                  <AgentAvatar
                    name={agent?.name || name}
                    src={getAvatarSrc(agent)}
                    avatar={getAvatarFallback(agent, '')}
                    fallbackAvatar="AI"
                    wrapperClassName="collab-speaker-chip-avatar"
                    imageClassName="collab-speaker-chip-avatar-img"
                    plainClassName="collab-speaker-chip-avatar-fallback"
                  />
                  <span className="collab-speaker-chip-name" title={name}>{name}</span>
                  <div className="collab-speaker-chip-actions">
                    <button
                      type="button"
                      className="collab-speaker-chip-btn"
                      onClick={() => moveUp(idx)}
                      disabled={idx === 0}
                      aria-label="上移"
                      title="上移"
                    >
                      <ChevronUp size={12} />
                    </button>
                    <button
                      type="button"
                      className="collab-speaker-chip-btn"
                      onClick={() => moveDown(idx)}
                      disabled={idx === value.length - 1}
                      aria-label="下移"
                      title="下移"
                    >
                      <ChevronDown size={12} />
                    </button>
                    <button
                      type="button"
                      className="collab-speaker-chip-btn collab-speaker-chip-remove"
                      onClick={() => removeAt(idx)}
                      aria-label="移除"
                      title="移除"
                    >
                      <X size={12} />
                    </button>
                  </div>
                </li>
              )
            })}
          </ol>
        )}
      </section>

      {/* 候选区 */}
      <section className="collab-speaker-section">
        <div className="collab-speaker-section-label">
          <span>添加角色</span>
          <span className="collab-speaker-section-hint">点击追加 · 可重复</span>
        </div>
        <ul className="collab-speaker-candidates">
          {participants.map((id) => {
            const agent = lookup(id)
            const name = agent?.name || id
            const countInSequence = value.filter((v) => v === id).length
            return (
              <li key={id}>
                <button
                  type="button"
                  className="collab-speaker-candidate"
                  onClick={() => append(id)}
                  title={countInSequence > 0 ? `已选 ${countInSequence} 次` : '添加到序列'}
                >
                  <AgentAvatar
                    name={agent?.name || name}
                    src={getAvatarSrc(agent)}
                    avatar={getAvatarFallback(agent, '')}
                    fallbackAvatar="AI"
                    wrapperClassName="collab-speaker-candidate-avatar"
                    imageClassName="collab-speaker-candidate-avatar-img"
                    plainClassName="collab-speaker-candidate-avatar-fallback"
                  />
                  <span className="collab-speaker-candidate-name">{name}</span>
                  {countInSequence > 0 && (
                    <span className="collab-speaker-candidate-badge">×{countInSequence}</span>
                  )}
                  <Plus size={12} className="collab-speaker-candidate-plus" />
                </button>
              </li>
            )
          })}
        </ul>
      </section>
    </div>
  )
}

interface CollabSpeakerTriggerProps {
  value: string[]
  agents: Agent[]
  isOpen: boolean
  onToggle: () => void
  buttonRef: React.RefObject<HTMLButtonElement>
}

/**
 * 底栏触发按钮 + 已选 chip 序列预览。
 * 未选时显示"@ 指定发言"；已选时显示编号 + 名字序列。
 */
export function CollabSpeakerTrigger({
  value,
  agents,
  isOpen,
  onToggle,
  buttonRef,
}: CollabSpeakerTriggerProps) {
  const lookup = (id: string) => agents.find((a) => a.id === id)
  const hasSelection = value.length > 0

  return (
    <button
      ref={buttonRef}
      type="button"
      className={`collab-speaker-trigger${hasSelection ? ' has-selection' : ''}${isOpen ? ' is-open' : ''}`}
      onClick={onToggle}
      title={hasSelection ? '点击修改发言顺序' : '点击指定发言顺序（默认全员）'}
    >
      {hasSelection ? (
        <span className="collab-speaker-trigger-chips">
          {value.map((id, idx) => {
            const name = lookup(id)?.name || id
            return (
              <span key={`${id}-${idx}`} className="collab-speaker-trigger-chip">
                <span className="collab-speaker-trigger-chip-index">{idx + 1}</span>
                <span className="collab-speaker-trigger-chip-name">{name}</span>
              </span>
            )
          })}
          <span className="collab-speaker-trigger-edit-hint">编辑</span>
        </span>
      ) : (
        <span className="collab-speaker-trigger-empty">
          <Users size={13} />
          <span>指定发言</span>
        </span>
      )}
    </button>
  )
}
