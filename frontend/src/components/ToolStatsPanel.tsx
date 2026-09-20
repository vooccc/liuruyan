/**
 * 运行观测面板（ObservabilityPanel）
 *
 * 2026-05-03 重新设计：从"功能堆叠"的旧 ToolStatsPanel 升级为统一的
 * 运行观测中心，收纳三大 tab：
 *   - 数据：per-tool 调用 / 成功率 / 耗时 / JSON 修复
 *   - 洞察：健康分排行 + 问题清单（自动生成的可执行建议）
 *   - 缓存：Prompt Cache 命中率 + 估算节省 + per agent×model 切片
 *
 * 视觉语言遵循柳如烟整体气质：
 *   - 主色 --primary-color（暖棕金 #c4a77d）+ color-mix 柔色层
 *   - 大圆角（20px 外壳 / 14px 卡片）+ 柔和大阴影
 *   - 右滑入 300ms cubic-bezier(0.2, 0.9, 0.2, 1)（跟 collab card 曲线同源）
 *   - 遮罩 + Esc + 点击遮罩三路关闭
 *   - 宽度克制到 720px（原 900px 占屏过猛）
 *
 * 文件名沿用 ToolStatsPanel 向后兼容（App.tsx 已 import 这个名字），
 * default export 也保持 ToolStatsPanel —— 不破坏现有触发点。
 */

import { useState, useEffect, useMemo, useCallback } from 'react'
import {
  Activity, BarChart3, Lightbulb, Gauge,
  RefreshCw, Trash2, X, ArrowUpDown,
  AlertCircle, AlertTriangle, Info,
  TrendingUp, Zap, Coins, Database,
} from 'lucide-react'
import { API_BASE } from '../api/base'
import { getAgentsLocal } from '../api/chat'
import {
  fetchPromptCacheStats,
  type PromptCacheSummary,
  type PromptCacheSummarySlot,
} from '../api/cacheStats'
// Tauri webview 下原生 confirm()/alert() 不弹（"dialog.confirm not allowed. Command not found"），
// 用项目内的自定义对话框替代。
import { useConfirmDialog, useAlertDialog } from './ConfirmDialog'

/* ========================================================================
   Types
   ======================================================================== */

interface ToolStats {
  total_calls: number
  success_rate: string
  failure_rate: string
  json_repair_rate: string
  json_repair_layers: Record<number, number>
  retry_distribution: Record<string, string>
  error_types: Record<string, number>
  avg_execution_time_ms: number | null
}

type StatsData = Record<string, ToolStats>

type SortField = 'name' | 'calls' | 'success' | 'time'
type SortOrder = 'asc' | 'desc'

interface HealthRow {
  tool: string
  total_calls: number
  success_rate: number
  failure_count: number
  health_score: number
  avg_time_ms: number | null
  p95_time_ms: number | null
}

interface InsightIssue {
  severity: 'critical' | 'warning' | 'info'
  category: string
  tool: string
  title: string
  detail: string
  suggestion: string
  metric: Record<string, unknown>
}

interface InsightsData {
  summary: {
    total_calls: number
    tools_observed: number
    overall_success_rate: number
    issues_count: number
    issues_by_severity: Record<string, number>
    issues_by_category: Record<string, number>
    message?: string
  }
  health: HealthRow[]
  issues: InsightIssue[]
}

type TabKey = 'stats' | 'insights' | 'cache'

const CATEGORY_LABELS: Record<string, string> = {
  low_success: '成功率低',
  json_repair: 'JSON 修复频繁',
  slow: '执行慢',
  streak_fail: '连续失败',
  regression: '近期回归',
  cold: '冷门工具',
}

interface ToolStatsPanelProps {
  onClose: () => void
}

/* ========================================================================
   Root component
   ======================================================================== */

export default function ToolStatsPanel({ onClose }: ToolStatsPanelProps) {
  const [tab, setTab] = useState<TabKey>('stats')

  // Esc 关闭（不吃 input focus 内的 Esc——简化起见，这里整个面板内 Esc 都关）
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  return (
    <>
      <div className="obs-backdrop" onClick={onClose} aria-hidden="true" />
      <aside className="obs-panel" role="dialog" aria-label="运行观测">
        <header className="obs-header">
          <div className="obs-header-lead">
            <span className="obs-header-icon"><Activity size={20} strokeWidth={1.8} /></span>
            <div className="obs-header-text">
              <h2>运行观测</h2>
              <p>工具调用 · 洞察 · Prompt Cache 命中率</p>
            </div>
          </div>
          <button type="button" className="obs-close" onClick={onClose} title="关闭 (Esc)">
            <X size={18} strokeWidth={1.8} />
          </button>
        </header>

        <nav className="obs-tabs" role="tablist">
          <TabBtn label="数据" icon={<BarChart3 size={14} strokeWidth={2} />} active={tab === 'stats'} onClick={() => setTab('stats')} />
          <TabBtn label="洞察" icon={<Lightbulb size={14} strokeWidth={2} />} active={tab === 'insights'} onClick={() => setTab('insights')} />
          <TabBtn label="缓存" icon={<Gauge size={14} strokeWidth={2} />} active={tab === 'cache'} onClick={() => setTab('cache')} />
        </nav>

        <div className="obs-body">
          {tab === 'stats' && <StatsTab />}
          {tab === 'insights' && <InsightsTab />}
          {tab === 'cache' && <CacheTab />}
        </div>
      </aside>

      <style>{PANEL_STYLES}</style>
    </>
  )
}

function TabBtn({
  label, icon, active, onClick, badge,
}: { label: string; icon: React.ReactNode; active: boolean; onClick: () => void; badge?: number }) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      className={`obs-tab ${active ? 'active' : ''}`}
      onClick={onClick}
    >
      <span className="obs-tab-icon">{icon}</span>
      <span>{label}</span>
      {badge !== undefined && badge > 0 && <span className="obs-tab-badge">{badge}</span>}
    </button>
  )
}

/* ========================================================================
   Tab 1：数据（per-tool stats）
   ======================================================================== */

