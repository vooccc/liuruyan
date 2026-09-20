import { Check, Copy, PenLine, RotateCcw, Trash2, Undo2 } from 'lucide-react'
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

interface EditMessageButtonProps {
  onEdit: () => void
  disabled?: boolean
  title?: string
}

/** 编辑用户消息：截断到该消息之前 + 把内容塞回输入框，等价 ChatGPT 的 edit-and-resubmit。
 *  仅在 user message 上显示。disabled 时（streaming/busy）保持渲染但不可点。
 *
 *  v4 (2026-05-22) 图标重选：原 Pencil 跟 InplaceEditButton 的 PenLine 视觉重复
 *  + 两者破坏性不对称（截断重发会**永久丢弃后续 N 轮对话**，原地编辑零破坏），
 *  故改用 RotateCcw（逆时针重做箭头）= 「修改并重新提问」的语义。
 *  破坏性提示放进 title 让用户 hover 即可知后果。 */
export function EditMessageButton({
  onEdit,
  disabled,
  title = '修改并重新提问（会清除之后所有对话）',
}: EditMessageButtonProps) {
  return (
    <button
      className="msg-action-btn"
      onClick={onEdit}
      disabled={disabled}
      title={title}
    >
      <RotateCcw size={14} />
    </button>
  )
}

interface InplaceEditButtonProps {
  onInplaceEdit: () => void
  active?: boolean
  disabled?: boolean
  title?: string
}

/** v4 (2026-05-22) L2 原地编辑：打开内联编辑器，**不截断、不重发**，只改本条
 *  消息的文本/思考内容到磁盘。与 EditMessageButton（截断+重发）区分。
 *  - user 消息：可改 content（纯文本）
 *  - assistant 消息：可改 text/thinking 段；工具调用相关段不可编辑
 *  active=true 时按钮显示为"已激活"态（编辑器已展开）。 */
export function InplaceEditButton({
  onInplaceEdit,
  active,
  disabled,
  title = '只改文字（不重发、不影响之后对话）',
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
