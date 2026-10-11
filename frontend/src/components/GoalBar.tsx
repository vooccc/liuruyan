import React, { useState, useEffect } from 'react'
import {
  Target,
  Play,
  Pause,
  RotateCcw,
  CheckCircle2,
  AlertTriangle,
  X,
  ChevronRight,
  Clock,
  Sparkles,
  Layers,
  FileCheck2,
} from 'lucide-react'
import { GoalState, updateSessionGoal } from '../api/chat'

interface GoalBarProps {
  sessionId: string | null
  goalState: GoalState | null
  onRefreshGoal?: () => void
  onResumeGoal?: () => void
}

export const GoalBar: React.FC<GoalBarProps> = ({
  sessionId,
  goalState,
  onRefreshGoal,
  onResumeGoal,
}) => {
  const [showDrawer, setShowDrawer] = useState(false)
  const [operating, setOperating] = useState(false)

  // 支持按 Esc 快捷关闭轨迹抽屉
  useEffect(() => {
    if (!showDrawer) return
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setShowDrawer(false)
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [showDrawer])

  if (!goalState || goalState.status === 'none') {
    return null
  }

  const handleAction = async (action: 'pause' | 'resume' | 'clear') => {
    if (!sessionId || operating) return
    setOperating(true)
    try {
      await updateSessionGoal(sessionId, { action })
      onRefreshGoal?.()
      if (action === 'resume') {
        onResumeGoal?.()
      }
    } catch (err) {
      console.error('[GoalBar] operation failed:', err)
    } finally {
      setOperating(false)
    }
  }

  const isActive = goalState.status === 'active'
  const isPaused = goalState.status === 'paused'
  const isBlocked = goalState.status === 'blocked'
  const isCompleted = goalState.status === 'completed'

  // 当前正在进行的轮次（用于避免刚启动时显示"0/15"导致用户困惑）
  const activeRound = isActive
    ? Math.min(goalState.used_turns + 1, goalState.max_turns)
    : goalState.used_turns

  // 进度百分比（至少 4% 发丝线可见度）
  const progressPct = isCompleted
    ? 100
    : Math.min(100, Math.max(4, Math.round((activeRound / (goalState.max_turns || 15)) * 100)))

  const statusConfig = {
    active: {
      color: 'var(--accent-purple, #8b5cf6)',
      badge: `推进中 · 第 ${activeRound}/${goalState.max_turns} 轮`,
      icon: <Sparkles size={13} />,
      dotClass: 'pulse',
      fillClass: 'is-active',
      badgeBg: 'color-mix(in srgb, var(--accent-purple, #8b5cf6) 14%, transparent)',
    },
    paused: {
      color: 'var(--text-secondary, #6b7280)',
      badge: `已暂停 · 第 ${goalState.used_turns}/${goalState.max_turns} 轮`,
      icon: <Pause size={13} />,
      dotClass: '',
      fillClass: 'is-paused',
      badgeBg: 'color-mix(in srgb, var(--text-secondary, #6b7280) 14%, transparent)',
    },
    completed: {
      color: '#10b981',
      badge: `已达成 · 共 ${goalState.used_turns} 轮`,
      icon: <CheckCircle2 size={13} />,
      dotClass: '',
      fillClass: 'is-completed',
      badgeBg: 'color-mix(in srgb, #10b981 14%, transparent)',
    },
    blocked: {
      color: '#ef4444',
      badge: `受阻 · 第 ${goalState.used_turns}/${goalState.max_turns} 轮`,
      icon: <AlertTriangle size={13} />,
      dotClass: '',
      fillClass: 'is-blocked',
      badgeBg: 'color-mix(in srgb, #ef4444 14%, transparent)',
    },
  }[goalState.status] || {
    color: '#6b7280',
    badge: '就绪',
    icon: <Target size={13} />,
    dotClass: '',
    fillClass: '',
    badgeBg: 'transparent',
  }

  return (
    <>
      <div className="goal-bar-header">
        {/* 底部细发丝进度条 */}
        <div className="goal-bar-progress-track">
          <div
            className={`goal-bar-progress-fill ${statusConfig.fillClass}`}
            style={{ width: `${progressPct}%` }}
          />
        </div>

        {/* 左侧目标概要与状态信标 */}
        <div
          className="goal-bar-main"
          onClick={() => setShowDrawer(true)}
          title="点击查看目标履约全过程与审计实据"
        >
          <span className="goal-bar-beacon" style={{ color: statusConfig.color }}>
            {statusConfig.icon}
          </span>

          <span
            className="goal-bar-badge"
            style={{
              backgroundColor: statusConfig.badgeBg,
              color: statusConfig.color,
            }}
          >
            <span className={`goal-bar-badge-dot ${statusConfig.dotClass}`} />
            <span>{statusConfig.badge}</span>
          </span>

          <div className="goal-bar-objective">
            <span className="goal-bar-objective-label">目标:</span>
            <span className="goal-bar-objective-text">{goalState.objective}</span>
          </div>
        </div>

        {/* 右侧动作控制 */}
        <div className="goal-bar-actions">
          {isActive && (
            <button
              type="button"
              className="goal-bar-btn"
              onClick={() => handleAction('pause')}
              disabled={operating}
              title="暂停目标自主推进"
            >
              <Pause size={12} />
              <span>暂停</span>
            </button>
          )}

          {(isPaused || isBlocked) && (
            <button
              type="button"
              className="goal-bar-btn goal-bar-btn-primary"
              onClick={() => handleAction('resume')}
              disabled={operating}
              title="继续恢复推进目标"
            >
              <Play size={12} />
              <span>继续</span>
            </button>
          )}

          <button
            type="button"
            className="goal-bar-btn goal-bar-btn-ghost"
            onClick={() => handleAction('clear')}
            disabled={operating}
            title="结束并清除目标契约"
          >
            <RotateCcw size={12} />
            <span>清除</span>
          </button>

          <button
            type="button"
            className="goal-bar-btn goal-bar-btn-trajectory"
            onClick={() => setShowDrawer(true)}
            title="查看完整审计轨迹"
          >
            <Layers size={12} />
            <span>履约轨迹{goalState.iterations.length > 0 ? ` (${goalState.iterations.length})` : ''}</span>
            <ChevronRight size={13} style={{ marginLeft: -2 }} />
          </button>
        </div>
      </div>

      {/* 审计轨迹右侧滑出抽屉 */}
      {showDrawer && (
        <>
          <div
            className="goal-drawer-backdrop"
            onClick={() => setShowDrawer(false)}
            aria-hidden="true"
          />

          <aside className="goal-drawer-panel" aria-label="目标履约轨迹">
            <div className="goal-drawer-header">
              <div className="goal-drawer-title-group">
                <Layers size={17} style={{ color: statusConfig.color }} />
                <span className="goal-drawer-title">目标履约轨迹</span>
                <span
                  className="goal-bar-badge"
                  style={{
                    backgroundColor: statusConfig.badgeBg,
                    color: statusConfig.color,
                    fontSize: '11px',
                  }}
                >
                  <span className={`goal-bar-badge-dot ${statusConfig.dotClass}`} />
                  <span>{statusConfig.badge}</span>
                </span>
              </div>
              <button
                type="button"
                className="goal-drawer-close-btn"
                onClick={() => setShowDrawer(false)}
                title="关闭抽屉 (Esc)"
              >
                <X size={16} />
              </button>
            </div>

            <div className="goal-drawer-body">
              {/* 目标契约 */}
              <div className="goal-drawer-section">
                <div className="goal-drawer-label">目标契约</div>
                <div className="goal-drawer-objective-card">
                  {goalState.objective}
                </div>
              </div>

              {/* 阻碍警报 */}
              {goalState.blocked_reason && (
                <div className="goal-drawer-blocked-alert">
                  <strong>阻碍卡点：</strong>
                  {goalState.blocked_reason}
                </div>
              )}

              {/* 推演历程 */}
              <div className="goal-drawer-section">
                <div className="goal-drawer-label">
                  推演历程（{isActive ? `第 ${activeRound} 轮推进中` : `共 ${goalState.iterations.length} 轮`}）
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                  {/* 若当前轮次正在推进中，展示动态呼吸卡片，消除"推演历程 0 轮 / 尚无记录"的误解 */}
                  {isActive && (
                    <div className="goal-active-iteration-card">
                      <div className="goal-active-iteration-header">
                        <div className="goal-active-iteration-title">
                          <span className="goal-bar-badge-dot pulse" style={{ backgroundColor: 'var(--accent-purple, #8b5cf6)' }} />
                          <span>第 {activeRound} 轮 · 正在推演执行中</span>
                        </div>
                        <span
                          style={{
                            fontSize: '11px',
                            padding: '1px 6px',
                            borderRadius: '4px',
                            background: 'color-mix(in srgb, var(--accent-purple, #8b5cf6) 20%, transparent)',
                            color: 'var(--accent-purple, #a855f7)',
                            fontWeight: 600,
                          }}
                        >
                          实时执行中
                        </span>
                      </div>
                      <p className="goal-active-iteration-desc">
                        模型正在自主执行工具、探测环境与编辑修改。本轮执行完毕后，验收判官将提取客观实据并在此生成本轮结论与审计快照。
                      </p>
                    </div>
                  )}

                  {/* 已完成轮次列表 */}
                  {goalState.iterations.map((it) => (
                    <div key={it.iteration_index} className="goal-iteration-item">
                      <div className="goal-iteration-header">
                        <span>第 {it.iteration_index} 轮</span>
                        <span style={{ color: 'var(--text-muted)', fontWeight: 400, fontSize: '11.5px' }}>
                          <Clock size={11} style={{ display: 'inline', marginRight: 3, verticalAlign: -1 }} />
                          {new Date(it.timestamp * 1000).toLocaleTimeString()}
                        </span>
                      </div>
                      <div className="goal-iteration-summary">
                        <strong>结论：</strong>
                        {it.summary || '已推进'}
                      </div>
                      {it.next_action && (
                        <div className="goal-iteration-action">
                          <strong>下一步动作：</strong>
                          {it.next_action}
                        </div>
                      )}
                      {it.evidence && it.evidence.length > 0 && (
                        <div className="goal-iteration-evidence">
                          <div style={{ display: 'flex', alignItems: 'center', gap: 4, fontWeight: 600 }}>
                            <FileCheck2 size={12} />
                            <span>客观验收证据：</span>
                          </div>
                          {it.evidence.map((ev, ei) => (
                            <div key={ei} style={{ wordBreak: 'break-all', opacity: 0.9 }}>
                              • {ev}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}

                  {/* 若非进行中且无历史记录 */}
                  {!isActive && goalState.iterations.length === 0 && (
                    <div style={{ fontSize: '12px', color: 'var(--text-muted)', fontStyle: 'italic', padding: '12px 0' }}>
                      尚无已完成的推进记录
                    </div>
                  )}
                </div>
              </div>
            </div>
          </aside>
        </>
      )}
    </>
  )
}
