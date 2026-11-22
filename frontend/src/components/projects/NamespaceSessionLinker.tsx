import { useEffect, useMemo, useState } from 'react'
import { Link2, Unlink, MessageSquare } from 'lucide-react'
import { ProjectNamespace, Session, setSessionProject } from '../../api/chat'

interface NamespaceSessionLinkerProps {
  project: ProjectNamespace
  allProjects: ProjectNamespace[]
  sessions: Session[]
  agentId: string
  currentSessionId?: string
  onChanged: () => Promise<void> | void
  onError: (msg: string) => void
}

/**
 * 项目关联会话区（精简版，避免与顶部当前会话条重复）：
 * 1. 展示已归属到当前项目的全部会话芯片（支持一键解绑，当前会话高亮）；
 * 2. 提供紧凑的下拉选择器用于将其他历史会话关联到本项目。
 */
export default function NamespaceSessionLinker({
  project,
  allProjects,
  sessions,
  agentId,
  currentSessionId,
  onChanged,
  onError,
}: NamespaceSessionLinkerProps) {
  const [selectedOtherId, setSelectedOtherId] = useState('')
  const [busyId, setBusyId] = useState<string | null>(null)

  const projectNameMap = useMemo(() => {
    const map = new Map<string, string>()
    for (const p of allProjects) map.set(p.id, p.name)
    return map
  }, [allProjects])

  const boundSessions = useMemo(
    () => sessions.filter(s => s.project_namespace === project.id),
    [sessions, project.id],
  )

  const candidateSessions = useMemo(
    () => sessions.filter(s => s.project_namespace !== project.id),
    [sessions, project.id],
  )

  useEffect(() => {
    setSelectedOtherId(prev => {
      if (prev && candidateSessions.some(s => s.id === prev)) return prev
      return candidateSessions[0]?.id || ''
    })
  }, [candidateSessions])

  async function bindSession(sessionId: string, targetNs: string | null) {
    setBusyId(sessionId)
    try {
      await setSessionProject(sessionId, targetNs, agentId)
      await onChanged()
    } catch (e) {
      onError(`会话绑定操作失败: ${(e as Error).message || e}`)
    }
    setBusyId(null)
  }

  const isArchived = project.status === 'archived'

  return (
    <div className="ns-linker-bar">
      <div className="ns-bound-header">
        <span className="namespace-bind-label">
          <MessageSquare size={12} />
          <span>已关联会话（{boundSessions.length}）</span>
        </span>
        {candidateSessions.length > 0 && !isArchived && (
          <div className="ns-bind-picker">
            <select
              className="ns-bind-select"
              value={selectedOtherId}
              onChange={e => setSelectedOtherId(e.target.value)}
            >
              {candidateSessions.map(s => (
                <option key={s.id} value={s.id}>
                  {s.id === currentSessionId ? '★ ' : ''}
                  {s.title || '无标题'}
                  {s.project_namespace
                    ? ` · ${projectNameMap.get(s.project_namespace) || s.project_namespace}`
                    : ' · 未绑定'}
                </option>
              ))}
            </select>
            <button
              className="text-btn ns-btn-xs"
              disabled={!selectedOtherId || busyId === selectedOtherId}
              onClick={() => selectedOtherId && void bindSession(selectedOtherId, project.id)}
              title="将所选会话归入本项目"
            >
              <Link2 size={11} /> 关联
            </button>
          </div>
        )}
      </div>

      {boundSessions.length > 0 ? (
        <div className="ns-bound-chips">
          {boundSessions.map(s => (
            <span
              key={s.id}
              className={`ns-session-chip ${s.id === currentSessionId ? 'is-current' : ''}`}
              title={`${s.title || '无标题'} · ${s.messageCount} 条消息`}
            >
              {s.id === currentSessionId && <span className="ns-current-dot" title="当前会话" />}
              <span className="ns-session-chip-title">{s.title || '无标题'}</span>
              <span className="ns-session-chip-count">{s.messageCount}条</span>
              <button
                type="button"
                disabled={busyId === s.id}
                onClick={() => void bindSession(s.id, null)}
                title="解绑此会话"
              >
                <Unlink size={11} />
              </button>
            </span>
          ))}
        </div>
      ) : (
        <span className="ns-meta-empty">暂无显式关联到此项目的会话</span>
      )}
    </div>
  )
}
