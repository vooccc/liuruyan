import { Check, Copy, PenLine, Trash2, Undo2 } from 'lucide-react'
import type { ReactNode } from 'react'

interface ConversationMessageMetaProps {
  className: string
  nameClassName?: string
  timeClassName?: string
  name: string
  time?: string
  order?: 'name-first' | 'time-first'
  trailing?: ReactNode
}

export function ConversationMessageMeta({
  className,
  nameClassName,
  timeClassName,
  name,
  time,
  order = 'name-first',
  trailing,
}: ConversationMessageMetaProps) {
  return (
    <div className={className}>
      {order === 'name-first' ? (
        <>
          <span className={nameClassName}>{name}</span>
          {time && <span className={timeClassName}>{time}</span>}
        </>
      ) : (
        <>
          {time && <span className={timeClassName}>{time}</span>}
          <span className={nameClassName}>{name}</span>
        </>
      )}
      {trailing}
    </div>
  )
}

interface CopyMessageButtonProps {
  copied: boolean
  onCopy: () => void
  title?: string
}

export function CopyMessageButton({
  copied,
  onCopy,
  title = '复制',
}: CopyMessageButtonProps) {
  return (
    <button className="msg-action-btn" onClick={onCopy} title={title}>
      {copied ? <Check size={14} /> : <Copy size={14} />}
      {copied ? '已复制' : ''}
    </button>
  )
}

interface InplaceEditButtonProps {
  onInplaceEdit: () => void
  active?: boolean
  disabled?: boolean
  title?: string
}

/** v6.1 唯一编辑入口（原 RotateCcw「修改并重新提问」按钮已并入此处）：
 *  打开内联编辑器（取消/保存/发送）。user 消息编辑器含「发送」= 保存并
 *  截断此条之后重发；assistant 消息只有取消/保存（只改文字）。
 *  active=true 时按钮显示为"已激活"态（编辑器已展开）。 */
export function InplaceEditButton({
  onInplaceEdit,
  active,
  disabled,
  title = '编辑本条消息（不截断、不影响之后对话）',
}: InplaceEditButtonProps) {
  return (
    <button
      className={`msg-action-btn ${active ? 'is-active' : ''}`}
      onClick={onInplaceEdit}
      disabled={disabled}
      title={title}
    >
      <PenLine size={14} />
    </button>
  )
}

interface RollbackMessageButtonProps {
  onRollback: () => void
  disabled?: boolean
  title?: string
}

/** 回滚到该消息：截断到该消息之前所有，含确认弹窗。
 *  user / assistant 两种消息都可显示。disabled 时不可点。 */
export function RollbackMessageButton({
  onRollback,
  disabled,
  title = '回滚到此处（删除该条及之后所有消息）',
}: RollbackMessageButtonProps) {
  return (
    <button
      className="msg-action-btn"
      onClick={onRollback}
      disabled={disabled}
      title={title}
    >
      <Undo2 size={14} />
    </button>
  )
}

interface DeleteMessageButtonProps {
  onDelete: () => void
  disabled?: boolean
  title?: string
}

/** v5 (2026-05-26) 删除单条消息：对齐 Cherry "选哪条删哪条"。
 *  - 删 user 普通消息：仅删该行
 *  - 删 assistant turn：连带删紧随其后的 tool_result/image artifact 行
 *  - 永远不留孤儿 tool_use/tool_result 配对，下次 LLM 调用零 400 风险
 *
 *  与 RollbackMessageButton（删该条及之后所有）区分：本按钮**只删该条**，
 *  之后的对话原样保留。破坏性比回滚小，但比原地编辑大（仍是不可逆删除）。 */
export function DeleteMessageButton({
  onDelete,
  disabled,
  title = '删除此条（不影响之后对话）',
}: DeleteMessageButtonProps) {
  return (
    <button
      className="msg-action-btn"
      onClick={onDelete}
      disabled={disabled}
      title={title}
    >
      <Trash2 size={14} />
    </button>
  )
}

/** 纸飞机发送图标：底部输入框发送按钮与内联编辑器「发送」（v6.1）共用。
 *  配合 .send-button-modern 圆形按钮使用，.send-glyph 类提供悬停位移动画。 */
export function SendGlyph() {
  return (
    <svg
      className="send-glyph"
      viewBox="0 0 24 24"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M22 2 11 13" />
      <path d="M22 2 15 22 11 13 2 9 22 2Z" fill="currentColor" fillOpacity={0.2} />
    </svg>
  )
}
