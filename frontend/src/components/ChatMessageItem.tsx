import { memo, useState } from 'react'
import { type Agent, type ContentSegment, type EditMessagePayload, type Message as MessageType } from '../api/chat'
import { ConversationMessageMeta, CopyMessageButton, DeleteMessageButton, InplaceEditButton, RollbackMessageButton } from './conversationUi'
import InplaceMessageEditor from './InplaceMessageEditor'
import { parseStoredRuntimeMessageContent } from './runtimeMessageContent'
import {
  RuntimeChainSegments,
} from './runtimeBlocks'
import AgentAvatar from './AgentAvatar'
import { EnhancedTextBlock } from './EnhancedTextBlock'
import { CheckCircle2 } from 'lucide-react'
import UserMessageAttachments from './UserMessageAttachments'
import { parseUserMessageAttachments, type UserMessageAttachment } from './userAttachmentFooter'

function isRouteSubmissionTextSegment(segment: ContentSegment) {
  if (segment.type !== 'text') return false
  const content = String(segment.content || '').trim()
  return content.startsWith('Router 选择 ')
}

function hasWorkerResultNotice(segments: ContentSegment[] | undefined) {
  return segments?.some((segment) => (
    segment.type === 'system_notice' && segment.kind === 'worker_result'
  ))
}

function hasFinalMergeText(message: MessageType) {
  const segments = message.segments || []
  const hasWorkerResult = hasWorkerResultNotice(segments)
  const hasTextSegment = segments.some((segment) => segment.type === 'text')
  return hasWorkerResult && hasTextSegment && !message.isStreaming
}

function hasTaskRoutedNotice(segments: ContentSegment[] | undefined) {
  return Boolean(segments?.some((segment) => (
    segment.type === 'system_notice' && segment.kind === 'task_routed'
  )))
}

function getVisibleChatSegments(
  segments: ContentSegment[] | undefined,
  hasTaskRouteNotice: boolean,
) {
  if (!segments?.length) return []
  if (!hasTaskRouteNotice) return segments

  return segments.filter((segment) => {
    if (isRouteSubmissionTextSegment(segment)) return false
    return true
  })
}

interface AskUserAnswerPayload {
  kind: 'ask_user_answer'
  answers: Array<{ question?: string; answer?: string }>
}

const ASK_USER_ANSWER_PREFIX = '[用户对 ask_user_question 的回答]'

function parseAskUserAnswerPayload(segments: ContentSegment[]): AskUserAnswerPayload | null {
  for (const segment of segments) {
    if (segment.type !== 'text') continue
    const content = String(segment.content || '').trim()
    if (!content.startsWith(ASK_USER_ANSWER_PREFIX)) continue
    const jsonText = content.slice(ASK_USER_ANSWER_PREFIX.length).trim()
    if (!jsonText) return null
    try {
      const parsed = JSON.parse(jsonText)
      if (
        parsed?.kind === 'ask_user_answer' &&
        Array.isArray(parsed.answers)
      ) {
        return parsed as AskUserAnswerPayload
      }
    } catch {
      return null
    }
  }
  return null
}

