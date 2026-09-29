import type { CSSProperties } from 'react'
import { PanelLeftClose, PanelLeftOpen, Trash2, X } from 'lucide-react'
import type { Agent } from '../api/chat'
import AgentAvatar from './AgentAvatar'
import { getAvatarFallback, getAvatarSrc } from './avatarUtils'
import { COLLAB_PHASE_STEPS } from './collabUiText'

interface CollabHeaderBarProps {
  embedded: boolean
  themeStyle: CSSProperties
  emoji: string
  currentPhaseLabel: string
  /** 当前阶段在 COLLAB_PHASE_STEPS 里的 index；-1 或 undefined 表示非场景模式，不显示阶段轨 */
  phaseIndex?: number
  /** quick 模式下传 undefined。非 undefined 时启用阶段轨 UI */
  title: string
  subtitle: string
  activeParticipants: string[]
  agents: Agent[]
  canDismissSession: boolean
  dismissTitle: string
  closeTitle: string
  onDismiss: () => void
  onClose: () => void
  /**
   * Sidebar 切换控件（仅 embedded 模式渲染 hamburger）。
   *
   * 议事厅模式下 App.tsx 的 .main-header 整个不渲染（CollabHeaderBar 自带头），
   * 单聊那个 .sidebar-toggle-btn 也跟着没了。这里补上同款按钮，让议事厅
   * 收 sidebar 后能从 header 重新展开，跟单聊体验对齐。
   */
  sidebarOpen?: boolean
  onToggleSidebar?: () => void
}

export default function CollabHeaderBar({
  embedded,
  themeStyle,
  emoji,
  currentPhaseLabel,
  phaseIndex,
  title,
  subtitle,
  activeParticipants,
  agents,
  canDismissSession,
  dismissTitle,
  closeTitle,
  onDismiss,
  onClose,
  sidebarOpen,
  onToggleSidebar,
}: CollabHeaderBarProps) {
  const showPhaseTrack = typeof phaseIndex === 'number' && phaseIndex >= 0
  const showSidebarToggle = embedded && typeof onToggleSidebar === 'function'

  return (
    <div className="collab-header-roof" style={themeStyle}>
      {/* 左：场景铭牌 + 标题 + 副信息（单列），不再重复 emoji/阶段 */}
      <div className="collab-header-brand">
        {showSidebarToggle && (
          <button
            type="button"
            className="sidebar-toggle-btn collab-sidebar-toggle"
            onClick={onToggleSidebar}
            aria-label={sidebarOpen ? '收起侧边栏' : '展开侧边栏'}
            title={sidebarOpen ? '收起侧边栏' : '展开侧边栏'}
          >
            {sidebarOpen ? <PanelLeftClose size={17} strokeWidth={1.8} /> : <PanelLeftOpen size={17} strokeWidth={1.8} />}
          </button>
        )}
        <span className="collab-header-emoji" aria-hidden="true">{emoji}</span>
        <div className="collab-header-text">
          <div className="collab-header-title" title={title}>{title}</div>
          <div className="collab-header-meta">{subtitle}</div>
        </div>
      </div>

      {/* 中：阶段进度轨（只在场景模式显示）；quick 模式这里留空，给中间大片呼吸 */}
      {showPhaseTrack && (
        <div
          className="collab-phase-track"
          role="progressbar"
          aria-valuemin={1}
          aria-valuemax={COLLAB_PHASE_STEPS.length}
          aria-valuenow={(phaseIndex as number) + 1}
          aria-label={`当前阶段：${currentPhaseLabel}`}
        >
          <div className="collab-phase-dots">
            {COLLAB_PHASE_STEPS.map((step, idx) => {
              const state = idx < (phaseIndex as number)
                ? 'past'
                : idx === phaseIndex
                ? 'current'
                : 'future'
              return (
                <span
                  key={step.key}
                  className={`collab-phase-dot is-${state}`}
                  title={`${step.icon} ${step.label}`}
                  aria-label={step.label}
                />
              )
            })}
          </div>
          <span className="collab-phase-ticker">{currentPhaseLabel}</span>
        </div>
      )}

      {/* 右：参与者头像堆 + 操作按钮（icon-only，hover 显示 tooltip，节省空间） */}
      <div className="collab-header-right">
        <div className="collab-inline-participants">
          {activeParticipants.map((agentId) => {
            const agent = agents.find((item) => item.id === agentId)
            return (
              <span key={agentId} className="collab-participant-chip" title={agent?.name || agentId}>
                <AgentAvatar
                  name={agent?.name}
                  src={getAvatarSrc(agent)}
                  avatar={getAvatarFallback(agent, '')}
                  fallbackAvatar="AI"
                  imageClassName="collab-participant-avatar-img"
                  plainClassName="collab-participant-avatar-fallback"
                />
              </span>
            )
          })}
        </div>

        <div className="collab-header-actions">
          {canDismissSession && (
            <button
              type="button"
              className="collab-icon-btn is-danger"
              onClick={onDismiss}
              title={dismissTitle}
              aria-label={dismissTitle}
            >
              <Trash2 size={15} />
            </button>
          )}
          {!embedded && (
            <button
              type="button"
              className="collab-icon-btn"
              onClick={onClose}
              title={closeTitle}
              aria-label={closeTitle}
            >
              <X size={16} />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