function StatsTab() {
  const [stats, setStats] = useState<StatsData | null>(null)
  // 当前 registry 实际注册的工具名集合。stats 里出现但不在此集合 = 已废弃/改名的幽灵工具。
  // 拿不到时（请求失败）退化为 null —— 全部按"已注册"处理，不打标也不挡删除。
  const [registeredNames, setRegisteredNames] = useState<Set<string> | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sortField, setSortField] = useState<SortField>('calls')
  const [sortOrder, setSortOrder] = useState<SortOrder>('desc')
  const [deletingTool, setDeletingTool] = useState<string | null>(null)
  const { confirm: showConfirm, dialog: confirmDialog } = useConfirmDialog()
  const { alert: showAlert, dialog: alertDialog } = useAlertDialog()

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      // 并行拉 stats 和当前注册工具名 — 其中一个失败不阻断另一个
      const [statsRes, namesRes] = await Promise.allSettled([
        fetch(`${API_BASE}/tools/stats`).then(r => r.ok ? r.json() : Promise.reject(new Error('获取统计失败'))),
        fetch(`${API_BASE}/tools/registered_names`).then(r => r.ok ? r.json() : Promise.reject(new Error('获取注册工具名失败'))),
      ])

      if (statsRes.status === 'fulfilled') {
        const data = statsRes.value
        if (data.message) { setStats(null); setError(data.message) }
        else setStats(data)
      } else {
        throw statsRes.reason
      }

      if (namesRes.status === 'fulfilled' && Array.isArray(namesRes.value?.names)) {
        setRegisteredNames(new Set<string>(namesRes.value.names))
      } else {
        setRegisteredNames(null)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '未知错误')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const deleteOne = async (toolName: string, isOrphan: boolean) => {
    const calls = stats?.[toolName]?.total_calls ?? 0
    const title = isOrphan ? `删除未注册工具「${toolName}」的历史` : `删除「${toolName}」的历史`
    const message = isOrphan
      ? `「${toolName}」不在当前注册名单里（可能是改名/废弃/MCP 掉线/模型拼错名字）。\n删掉它的 ${calls} 条历史记录？`
      : `确定要删除「${toolName}」的 ${calls} 条历史记录吗？此操作不可撤销。`
    const ok = await showConfirm(title, message, 'danger', '删除')
    if (!ok) return
    setDeletingTool(toolName)
    try {
      const r = await fetch(`${API_BASE}/tools/stats/${encodeURIComponent(toolName)}`, { method: 'DELETE' })
      if (!r.ok) throw new Error('删除失败')
      // 局部更新 stats，避免整个面板闪烁
      setStats(prev => {
        if (!prev) return prev
        const next = { ...prev }
        delete next[toolName]
        return next
      })
    } catch (e) {
      void showAlert(e instanceof Error ? e.message : '删除失败')
    } finally {
      setDeletingTool(null)
    }
  }

  const handleSort = (f: SortField) => {
    if (sortField === f) setSortOrder(o => o === 'asc' ? 'desc' : 'asc')
    else { setSortField(f); setSortOrder('desc') }
  }

  const parseRate = (r: string) => parseFloat(r.replace('%', ''))
  const sortedTools = useMemo(() => {
    if (!stats) return []
    return Object.entries(stats).sort(([nA, a], [nB, b]) => {
      let cv = 0
      switch (sortField) {
        case 'name': cv = nA.localeCompare(nB); break
        case 'calls': cv = a.total_calls - b.total_calls; break
        case 'success': cv = parseRate(a.success_rate) - parseRate(b.success_rate); break
        case 'time':
          cv = (a.avg_execution_time_ms ?? 0) - (b.avg_execution_time_ms ?? 0); break
      }
      return sortOrder === 'asc' ? cv : -cv
    })
  }, [stats, sortField, sortOrder])

  const clearStats = async () => {
    const ok = await showConfirm('清空全部工具统计', '确定要清空所有工具统计数据吗？此操作不可撤销。', 'danger', '清空')
    if (!ok) return
    try {
      const r = await fetch(`${API_BASE}/tools/stats`, { method: 'DELETE' })
      if (!r.ok) throw new Error('清空失败')
      setStats(null)
    } catch (e) {
      void showAlert(e instanceof Error ? e.message : '清空失败')
    }
  }

  return (
    <div className="obs-tab-pane">
      {/* Tauri webview 下原生 confirm/alert 不可用，挂自定义 dialog 节点 */}
      {confirmDialog}
      {alertDialog}
      <TabActionBar
        onRefresh={load}
        loading={loading}
        extra={
          <button type="button" className="obs-action-btn danger" onClick={clearStats} title="清空历史">
            <Trash2 size={13} strokeWidth={2} /> 清空
          </button>
        }
      />

      {error && <ErrorBanner text={error} />}

      {!error && !stats && !loading && <EmptyState icon={<BarChart3 size={28} />} text="还没有工具调用记录" hint="发起几次对话让 agent 用工具后再回来看" />}

      {stats && sortedTools.length > 0 && (
        <div className="obs-table-wrap">
          <table className="obs-table">
            <thead>
              <tr>
                <ThSort field="name" label="工具名称" current={sortField} onSort={handleSort} />
                <ThSort field="calls" label="调用" current={sortField} onSort={handleSort} align="right" />
                <ThSort field="success" label="成功率" current={sortField} onSort={handleSort} align="right" />
                <ThSort field="time" label="耗时" current={sortField} onSort={handleSort} align="right" />
                <th className="obs-th right">修复率</th>
                <th className="obs-th right">错误</th>
                <th className="obs-th right" style={{ width: 48 }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {sortedTools.map(([name, s]) => {
                const sr = parseRate(s.success_rate)
                const hasErr = Object.keys(s.error_types).length > 0
                const hasRep = Object.keys(s.json_repair_layers).length > 0
                // registeredNames 拿不到 (null) 时不打标 —— 不要在 fetch 失败的情况下误标所有工具为未注册
                const isOrphan = registeredNames !== null && !registeredNames.has(name)
                const isDeleting = deletingTool === name
                return (
                  <tr key={name} className={isOrphan ? 'obs-tr-orphan' : ''}>
                    <td className="obs-td name">
                      <span>{name}</span>
                      {isOrphan && (
                        <span className="obs-orphan-badge" title="此工具不在当前 registry 注册名单里。可能原因：改名/废弃/MCP 掉线/模型拼错。建议删掉历史记录避免占位。">
                          未注册
                        </span>
                      )}
                    </td>
                    <td className="obs-td num">{s.total_calls.toLocaleString()}</td>
                    <td className="obs-td num">
                      <RatePill rate={sr} text={s.success_rate} />
                    </td>
                    <td className="obs-td num">
                      {s.avg_execution_time_ms !== null ? formatMs(s.avg_execution_time_ms) : '—'}
                    </td>
                    <td className="obs-td num">
                      {hasRep ? (
                        <span className="obs-mini warn" title={Object.entries(s.json_repair_layers).map(([l, c]) => `层${l}: ${c}次`).join('\n')}>
                          {s.json_repair_rate}
                        </span>
                      ) : <span className="obs-muted">—</span>}
                    </td>
                    <td className="obs-td num">
                      {hasErr ? (
                        <span className="obs-mini bad" title={Object.entries(s.error_types).map(([e, c]) => `${e}: ${c}次`).join('\n')}>
                          {Object.values(s.error_types).reduce((a, b) => a + b, 0)}
                        </span>
                      ) : <span className="obs-muted">—</span>}
                    </td>
                    <td className="obs-td num">
                      <button
                        type="button"
                        className="obs-row-del"
                        title={isOrphan ? '删除此条历史（推荐——已不在 registry）' : '删除此条历史'}
                        disabled={isDeleting}
                        onClick={() => deleteOne(name, isOrphan)}
                      >
                        {isDeleting
                          ? <RefreshCw size={13} strokeWidth={2} className="obs-spin" />
                          : <Trash2 size={13} strokeWidth={2} />}
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/* ========================================================================
   Tab 2：洞察（health + issues）
   ======================================================================== */

function InsightsTab() {
  const [data, setData] = useState<InsightsData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sevFilter, setSevFilter] = useState<Record<string, boolean>>({
    critical: true, warning: true, info: true,
  })

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await fetch(`${API_BASE}/tools/stats/insights`)
      if (!r.ok) throw new Error('获取洞察失败')
      const d: InsightsData = await r.json()
      setData(d)
      if (d.summary?.message) setError(d.summary.message)
    } catch (e) {
      setError(e instanceof Error ? e.message : '未知错误')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const filteredIssues = data?.issues.filter(i => sevFilter[i.severity]) ?? []

  return (
    <div className="obs-tab-pane">
      <TabActionBar onRefresh={load} loading={loading} />
      {error && <ErrorBanner text={error} />}

      {!data && !loading && !error && <EmptyState icon={<Lightbulb size={28} />} text="洞察数据加载中" hint="" />}

      {data && (
        <>
          <div className="obs-summary-row">
            <SummaryPill icon={<Activity size={14} />} label="总调用" value={data.summary.total_calls.toLocaleString()} />
            <SummaryPill
              icon={<TrendingUp size={14} />}
              label="整体成功率"
              value={`${(data.summary.overall_success_rate * 100).toFixed(1)}%`}
              tone={data.summary.overall_success_rate >= 0.95 ? 'good' : data.summary.overall_success_rate >= 0.8 ? 'warn' : 'bad'}
            />
            <SummaryPill
              icon={<AlertCircle size={14} />}
              label="问题"
              value={String(data.summary.issues_count)}
              extra={
                <>
                  {data.summary.issues_by_severity?.critical > 0 && <span className="obs-sev-chip critical">{data.summary.issues_by_severity.critical} critical</span>}
                  {data.summary.issues_by_severity?.warning > 0 && <span className="obs-sev-chip warning">{data.summary.issues_by_severity.warning} warning</span>}
                  {data.summary.issues_by_severity?.info > 0 && <span className="obs-sev-chip info">{data.summary.issues_by_severity.info} info</span>}
                </>
              }
            />
          </div>

          <h3 className="obs-section-title">健康分排行（最差在前）</h3>
          <div className="obs-health-list">
            {data.health.slice(0, 12).map((h) => {
              const cls = h.health_score < 50 ? 'bad' : h.health_score < 75 ? 'warn' : 'good'
              return (
                <div key={h.tool} className="obs-health-row">
                  <span className="obs-health-tool">{h.tool}</span>
                  <span className="obs-health-bar-wrap">
                    <span className={`obs-health-bar ${cls}`} style={{ width: `${Math.max(2, h.health_score)}%` }} />
                  </span>
                  <span className={`obs-health-score ${cls}`}>{h.health_score.toFixed(1)}</span>
                  <span className="obs-health-meta">
                    {h.total_calls} 次 · {(h.success_rate * 100).toFixed(0)}% 成功
                    {h.p95_time_ms !== null && ` · P95 ${formatMs(h.p95_time_ms)}`}
                  </span>
                </div>
              )
            })}
          </div>

          <h3 className="obs-section-title obs-section-title-with-filter">
            问题清单
            <span className="obs-sev-filter">
              {(['critical', 'warning', 'info'] as const).map((s) => (
                <label key={s} className={`obs-sev-toggle ${sevFilter[s] ? `active ${s}` : ''}`}>
                  <input
                    type="checkbox"
                    checked={sevFilter[s]}
                    onChange={(e) => setSevFilter(p => ({ ...p, [s]: e.target.checked }))}
                  />
                  {s}
                </label>
              ))}
            </span>
          </h3>
          <div className="obs-issues-list">
            {filteredIssues.length === 0 ? (
              <div className="obs-empty-inline">✓ 当前过滤条件下没有问题</div>
            ) : (
              filteredIssues.map((issue, idx) => {
                const Icon = issue.severity === 'critical' ? AlertCircle : issue.severity === 'warning' ? AlertTriangle : Info
                return (
                  <div key={idx} className={`obs-issue-card ${issue.severity}`}>
                    <div className="obs-issue-head">
                      <Icon size={14} />
                      <span className="obs-issue-title">{issue.title}</span>
                      <span className="obs-issue-cat">{CATEGORY_LABELS[issue.category] || issue.category}</span>
                    </div>
                    <div className="obs-issue-detail">{issue.detail}</div>
                    <div className="obs-issue-suggestion">
                      <Lightbulb size={12} /> {issue.suggestion}
                    </div>
                  </div>
                )
              })
            )}
          </div>
        </>
      )}
    </div>
  )
}

/* ========================================================================
   Tab 3：缓存命中率
   ======================================================================== */

function CacheTab() {
  const [summary, setSummary] = useState<PromptCacheSummary | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // agent_id → 角色名（从active localStorage 同步读，不走后端）
  const agentNameMap = useMemo(() => {
    const m: Record<string, string> = {}
    try {
      for (const a of getAgentsLocal()) m[a.id] = a.name
    } catch { /* localStorage 不可用时 fallback 到 id */ }
    return m
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const resp = await fetchPromptCacheStats(1000, false)
      setSummary(resp.summary)
    } catch (e) {
      setError(e instanceof Error ? e.message : '未知错误')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const hitRatioTone = summary && summary.hit_ratio >= 0.7 ? 'good'
    : summary && summary.hit_ratio >= 0.5 ? 'warn'
    : summary && summary.call_count > 0 ? 'bad'
    : 'neutral'

  return (
    <div className="obs-tab-pane">
      <TabActionBar onRefresh={load} loading={loading} />
      {error && <ErrorBanner text={error} />}

      {!summary && !loading && !error && <EmptyState icon={<Gauge size={28} />} text="还没有缓存统计数据" hint="发起几次对话后回来看命中情况" />}

      {summary && summary.call_count === 0 && (
        <EmptyState icon={<Gauge size={28} />} text="还没有调用记录" hint={`最近 1000 次采样为空。开始对话后缓存统计会自动积累到 prompt_cache_stats.jsonl`} />
      )}

      {summary && summary.call_count > 0 && (
        <>
          {/* 主命中率大卡 */}
          <div className={`obs-hero-card tone-${hitRatioTone}`}>
            <div className="obs-hero-left">
              <div className="obs-hero-label">整体命中率</div>
              <div className="obs-hero-value">
                {(summary.hit_ratio * 100).toFixed(1)}
                <span className="obs-hero-unit">%</span>
              </div>
              <div className="obs-hero-sub">
                {hitRatioTone === 'good' && '✅ 健康——前缀稳定，缓存工作良好'}
                {hitRatioTone === 'warn' && '⚠️ 偏低——检查最近有没有改 system prompt / tools'}
                {hitRatioTone === 'bad' && '🔴 严重偏低——查纪律文档 § 三自检清单'}
              </div>
            </div>
            <div className="obs-hero-right">
              <div className="obs-hero-mini">
                <span className="obs-hero-mini-label">采样调用</span>
                <span className="obs-hero-mini-value">{summary.call_count.toLocaleString()}</span>
              </div>
              <div className="obs-hero-mini">
                <span className="obs-hero-mini-label">估算节省</span>
                <span className="obs-hero-mini-value">
                  ~{formatTokens(summary.estimated_savings_tokens)}
                </span>
              </div>
            </div>
          </div>

          {/* 三个次级指标 */}
          <div className="obs-summary-row">
            <SummaryPill icon={<Database size={14} />} label="缓存读命中" value={formatTokens(summary.total_cache_read)} hint="tokens" />
            <SummaryPill icon={<Zap size={14} />} label="缓存写入" value={formatTokens(summary.total_cache_create)} hint="tokens (首次付费写)" />
            <SummaryPill icon={<Coins size={14} />} label="未缓存输入" value={formatTokens(summary.total_input)} hint="tokens" />
          </div>

          {/* 按 agent × model 切片 */}
          {Object.keys(summary.by_agent_model).length > 0 && (
            <>
              <h3 className="obs-section-title">按 Agent × Model 切片（按命中量排序）</h3>
              <div className="obs-table-wrap">
                <table className="obs-table">
                  <thead>
                    <tr>
                      <th className="obs-th">Agent</th>
                      <th className="obs-th">Model</th>
                      <th className="obs-th right">次数</th>
                      <th className="obs-th right">命中率</th>
                      <th className="obs-th right">Cache Read</th>
                      <th className="obs-th right">估算节省</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(summary.by_agent_model)
                      .sort(([, a], [, b]) => b.total_cache_read - a.total_cache_read)
                      .map(([key, slot]) => <SliceRow key={key} agentModel={key} slot={slot} agentNameMap={agentNameMap} />)}
                  </tbody>
                </table>
              </div>
            </>
          )}

          <div className="obs-footer-hint">
            数据源：后端 <code>prompt_cache_stats.jsonl</code> · 最近 1000 次 LLM 调用 ·
            Anthropic 口径估算（<code>cache_read × 0.9</code>）
          </div>
        </>
      )}
    </div>
  )
}

function SliceRow({ agentModel, slot, agentNameMap }: {
  agentModel: string
  slot: PromptCacheSummarySlot
  agentNameMap: Record<string, string>
}) {
  const [agentId, model] = agentModel.split('|')
  const tone = slot.hit_ratio >= 0.7 ? 'good' : slot.hit_ratio >= 0.5 ? 'warn' : 'bad'
  // 优先显示角色名；查不到则回退到 agent_id（例如已删除的角色 / 跨实例记录）
  const agentDisplay = agentNameMap[agentId] || agentId
  return (
    <tr>
      <td className="obs-td name" title={agentId !== agentDisplay ? `agent_id: ${agentId}` : undefined}>
        {agentDisplay || <span className="obs-muted">(未知)</span>}
      </td>
      <td className="obs-td mono">{model || <span className="obs-muted">(未知)</span>}</td>
      <td className="obs-td num">{slot.call_count}</td>
      <td className="obs-td num">
        <span className={`obs-mini ${tone}`}>{(slot.hit_ratio * 100).toFixed(1)}%</span>
      </td>
      <td className="obs-td num">{formatTokens(slot.total_cache_read)}</td>
      <td className="obs-td num save">~{formatTokens(slot.estimated_savings_tokens)}</td>
    </tr>
  )
}

/* ========================================================================
   Shared small components
   ======================================================================== */

function TabActionBar({
  onRefresh, loading, extra,
}: { onRefresh: () => void; loading: boolean; extra?: React.ReactNode }) {
  return (
    <div className="obs-action-bar">
      <button type="button" className="obs-action-btn" onClick={onRefresh} disabled={loading} title="刷新">
        <RefreshCw size={13} strokeWidth={2} className={loading ? 'obs-spin' : ''} />
        刷新
      </button>
      {extra}
    </div>
  )
}

function ThSort({
  field, label, current, onSort, align,
}: { field: SortField; label: string; current: SortField; onSort: (f: SortField) => void; align?: 'right' }) {
  return (
    <th className={`obs-th sortable ${align === 'right' ? 'right' : ''}`} onClick={() => onSort(field)}>
      <span className="obs-th-content">
        {label}
        {current === field && <ArrowUpDown size={12} />}
      </span>
    </th>
  )
}

function RatePill({ rate, text }: { rate: number; text: string }) {
  const tone = rate >= 95 ? 'good' : rate >= 80 ? 'warn' : 'bad'
  return <span className={`obs-mini ${tone}`}>{text}</span>
}

function SummaryPill({
  icon, label, value, tone, hint, extra,
}: {
  icon: React.ReactNode; label: string; value: string
  tone?: 'good' | 'warn' | 'bad' | 'neutral'; hint?: string; extra?: React.ReactNode
}) {
  return (
    <div className={`obs-summary-pill tone-${tone ?? 'neutral'}`}>
      <span className="obs-summary-pill-icon">{icon}</span>
      <div className="obs-summary-pill-body">
        <span className="obs-summary-pill-label">{label}</span>
        <span className="obs-summary-pill-value">{value}</span>
        {hint && <span className="obs-summary-pill-hint">{hint}</span>}
        {extra && <span className="obs-summary-pill-extra">{extra}</span>}
      </div>
    </div>
  )
}

function ErrorBanner({ text }: { text: string }) {
  return (
    <div className="obs-error-banner">
      <AlertCircle size={15} /> {text}
    </div>
  )
}

function EmptyState({ icon, text, hint }: { icon: React.ReactNode; text: string; hint: string }) {
  return (
    <div className="obs-empty">
      <div className="obs-empty-icon">{icon}</div>
      <div className="obs-empty-text">{text}</div>
      {hint && <div className="obs-empty-hint">{hint}</div>}
    </div>
  )
}

/* ========================================================================
   Helpers
   ======================================================================== */

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms.toFixed(1)} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

function formatTokens(n: number): string {
  if (!isFinite(n)) return '—'
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}K`
  return `${(n / 1_000_000).toFixed(2)}M`
}

/* ========================================================================
   Styles
   ======================================================================== */

const PANEL_STYLES = `
  /* ---------- 遮罩 & 滑入面板 ---------- */
  .obs-backdrop {
    /* 不盖侧边栏：从 sidebar 右侧开始，跳生令侧边栏可点，
       静态体验跳似 "在主界面供作叠加层" 而非全屏遮罩弹窗。 */
    position: fixed;
    top: 0;
    bottom: 0;
    left: 280px;
    right: 0;
    background: color-mix(in srgb, var(--bg-color) 55%, transparent);
    backdrop-filter: blur(4px);
    z-index: 999;
    animation: obs-fade-in 280ms ease-out;
  }

  @media (max-width: 768px) {
    .obs-backdrop {
      left: 0;
    }
  }

  .obs-panel {
    /* 左边对齐主界面侧边栏右侧（sidebar width = 280px）；
       右边到屏边，中间区域全面接管，面板够长不产生横向压迫感。
       小屏防守：max-width: 768px 带全区域隐藏 sidebar，这里应该跨去；
       不限制 width，flex 自由画。 */
    position: fixed;
    top: 0;
    bottom: 0;
    left: 280px;
    right: 0;
    background: var(--bg-color);
    border-left: 1px solid color-mix(in srgb, var(--border-color) 70%, transparent);
    box-shadow: -24px 0 60px -20px color-mix(in srgb, black 35%, transparent);
    border-top-left-radius: 20px;
    border-bottom-left-radius: 20px;
    z-index: 1000;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    animation: obs-slide-in 320ms cubic-bezier(0.2, 0.9, 0.2, 1);
  }

  /* 小屏（sidebar 隐藏为 fixed overlay）面板贴全屏 */
  @media (max-width: 768px) {
    .obs-panel {
      left: 0;
      border-top-left-radius: 0;
      border-bottom-left-radius: 0;
    }
  }

  @keyframes obs-fade-in {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  @keyframes obs-slide-in {
    from { transform: translateX(16px); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
  }

  /* ---------- Header ---------- */
  .obs-header {
    flex-shrink: 0;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    padding: 22px 26px 18px;
    background:
      radial-gradient(ellipse 500px 140px at 0% 0%,
        color-mix(in srgb, var(--primary-color) 10%, transparent) 0%, transparent 70%),
      color-mix(in srgb, var(--bg-card) 40%, var(--bg-color));
    border-bottom: 1px solid color-mix(in srgb, var(--border-light) 70%, transparent);
  }

  .obs-header-lead {
    display: flex;
    gap: 14px;
    align-items: flex-start;
  }

  .obs-header-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 42px;
    height: 42px;
    border-radius: 12px;
    background:
      linear-gradient(135deg,
        color-mix(in srgb, var(--primary-color) 18%, var(--bg-card)),
        color-mix(in srgb, var(--primary-color) 8%, var(--bg-card)));
    border: 1px solid color-mix(in srgb, var(--primary-color) 22%, transparent);
    color: var(--primary-color);
    flex-shrink: 0;
  }

  .obs-header-text h2 {
    margin: 0 0 2px;
    font-size: 17px;
    font-weight: 600;
    color: var(--text-color);
    letter-spacing: 0.01em;
  }

  .obs-header-text p {
    margin: 0;
    font-size: 12.5px;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .obs-close {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 34px;
    height: 34px;
    border-radius: 10px;
    background: transparent;
    border: 1px solid color-mix(in srgb, var(--border-color) 55%, transparent);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 0.16s ease;
    flex-shrink: 0;
  }

  .obs-close:hover {
    background: color-mix(in srgb, var(--bg-tertiary) 60%, transparent);
    color: var(--text-color);
    border-color: color-mix(in srgb, var(--border-color) 90%, transparent);
  }

  /* ---------- Tabs (pill style) ---------- */
  .obs-tabs {
    flex-shrink: 0;
    display: flex;
    gap: 4px;
    padding: 10px 26px 0;
    background: var(--bg-color);
    border-bottom: 1px solid color-mix(in srgb, var(--border-light) 55%, transparent);
  }

  .obs-tab {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 8px 14px 10px;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-secondary);
    font-size: 13px;
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s;
    position: relative;
    top: 1px;
  }

  .obs-tab:hover {
    color: var(--text-color);
  }

  .obs-tab.active {
    color: var(--primary-color);
    border-bottom-color: var(--primary-color);
    font-weight: 500;
  }

  .obs-tab-icon {
    display: inline-flex;
    opacity: 0.85;
  }

  .obs-tab.active .obs-tab-icon {
    opacity: 1;
  }

  .obs-tab-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 16px;
    padding: 0 5px;
    border-radius: 8px;
    background: color-mix(in srgb, var(--accent-red) 85%, transparent);
    color: white;
    font-size: 10px;
    font-weight: 600;
    margin-left: 2px;
  }

  /* ---------- Body ---------- */
  .obs-body {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    background: var(--bg-color);
  }

  .obs-tab-pane {
    padding: 18px 26px 28px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  /* ---------- Action bar (每 tab 内部) ---------- */
  .obs-action-bar {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
  }

  /* 兼容残留引用——4 tab 中部分自定义内容仍可能套这个 class */
  .obs-action-bar-left {
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .obs-action-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    background: color-mix(in srgb, var(--bg-card) 85%, transparent);
    border: 1px solid color-mix(in srgb, var(--border-color) 55%, transparent);
    border-radius: 8px;
    color: var(--text-secondary);
    font-size: 12.5px;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .obs-action-btn:hover:not(:disabled) {
    background: color-mix(in srgb, var(--bg-tertiary) 70%, transparent);
    border-color: color-mix(in srgb, var(--primary-color) 35%, var(--border-color));
    color: var(--text-color);
  }

  .obs-action-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .obs-action-btn.danger:hover:not(:disabled) {
    border-color: color-mix(in srgb, var(--accent-red) 55%, transparent);
    color: var(--accent-red);
    background: color-mix(in srgb, var(--accent-red) 10%, transparent);
  }

  .obs-spin { animation: obs-spinning 0.9s linear infinite; }
  @keyframes obs-spinning {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  /* 单条删除按钮（每行右侧） */
  .obs-row-del {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 26px;
    height: 24px;
    padding: 0;
    border-radius: 6px;
    background: transparent;
    border: 1px solid transparent;
    color: var(--text-muted);
    cursor: pointer;
    transition: all 0.14s ease;
  }
  .obs-row-del:hover:not(:disabled) {
    background: color-mix(in srgb, var(--accent-red) 12%, transparent);
    color: var(--accent-red);
  }
  .obs-row-del:disabled { opacity: 0.45; cursor: not-allowed; }

  /* 已废弃徽章（name 列内联） */
  .obs-orphan-badge {
    display: inline-block;
    margin-left: 8px;
    padding: 1px 7px;
    font-size: 10.5px;
    font-weight: 500;
    line-height: 1.5;
    color: var(--accent-red);
    background: color-mix(in srgb, var(--accent-red) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent-red) 30%, transparent);
    border-radius: 999px;
    vertical-align: middle;
  }
  .obs-tr-orphan .obs-td.name { color: var(--text-secondary); }
  .obs-tr-orphan:hover .obs-row-del { color: var(--accent-red); }

  /* ---------- Error banner / empty state ---------- */
  .obs-error-banner {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    background: color-mix(in srgb, var(--accent-red) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent-red) 30%, transparent);
    border-radius: 10px;
    color: var(--accent-red);
    font-size: 13px;
  }

  .obs-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
    padding: 56px 24px 44px;
    text-align: center;
    color: var(--text-muted);
  }

  .obs-empty-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background: color-mix(in srgb, var(--bg-card) 85%, transparent);
    border: 1px solid color-mix(in srgb, var(--border-light) 70%, transparent);
    color: color-mix(in srgb, var(--primary-color) 60%, var(--text-muted));
  }

  .obs-empty-text {
    font-size: 14px;
    color: var(--text-secondary);
    margin-top: 2px;
  }

  .obs-empty-hint {
    font-size: 12.5px;
    color: var(--text-muted);
    max-width: 280px;
    line-height: 1.6;
  }

  .obs-empty-inline {
    padding: 32px 16px;
    text-align: center;
    font-size: 13px;
    color: var(--text-muted);
    background: color-mix(in srgb, var(--bg-card) 40%, transparent);
    border: 1px dashed color-mix(in srgb, var(--border-color) 55%, transparent);
    border-radius: 10px;
  }

  /* ---------- Section title ---------- */
  .obs-section-title {
    margin: 10px 0 2px;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--text-secondary);
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .obs-section-title-with-filter {
    display: flex;
    justify-content: space-between;
    align-items: center;
    text-transform: none;
    letter-spacing: 0;
    font-size: 13px;
  }

  /* ---------- Table ---------- */
  .obs-table-wrap {
    background: color-mix(in srgb, var(--bg-card) 70%, transparent);
    border: 1px solid color-mix(in srgb, var(--border-light) 65%, transparent);
    border-radius: 12px;
    overflow: hidden;
  }

  .obs-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  .obs-th {
    padding: 10px 14px;
    text-align: left;
    font-weight: 500;
    font-size: 11.5px;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    text-transform: uppercase;
    background: color-mix(in srgb, var(--bg-tertiary) 45%, transparent);
    border-bottom: 1px solid color-mix(in srgb, var(--border-light) 70%, transparent);
    user-select: none;
  }

  .obs-th.right { text-align: right; }
  .obs-th.sortable { cursor: pointer; transition: color 0.15s; }
  .obs-th.sortable:hover { color: var(--text-color); }
  .obs-th-content {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  .obs-table tbody tr {
    border-bottom: 1px solid color-mix(in srgb, var(--border-light) 40%, transparent);
    transition: background 0.12s;
  }
  .obs-table tbody tr:last-child { border-bottom: none; }
  .obs-table tbody tr:hover {
    background: color-mix(in srgb, var(--bg-tertiary) 30%, transparent);
  }

  .obs-td {
    padding: 10px 14px;
    color: var(--text-color);
    font-size: 13px;
    vertical-align: middle;
  }

  .obs-td.name { font-weight: 500; }
  .obs-td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .obs-td.mono { font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 12px; color: var(--text-secondary); }
  .obs-td.save { color: color-mix(in srgb, var(--accent-green) 80%, var(--text-color)); }

  .obs-muted { color: var(--text-muted); }

  /* ---------- Mini pill (rate / count chip) ---------- */
  .obs-mini {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11.5px;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
  }

  .obs-mini.good {
    background: color-mix(in srgb, var(--accent-green) 14%, transparent);
    color: var(--accent-green);
  }
  .obs-mini.warn {
    background: color-mix(in srgb, #d29922 14%, transparent);
    color: #d29922;
  }
  .obs-mini.bad {
    background: color-mix(in srgb, var(--accent-red) 14%, transparent);
    color: var(--accent-red);
  }
  .obs-mini.neutral {
    background: color-mix(in srgb, var(--bg-tertiary) 65%, transparent);
    color: var(--text-secondary);
  }

  /* ---------- Hero card (cache tab 主命中率大卡) ---------- */
  .obs-hero-card {
    display: flex;
    align-items: stretch;
    gap: 20px;
    padding: 22px 24px;
    border-radius: 16px;
    background:
      linear-gradient(135deg,
        color-mix(in srgb, var(--primary-color) 10%, var(--bg-card)),
        color-mix(in srgb, var(--primary-color) 3%, var(--bg-card)));
    border: 1px solid color-mix(in srgb, var(--primary-color) 25%, transparent);
    box-shadow:
      0 1px 3px color-mix(in srgb, black 15%, transparent),
      inset 0 1px 0 color-mix(in srgb, white 3%, transparent);
  }

  .obs-hero-card.tone-good {
    background:
      linear-gradient(135deg,
        color-mix(in srgb, var(--accent-green) 12%, var(--bg-card)),
        color-mix(in srgb, var(--primary-color) 5%, var(--bg-card)));
    border-color: color-mix(in srgb, var(--accent-green) 30%, transparent);
  }
  .obs-hero-card.tone-warn {
    background:
      linear-gradient(135deg,
        color-mix(in srgb, #d29922 12%, var(--bg-card)),
        color-mix(in srgb, var(--primary-color) 5%, var(--bg-card)));
    border-color: color-mix(in srgb, #d29922 30%, transparent);
  }
  .obs-hero-card.tone-bad {
    background:
      linear-gradient(135deg,
        color-mix(in srgb, var(--accent-red) 12%, var(--bg-card)),
        color-mix(in srgb, var(--primary-color) 5%, var(--bg-card)));
    border-color: color-mix(in srgb, var(--accent-red) 30%, transparent);
  }

  .obs-hero-left {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .obs-hero-label {
    font-size: 11.5px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text-muted);
  }

  .obs-hero-value {
    font-size: 44px;
    font-weight: 600;
    color: var(--text-color);
    line-height: 1;
    font-variant-numeric: tabular-nums;
    font-feature-settings: 'tnum' 1;
    display: flex;
    align-items: baseline;
    gap: 4px;
  }

  .obs-hero-card.tone-good .obs-hero-value { color: var(--accent-green); }
  .obs-hero-card.tone-warn .obs-hero-value { color: #d29922; }
  .obs-hero-card.tone-bad .obs-hero-value { color: var(--accent-red); }

  .obs-hero-unit {
    font-size: 22px;
    font-weight: 400;
    opacity: 0.7;
  }

  .obs-hero-sub {
    font-size: 12.5px;
    color: var(--text-secondary);
    margin-top: 4px;
  }

  .obs-hero-right {
    display: flex;
    flex-direction: column;
    gap: 10px;
    justify-content: center;
    min-width: 140px;
    padding-left: 20px;
    border-left: 1px solid color-mix(in srgb, var(--border-light) 60%, transparent);
  }

  .obs-hero-mini {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .obs-hero-mini-label {
    font-size: 11px;
    color: var(--text-muted);
    letter-spacing: 0.03em;
  }

  .obs-hero-mini-value {
    font-size: 18px;
    font-weight: 500;
    color: var(--text-color);
    font-variant-numeric: tabular-nums;
  }

  /* ---------- Summary pill row ---------- */
  .obs-summary-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 10px;
  }

  .obs-summary-pill {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    padding: 12px 14px;
    border-radius: 12px;
    background: color-mix(in srgb, var(--bg-card) 75%, transparent);
    border: 1px solid color-mix(in srgb, var(--border-light) 60%, transparent);
    transition: border-color 0.15s;
  }

  .obs-summary-pill:hover {
    border-color: color-mix(in srgb, var(--primary-color) 25%, var(--border-color));
  }

  .obs-summary-pill.tone-good .obs-summary-pill-value { color: var(--accent-green); }
  .obs-summary-pill.tone-warn .obs-summary-pill-value { color: #d29922; }
  .obs-summary-pill.tone-bad .obs-summary-pill-value { color: var(--accent-red); }

  .obs-summary-pill-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    border-radius: 8px;
    background: color-mix(in srgb, var(--primary-color) 12%, var(--bg-tertiary));
    color: color-mix(in srgb, var(--primary-color) 80%, var(--text-secondary));
    flex-shrink: 0;
  }

  .obs-summary-pill-body {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .obs-summary-pill-label {
    font-size: 11px;
    color: var(--text-muted);
    letter-spacing: 0.02em;
  }

  .obs-summary-pill-value {
    font-size: 19px;
    font-weight: 600;
    color: var(--text-color);
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
  }

  .obs-summary-pill-hint {
    font-size: 10.5px;
    color: var(--text-muted);
  }

  .obs-summary-pill-extra {
    display: inline-flex;
    gap: 4px;
    flex-wrap: wrap;
    margin-top: 4px;
  }

  /* ---------- Severity chips ---------- */
  .obs-sev-chip {
    display: inline-block;
    padding: 2px 6px;
    border-radius: 6px;
    font-size: 10.5px;
    font-weight: 500;
    text-transform: lowercase;
  }
  .obs-sev-chip.critical { background: color-mix(in srgb, var(--accent-red) 18%, transparent); color: var(--accent-red); }
  .obs-sev-chip.warning { background: color-mix(in srgb, #d29922 18%, transparent); color: #d29922; }
  .obs-sev-chip.info { background: color-mix(in srgb, var(--accent-blue) 18%, transparent); color: var(--accent-blue); }

  /* ---------- Severity filter toggles ---------- */
  .obs-sev-filter {
    display: inline-flex;
    gap: 4px;
  }

  .obs-sev-toggle {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 8px;
    border-radius: 999px;
    border: 1px solid color-mix(in srgb, var(--border-color) 55%, transparent);
    font-size: 11px;
    color: var(--text-muted);
    cursor: pointer;
    user-select: none;
    transition: all 0.15s;
  }

  .obs-sev-toggle input { display: none; }

  .obs-sev-toggle:hover { color: var(--text-secondary); }

  .obs-sev-toggle.active.critical {
    border-color: color-mix(in srgb, var(--accent-red) 40%, transparent);
    color: var(--accent-red);
    background: color-mix(in srgb, var(--accent-red) 10%, transparent);
  }
  .obs-sev-toggle.active.warning {
    border-color: color-mix(in srgb, #d29922 40%, transparent);
    color: #d29922;
    background: color-mix(in srgb, #d29922 10%, transparent);
  }
  .obs-sev-toggle.active.info {
    border-color: color-mix(in srgb, var(--accent-blue) 40%, transparent);
    color: var(--accent-blue);
    background: color-mix(in srgb, var(--accent-blue) 10%, transparent);
  }

  /* ---------- Health list ---------- */
  .obs-health-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .obs-health-row {
    display: grid;
    grid-template-columns: 130px 1fr 50px auto;
    gap: 12px;
    align-items: center;
    padding: 8px 12px;
    border-radius: 8px;
    background: color-mix(in srgb, var(--bg-card) 55%, transparent);
    border: 1px solid color-mix(in srgb, var(--border-light) 40%, transparent);
    font-size: 12.5px;
  }

  .obs-health-tool {
    color: var(--text-color);
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .obs-health-bar-wrap {
    height: 6px;
    background: color-mix(in srgb, var(--bg-tertiary) 85%, transparent);
    border-radius: 3px;
    overflow: hidden;
  }

  .obs-health-bar {
    display: block;
    height: 100%;
    border-radius: 3px;
    transition: width 0.35s ease;
  }

  .obs-health-bar.good { background: var(--accent-green); }
  .obs-health-bar.warn { background: #d29922; }
  .obs-health-bar.bad { background: var(--accent-red); }

  .obs-health-score {
    font-variant-numeric: tabular-nums;
    font-weight: 600;
    text-align: right;
  }
  .obs-health-score.good { color: var(--accent-green); }
  .obs-health-score.warn { color: #d29922; }
  .obs-health-score.bad { color: var(--accent-red); }

  .obs-health-meta {
    color: var(--text-muted);
    font-size: 11.5px;
    white-space: nowrap;
  }

  /* ---------- Issues ---------- */
  .obs-issues-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .obs-issue-card {
    padding: 11px 14px;
    border-radius: 10px;
    background: color-mix(in srgb, var(--bg-card) 70%, transparent);
    border: 1px solid color-mix(in srgb, var(--border-light) 60%, transparent);
    border-left-width: 3px;
  }

  .obs-issue-card.critical { border-left-color: var(--accent-red); }
  .obs-issue-card.warning { border-left-color: #d29922; }
  .obs-issue-card.info { border-left-color: var(--accent-blue); }

  .obs-issue-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
  }

  .obs-issue-card.critical .obs-issue-head { color: var(--accent-red); }
  .obs-issue-card.warning .obs-issue-head { color: #d29922; }
  .obs-issue-card.info .obs-issue-head { color: var(--accent-blue); }

  .obs-issue-title {
    font-weight: 500;
    font-size: 13px;
    color: var(--text-color);
  }

  .obs-issue-cat {
    margin-left: auto;
    padding: 2px 7px;
    border-radius: 6px;
    background: color-mix(in srgb, var(--bg-tertiary) 70%, transparent);
    color: var(--text-muted);
    font-size: 10.5px;
    letter-spacing: 0.02em;
  }

  .obs-issue-detail {
    font-size: 12.5px;
    color: var(--text-secondary);
    line-height: 1.55;
  }

  .obs-issue-suggestion {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    margin-top: 6px;
    padding: 4px 8px 4px 6px;
    border-radius: 6px;
    background: color-mix(in srgb, var(--primary-color) 9%, transparent);
    color: color-mix(in srgb, var(--primary-color) 90%, var(--text-secondary));
    font-size: 11.5px;
    font-weight: 500;
  }

  /* ---------- Footer hint (cache tab 底部数据源说明) ---------- */
  .obs-footer-hint {
    margin-top: 6px;
    padding-top: 10px;
    border-top: 1px dashed color-mix(in srgb, var(--border-light) 55%, transparent);
    font-size: 11px;
    color: var(--text-muted);
    line-height: 1.6;
  }
  .obs-footer-hint code {
    background: color-mix(in srgb, var(--bg-tertiary) 70%, transparent);
    padding: 1px 5px;
    border-radius: 4px;
    font-size: 10.5px;
  }
`
