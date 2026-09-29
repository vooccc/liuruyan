/**
 * 顶栏 per-session token 累计消耗显示。
 *
 * - 紧凑显示：↑input ↓output · calls
 * - hover 弹出详细 tooltip，上下两段：
 *   · 上半"本次会话累计"= 真实计费账单（API usage 累加）
 *   · 下半"上下文构成"= 后端 build_messages 的分段估算（消息/工具/系统/动态注入）
 *     + 窗口占用进度条（含压缩触发线标记）+ 超标提示
 * - 空状态（calls=0 或未连接）显示占位 "—"
 *
 * selector 只订阅 sessions[sid].tokenUsage，sid/usage 变更才重渲。
 */

import { useState, useMemo } from 'react'
import { useAppStore, type ContextBreakdown, type TokenUsageState } from '../stores/useAppStore'

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

/** 分段渲染顺序与颜色（和进度条堆叠顺序一致） */
const BREAKDOWN_SEGMENTS: Array<{ key: keyof ContextBreakdown; label: string; color: string }> = [
  { key: 'messages', label: '消息历史', color: '#4a9eff' },
  { key: 'tools', label: '工具定义', color: '#a371f7' },
  { key: 'system', label: '系统提示词', color: '#2e9e5f' },
  { key: 'extra', label: '动态注入', color: '#8a8a8a' },
]

/** 跳设置→工具 tab（App.tsx 全局监听 lry:open-settings） */
function openSettingsTab(tab: string, agentId?: string | null): void {
  try {
    window.dispatchEvent(new CustomEvent('lry:open-settings', { detail: { tab, agentId } }))
  } catch { /* ignore */ }
}

export function SessionTokenUsageBadge({ sessionId }: Props) {
  const usage = useAppStore((s) =>
    sessionId ? s.sessions[sessionId]?.tokenUsage ?? null : null,
  )
  const [hover, setHover] = useState(false)

  const display = useMemo(() => {
    if (!usage || usage.calls === 0) return null
    const bd = usage.contextBreakdown
    const segs = bd
      ? BREAKDOWN_SEGMENTS.map((s) => ({
          ...s,
          value: Number(bd[s.key]) || 0,
          pct: bd.total > 0 ? Math.round(((Number(bd[s.key]) || 0) / bd.total) * 100) : 0,
        }))
      : null
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
      bd,
      segs,
      bdPct: bd?.contextLength ? Math.round((bd.total / bd.contextLength) * 100) : null,
      compactPct: bd?.contextLength && bd.compactThreshold
        ? Math.round((bd.compactThreshold / bd.contextLength) * 100)
        : null,
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

          {display.bd && display.segs && (
            <>
              <div className="token-usage-badge__divider" />
              <div className="token-usage-badge__tooltip-title">
                上下文构成（本轮请求 · 估算{display.bd.model ? ` · ${display.bd.model}` : ''}）
              </div>

              {display.bd.contextLength ? (
                <>
                  <div className="token-usage-badge__row">
                    <span>窗口占用</span>
                    <span className="token-usage-badge__row-num">
                      {fmtNum(display.bd.total)} / {fmtNum(display.bd.contextLength)}
                      （{display.bdPct}%）
                    </span>
                  </div>
                  <div className="token-usage-badge__ctxbar">
                    <div
                      className="token-usage-badge__ctxbar-fill"
                      style={{ width: `${Math.min(display.bdPct ?? 0, 100)}%` }}
                    />
                    {display.compactPct != null && display.compactPct <= 100 && (
                      <div
                        className="token-usage-badge__ctxbar-marker"
                        style={{ left: `${display.compactPct}%` }}
                        title={`上下文压缩触发线（${fmtNum(display.bd.compactThreshold ?? 0)} token）`}
                      />
                    )}
                  </div>
                </>
              ) : null}

              {display.segs.map((s) => (
                <div className="token-usage-badge__row" key={s.key as string}>
                  <span>
                    <span
                      className="token-usage-badge__segdot"
                      style={{ background: s.color }}
                    />
                    {s.label}
                  </span>
                  <span className="token-usage-badge__row-num">
                    {fmtNum(s.value)}（{s.pct}%）
                  </span>
                </div>
              ))}

              {(() => {
                const hints: Array<{ text: string; tab?: string }> = []
                const segs = display.segs ?? []
                const toolsSeg = segs.find((s) => s.key === 'tools')
                const msgsSeg = segs.find((s) => s.key === 'messages')
                if (toolsSeg && toolsSeg.pct > 40 && display.bd!.total > 0) {
                  hints.push({ text: '工具定义偏高 → 设置→工具 调档位', tab: 'tools' })
                }
                if (msgsSeg && msgsSeg.pct > 70 && display.bd!.total > 0) {
                  hints.push({ text: '消息历史偏高 → 建议压缩上下文或开新会话' })
                }
                if (hints.length === 0) return null
                return hints.map((h, i) => (
                  <div className="token-usage-badge__hint" key={i}>
                    💡{' '}
                    {h.tab ? (
                      <button
                        type="button"
                        className="token-usage-badge__hintbtn"
                        onClick={() => openSettingsTab(h.tab!)}
                      >
                        {h.text}
                      </button>
                    ) : (
                      <span>{h.text}</span>
                    )}
                  </div>
                ))
              })()}
            </>
          )}
        </div>
      )}
    </div>
  )
}
