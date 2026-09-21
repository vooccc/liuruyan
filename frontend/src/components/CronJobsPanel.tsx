import { useEffect, useRef, useState } from 'react'
import { Clock, RefreshCw, Trash2, X, Play, Pause, Sparkles, Check } from 'lucide-react'
import { API_BASE } from '../api/base'
import { startPolling } from '../lib/pollingLoop'
import { useConfirmDialog } from './ConfirmDialog'

/**
 * 定时任务面板（仅 list / 暂停-恢复 / 删除；创建仍走对话）
 *
 * 数据流：
 *   - 打开时 GET /cron/jobs?agent_id=X 全量加载
 *   - 订阅 /events/global SSE，cron_fired 时按 job_id 增量刷
 *     fire_count / last_fired_at / next_fire_at（不重新整体拉取）
 *
 * 作用域：per-agent（cron 跨 session 共享）。
 */

interface CronJob {
  job_id: string
  agent_id: string
  cron: string
  description: string
  prompt: string
  mode: 'recurring' | 'once'
  enabled: boolean
  fire_count: number
  last_fired_at: number | null
  next_fire_at: string | null
  created_at: number
}

interface CronJobsPanelProps {
  agentId: string
  onClose: () => void
}

function formatRelative(ts: number | null): string {
  if (!ts) return '从未触发'
  const now = Date.now() / 1000
  const diff = now - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return new Date(ts * 1000).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

function formatNextFire(iso: string | null): string {
  if (!iso) return '—'
  // ISO "2026-05-01T09:03" → "5/1 09:03"
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  if (sameDay) {
    return `今天 ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  }
  const tomorrow = new Date(now); tomorrow.setDate(tomorrow.getDate() + 1)
  if (d.toDateString() === tomorrow.toDateString()) {
    return `明天 ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  }
  return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
}

export default function CronJobsPanel({ agentId, onClose }: CronJobsPanelProps) {
  const [jobs, setJobs] = useState<CronJob[]>([])
  const [loading, setLoading] = useState(false)
  const [featureEnabled, setFeatureEnabled] = useState(true)
  // Tauri webview 不支持 window.confirm
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog()
  const [highlight, setHighlight] = useState<string | null>(null)  // job_id 触发后短暂高亮
  const seenEventIds = useRef<Set<string>>(new Set())

  const fetchJobs = async () => {
    if (!agentId) return
    try {
      setLoading(true)
      const res = await fetch(`${API_BASE}/cron/jobs?agent_id=${encodeURIComponent(agentId)}`)
      const data = await res.json()
      if (data.ok) {
        // 最新创建的排最上面（用户最常关心刚建的那条）
        const sorted = [...(data.jobs || [])].sort((a: CronJob, b: CronJob) =>
          (b.created_at || 0) - (a.created_at || 0)
        )
        setJobs(sorted)
        setFeatureEnabled(data.feature_enabled !== false)
      }
    } catch (e) {
      console.error('[CronJobsPanel] fetch failed', e)
    } finally {
      setLoading(false)
    }
  }

  const toggleEnabled = async (job: CronJob) => {
    try {
      const res = await fetch(`${API_BASE}/cron/jobs/${encodeURIComponent(job.job_id)}/enabled`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !job.enabled, agent_id: agentId }),
      })
      if (res.ok) {
        setJobs(prev => prev.map(j => j.job_id === job.job_id ? { ...j, enabled: !job.enabled } : j))
      }
    } catch (e) {
      console.error('[CronJobsPanel] toggle failed', e)
    }
  }

  const deleteJob = async (job: CronJob) => {
    const ok = await askConfirm(
      '删除定时任务',
      `确认删除『${job.description}』？此操作不可撤销。`,
      'danger',
      '删除',
    )
    if (!ok) return
    try {
      const res = await fetch(
        `${API_BASE}/cron/jobs/${encodeURIComponent(job.job_id)}?agent_id=${encodeURIComponent(agentId)}`,
        { method: 'DELETE' }
      )
      if (res.ok) {
        setJobs(prev => prev.filter(j => j.job_id !== job.job_id))
      }
    } catch (e) {
      console.error('[CronJobsPanel] delete failed', e)
    }
  }

  // 打开 / agentId 变化时立刻拉一次；之后 30s 兜底轮询（cron_jobs_changed 事件
  // 通常会先到，轮询只是网络断连/事件丢的兜底）。
  useEffect(() => {
    return startPolling(fetchJobs, 30000)
  }, [agentId])

  // SSE 增量：cron_fired 局部更新；cron_jobs_changed 触发整列表 refetch
  // ⚠️ SSE 规范：后端推的事件带 `event: cron_fired` 字段时，浏览器 EventSource
  // 只把它分发给 addEventListener(eventType, cb)，**不走 onmessage**。
  // 必须按事件名分别订阅（曾经踩坑：onmessage 永远收不到事件，蓝光不闪 + toast 不弹）
  useEffect(() => {
    console.log('[CronJobsPanel] SSE subscribe agentId=', agentId)
    const es = new EventSource(`${API_BASE}/events/global`)
    es.onopen = () => console.log('[CronJobsPanel] SSE opened')
    es.onerror = (e) => console.warn('[CronJobsPanel] SSE error', e)

    const handle = (e: MessageEvent) => {
      try {
        const event = JSON.parse(e.data)
        if (!event || !event.event_id) return
        console.log('[CronJobsPanel] SSE event', event.type, 'agent_id=', event.agent_id, 'panelAgent=', agentId)
        if (event.agent_id && event.agent_id !== agentId) {
          console.log('[CronJobsPanel] event filtered by agent_id mismatch')
          return
        }

        const idStr = String(event.event_id)
        if (seenEventIds.current.has(idStr)) return
        seenEventIds.current.add(idStr)
        if (seenEventIds.current.size > 100) {
          const arr = Array.from(seenEventIds.current)
          seenEventIds.current = new Set(arr.slice(arr.length / 2))
        }

        if (event.type === 'cron_jobs_changed') {
          fetchJobs()
          return
        }

        if (event.type === 'cron_fired') {
          const p = event.payload || {}
          const fired_job_id: string = p.job_id
          if (!fired_job_id) return
          console.log('[CronJobsPanel] cron_fired', fired_job_id, 'fire_count=', p.fire_count)
          setJobs(prev => prev.map(j => j.job_id === fired_job_id ? {
            ...j,
            fire_count: typeof p.fire_count === 'number' ? p.fire_count : j.fire_count + 1,
            last_fired_at: event.ts ?? j.last_fired_at,
            next_fire_at: p.next_fire_at ?? j.next_fire_at,
            enabled: p.mode === 'once' ? false : j.enabled,
          } : j))
          setHighlight(fired_job_id)
          setTimeout(() => setHighlight(curr => curr === fired_job_id ? null : curr), 3500)
        }
      } catch (err) { console.warn('[CronJobsPanel] event parse failed', err) }
    }

    es.addEventListener('cron_fired', handle)
    es.addEventListener('cron_jobs_changed', handle)
    es.onmessage = handle  // 兜底：默认 type=message 的事件也接（SSE 协议两条路径）

    return () => {
      console.log('[CronJobsPanel] SSE close')
      es.close()
    }
  }, [agentId])

  return (
    <div className="cron-panel">
      <div className="cron-panel-header">
        <div className="cron-panel-title">
          <Clock size={16} strokeWidth={1.5} />
          <span>定时任务</span>
          {jobs.length > 0 && <span className="cron-panel-count">{jobs.length}</span>}
        </div>
        <div className="cron-panel-actions">
          <button onClick={fetchJobs} disabled={loading} title="刷新" className="cron-btn-icon">
            <RefreshCw size={14} strokeWidth={1.5} className={loading ? 'spinning' : ''} />
          </button>
          <button onClick={onClose} title="关闭" className="cron-btn-icon cron-btn-close">
            <X size={14} strokeWidth={1.5} />
          </button>
        </div>
      </div>

      <div className="cron-panel-body">
        {!featureEnabled ? (
          <EmptyState
            title="功能未启用"
            hint="设置 FEATURE_CRON_TOOL=true 后重启后端。"
          />
        ) : jobs.length === 0 ? (
          <EmptyState
            title="还没有定时任务"
            hint={'跟当前角色说『每天早 9:03 提醒我喝水』即可建一条。\ncron 表达式让她翻译比手写顺手。'}
          />
        ) : (
          <div className="cron-list">
            {jobs.map(job => (
              <CronJobCard
                key={job.job_id}
                job={job}
                pulsing={highlight === job.job_id}
                onToggle={() => toggleEnabled(job)}
                onDelete={() => deleteJob(job)}
              />
            ))}
          </div>
        )}
      </div>

      <style>{`
        .cron-panel {
          position: fixed;
          top: 60px;
          bottom: 160px;
          right: 20px;
          width: 440px;
          max-width: calc(100vw - 40px);
          background: var(--bg-color);
          border: 1px solid var(--border-color);
          border-radius: 12px;
          box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18), 0 2px 8px rgba(0,0,0,0.08);
          z-index: 1000;
          display: flex;
          flex-direction: column;
          overflow: hidden;
          animation: cronPanelIn 240ms cubic-bezier(0.2, 0.9, 0.3, 1);
        }
        @keyframes cronPanelIn {
          from { opacity: 0; transform: translateX(8px) scale(0.98); }
          to   { opacity: 1; transform: translateX(0) scale(1); }
        }

        .cron-panel-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding: 14px 18px;
          border-bottom: 1px solid var(--border-light);
          flex-shrink: 0;
        }
        .cron-panel-title {
          display: flex;
          align-items: center;
          gap: 8px;
          font-size: 14px;
          font-weight: 500;
          color: var(--text-color);
          letter-spacing: 0.2px;
        }
        .cron-panel-count {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          min-width: 20px;
          height: 18px;
          padding: 0 6px;
          background: var(--bg-tertiary);
          color: var(--text-secondary);
          border-radius: 9px;
          font-size: 11px;
          font-weight: 500;
        }
        .cron-panel-actions { display: flex; gap: 6px; }
        .cron-btn-icon {
          display: flex; align-items: center; justify-content: center;
          width: 28px; height: 28px;
          background: transparent;
          border: 1px solid transparent;
          border-radius: 6px;
          color: var(--text-secondary);
          cursor: pointer;
          transition: all 140ms;
        }
        .cron-btn-icon:hover:not(:disabled) {
          background: var(--bg-secondary);
          color: var(--text-color);
        }
        .cron-btn-icon:disabled { opacity: 0.4; cursor: not-allowed; }
        .cron-btn-close:hover { background: rgba(248,81,73,0.08); color: #f85149; }

        .cron-panel-body {
          flex: 1;
          overflow-y: auto;
          padding: 14px;
        }
        .cron-list {
          display: flex;
          flex-direction: column;
          gap: 8px;
        }

        /* ====== 单个 Cron 卡片 ====== */
        .cron-card {
          position: relative;
          padding: 10px 12px 10px 14px;
          background: var(--bg-card);
          border: 1px solid var(--border-light);
          border-radius: 10px;
          transition: all 180ms;
          overflow: hidden;
        }
        .cron-card:hover {
          border-color: var(--border-color);
          transform: translateY(-1px);
          box-shadow: 0 4px 12px rgba(0,0,0,0.06);
        }
        /* 手动暂停 recurring：微弱降透（保字可读）；once 完成不降透（用对勾表达） */
        .cron-card.paused { opacity: 0.82; }
        .cron-card.completed {
          background: color-mix(in srgb, var(--accent-green) 4%, var(--bg-card));
          border-color: color-mix(in srgb, var(--accent-green) 18%, var(--border-light));
        }
        /* 触发瞬间：蓝色光晕 + 弹两次。!important 胜过 .completed 同时态。 */
        .cron-card.pulsing {
          animation: cronPulse 3.5s ease-out;
          border-color: var(--accent-blue) !important;
          background: color-mix(in srgb, var(--accent-blue) 10%, var(--bg-card)) !important;
        }
        @keyframes cronPulse {
          0%, 30%, 60%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent-blue) 0%, transparent); }
          12%, 42% { box-shadow: 0 0 0 10px color-mix(in srgb, var(--accent-blue) 28%, transparent); }
        }

        /* 触发徽章：右上角浮一个 ⚡ 刚刚触发，3.5s 后整体 unmount */
        .cron-fired-badge {
          position: absolute;
          top: -6px;
          right: 10px;
          z-index: 2;
          font-size: 10.5px;
          font-weight: 600;
          color: white;
          background: var(--accent-blue);
          padding: 3px 9px;
          border-radius: 999px;
          box-shadow: 0 2px 8px color-mix(in srgb, var(--accent-blue) 40%, transparent);
          animation: cronBadgeIn 320ms cubic-bezier(0.2, 1.4, 0.4, 1);
          letter-spacing: 0.5px;
          pointer-events: none;
        }
        @keyframes cronBadgeIn {
          from { opacity: 0; transform: translateY(4px) scale(0.7); }
          to   { opacity: 1; transform: translateY(0) scale(1); }
        }
        /* 左侧色条 = 状态锚点 */
        .cron-card::before {
          content: '';
          position: absolute;
          left: 0; top: 10px; bottom: 10px;
          width: 3px;
          border-radius: 0 2px 2px 0;
          transition: background 180ms;
          background: var(--text-muted);
        }
        .cron-card.mode-once::before { background: #a78bfa; }
        .cron-card.mode-recurring::before { background: var(--accent-blue); }
        .cron-card.paused::before { background: var(--text-muted); }
        .cron-card.completed::before { background: var(--accent-green); }

        .cron-card-row1 {
          display: flex;
          align-items: center;
          gap: 7px;
          margin-bottom: 6px;
        }
        .cron-card-desc {
          flex: 1;
          font-size: 14px;
          font-weight: 500;
          color: var(--text-color);
          line-height: 1.4;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          display: inline-flex;
          align-items: center;
          gap: 6px;
          min-width: 0;  /* flex item 才能被 ellipsis 正确截断 */
        }
        .cron-card-desc .check-icon {
          color: var(--accent-green);
          flex-shrink: 0;
        }
        /* 完成 / 暂停 → desc 文字 + 整个"下次 XXX"字段（含"下次"二字）加删除线 */
        .cron-card.completed .cron-card-desc > span,
        .cron-card.paused .cron-card-desc > span {
          text-decoration: line-through;
          text-decoration-thickness: 1px;
          text-decoration-color: color-mix(in srgb, currentColor 55%, transparent);
        }
        .cron-card.completed .cron-stats .next-fire,
        .cron-card.paused .cron-stats .next-fire {
          text-decoration: line-through;
          text-decoration-thickness: 1px;
          text-decoration-color: color-mix(in srgb, currentColor 55%, transparent);
        }

        /* 圆点 + 文字徽章 */
        .cron-tag {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          font-size: 11px;
          color: var(--text-secondary);
          padding: 2px 7px;
          border-radius: 999px;
          background: var(--bg-secondary);
          flex-shrink: 0;
        }
        .cron-tag .dot {
          width: 5px; height: 5px;
          border-radius: 50%;
          background: var(--text-muted);
        }
        .cron-tag.tag-once .dot { background: #a78bfa; }
        .cron-tag.tag-recurring .dot { background: var(--accent-blue); }
        .cron-tag.tag-paused {
          color: var(--text-muted);
        }
        .cron-tag.tag-paused .dot { background: var(--text-muted); }
        .cron-tag.tag-completed {
          color: var(--accent-green);
          background: color-mix(in srgb, var(--accent-green) 8%, var(--bg-secondary));
        }
        .cron-tag.tag-completed .dot { background: var(--accent-green); }

        .cron-card-actions { display: flex; gap: 4px; }
        .cron-card-btn {
          width: 26px; height: 26px;
          display: flex; align-items: center; justify-content: center;
          background: transparent;
          border: 1px solid var(--border-color);
          border-radius: 6px;
          cursor: pointer;
          transition: all 140ms;
          color: var(--text-secondary);
        }
        .cron-card-btn:hover:not(:disabled) {
          background: var(--bg-secondary);
          color: var(--text-color);
        }
        .cron-card-btn.toggle-on { color: var(--accent-green); }
        .cron-card-btn.delete:hover:not(:disabled) {
          background: rgba(248,81,73,0.08);
          color: #f85149;
          border-color: rgba(248,81,73,0.3);
        }
        .cron-card-btn:disabled { opacity: 0.35; cursor: not-allowed; }

        .cron-expr {
          display: inline-block;
          font-family: 'SFMono-Regular', Consolas, 'Courier New', monospace;
          font-size: 12px;
          color: var(--accent-blue);
          background: var(--code-bg);
          padding: 3px 8px;
          border-radius: 4px;
          letter-spacing: 0.5px;
          margin-bottom: 6px;
        }

        .cron-stats {
          display: flex;
          gap: 12px;
          flex-wrap: wrap;
          font-size: 11.5px;
          color: var(--text-muted);
          line-height: 1.5;
        }
        .cron-stats > span { display: inline-flex; align-items: center; gap: 4px; }
        .cron-stats b {
          color: var(--text-secondary);
          font-weight: 500;
        }
        .cron-stats .next-fire { color: var(--text-secondary); }
        .cron-stats .next-fire.completed { color: var(--accent-green); }
        .cron-stats .next-fire.paused { color: var(--text-muted); }

        /* ====== Empty state ====== */
        .cron-empty {
          padding: 48px 24px;
          text-align: center;
          color: var(--text-muted);
        }
        .cron-empty-icon {
          display: inline-flex;
          width: 44px; height: 44px;
          margin-bottom: 14px;
          align-items: center; justify-content: center;
          border-radius: 50%;
          background: var(--bg-secondary);
          color: var(--text-secondary);
        }
        .cron-empty-title {
          font-size: 14px;
          color: var(--text-color);
          margin-bottom: 6px;
          font-weight: 500;
        }
        .cron-empty-hint {
          font-size: 12px;
          line-height: 1.7;
          white-space: pre-line;
        }

        .spinning { animation: spin 1s linear infinite; }
        @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
      `}</style>
      {confirmDialog}
    </div>
  )
}

