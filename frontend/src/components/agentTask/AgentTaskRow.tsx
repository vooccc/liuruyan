import { Wrench, Zap, Clock, ChevronDown, ChevronRight, Users, Sparkles, Brain, ArrowRight, ArrowLeft } from 'lucide-react'
import { renderChatMarkdown } from '../chatMarkdown'
import type { AgentTaskActivity, AgentTaskStat, AgentTaskStatus } from './types'

export type AgentTaskKind = 'worker' | 'subagent'

export const TASK_STATUS_CONFIG: Record<AgentTaskStatus, { label: string; tone: string }> = {
  queued: { label: '排队中', tone: '#f59e0b' },
  running: { label: '执行中', tone: '#3b82f6' },
  completed: { label: '已完成', tone: '#10b981' },
  failed: { label: '失败', tone: '#ef4444' },
  cancelled: { label: '已取消', tone: '#94a3b8' },
}

export const TASK_KIND_CONFIG: Record<AgentTaskKind, { Icon: typeof Wrench; runningTone: string; defaultLabel: string }> = {
  worker: { Icon: Users, runningTone: '#3b82f6', defaultLabel: '执行任务' },
  subagent: { Icon: Sparkles, runningTone: '#a855f7', defaultLabel: '召唤专家' },
}

export function formatElapsed(startedAt?: string, nowMs?: number, completedAt?: string): string {
  if (!startedAt) return '00:00'
  const endMs = completedAt ? new Date(completedAt).getTime() : (nowMs ?? Date.now())
  const ms = endMs - new Date(startedAt).getTime()
  if (!Number.isFinite(ms) || ms < 0) return '00:00'
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`
}

export function formatTokens(thinkingChars: number): string {
  const tokens = Math.floor(thinkingChars / 2.5)
  if (tokens < 1000) return `${tokens}`
  return `${(tokens / 1000).toFixed(1)}k`
}

export interface AgentTaskRowProps {
  task: AgentTaskStat
  kind?: AgentTaskKind
  expanded: boolean
  onToggle: () => void
  nowMs: number
  iconOverride?: React.ReactNode
}

export function AgentTaskRow({
  task,
  kind = 'subagent',
  expanded,
  onToggle,
  nowMs,
  iconOverride,
}: AgentTaskRowProps) {
  const status = task.status
  const isTerminal = status === 'completed' || status === 'failed' || status === 'cancelled'
  const collapsed = isTerminal && !expanded
  const kindCfg = TASK_KIND_CONFIG[kind]
  const tone = isTerminal ? TASK_STATUS_CONFIG[status].tone : kindCfg.runningTone
  const label = TASK_STATUS_CONFIG[status].label
  const elapsed = formatElapsed(task.startedAt, nowMs, task.completedAt)
  const tokens = formatTokens(task.thinkingChars)
  const canExpand =
    (status === 'completed' && !!task.resultText) ||
    status === 'failed' ||
    status === 'cancelled' ||
    status === 'running' ||
    status === 'queued'
  const title = task.title || kindCfg.defaultLabel
  const KindIcon = kindCfg.Icon
  const Chevron = expanded ? ChevronDown : ChevronRight

  return (
    <div
      className={`dispatch-row dispatch-row-${status} dispatch-row-kind-${kind}${collapsed ? ' dispatch-row-collapsed' : ''}`}
      style={{ borderColor: `${tone}40` }}
    >
      <div className="dispatch-row-header">
        {iconOverride ? (
          <span className="dispatch-row-kind-icon" style={{ color: tone }} aria-hidden>
            {iconOverride}
          </span>
        ) : (
          <KindIcon size={13} className="dispatch-row-kind-icon" style={{ color: tone }} aria-hidden />
        )}
        <span
          className="dispatch-row-pulse"
          style={{ background: tone, animationPlayState: status === 'running' ? 'running' : 'paused' }}
        />
        <span className="dispatch-row-title" title={title}>{title}</span>
        <span className="dispatch-row-status" style={{ color: tone }}>{label}</span>
        {canExpand && (
          <button
            type="button"
            className="dispatch-row-chevron"
            onClick={onToggle}
            aria-label={expanded ? '折叠结果' : '展开结果'}
          >
            <Chevron size={14} aria-hidden />
          </button>
        )}
      </div>

      {!collapsed && (
      <div className="dispatch-row-stats">
        <span className="dispatch-row-stat">
          <Wrench size={11} aria-hidden />
          <span>{task.toolsCompleted}/{Math.max(task.toolsCalled, task.toolsCompleted)}</span>
        </span>
        <span className="dispatch-row-stat">
          <Zap size={11} aria-hidden />
          <span>{tokens}</span>
        </span>
        <span className="dispatch-row-stat dispatch-row-stat-elapsed">
          <Clock size={11} aria-hidden />
          <span>{elapsed}</span>
        </span>
      </div>
      )}

      {!collapsed && (
      <div className="dispatch-row-progress">
        <div
          className="dispatch-row-progress-bar"
          style={{
            width: '100%',
            background: isTerminal
              ? `linear-gradient(90deg, ${tone}b0, ${tone})`
              : `${tone}26`,
          }}
        />
      </div>
      )}

      {task.errorMessage && (status === 'failed' || status === 'cancelled') && !expanded && !collapsed && (
        <div className="dispatch-row-error">{firstLine(task.errorMessage)}</div>
      )}

      {expanded && canExpand && (
        <div className="dispatch-row-expand">
          {task.recentActivity && task.recentActivity.length > 0 && (
            <ActivityTimeline activity={task.recentActivity} tone={tone} />
          )}

          {(status === 'running' || status === 'queued')
            && (!task.recentActivity || task.recentActivity.length === 0)
            && task.thinkingChars === 0
            && task.toolsCalled === 0 && (
            <div className="dispatch-row-running-detail">
              <div className="dispatch-row-running-line">
                <span className="dispatch-row-running-dot" style={{ background: tone }} />
                <span>
                  {status === 'queued' ? '排队等待执行槽…' : '正在执行…'}
                </span>
              </div>
              <div className="dispatch-row-running-hint">
                还没拿到内部事件——可能 thinking/tool 还没流回主会话。
              </div>
            </div>
          )}

          {(status === 'running' || status === 'queued')
            && (!task.recentActivity || task.recentActivity.length === 0)
            && (task.thinkingChars > 0 || task.toolsCalled > 0) && (
            <div className="dispatch-row-running-detail">
              <div className="dispatch-row-running-line">
                <span className="dispatch-row-running-dot" style={{ background: tone }} />
                <span>
                  {task.toolsCalled > 0
                    ? <>已调用 <strong>{task.toolsCompleted}/{task.toolsCalled}</strong> 个工具</>
                    : <>模型正在生成正文 · <strong>{tokens}</strong></>}
                </span>
              </div>
            </div>
          )}

          {status === 'completed' && task.resultText && (
            <div
              className="dispatch-row-result"
              dangerouslySetInnerHTML={{ __html: renderChatMarkdown(task.resultText) }}
            />
          )}
          {(status === 'failed' || status === 'cancelled') && task.errorMessage && (
            <pre className="dispatch-row-error-detail">{task.errorMessage}</pre>
          )}
          {(status === 'failed' || status === 'cancelled') && task.resultText && (
            <div
              className="dispatch-row-result"
              dangerouslySetInnerHTML={{ __html: renderChatMarkdown(task.resultText) }}
            />
          )}
        </div>
      )}
    </div>
  )
}

function firstLine(text: string): string {
  const trimmed = (text || '').trim()
  const idx = trimmed.indexOf('\n')
  return idx === -1 ? trimmed : trimmed.slice(0, idx) + '…'
}

function ActivityTimeline({ activity, tone }: { activity: AgentTaskActivity[]; tone: string }) {
  return (
    <div className="dispatch-row-activity">
      {activity.map((item) => {
        if (item.type === 'thinking') {
          return (
            <div key={item.id} className="dispatch-row-activity-item dispatch-row-activity-thinking">
              <Brain size={11} style={{ color: tone, opacity: 0.75 }} aria-hidden />
              <span className="dispatch-row-activity-text">{item.text}</span>
            </div>
          )
        }
        if (item.type === 'tool_call') {
          return (
            <div key={item.id} className="dispatch-row-activity-item dispatch-row-activity-tool-call">
              <ArrowRight size={11} style={{ color: tone }} aria-hidden />
              <span className="dispatch-row-activity-tool-name">{item.toolName}</span>
              {item.text && <span className="dispatch-row-activity-text">{item.text}</span>}
            </div>
          )
        }
        return (
          <div key={item.id} className="dispatch-row-activity-item dispatch-row-activity-tool-result">
            <ArrowLeft size={11} style={{ color: tone, opacity: 0.7 }} aria-hidden />
            <span className="dispatch-row-activity-tool-name">{item.toolName}</span>
            {item.text && <span className="dispatch-row-activity-text">{item.text}</span>}
          </div>
        )
      })}
    </div>
  )
}

export default AgentTaskRow
