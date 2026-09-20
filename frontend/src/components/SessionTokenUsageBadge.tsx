/**
 * 顶栏 per-session token 累计消耗显示。
 *
 * - 紧凑显示：↑input ↓output · calls
 * - hover 弹出详细 tooltip：4 字段拆分 + cache 命中率 + 最近调用时间
 * - 空状态（calls=0 或未连接）显示占位 "—"
 *
 * selector 只订阅 sessions[sid].tokenUsage，sid/usage 变更才重渲。
 */

import { useState, useMemo } from 'react'
import { useAppStore, type TokenUsageState } from '../stores/useAppStore'

interface Props {
  sessionId: string | null | undefined
}

function fmtNum(n: number): string {
  if (!n || n < 0) return '0'
  if (n < 1000) return String(n)
  if (n < 1_000_000) return (n / 1000).toFixed(n < 10000 ? 1 : 0).replace(/\.0$/, '') + 'k'
  return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M'
}

function fmtTime(iso?: string): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return '—'
    const hh = String(d.getHours()).padStart(2, '0')
    const mm = String(d.getMinutes()).padStart(2, '0')
    const ss = String(d.getSeconds()).padStart(2, '0')
    return `${hh}:${mm}:${ss}`
  } catch {
    return '—'
  }
}

function cacheHitRate(usage: TokenUsageState): number {
  // cache_read 占总入参比例：cache_read / (input + cache_read + cache_create)
  const denom = usage.input + usage.cacheRead + usage.cacheCreate
  if (denom <= 0) return 0
  return Math.round((usage.cacheRead / denom) * 100)
}

export function SessionTokenUsageBadge({ sessionId }: Props) {
  const usage = useAppStore((s) =>
    sessionId ? s.sessions[sessionId]?.tokenUsage ?? null : null,
  )
  const [hover, setHover] = useState(false)

  const display = useMemo(() => {
    if (!usage || usage.calls === 0) return null
    return {
      input: fmtNum(usage.input),
      output: fmtNum(usage.output),
      cacheRead: fmtNum(usage.cacheRead),
      cacheCreate: fmtNum(usage.cacheCreate),
      total: fmtNum(usage.total),
      calls: usage.calls,
      cacheRate: cacheHitRate(usage),
      lastCallAt: fmtTime(usage.lastCallAt),
      raw: usage,
    }
  }, [usage])

  if (!display) {
    return (
      <div className="token-usage-badge token-usage-badge--empty" aria-label="本次会话 token 消耗">
        <span className="token-usage-badge__placeholder">—</span>
      </div>
    )
  }

  return (
    <div
      className="token-usage-badge"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      aria-label={`本次会话累计：入参 ${display.raw.input}，出参 ${display.raw.output}，调用 ${display.calls} 次`}
    >
      <span className="token-usage-badge__compact">
        <span className="token-usage-badge__arrow">↑</span>
        <span className="token-usage-badge__num">{display.input}</span>
        <span className="token-usage-badge__arrow">↓</span>
        <span className="token-usage-badge__num">{display.output}</span>
        <span className="token-usage-badge__sep">·</span>
        <span className="token-usage-badge__calls">{display.calls}</span>
        {display.raw.cacheRead > 0 && (
          <>
            <span className="token-usage-badge__sep">·</span>
            <span
              className={`token-usage-badge__cache tone-${
                display.cacheRate >= 70 ? 'good'
                  : display.cacheRate >= 50 ? 'warn'
                  : 'bad'
              }`}
              title={`缓存命中 ${display.raw.cacheRead.toLocaleString()} / ${(
                display.raw.input + display.raw.cacheRead + display.raw.cacheCreate
              ).toLocaleString()}`}
            >
              ⚡{display.cacheRate}%
            </span>
          </>
        )}
      </span>

      {hover && (
        <div className="token-usage-badge__tooltip" role="tooltip">
          <div className="token-usage-badge__tooltip-title">本次会话累计</div>
          <div className="token-usage-badge__row">
            <span>入参</span>
            <span className="token-usage-badge__row-num">{display.raw.input.toLocaleString()}</span>
          </div>
          <div className="token-usage-badge__row">
            <span>出参</span>
            <span className="token-usage-badge__row-num">{display.raw.output.toLocaleString()}</span>
          </div>
          <div className="token-usage-badge__row">
            <span>缓存命中</span>
            <span className="token-usage-badge__row-num">
              {display.raw.cacheRead.toLocaleString()} ({display.cacheRate}%)
            </span>
          </div>
          <div className="token-usage-badge__row">
            <span>缓存创建</span>
            <span className="token-usage-badge__row-num">{display.raw.cacheCreate.toLocaleString()}</span>
          </div>
          <div className="token-usage-badge__divider" />
          <div className="token-usage-badge__row">
            <span>总计</span>
            <span className="token-usage-badge__row-num">{display.raw.total.toLocaleString()}</span>
          </div>
          <div className="token-usage-badge__row">
            <span>调用次数</span>
            <span className="token-usage-badge__row-num">{display.calls}</span>
          </div>
          <div className="token-usage-badge__row token-usage-badge__row--muted">
            <span>最近</span>
            <span className="token-usage-badge__row-num">{display.lastCallAt}</span>
          </div>
        </div>
      )}
    </div>
  )
}
