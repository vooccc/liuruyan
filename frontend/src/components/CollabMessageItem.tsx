import { ChevronDown, Loader2, Wrench } from 'lucide-react'
import { useCallback, useState, type ReactNode } from 'react'
import type { Agent, ContentSegment, EditMessagePayload } from '../api/chat'
import AgentAvatar from './AgentAvatar'
import { getAvatarFallback, getAvatarSrc } from './avatarUtils'
import { ConversationMessageMeta, CopyMessageButton, DeleteMessageButton, InplaceEditButton } from './conversationUi'
import AskUserQuestionCard, { type AskUserAnswer } from './AskUserQuestionCard'
import InplaceMessageEditor from './InplaceMessageEditor'
import { renderChatMarkdown } from './chatMarkdown'
import {
  RuntimeChainSegments,
  tryParseAskPayload,
  type AskStatesByAskId,
} from './runtimeBlocks'
import { getRuntimeChainSummary, hasDetailedRuntimeChain } from './runtimeDisplay'
import { summarizeCollabTools } from './collabRuntime'

export interface CollabBubble {
  id: string
  agentId: string
  agentName: string
  avatar: string
  /**
   * 角色头像图片路径快照（来自 agent.avatar_url），bubble 构建时固化。
   *
   * 为啥不靠渲染时的 `agents.find` 现查？
   * - history 加载完后用户可能改 agents（删/重命名/重建），此时 find 拿不到，
   *   src 退化成 null，AgentAvatar 走"首字兜底"——视觉上整片消息突然变成首字头像。
   * - 收到 streaming 事件那一刻 agents 还可能在 async 同步中。
   * 把 url 锁在 bubble 上，渲染就只读 bubble 一份事实。
   */
  avatarUrl?: string
  content: string
  isStreaming: boolean
  segments: ContentSegment[]
  chainExpanded: boolean
  /**
   * 会话内的可见行下标（2026-09-30）：历史加载时由 useCollabHistoryLoader 注入。
   * 编辑/删除按它定位后端消息；流式中的本地气泡没有此字段 → 不显示编辑/删除。
   * 注意同一轮所有角色气泡共享同一条助手消息的下标。
   */
  sessionMessageIndex?: number
}

interface CollabMessageItemProps {
  bubble: CollabBubble
  agents: Agent[]
  isMe: boolean
  isSystem: boolean
  copied: boolean
  onCopy: () => void
  onToggleChain: () => void
  /** ask_user 卡片状态（P1 议事厅闭环）：CollabChat 持有，跨 bubble 共享 */
  askStatesByAskId?: AskStatesByAskId
  /** ask_user 答案回流（POST /chat/ask/answer）。未接线时提问卡只展示不可交互 */
  onSubmitAskAnswer?: (askId: string, toolUseId: string | undefined, answers: AskUserAnswer[]) => Promise<void> | void
  /** 编辑保存（只改文字）。未传或气泡无 sessionMessageIndex 时不显示编辑按钮 */
  onEditSave?: (sessionMessageIndex: number, payload: EditMessagePayload) => Promise<void> | void
  /** 编辑发送（截断该条之后 + 用新文本重跑一轮） */
  onEditSend?: (sessionMessageIndex: number, text: string) => Promise<void> | void
  /** 删除：user 气泡 = 删整轮；角色气泡 = 删该轮所有角色发言（确认在 CollabChat 做） */
  onDelete?: (bubble: CollabBubble) => Promise<void> | void
  /** 协作进行中禁用编辑/删除 */
  busy?: boolean
}

function normalizeBubbleContent(text: string): string {
  return text
    .replace(/\r\n?/g, '\n')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n[ \t]+/g, '\n')
    .split('\n')
    .map((line) => line.trimEnd())
    .join('\n')
    .replace(/^(?:\s*\n)+/, '')
    .replace(/(?:\n\s*)+$/, '')
    .replace(/\n\s*\n+/g, '\n')
}

/**
 * markdown 正文预处理：比 normalizeBubbleContent 轻——不去掉段间空行
 *（空行是 markdown 的段落/列表边界），只修行尾空白 + 掐头去尾。
 */
function normalizeMarkdownContent(text: string): string {
  return text
    .replace(/\r\n?/g, '\n')
    .replace(/[ \t]+\n/g, '\n')
    .split('\n')
    .map((line) => line.trimEnd())
    .join('\n')
    .replace(/^(?:\s*\n)+/, '')
    .replace(/(?:\n\s*)+$/, '')
}