function AskUserAnswerSummary({ payload }: { payload: AskUserAnswerPayload }) {
  return (
    <div className="ask-answer-summary" aria-label="已提交回答">
      <div className="ask-answer-summary-head">
        <CheckCircle2 size={14} />
        <span>已提交回答</span>
      </div>
      <div className="ask-answer-summary-list">
        {payload.answers.map((item, index) => (
          <div key={index} className="ask-answer-summary-item">
            {item.question && <div className="ask-answer-summary-question">{item.question}</div>}
            <div className="ask-answer-summary-answer">{item.answer || '未填写'}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

interface ChatMessageItemProps {
  message: MessageType
  sourceAgent: Agent | null
  avatarSrc: string | null
  timeLabel: string
  copied: boolean
  onCopy: () => void
  expandedThinking: Set<string>
  onToggleThinking: (thinkingKey: string) => void
  expandedToolCalls: Set<string>
  onToggleToolCall: (toolKey: string) => void
  renderMarkdown: (text: string) => string
  askStatesByAskId?: import('./runtimeBlocks').AskStatesByAskId
  onSubmitAskAnswer?: (askId: string, toolUseId: string | undefined, answers: import('./AskUserQuestionCard').AskUserAnswer[]) => void
  /** v6.1 编辑本条（user + assistant）：打开内联编辑器（取消/保存/发送）。
   *  user 消息的编辑器含「发送」= 保存并截断此条之后重发（原「修改并重新提问」
   *  语义）；assistant 只有取消/保存。 */
  onInplaceEdit?: (payload: EditMessagePayload) => Promise<void>
  /** v6.1 仅 user 消息传入：内联编辑器「发送」= 截断此条之后 + 以新文本重发。
   *  父组件（Chat）负责截断时机与发送流程；抛错时编辑器保持打开。 */
  onSendEdit?: (text: string) => Promise<void>
  /** v6 标记式修改：本条在「编辑重发」中的标记态——
   *  'target' = 本条是被标记待重发的消息（高亮，仅发送在途的短暂窗口）；
   *  'after'  = 本条在标记消息之后，发送时将被替换（视觉压暗提示）；
   *  null     = 无标记。 */
  editMark?: 'target' | 'after' | null
  /** 回滚到本条（user / assistant 都可）：截断到本条之前 + 确认弹窗 */
  onRollback?: () => void
  /** v5 (2026-05-26) 删除本条（user / assistant 都可）：仅删该条，
   *  assistant 会连带删紧随其后的 tool_result artifact；之后的对话原样保留。 */
  onDelete?: () => void
  /** 父组件正在 streaming / busy；为 true 时编辑/回滚按钮 disabled */
  isBusy?: boolean
  sessionId?: string | null
}

function ChatMessageItem({
  message,
  sourceAgent,
  avatarSrc,
  timeLabel,
  copied,
  onCopy,
  expandedThinking,
  onToggleThinking,
  expandedToolCalls,
  onToggleToolCall,
  renderMarkdown,
  askStatesByAskId,
  onSubmitAskAnswer,
  onInplaceEdit,
  onSendEdit,
  editMark,
  onRollback,
  onDelete,
  isBusy,
  sessionId,
}: ChatMessageItemProps) {
  // v4 (2026-05-22) L2 原地编辑：本地展开/收起 state，不上提到父级
  // （编辑器是组件局部 UI 模态，没必要污染父级 store / re-render）
  const [isInplaceEditing, setIsInplaceEditing] = useState(false)
  const hasTaskRouteNotice = hasTaskRoutedNotice(message.segments || [])
  const rawVisibleSegments = getVisibleChatSegments(
    message.segments || [],
    hasTaskRouteNotice,
  )

  // 用户消息：把 backend 拼在 message 末尾的"[附件 ...]\n- /path"footer 抽出来，
  // text segment 展示用 cleanedText（不让用户气泡里出现一片裸路径），footer 里的图
  // 单独走 UserMessageAttachments 组件渲染缩略图卡。
  // - 仅 cleanedText 为空 + 至少有一张附件时丢掉空 text segment（防纯图消息出空气泡）
  // - 非 user 消息原样透传
  const userAttachments: UserMessageAttachment[] = []
  const visibleSegments = (() => {
    if (message.role !== 'user') return rawVisibleSegments
    const out: ContentSegment[] = []
    for (const seg of rawVisibleSegments) {
      if (seg.type !== 'text') {
        out.push(seg)
        continue
      }
      const parsed = parseUserMessageAttachments(String(seg.content || ''))
      if (parsed.attachments.length > 0) {
        userAttachments.push(...parsed.attachments)
      }
      const cleaned = parsed.cleanedText
      if (!cleaned && parsed.attachments.length > 0) {
        // 纯附件消息：丢掉这个 text segment
        continue
      }
      out.push({ ...seg, content: cleaned })
    }
    return out
  })()
  const askUserAnswerPayload = message.role === 'user'
    ? parseAskUserAnswerPayload(visibleSegments)
    : null
  const hasVisibleSegments = visibleSegments.length > 0 && !askUserAnswerPayload
  const hasUserAttachments = userAttachments.length > 0
  const isFinalMerge = hasFinalMergeText(message)
  const hasWorkerResult = hasWorkerResultNotice(message.segments)

  // 2026-05-17：发送 → 首块到达之间的"等待占位"判定。
  // 流式中且 segments 全空 + content 全空 → 显示「正在思考...」+ Brain 图标，
  // 取代细 ▍ cursor 给用户更明确的"AI 正在准备"语义反馈。
  // 一旦首块（thinking/content/tool_call 任意）到达就关闭占位、走正常渲染。
  const isWaitingFirstChunk = Boolean(
    message.isStreaming
    && (!message.segments || message.segments.length === 0)
    && !(message.content || '').trim()
  )
  const isWaitingAfterToolResult = (() => {
    if (!message.isStreaming) return false
    const segments = message.segments || []
    const lastSegment = segments[segments.length - 1]
    return lastSegment?.type === 'tool_result'
  })()
  return (
    <div
      className={`message-row ${message.role}${message.isStreaming ? ' is-streaming' : ''}${
        editMark === 'target' ? ' is-edit-target' : ''
      }${editMark === 'after' ? ' is-edit-dimmed' : ''}`}
    >
      <div className={`message-avatar ${message.role}`}>
        {message.role === 'assistant' ? (
          <AgentAvatar
            name={sourceAgent?.name}
            src={avatarSrc}
            imageClassName="message-avatar-img"
            plainClassName="message-avatar-fallback"
          />
        ) : '我'}
      </div>

      <div className="message-body">
        <ConversationMessageMeta
          className="message-header"
          nameClassName="message-name"
          timeClassName="message-time"
          name={message.role === 'assistant' ? (sourceAgent?.name || '助手') : '我'}
          time={timeLabel}
          order={message.role === 'assistant' ? 'name-first' : 'time-first'}
        />

        <div className="message-content-wrapper">
          {/* v4 (2026-05-22) L2 原地编辑：编辑模式下用 InplaceMessageEditor 占满
              content-wrapper，其它正文（思考/工具调用卡 / 占位）一律不渲染，
              避免运行态 UI 与编辑器视觉冲突。退出编辑后正常恢复。 */}
          {isInplaceEditing && onInplaceEdit ? (
            <InplaceMessageEditor
              message={message}
              onCancel={() => setIsInplaceEditing(false)}
              onSave={async (payload) => {
                await onInplaceEdit(payload)
                setIsInplaceEditing(false)
              }}
              onSend={message.role === 'user' && onSendEdit ? async (text) => {
                await onSendEdit(text)
                // 发送成功：此条消息将被截断替换，直接收起编辑器；
                // 失败时 onSendEdit 抛错，走编辑器内错误横幅并保持打开
                setIsInplaceEditing(false)
              } : undefined}
            />
          ) : (
          <>
          {isWaitingFirstChunk && (
            // 2026-05-17 v2：占位 = 空的"思考中..."卡片头骨架，
            // className 完全复用 RuntimeThinkingBlock 的 chain-segment + chain-thinking + chain-segment-header，
            // 首个 thinking/text segment 到达时占位卸载、真实卡挂上，
            // 由于头部 DOM 结构与样式一致 → 视觉无缝衔接（仅 body 在原地展开）。
            <div className="chain-segment chain-thinking is-thinking-placeholder" aria-live="polite">
              <div className="chain-segment-header" aria-disabled="true">
                <span className="chain-label">
                  思考中<span className="chain-thinking-inline-dots" aria-hidden="true">...</span>
                </span>
              </div>
            </div>
          )}
          {hasUserAttachments && (
            // 用户消息上传的图片缩略图带（点击可看大图）。在文本之前渲染，
            // 跟微信/Cascade 习惯一致：图在上、文字在下。
            <UserMessageAttachments attachments={userAttachments} />
          )}
          {askUserAnswerPayload && <AskUserAnswerSummary payload={askUserAnswerPayload} />}
          {hasVisibleSegments && (
            <>
              <RuntimeChainSegments
                segments={visibleSegments}
                variant="chat"
                messageId={message.id}
                sessionId={sessionId}
                sourceAgent={sourceAgent}
                streaming={message.isStreaming}
                expandedThinking={expandedThinking}
                onToggleThinking={onToggleThinking}
                expandedToolCalls={expandedToolCalls}
                onToggleToolCall={onToggleToolCall}
                renderMarkdown={renderMarkdown}
                askStatesByAskId={askStatesByAskId}
                onSubmitAskAnswer={onSubmitAskAnswer}
              />
            </>
          )}

          {(() => {
            if (message.role !== 'assistant') return null
            if ((message.segments?.length || 0) > 0) return null

            const displayText = parseStoredRuntimeMessageContent(message.content || '').text
            if (!displayText.trim()) return null

            // 使用增强文本块（支持打字机效果）
            return (
              <EnhancedTextBlock
                content={displayText}
                isWorkerResult={hasWorkerResult}
                isFinalMerge={isFinalMerge}
                enableTypewriter={isFinalMerge}
                renderMarkdown={renderMarkdown}
              />
            )
          })()}

          {isWaitingAfterToolResult && (
            <div className="chain-segment chain-thinking is-thinking-placeholder" aria-live="polite">
              <div className="chain-segment-header" aria-disabled="true">
                <span className="chain-label">
                  思考中<span className="chain-thinking-inline-dots" aria-hidden="true">...</span>
                </span>
              </div>
            </div>
          )}

          </>
          )}
        </div>

        {!message.isStreaming && message.content && !hasTaskRouteNotice && !isInplaceEditing && (
          <div className="message-actions">
            {/* v6.1 按钮合并：原「修改并重新提问」（RotateCcw，截断重发）并入
                内联编辑器的「发送」按钮；PenLine 成为唯一编辑入口 ——
                user 消息编辑器内可选「保存」（只改文字）或「发送」（截断重发）。
                顺序：回滚（左）→ 编辑 → 删除 → 复制（右），破坏性从左到右递减。 */}
            {onRollback && (
              <RollbackMessageButton onRollback={onRollback} disabled={isBusy} />
            )}
            {onInplaceEdit && (message.role === 'user' || message.role === 'assistant') && (
              <InplaceEditButton
                onInplaceEdit={() => setIsInplaceEditing(true)}
                active={isInplaceEditing}
                disabled={isBusy}
                title={message.role === 'user'
                  ? '编辑消息：保存 = 只改文字；发送 = 替换之后对话重新提问'
                  : undefined}
              />
            )}
            {onDelete && (
              <DeleteMessageButton onDelete={onDelete} disabled={isBusy} />
            )}
            <CopyMessageButton copied={copied} onCopy={onCopy} />
          </div>
        )}
      </div>
    </div>
  )
}

/**
 * memo 比较器：让长会话流式时只重渲染「正在流式的那条消息」，跳过所有历史消息。
 *
 * 关键设计——**忽略回调身份**（onCopy / onToggleThinking / onToggleToolCall /
 * onInplaceEdit / onSendEdit / onRollback / onDelete /
 * onSubmitAskAnswer）：ChatTimeline 在 map 里给每条消息每次 render 都新建这些内联
 * 回调，但它们只闭包 `message`（下面按引用比较）与父级稳定函数，行为等价。若按身份
 * 比较，memo 永远命中不了。改为只比较真正影响本条渲染的数据：
 * - message 引用（流式时只有当前条换新引用，历史条引用不变）
 * - 展示态标量 / 稳定引用（sourceAgent / avatarSrc / timeLabel / copied / 两个展开
 *   Set / renderMarkdown / askStatesByAskId / isBusy / sessionId）。流式期间用户不
 *   toggle，两个 Set 引用恒定 → 历史条被跳过；toggle（罕见）时 Set 换引用 → 整列表
 *   重渲染，可接受。
 * - 可选回调的**存在性**（!! 比较）：按钮按存在性渲染，存在性翻转必须重渲染。
 */
export function arePropsEqual(prev: ChatMessageItemProps, next: ChatMessageItemProps): boolean {
  return (
    prev.message === next.message &&
    prev.sourceAgent === next.sourceAgent &&
    prev.avatarSrc === next.avatarSrc &&
    prev.timeLabel === next.timeLabel &&
    prev.copied === next.copied &&
    prev.expandedThinking === next.expandedThinking &&
    prev.expandedToolCalls === next.expandedToolCalls &&
    prev.renderMarkdown === next.renderMarkdown &&
    prev.askStatesByAskId === next.askStatesByAskId &&
    prev.isBusy === next.isBusy &&
    prev.sessionId === next.sessionId &&
    prev.editMark === next.editMark &&
    !!prev.onInplaceEdit === !!next.onInplaceEdit &&
    !!prev.onSendEdit === !!next.onSendEdit &&
    !!prev.onRollback === !!next.onRollback &&
    !!prev.onDelete === !!next.onDelete &&
    !!prev.onSubmitAskAnswer === !!next.onSubmitAskAnswer
  )
}

export default memo(ChatMessageItem, arePropsEqual)