// ============================================================
// 子组件
// ============================================================

function EmptyState({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="cron-empty">
      <div className="cron-empty-icon">
        <Sparkles size={20} strokeWidth={1.4} />
      </div>
      <div className="cron-empty-title">{title}</div>
      <div className="cron-empty-hint">{hint}</div>
    </div>
  )
}

function CronJobCard({
  job,
  pulsing,
  onToggle,
  onDelete,
}: {
  job: CronJob
  pulsing: boolean
  onToggle: () => void
  onDelete: () => void
}) {
  const isOnce = job.mode === 'once'
  const isOnceCompleted = isOnce && !job.enabled && job.fire_count > 0
  const isPaused = !job.enabled && !isOnceCompleted  // 手动暂停（recurring）
  const cardClass = [
    'cron-card',
    `mode-${job.mode}`,
    isOnceCompleted && 'completed',
    isPaused && 'paused',
    pulsing && 'pulsing',
  ].filter(Boolean).join(' ')

  // 状态徽章 + "下次"显示文案分三态
  let tag: { cls: string; text: string }
  let nextText: string
  let nextCls: string
  if (isOnceCompleted) {
    tag = { cls: 'tag-completed', text: '已完成' }
    nextText = '一次性已完成'
    nextCls = 'completed'
  } else if (isPaused) {
    tag = { cls: 'tag-paused', text: '已暂停' }
    nextText = '已暂停'
    nextCls = 'paused'
  } else {
    tag = { cls: `tag-${job.mode}`, text: isOnce ? '一次性' : '周期' }
    nextText = formatNextFire(job.next_fire_at)
    nextCls = ''
  }

  return (
    <div className={cardClass}>
      {pulsing && <div className="cron-fired-badge">⚡ 刚刚触发</div>}
      <div className="cron-card-row1">
        <div className="cron-card-desc" title={job.description}>
          {isOnceCompleted && <Check size={13} strokeWidth={2.4} className="check-icon" />}
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{job.description}</span>
        </div>
        <span className={`cron-tag ${tag.cls}`}>
          <span className="dot" />{tag.text}
        </span>
        <div className="cron-card-actions">
          <button
            onClick={onToggle}
            title={job.enabled ? '暂停' : '恢复'}
            disabled={isOnceCompleted}
            className={`cron-card-btn ${job.enabled ? 'toggle-on' : ''}`}
          >
            {job.enabled ? <Pause size={12} /> : <Play size={12} />}
          </button>
          <button onClick={onDelete} title="删除" className="cron-card-btn delete">
            <Trash2 size={12} />
          </button>
        </div>
      </div>

      <div className="cron-expr">{job.cron}</div>

      <div className="cron-stats">
        <span>触发 <b>{job.fire_count}</b> 次</span>
        <span>上次 <b>{formatRelative(job.last_fired_at)}</b></span>
        <span className={`next-fire ${nextCls}`}>下次 <b>{nextText}</b></span>
      </div>
    </div>
  )
}