export default function CollabMessageItem({
  bubble,
  agents,
  isMe,
  isSystem,
  copied,
  onCopy,
  onToggleChain,
  askStatesByAskId,
  onSubmitAskAnswer,
  onEditSave,
  onEditSend,
  onDelete,
  busy = false,
}: CollabMessageItemProps) {
  const detailedChain = hasDetailedRuntimeChain(bubble.segments)
  const chainSummary = getRuntimeChainSummary(bubble.segments)
  const toolChips = summarizeCollabTools(bubble.segments)
  const displayName = isMe ? '我' : bubble.agentName
  const sourceAgent = !isMe && !isSystem
    ? agents.find((agent) => agent.id === bubble.agentId) || null
    : null
  // bubble.avatarUrl 是构建时固化的图片路径快照；agents 列表事后变动时这条仍是真相源。
  const avatarEntity = !isMe && !isSystem
    ? {
        avatar: sourceAgent?.avatar ?? bubble.avatar,
        avatar_url: sourceAgent?.avatar_url ?? bubble.avatarUrl,
      }
    : null

  // P1 2026-09-29：正文从纯文本升级为 markdown（与单聊同一 renderChatMarkdown
  // 管线：GFM 表格、breaks、代码高亮、复制按钮）。首 chunk 前内容为空时退回
  // 纯文本渲染路径，流式光标继续走 span。
  const rawBody = normalizeMarkdownContent(bubble.content || '')
  const bodyHtml = rawBody.trim() ? renderChatMarkdown(rawBody) : ''

  // 内联 ask 卡（P1 议事厅闭环）：提问必须常驻可见——链路面板会被自动折叠，
  // 塞面板里会把问题藏起来。扫描 tool_call 段的 awaiting/answered 载荷逐条出卡；
  // 与通用工厂面板里的渲染互斥（collab 不给 RuntimeChainSegments 传 ask props）。
  const askCards = (() => {
    if (isMe || isSystem) return null
    const nodes: ReactNode[] = []
    bubble.segments.forEach((segment, index) => {
      if (segment.type !== 'tool_call') return
      const resultSegment = bubble.segments.find((candidate) => (
        candidate.type === 'tool_result'
          && (candidate.tool_use_id || '') === (segment.tool_use_id || '')
      ))
      const payload = tryParseAskPayload(resultSegment)
      if (!payload) return
      const askState = askStatesByAskId?.[payload.ask_id]
      const executionState = payload.resolvedAnswers?.length
        ? ('submitted' as const)
        : askState?.state
      nodes.push(
        <AskUserQuestionCard
          key={`collab-ask-${bubble.id}-${index}`}
          payload={payload}
          toolUseId={segment.tool_use_id}
          executionState={executionState}
          executionError={askState?.error ?? null}
          onSubmit={onSubmitAskAnswer}
        />,
      )
    })
    return nodes.length > 0 ? nodes : null
  })()

  // 链路内部块的独立折叠状态（2026-09-30 P1 打磨）：key 由 RuntimeChainSegments
  // 按 `${messageId}-think-${i}` / `${messageId}-tc-${i}` 规则构造，这里只存集合。
  // 状态随 bubble 挂载存续；整条链路收起再展开时回到自然状态，符合直觉。
  const [expandedThinking, setExpandedThinking] = useState<Set<string>>(() => new Set())
  const [expandedToolCalls, setExpandedToolCalls] = useState<Set<string>>(() => new Set())
  const toggleThinking = useCallback((key: string) => {
    setExpandedThinking((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])
  const toggleToolCall = useCallback((key: string) => {
    setExpandedToolCalls((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  // 内联编辑（2026-09-30）：仅历史加载的用户气泡可编辑（有 sessionMessageIndex）。
  // 编辑器由 CollabMessageItem 本地挂载；保存/重发完成后父级重拉历史，本气泡
  // 重建、编辑态自然消失。出错时编辑器保持打开（InplaceMessageEditor 自带错误展示）。
  const [isEditing, setIsEditing] = useState(false)
  const canMutate = bubble.sessionMessageIndex !== undefined && !isSystem && !bubble.isStreaming
  const handleEditSave = useCallback(async (payload: EditMessagePayload) => {
    if (!onEditSave || bubble.sessionMessageIndex === undefined) return
    await onEditSave(bubble.sessionMessageIndex, payload)
    setIsEditing(false)
  }, [bubble.sessionMessageIndex, onEditSave])
  const handleEditSend = useCallback(async (text: string) => {
    if (!onEditSend || bubble.sessionMessageIndex === undefined) return
    await onEditSend(bubble.sessionMessageIndex, text)
    setIsEditing(false)
  }, [bubble.sessionMessageIndex, onEditSend])
  const handleDelete = useCallback(() => {
    if (!onDelete || !canMutate) return
    void onDelete(bubble)
  }, [bubble, canMutate, onDelete])

  return (
    <div className={`collab-message-row ${isMe ? 'is-user' : isSystem ? 'is-system' : 'is-agent'}`}>
      {!isMe && (
        <span className={`collab-message-avatar ${isSystem ? 'is-system' : 'is-agent'}`}>
          {isSystem ? (
            bubble.avatar
          ) : (
            <AgentAvatar
              name={sourceAgent?.name || bubble.agentName}
              src={getAvatarSrc(avatarEntity)}
              avatar={getAvatarFallback(avatarEntity, bubble.avatar || '')}
              fallbackAvatar="AI"
              imageClassName="collab-avatar-img"
              plainClassName="collab-avatar-fallback"
            />
          )}
        </span>
      )}

      <div className={`collab-message-main ${isMe ? 'is-user' : isSystem ? 'is-system' : 'is-agent'}`}>
        {!isSystem && (
          <ConversationMessageMeta
            className={`collab-message-header ${isMe ? 'is-user' : 'is-agent'}`}
            nameClassName="collab-message-name"
            name={displayName}
            trailing={bubble.isStreaming ? <Loader2 size={10} className="animate-spin collab-stream-loader" /> : undefined}
          />
        )}

        <div className={`collab-bubble-card ${isMe ? 'is-user' : isSystem ? 'is-system' : 'is-agent'}`}>
          {isEditing ? (
            <div className="collab-inplace-shell">
              <InplaceMessageEditor
                message={isMe
                  ? { id: Date.now(), role: 'user', content: bubble.content, isStreaming: false, segments: [] }
                  : // 角色气泡：assistant 模式——编辑器按 block_index 为该角色的
                    // text/thinking 段建槽位，保存走 block_patches（同单聊）
                    { id: Date.now(), role: 'assistant', content: bubble.content, isStreaming: false, segments: bubble.segments }}
                onSave={handleEditSave}
                onSend={isMe && onEditSend ? handleEditSend : undefined}
                onCancel={() => setIsEditing(false)}
              />
            </div>
          ) : (
            <>
              {!isMe && !isSystem && detailedChain && (
                <div className="collab-chain-shell">
                  <button
                    type="button"
                    className={`collab-chain-toggle ${bubble.chainExpanded ? 'expanded' : ''}`}
                    onClick={onToggleChain}
                  >
                    <span className="collab-chain-toggle-main">
                      <span className="collab-chain-toggle-label">{bubble.chainExpanded ? '收起链路' : '展开链路'}</span>
                      {chainSummary && <span className="collab-chain-toggle-summary">{chainSummary}</span>}
                    </span>
                    <ChevronDown size={14} className="collab-chain-toggle-icon" />
                  </button>

                  {bubble.chainExpanded && (
                    <div className="collab-chain-panel">
                      <RuntimeChainSegments
                        segments={bubble.segments}
                        variant="collab"
                        messageId={bubble.id}
                        expandedThinking={expandedThinking}
                        onToggleThinking={toggleThinking}
                        expandedToolCalls={expandedToolCalls}
                        onToggleToolCall={toggleToolCall}
                      />
                    </div>
                  )}
                </div>
              )}

              <div className={`collab-bubble-body${bubble.isStreaming && bodyHtml ? ' is-streaming' : ''}`}>
                {bodyHtml ? (
                  <div
                    className="message-content collab-md-content"
                    dangerouslySetInnerHTML={{ __html: bodyHtml }}
                  />
                ) : (
                  <>
                    {normalizeBubbleContent(bubble.content || '')}
                    {bubble.isStreaming && (
                      <span className={`collab-stream-caret ${isMe ? 'is-user' : 'is-agent'}`}>▍</span>
                    )}
                  </>
                )}
              </div>

              {askCards}

              {!isMe && !isSystem && !bubble.chainExpanded && toolChips.length > 0 && (
                <button
                  type="button"
                  className="collab-bubble-toolchips"
                  onClick={onToggleChain}
                  title="查看工具调用详情"
                >
                  {toolChips.map((chip, index) => (
                    <span
                      key={`${chip.name}-${index}`}
                      className={`collab-toolchip ${chip.done ? 'is-done' : 'is-calling'}`}
                    >
                      <Wrench size={11} aria-hidden />
                      <span className="collab-toolchip-name">{chip.name}</span>
                      <span className="collab-toolchip-state">{chip.done ? '已完成' : '调用中'}</span>
                    </span>
                  ))}
                </button>
              )}
            </>
          )}
        </div>

        {!bubble.isStreaming && bubble.content && (
          <div className={`collab-message-actions ${isMe ? 'is-user' : 'is-agent'}`}>
            {canMutate && onEditSave && (
              <InplaceEditButton
                onInplaceEdit={() => setIsEditing(true)}
                disabled={busy}
                title={isMe
                  ? '编辑本条（保存=只改文字 / 发送=截断之后重跑一轮）'
                  : '编辑这条发言（只改文字，不影响之后对话）'}
              />
            )}
            {canMutate && onDelete && (
              <DeleteMessageButton
                onDelete={handleDelete}
                disabled={busy}
                title={isMe
                  ? '删除这一轮（你的消息 + 本轮全部角色回复），之后对话保留'
                  : '删除该角色在本轮的这条发言（其他角色保留）'}
              />
            )}
            <CopyMessageButton copied={copied} onCopy={onCopy} />
          </div>
        )}
      </div>

      {isMe && <span className="collab-message-avatar is-user">我</span>}
    </div>
  )
}
