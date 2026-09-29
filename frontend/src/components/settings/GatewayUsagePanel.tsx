/**
 * Gateway 用量统计面板（M4-C）。
 *
 * 设计文档：_docs/GATEWAY_M4.md
 *
 * 用户故事：
 *   - 想看清"上周哪个 provider 调最多 / 哪个 token 用得凶 / 错误率多少"
 *   - 想看"最近 N 次 fallback 切换都是为啥"——直接读 audit log 不现实
 *   - 不想引图表库（recharts/echarts 体积大），自己画极简水平柱状图
 *
 * 实现：
 *   - 后端每次请求扫一遍 jsonl 内存聚合，前端只是 view
 *   - 时间范围按天预设：今天 / 7天 / 30天 / 自定义
 *   - 维度切换：provider / model / token / day / endpoint
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertCircle, Activity, BarChart3, ChevronDown, ChevronRight, Loader2, RefreshCw } from 'lucide-react'
import {
  type GatewayFallbackJumpEvent,
  type GatewayUsage,
  type GatewayUsageGroupBy,
  getGatewayRecentFallbackJumps,
  getGatewayUsage,
} from '../../api/gateway'

interface Props {
  enabled: boolean
}

type RangeKey = 'today' | '7d' | '30d' | 'all'

export default function GatewayUsagePanel({ enabled }: Props) {
  // 默认展开——进这个 tab 就是为了看数据，还要点一下是多余一步
  const [open, setOpen] = useState(true)
  const [usage, setUsage] = useState<GatewayUsage | null>(null)
  const [jumps, setJumps] = useState<GatewayFallbackJumpEvent[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [groupBy, setGroupBy] = useState<GatewayUsageGroupBy>('provider')
  const [rangeKey, setRangeKey] = useState<RangeKey>('7d')

  const dateRange = useMemo(() => computeRange(rangeKey), [rangeKey])

  const refresh = useCallback(async () => {
    if (!enabled) return
    setLoading(true)
    setError(null)
    try {
      const [u, j] = await Promise.all([
        getGatewayUsage({ groupBy, start: dateRange.start, end: dateRange.end, topN: 50 }),
        getGatewayRecentFallbackJumps(15),
      ])
      setUsage(u)
      setJumps(j)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [enabled, groupBy, dateRange.start, dateRange.end])

  useEffect(() => {
    if (open) void refresh()
  }, [open, refresh])

  if (!enabled) return null

  const totalCalls = usage?.total_calls ?? 0
  const errorRate = usage?.error_rate ?? 0
  const totalTokens = usage?.total_tokens ?? 0
  const buckets = usage?.buckets ?? []
  const maxCalls = buckets.length ? Math.max(...buckets.map((b) => b.calls)) : 1

  return (
    <section style={cardStyle}>
      <div style={sectionHeadStyle}>
        <button type="button" style={collapseHeadStyle} onClick={() => setOpen((v) => !v)}>
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <span style={{ marginLeft: 4 }}>
            <BarChart3 size={12} style={{ verticalAlign: -2, marginRight: 4 }} />
            用量统计
            {open && usage && (
              <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--text-tertiary, #888)', marginLeft: 6 }}>
                · {totalCalls} 次调用 · 错误率 {(errorRate * 100).toFixed(1)}%
              </span>
            )}
          </span>
        </button>
        {open && (
          <div style={{ display: 'flex', gap: 6 }}>
            <button type="button" style={btnGhostStyle} onClick={() => void refresh()} disabled={loading}>
              {loading ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />}
              <span style={{ marginLeft: 4 }}>刷新</span>
            </button>
          </div>
        )}
      </div>

      {open && (
        <>
          {/* 控制栏：时间范围 + 分组维度 */}
          <div style={controlBarStyle}>
            <div style={{ display: 'flex', gap: 4 }}>
              {(['today', '7d', '30d', 'all'] as RangeKey[]).map((k) => (
                <button
                  key={k}
                  type="button"
                  style={rangeKey === k ? btnActiveStyle : btnGhostStyle}
                  onClick={() => setRangeKey(k)}
                >
                  {labelOfRange(k)}
                </button>
              ))}
            </div>
            <select
              value={groupBy}
              onChange={(e) => setGroupBy(e.target.value as GatewayUsageGroupBy)}
              style={selectStyle}
            >
              <option value="provider">按 Provider</option>
              <option value="model">按 Model</option>
              <option value="token">按 Token</option>
              <option value="day">按天</option>
              <option value="endpoint">按 Endpoint</option>
              <option value="client_ip">按 客户端 IP</option>
            </select>
          </div>

          {error && (
            <div style={errBoxStyle}>
              <AlertCircle size={13} />
              <span style={{ flex: 1 }}>{error}</span>
              <button type="button" style={btnGhostStyle} onClick={() => setError(null)}>×</button>
            </div>
          )}

          {/* 顶部 KPI */}
          {usage && (
            <div style={kpiRowStyle}>
              <Kpi label="总调用" value={totalCalls.toLocaleString()} />
              <Kpi label="错误率" value={`${(errorRate * 100).toFixed(2)}%`} tone={errorRate > 0.05 ? 'warn' : 'ok'} />
              <Kpi label="Prompt tokens" value={(usage.total_prompt_tokens || 0).toLocaleString()} />
              <Kpi label="Completion tokens" value={(usage.total_completion_tokens || 0).toLocaleString()} />
              <Kpi label="总 tokens" value={totalTokens.toLocaleString()} />
            </div>
          )}

          {/* 柱状图 */}
          {buckets.length === 0 && !loading && (
            <div style={emptyStyle}>
              该时间范围内没有调用记录。可能：Gateway 还没被外部客户端用过；或当前范围太窄。
            </div>
          )}
          {buckets.length > 0 && (
            <div style={chartStyle}>
              {buckets.map((b) => {
                const pct = (b.calls / maxCalls) * 100
                const errorPct = b.calls ? (b.errors / b.calls) * 100 : 0
                return (
                  <div key={b.key} style={barRowStyle}>
                    <div style={{ ...barLabelStyle, color: 'var(--text-color)' }} title={b.key}>
                      {b.key}
                    </div>
                    <div style={barTrackStyle}>
                      <div style={{ ...barFillStyle, width: `${pct}%` }} />
                      {errorPct > 0 && (
                        <div
                          style={{
                            ...barErrFillStyle,
                            width: `${(b.errors / maxCalls) * 100}%`,
                          }}
                          title={`${b.errors} 错误`}
                        />
                      )}
                    </div>
                    <div style={barCountStyle}>
                      {b.calls.toLocaleString()}
                      {b.errors > 0 && (
                        <span style={{ color: '#e5484d', marginLeft: 4 }}>({b.errors} err)</span>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {/* 详细表 */}
          {buckets.length > 0 && (
            <details style={{ marginTop: 12 }}>
              <summary style={{ cursor: 'pointer', fontSize: 11.5, color: 'var(--text-secondary)' }}>
                展开详细数据（P50/P95 延迟 / 流式比例 / fallback 跳转）
              </summary>
              <div style={{ overflowX: 'auto', marginTop: 8 }}>
                <table style={tableStyle}>
                  <thead>
                    <tr>
                      <th style={thStyle}>{labelOfDim(groupBy)}</th>
                      <th style={thStyle}>调用</th>
                      <th style={thStyle}>错误</th>
                      <th style={thStyle}>P50 ms</th>
                      <th style={thStyle}>P95 ms</th>
                      <th style={thStyle}>流式</th>
                      <th style={thStyle}>Tokens</th>
                      {groupBy === 'provider' && <th style={thStyle}>Fallback ↗ ↘</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {buckets.map((b) => (
                      <tr key={b.key}>
                        <td style={{ ...tdStyle, fontFamily: 'monospace' }}>{b.key}</td>
                        <td style={tdStyle}>{b.calls}</td>
                        <td style={{ ...tdStyle, color: b.errors ? '#e5484d' : undefined }}>{b.errors}</td>
                        <td style={tdStyle}>{b.p50_latency_ms ?? '—'}</td>
                        <td style={tdStyle}>{b.p95_latency_ms ?? '—'}</td>
                        <td style={tdStyle}>
                          {b.stream_calls}/{b.calls}
                        </td>
                        <td style={tdStyle}>{b.total_tokens.toLocaleString()}</td>
                        {groupBy === 'provider' && (
                          <td style={tdStyle}>
                            <span style={{ color: '#3b9eff' }}>↗{b.fallback_jumps_in}</span>{' '}
                            <span style={{ color: '#e5484d' }}>↘{b.fallback_jumps_out}</span>
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}

          {/* 最近 fallback 跳转 */}
          <div style={{ marginTop: 14 }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6, color: 'var(--text-color)' }}>
              <Activity size={12} style={{ verticalAlign: -2, marginRight: 4 }} />
              最近 Fallback 跳转 {jumps.length > 0 && <span style={{ fontWeight: 400, color: 'var(--text-tertiary, #888)', fontSize: 11 }}>· {jumps.length} 次</span>}
            </div>
            {jumps.length === 0 ? (
              <div style={{ ...subHintStyle, padding: '8px 0' }}>
                没有切换记录。所有调用都直达首选 provider，链路健康。
              </div>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 0, listStyle: 'none' }}>
                {jumps.map((j, i) => (
                  <li key={`${j.ts}-${i}`} style={jumpRowStyle}>
                    <code style={{ ...codeStyle, fontSize: 10.5, color: 'var(--text-tertiary, #888)' }}>
                      {formatTs(j.ts)}
                    </code>
                    <span style={{ marginLeft: 6, fontFamily: 'monospace', fontSize: 11 }}>
                      <code style={candCodeStyle}>{j.from_provider}</code>
                      <span style={{ margin: '0 4px', color: 'var(--text-tertiary, #888)' }}>→</span>
                      <code style={{ ...candCodeStyle, background: 'rgba(59,158,255,0.12)', color: '#3b9eff' }}>
                        {j.to_provider}
                      </code>
                    </span>
                    <span style={{ marginLeft: 8, fontSize: 11, color: '#e5484d' }}>
                      {j.from_status ? `${j.from_status} · ` : ''}
                      {j.from_result || ''}
                    </span>
                    <span style={{ marginLeft: 'auto', fontSize: 10.5, color: 'var(--text-tertiary, #888)' }}>
                      {j.model_requested}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <p style={{ ...hintStyle, marginTop: 12 }}>
            数据源：<code style={codeStyle}>~/.liuruyan/gateway_audit.jsonl</code>。每次刷新都重扫一遍 jsonl 内存聚合，
            不存数据库。本地网关流量 &lt; 100k/月时性能足够；体量更大时考虑 SQLite 后端（M5+）。
          </p>
        </>
      )}
    </section>
  )
}

/* ─────────── 子组件 ─────────── */

function Kpi({ label, value, tone }: { label: string; value: string; tone?: 'ok' | 'warn' }) {
  return (
    <div style={kpiBoxStyle}>
      <div style={{ fontSize: 10.5, letterSpacing: 0.5, color: 'var(--text-tertiary, #888)' }}>{label}</div>
      <div
        style={{
          fontSize: 20,
          fontWeight: 600,
          letterSpacing: 0.2,
          fontVariantNumeric: 'tabular-nums',
          color: tone === 'warn' ? '#e5484d' : 'var(--text-color)',
          marginTop: 3,
        }}
      >
        {value}
      </div>
    </div>
  )
}

/* ─────────── helpers ─────────── */

function computeRange(key: RangeKey): { start?: string; end?: string } {
  if (key === 'all') return {}
  const now = new Date()
  const end = formatYmd(now)
  let start: string
  if (key === 'today') {
    start = end
  } else if (key === '7d') {
    const d = new Date(now.getTime() - 6 * 24 * 3600 * 1000)
    start = formatYmd(d)
  } else {
    const d = new Date(now.getTime() - 29 * 24 * 3600 * 1000)
    start = formatYmd(d)
  }
  return { start, end: `${end}T23:59:59` }
}

function formatYmd(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function formatTs(ts: string): string {
  if (!ts || ts.length < 16) return ts
  return ts.slice(0, 16).replace('T', ' ')
}

function labelOfRange(k: RangeKey): string {
  return { today: '今天', '7d': '近 7 天', '30d': '近 30 天', all: '全部' }[k]
}

function labelOfDim(d: GatewayUsageGroupBy): string {
  return { provider: 'Provider', model: 'Model', token: 'Token', day: '日期', endpoint: 'Endpoint', client_ip: '客户端 IP' }[d]
}

/* ─────────── styles ─────────── */

const cardStyle: React.CSSProperties = {
  background: 'var(--card-bg, rgba(0,0,0,0.02))',
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  borderRadius: 12,
  padding: '14px 18px',
  boxShadow: '0 1px 2px rgba(0,0,0,0.04), 0 8px 24px -12px rgba(0,0,0,0.10)',
}

const sectionHeadStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  fontSize: 12.5,
  fontWeight: 600,
  color: 'var(--text-color)',
  marginBottom: 8,
  gap: 8,
  flexWrap: 'wrap',
  // 锁定行高 = 按钮（4px*2 padding + ~13px 文字 + 1px*2 border ≈ 28px）；这样
  // 按钮在 collapsed/expanded 之间出现/消失时，标题文字垂直位置保持不变。
  minHeight: 28,
}

const collapseHeadStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  background: 'transparent',
  border: 'none',
  cursor: 'pointer',
  padding: 0,
  font: 'inherit',
  color: 'inherit',
}

const btnBaseStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  padding: '4px 10px',
  border: '1px solid var(--border-color, rgba(0,0,0,0.12))',
  borderRadius: 5,
  background: 'var(--card-bg, transparent)',
  fontSize: 11.5,
  cursor: 'pointer',
  color: 'var(--text-color)',
}

const btnGhostStyle: React.CSSProperties = { ...btnBaseStyle }
const btnActiveStyle: React.CSSProperties = {
  ...btnBaseStyle,
  background: 'var(--primary-color, #5e6ad2)',
  color: '#fff',
  borderColor: 'transparent',
}

const controlBarStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  marginBottom: 10,
  gap: 6,
  flexWrap: 'wrap',
}

const selectStyle: React.CSSProperties = {
  padding: '4px 8px',
  borderRadius: 5,
  border: '1px solid var(--border-color, rgba(0,0,0,0.12))',
  background: 'var(--card-bg, transparent)',
  fontSize: 11.5,
  color: 'var(--text-color)',
  cursor: 'pointer',
}

const kpiRowStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))',
  gap: 8,
  marginBottom: 12,
}

const kpiBoxStyle: React.CSSProperties = {
  padding: '11px 14px',
  background: 'var(--bg-secondary, rgba(0,0,0,0.03))',
  borderRadius: 10,
  border: '1px solid var(--border-color, rgba(0,0,0,0.06))',
}

const chartStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 4,
}

const barRowStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: '180px 1fr 120px',
  gap: 8,
  alignItems: 'center',
  fontSize: 11,
  padding: '2px 0',
}

const barLabelStyle: React.CSSProperties = {
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 11,
}

const barTrackStyle: React.CSSProperties = {
  position: 'relative',
  background: 'rgba(0,0,0,0.04)',
  borderRadius: 3,
  height: 14,
  overflow: 'hidden',
}

const barFillStyle: React.CSSProperties = {
  position: 'absolute',
  left: 0,
  top: 0,
  bottom: 0,
  background: 'var(--primary-color, #5e6ad2)',
  borderRadius: 3,
}

const barErrFillStyle: React.CSSProperties = {
  position: 'absolute',
  left: 0,
  top: 0,
  bottom: 0,
  background: 'rgba(229,72,77,0.6)',
  borderRadius: 3,
}

const barCountStyle: React.CSSProperties = {
  fontSize: 11,
  color: 'var(--text-secondary)',
  textAlign: 'right',
  fontVariantNumeric: 'tabular-nums',
}

const tableStyle: React.CSSProperties = {
  width: '100%',
  fontSize: 11,
  borderCollapse: 'collapse',
}

const thStyle: React.CSSProperties = {
  textAlign: 'left',
  padding: '4px 8px',
  borderBottom: '1px solid var(--border-color, rgba(0,0,0,0.1))',
  fontWeight: 600,
  color: 'var(--text-secondary)',
  fontSize: 10.5,
}

const tdStyle: React.CSSProperties = {
  padding: '4px 8px',
  borderBottom: '1px solid var(--border-color, rgba(0,0,0,0.04))',
  color: 'var(--text-color)',
  fontVariantNumeric: 'tabular-nums',
}

const jumpRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  padding: '4px 0',
  borderBottom: '1px solid var(--border-color, rgba(0,0,0,0.04))',
}

const candCodeStyle: React.CSSProperties = {
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 10.5,
  background: 'rgba(0,0,0,0.05)',
  padding: '1px 5px',
  borderRadius: 3,
}

const codeStyle: React.CSSProperties = {
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 10.5,
  background: 'rgba(0,0,0,0.05)',
  padding: '1px 4px',
  borderRadius: 3,
}

const errBoxStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  padding: 8,
  fontSize: 11.5,
  color: '#e5484d',
  background: 'rgba(229,72,77,0.08)',
  borderRadius: 6,
  marginBottom: 8,
}

const emptyStyle: React.CSSProperties = {
  padding: '12px',
  fontSize: 12,
  color: 'var(--text-secondary)',
  background: 'rgba(0,0,0,0.02)',
  borderRadius: 6,
  lineHeight: 1.6,
}

const hintStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 11,
  color: 'var(--text-tertiary, #888)',
  lineHeight: 1.6,
}

const subHintStyle: React.CSSProperties = {
  fontSize: 11,
  color: 'var(--text-tertiary, #888)',
}
