import { useCallback, useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import { ChevronUp, ChevronDown } from 'lucide-react'
import type { Agent, EditMessagePayload, Message as MessageType } from '../api/chat'
import ChatMessageItem from './ChatMessageItem'
import ChatWelcomePanel from './ChatWelcomePanel'
import { CHAT_SLASH_COMMANDS } from './chatUiText'
import { isRitualMarker, type ChatTimelineItem } from './chatExternalHandle'
import { renderChatMarkdown } from './chatMarkdown'
import { getAvatarSrc } from './avatarUtils'

interface ChatTimelineProps {
  timelineItems: ChatTimelineItem[]
  currentAgent: Agent | null
  copiedId: number | null
  expandedThinking: Set<string>
  expandedToolCalls: Set<string>
  inputRef: MutableRefObject<HTMLTextAreaElement | null>
  messagesContainerRef: MutableRefObject<HTMLDivElement | null>
  messagesEndRef: MutableRefObject<HTMLDivElement | null>
  userScrolledUpRef: MutableRefObject<boolean>
  setInput: Dispatch<SetStateAction<string>>
  setExpandedThinking: Dispatch<SetStateAction<Set<string>>>
  setExpandedToolCalls: Dispatch<SetStateAction<Set<string>>>
  copyContent: (id: number, content: string) => void
  getMessageSourceAgent: (message: MessageType) => Agent | null
  askStatesByAskId?: import('./runtimeBlocks').AskStatesByAskId
  onSubmitAskAnswer?: (askId: string, toolUseId: string | undefined, answers: import('./AskUserQuestionCard').AskUserAnswer[]) => void
  /** 编辑 user 消息：截断到该消息之前 + 内容塞回输入框 */
  onEditUserMessage?: (message: MessageType) => void
  /** v4 (2026-05-22) L2 原地编辑：不截断、不重发，仅改本条磁盘文本。 */
  onInplaceEditMessage?: (message: MessageType, payload: EditMessagePayload) => Promise<void>
  /** 回滚到该消息：截断到该消息之前（含确认弹窗） */
  onRollbackToMessage?: (message: MessageType) => void
  /** v5 (2026-05-26) 删除单条消息（user / assistant 都可，含确认弹窗） */
  onDeleteMessage?: (message: MessageType) => void
  /** 当前会话是否在 streaming / busy；为 true 时编辑/回滚按钮 disabled */
  isBusy?: boolean
  sessionId?: string | null
}

function formatTime(timestamp?: string) {
  if (!timestamp) return ''
  try {
    return new Date(timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  } catch {
    return ''
  }
}

const INITIAL_VISIBLE_COUNT = 25
const LOAD_MORE_STEP = 25

export default function ChatTimeline({
  timelineItems,
  currentAgent,
  copiedId,
  expandedThinking,
  expandedToolCalls,
  inputRef,
  messagesContainerRef,
  messagesEndRef,
  userScrolledUpRef,
  setInput,
  setExpandedThinking,
  setExpandedToolCalls,
  copyContent,
  getMessageSourceAgent,
  askStatesByAskId,
  onSubmitAskAnswer,
  onEditUserMessage,
  onInplaceEditMessage,
  onRollbackToMessage,
  onDeleteMessage,
  isBusy,
  sessionId,
}: ChatTimelineProps) {
  // 记上一次 onScroll 的 scrollTop，用于判定滚动方向（到底 → 不是用户上滑）。
  // 本质上用 scrollTop 单调性替代了程序滚动锁：我们程序滚动总是向下推
  // （scrollTop 增加），用户上滑总是 scrollTop 减少，方向完美区分两者。
  const lastScrollTopRef = useRef(0)

  // 微信级首屏性能优化：长会话默认只挂载最新 25 条，上滑按需平滑展开
  const [visibleCount, setVisibleCount] = useState(INITIAL_VISIBLE_COUNT)
  const isLoadingMoreRef = useRef(false)

  // 仿 Cline Desktop：当用户上滑离开底部时，右下角浮现「一键回到底部」悬浮圆钮
  const [showScrollBottom, setShowScrollBottom] = useState(false)

  // 当会话切换时，重置可见数量为初始值（25条），重置回到底部按钮状态
  useEffect(() => {
    setVisibleCount(INITIAL_VISIBLE_COUNT)
    setShowScrollBottom(false)
  }, [sessionId])

  // 当新消息追加时（发送消息 / AI 流式响应），保持尾部增长自动可见
  const prevLengthRef = useRef(timelineItems.length)
  useEffect(() => {
    if (timelineItems.length > prevLengthRef.current) {
      const diff = timelineItems.length - prevLengthRef.current
      setVisibleCount((prev) => prev + diff)
    }
    prevLengthRef.current = timelineItems.length
  }, [timelineItems.length])

  const totalCount = timelineItems.length
  const hasMoreHistory = totalCount > visibleCount
  const hiddenCount = Math.max(0, totalCount - visibleCount)
  const visibleItems = hasMoreHistory ? timelineItems.slice(-visibleCount) : timelineItems

  const handleLoadMoreHistory = useCallback(() => {
    if (isLoadingMoreRef.current) return
    isLoadingMoreRef.current = true

    const container = messagesContainerRef.current
    const prevScrollHeight = container?.scrollHeight || 0
    const prevScrollTop = container?.scrollTop || 0

    setVisibleCount((prev) => Math.min(timelineItems.length, prev + LOAD_MORE_STEP))

    requestAnimationFrame(() => {
      if (container) {
        const newScrollHeight = container.scrollHeight
        container.scrollTop = prevScrollTop + (newScrollHeight - prevScrollHeight)
      }
      setTimeout(() => {
        isLoadingMoreRef.current = false
      }, 100)
    })
  }, [timelineItems.length, messagesContainerRef])

  const handleScrollToBottom = useCallback(() => {
    const container = messagesContainerRef.current
    if (container) {
      container.scrollTo({
        top: container.scrollHeight,
        behavior: 'smooth',
      })
    }
    userScrolledUpRef.current = false
    setShowScrollBottom(false)
  }, [messagesContainerRef, userScrolledUpRef])

  return (
    <div className="chat-timeline-wrapper">
      <div
        className="messages-container"
        ref={messagesContainerRef}
        onWheel={(event) => {
          // 用户主动上滑 = 强意图信号。阈值放到 -5 容忍触控板细微；
          // **不查 distanceFromBottom**——onWheel 触发时浏览器还没更新 scrollTop
          // （默认滚动行为后置），scrollTop 是滚动前的值，distance 不可信。
          // 十几 px 后 onScroll 近底判定会自动 reset（用户手抖反悔不会被锁死）。
          if (event.deltaY < -5) {
            userScrolledUpRef.current = true
          }
        }}
        onScroll={(event) => {
          const element = event.currentTarget
          const { scrollTop, scrollHeight, clientHeight } = element
          const distanceFromBottom = scrollHeight - clientHeight - scrollTop
          const lastTop = lastScrollTopRef.current
          lastScrollTopRef.current = scrollTop

          // 1. 靠近底部 → reset（用户下滑回到底部，恢复跟随）
          // 阈值 80：比 set 阈值 120 留 40px 安全带，避免上滑经过中间区间时
          // "set→reset"震荡。用户感受：下滑回底部时不需要"严丝合缝"贴底才恢复。
          if (distanceFromBottom <= 80) {
            userScrolledUpRef.current = false
            setShowScrollBottom(false)
            return
          }

          // 离底超过阈值时展示回到底部按钮
          if (distanceFromBottom > 120) {
            setShowScrollBottom(true)
          }

          // 2. 离底远 + scrollTop **减少**（反向滑动）= 用户拖滚动条/独立拖拽上滑。
          // 我们的程序滚动总是 scrollTop 单调增加（推向 scrollHeight），
          // 不可能进这个分支——本质上用方向替代了程序锁，更简洁。
          if (scrollTop < lastTop - 2 && distanceFromBottom > 120) {
            userScrolledUpRef.current = true
          }

          // 3. 用户上滑靠近顶部 (scrollTop < 120) 且还有更早历史时，自动静默加载更早一批
          if (hasMoreHistory && scrollTop < 120 && scrollTop < lastTop) {
            handleLoadMoreHistory()
          }
        }}
      >
        {timelineItems.length === 0 && (
          <ChatWelcomePanel
            currentAgent={currentAgent}
            avatarSrc={getAvatarSrc(currentAgent)}
            quickCommands={CHAT_SLASH_COMMANDS.slice(0, 4)}
            onQuickCommand={(command) => {
              setInput(`${command} `)
              inputRef.current?.focus()
            }}
          />
        )}

        {hasMoreHistory && (
          <div className="chat-history-load-more-row">
            <button
              type="button"
              className="chat-history-load-more-btn"
              onClick={handleLoadMoreHistory}
            >
              <ChevronUp size={13} />
              <span>查看更早的历史记录 · 剩余 {hiddenCount} 条</span>
            </button>
          </div>
        )}

        {visibleItems.map((item) => {
          if (isRitualMarker(item)) {
            return (
              <div key={item.id} className={`chat-ritual-marker-row chat-ritual-marker-${item.phase.replace('consult_', '')}`}>
                <div className={`attr-divider chat-ritual-marker chat-ritual-marker-${item.phase.replace('consult_', '')}`}>
                  <span>{item.content}</span>
                </div>
              </div>
            )
          }

          const message = item
          const sourceAgent = message.role === 'assistant' ? getMessageSourceAgent(message) : null

          return (
            <ChatMessageItem
              key={message.id}
              message={message}
              sourceAgent={sourceAgent}
              avatarSrc={getAvatarSrc(sourceAgent)}
              timeLabel={formatTime(message.timestamp)}
              copied={copiedId === message.id}
              onCopy={() => copyContent(message.id, message.content || '')}
              expandedThinking={expandedThinking}
              onToggleThinking={(thinkingKey) => setExpandedThinking((prev) => {
                const next = new Set(prev)
                next.has(thinkingKey) ? next.delete(thinkingKey) : next.add(thinkingKey)
                return next
              })}
              expandedToolCalls={expandedToolCalls}
              onToggleToolCall={(toolKey) => setExpandedToolCalls((prev) => {
                const next = new Set(prev)
                next.has(toolKey) ? next.delete(toolKey) : next.add(toolKey)
                return next
              })}
              renderMarkdown={renderChatMarkdown}
              askStatesByAskId={askStatesByAskId}
              onSubmitAskAnswer={onSubmitAskAnswer}
              onEdit={onEditUserMessage ? () => onEditUserMessage(message) : undefined}
              onInplaceEdit={onInplaceEditMessage ? (payload) => onInplaceEditMessage(message, payload) : undefined}
              onRollback={onRollbackToMessage ? () => onRollbackToMessage(message) : undefined}
              onDelete={onDeleteMessage ? () => onDeleteMessage(message) : undefined}
              isBusy={!!isBusy}
              sessionId={sessionId}
            />
          )
        })}

        <div ref={messagesEndRef} />
      </div>

      <div className={`chat-scroll-bottom-container ${showScrollBottom ? 'visible' : ''}`}>
        <div className="chat-scroll-bottom-inner">
          <button
            type="button"
            className="chat-scroll-bottom-btn"
            onClick={handleScrollToBottom}
            title="回到底部"
            aria-label="回到底部"
          >
            <ChevronDown size={18} strokeWidth={2.2} />
          </button>
        </div>
      </div>
    </div>
  )
}
