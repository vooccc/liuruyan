import { ChevronDown, Loader2 } from 'lucide-react'
import type { Agent, ContentSegment } from '../api/chat'
import AgentAvatar from './AgentAvatar'
import { getAvatarFallback, getAvatarSrc } from './avatarUtils'
import { ConversationMessageMeta, CopyMessageButton } from './conversationUi'
import {
  RuntimeChainSegments,
} from './runtimeBlocks'
import { getRuntimeChainSummary, hasDetailedRuntimeChain } from './runtimeDisplay'

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
}

interface CollabMessageItemProps {
  bubble: CollabBubble
  agents: Agent[]
  isMe: boolean
  isSystem: boolean
  copied: boolean
  onCopy: () => void
  onToggleChain: () => void
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

export default function CollabMessageItem({
  bubble,
  agents,
  isMe,
  isSystem,
  copied,
  onCopy,
  onToggleChain,
}: CollabMessageItemProps) {
  const detailedChain = hasDetailedRuntimeChain(bubble.segments)
  const chainSummary = getRuntimeChainSummary(bubble.segments)
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
                  />
                </div>
              )}
            </div>
          )}

          <div className="collab-bubble-body">
            {normalizeBubbleContent(bubble.content || '')}
            {bubble.isStreaming && (
              <span className={`collab-stream-caret ${isMe ? 'is-user' : 'is-agent'}`}>▍</span>
            )}
          </div>
        </div>

        {!bubble.isStreaming && bubble.content && (
          <div className={`collab-message-actions ${isMe ? 'is-user' : 'is-agent'}`}>
            <CopyMessageButton copied={copied} onCopy={onCopy} />
          </div>
        )}
      </div>

      {isMe && <span className="collab-message-avatar is-user">我</span>}
    </div>
  )
}
