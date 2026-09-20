/**
 * Computer Use Replay 面板（M7）。
 *
 * 流程：
 *   1. 输入 last_n / 可选 session_id 过滤 → "Preview" → 后端 dry_run 构 plan
 *   2. 看 plan 步骤列表 → 调速度 / abort_on_error → "真执行"（必须二次确认）
 *   3. 执行结果以彩色 chip 显示每步状态
 *
 * 安全：
 *   - 默认 dry_run=true
 *   - 真执行前 useConfirmDialog 弹自定义确认框（Tauri webview 不支持 window.confirm）
 *   - replay_session_id 自动生成 `replay-${timestamp}` 让用户不必手填
 */
import { useCallback, useState } from 'react'
import { History, Play, Loader2, CheckCircle2, XCircle, FastForward } from 'lucide-react'
import {
  type ReplayResponse,
  type ReplayStep,
  type ReplayStepResult,
  postComputerUseReplay,
} from '../../api/computerUse'
import { useConfirmDialog } from '../ConfirmDialog'

interface Props {
  /** 可选：默认按这个 session_id 过滤（来自 audit 列表 selection） */
  defaultSessionId?: string
}

export default function ComputerUseReplayPanel({ defaultSessionId }: Props) {
  const [sessionFilter, setSessionFilter] = useState(defaultSessionId ?? '')
  const [lastN, setLastN] = useState(20)
  const [speed, setSpeed] = useState(1.0)
  const [abortOnError, setAbortOnError] = useState(true)
  const [resp, setResp] = useState<ReplayResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Tauri webview 不支持 window.confirm
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog()

  const handlePreview = useCallback(async () => {
    setBusy(true)
    setErr(null)
    try {
      const r = await postComputerUseReplay({
        session_id: sessionFilter.trim() || undefined,
        last_n: lastN,
        dry_run: true,
      })
      setResp(r)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }, [sessionFilter, lastN])

  const handleRealRun = useCallback(async () => {
    if (!resp || resp.plan.step_count === 0) return
    const ok = await askConfirm(
      '真实执行回放',
      `即将真实执行 ${resp.plan.step_count} 步操作（${speed}x 速度，${abortOnError ? '失败即停' : '失败继续'}）。\n这会真的移动鼠标、敲键盘。确定吗？`,
      'danger',
      '真实执行',
    )
    if (!ok) return

    setBusy(true)
    setErr(null)
    try {
      const replaySessionId = `replay-${Date.now()}`
      const r = await postComputerUseReplay({
        session_id: sessionFilter.trim() || undefined,
        last_n: lastN,
        dry_run: false,
        speed_multiplier: speed,
        abort_on_error: abortOnError,
        replay_session_id: replaySessionId,
      })
      setResp(r)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }, [resp, speed, abortOnError, sessionFilter, lastN, askConfirm])

  return (
    <section style={SECTION_STYLE}>
      <div style={HEADER_ROW}>
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6 }}>
          <History size={14} /> 回放（Replay）
        </h3>
      </div>
      <p style={HINT_STYLE}>
        从审计日志重新执行最近的操作序列。先 <strong>预览（dry-run）</strong>看计划，再决定是否真执行。
        每步仍走完整安全栏（速率/黑名单/视觉闭环全生效）。
      </p>

      {/* 控件 */}
      <div style={CONTROLS_ROW}>
        <label style={LBL_STYLE}>
          按 session 过滤
          <input
            type="text"
            value={sessionFilter}
            onChange={(e) => setSessionFilter(e.target.value)}
            placeholder="留空 = 全局"
            style={INPUT_STYLE}
            disabled={busy}
          />
        </label>
        <label style={LBL_STYLE}>
          扫多少条
          <input
            type="number"
            min={1}
            max={500}
            value={lastN}
            onChange={(e) => setLastN(Math.max(1, Math.min(500, Number(e.target.value) || 20)))}
            style={{ ...INPUT_STYLE, width: 70 }}
            disabled={busy}
          />
        </label>
        <button
          type="button"
          onClick={() => void handlePreview()}
          disabled={busy}
          style={BTN_PRIMARY}
        >
          {busy ? <Loader2 size={12} className="spin" /> : <History size={12} />}
          预览计划
        </button>
      </div>

      {err && <div style={ERR_STYLE}>错误：{err}</div>}

      {resp && <PlanView resp={resp} />}

      {/* 真执行控件（只在已 preview 且非空时显示） */}
      {resp && resp.plan.step_count > 0 && (
        <div style={{ ...CONTROLS_ROW, marginTop: 12, paddingTop: 12, borderTop: '1px dashed var(--border-color)' }}>
          <label style={LBL_STYLE}>
            速度
            <select
              value={speed}
              onChange={(e) => setSpeed(Number(e.target.value))}
              style={{ ...INPUT_STYLE, width: 90 }}
              disabled={busy}
            >
              <option value={0.5}>0.5x（慢放）</option>
              <option value={1}>1x（原速）</option>
              <option value={2}>2x</option>
              <option value={5}>5x</option>
              <option value={10}>10x（最快）</option>
            </select>
          </label>
          <label style={{ ...LBL_STYLE, flexDirection: 'row', alignItems: 'center', gap: 6 }}>
            <input
              type="checkbox"
              checked={abortOnError}
              onChange={(e) => setAbortOnError(e.target.checked)}
              disabled={busy}
            />
            失败即停
          </label>
          <button
            type="button"
            onClick={() => void handleRealRun()}
            disabled={busy}
            style={BTN_DANGER}
          >
            {busy ? <Loader2 size={12} className="spin" /> : <Play size={12} />}
            真实执行
          </button>
        </div>
      )}
      {confirmDialog}
    </section>
  )
}

// ===========================================================================
// Plan 渲染
// ===========================================================================
function PlanView({ resp }: { resp: ReplayResponse }) {
  const { plan, execution, dry_run } = resp
  const resultByIndex = new Map<number, ReplayStepResult>()
  if (execution) {
    for (const r of execution.results) resultByIndex.set(r.index, r)
  }

  return (
    <div style={{ marginTop: 8 }}>
      <div style={SUMMARY_STYLE}>
        <span><strong>{plan.step_count}</strong> 步</span>
        <span style={DOT_STYLE}>•</span>
        <span>跳过 {plan.skipped_count} 个只读 action</span>
        {plan.duration_estimate_sec > 0 && (
          <>
            <span style={DOT_STYLE}>•</span>
            <span>原录制耗时 {plan.duration_estimate_sec.toFixed(1)}s</span>
          </>
        )}
        <span style={DOT_STYLE}>•</span>
        <span style={{ color: dry_run ? 'var(--text-muted)' : 'var(--accent-blue)' }}>
          {dry_run ? '预览模式' : execution?.completed ? '已完成' : `中断于第 ${execution?.aborted_at} 步`}
        </span>
      </div>

      {plan.step_count === 0 ? (
        <div style={EMPTY_STYLE}>
          没有可回放的步骤。审计里都是只读 action（screenshot/list_*）或没匹配到 session。
        </div>
      ) : (
        <div style={STEPS_LIST}>
          {plan.steps.map((s) => {
            const result = resultByIndex.get(s.index)
            return <StepRow key={s.index} step={s} result={result} dryRun={dry_run} />
          })}
        </div>
      )}
    </div>
  )
}

function StepRow({
  step,
  result,
  dryRun,
}: {
  step: ReplayStep
  result?: ReplayStepResult
  dryRun: boolean
}) {
  const status = (() => {
    if (dryRun) return 'pending'
    if (!result) return 'skipped'
    if (result.skipped_reason === 'dry_run') return 'pending'
    return result.ok ? 'ok' : 'fail'
  })()

  const Icon = status === 'ok'
    ? CheckCircle2
    : status === 'fail'
      ? XCircle
      : status === 'pending'
        ? FastForward
        : FastForward

  const color = status === 'ok'
    ? 'var(--accent-green)'
    : status === 'fail'
      ? 'var(--accent-red)'
      : 'var(--text-muted)'

  return (
    <div style={STEP_ROW}>
      <Icon size={12} style={{ color, flex: '0 0 auto' }} />
      <span style={{ fontFamily: 'ui-monospace, monospace', color: 'var(--accent-blue)' }}>
        {step.action}
      </span>
      <span style={STEP_PAYLOAD} title={JSON.stringify(step.payload)}>
        {summarizePayload(step.payload)}
      </span>
      {step.delay_before_ms > 0 && (
        <span style={DELAY_BADGE}>+{Math.round(step.delay_before_ms)}ms</span>
      )}
      {result?.error && (
        <span style={ERR_INLINE} title={result.error}>{truncate(result.error, 30)}</span>
      )}
    </div>
  )
}

// ===========================================================================
// Helpers
// ===========================================================================
function summarizePayload(payload: Record<string, unknown>): string {
  if ('coordinate' in payload && Array.isArray(payload.coordinate)) {
    return `(${(payload.coordinate as number[]).join(', ')})`
  }
  if ('text' in payload && typeof payload.text === 'string') {
    return JSON.stringify(truncate(payload.text, 24))
  }
  if ('scroll_direction' in payload) {
    return `${payload.scroll_direction} ×${payload.scroll_amount ?? 3}`
  }
  return ''
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n) + '…'
}

// ===========================================================================
// Styles
// ===========================================================================
const SECTION_STYLE: React.CSSProperties = {
  padding: 16,
  background: 'var(--bg-card)',
  border: '1px solid var(--border-color)',
  borderRadius: 8,
  marginTop: 12,
}

const HEADER_ROW: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  marginBottom: 8,
}

const HINT_STYLE: React.CSSProperties = {
  margin: '0 0 12px 0',
  fontSize: 12,
  color: 'var(--text-secondary)',
}

const CONTROLS_ROW: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-end',
  gap: 12,
  flexWrap: 'wrap',
}

const LBL_STYLE: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  fontSize: 11,
  color: 'var(--text-secondary)',
  gap: 4,
}

const INPUT_STYLE: React.CSSProperties = {
  fontSize: 13,
  padding: '4px 8px',
  border: '1px solid var(--border-color)',
  borderRadius: 4,
  background: 'var(--bg-card)',
  color: 'var(--text-color)',
  minWidth: 140,
}

const BTN_PRIMARY: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  padding: '6px 12px',
  fontSize: 13,
  border: '1px solid #0ea5e9',
  background: '#0ea5e9',
  color: '#fff',
  borderRadius: 4,
  cursor: 'pointer',
}

const BTN_DANGER: React.CSSProperties = {
  ...BTN_PRIMARY,
  border: '1px solid #ef4444',
  background: '#ef4444',
}

const ERR_STYLE: React.CSSProperties = {
  marginTop: 8,
  fontSize: 12,
  color: 'var(--accent-red)',
  padding: '6px 10px',
  background: 'rgba(239, 68, 68, 0.08)',
  borderRadius: 4,
}

const SUMMARY_STYLE: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  fontSize: 12,
  color: 'var(--text-secondary)',
  padding: '6px 0',
}

const DOT_STYLE: React.CSSProperties = { color: 'var(--text-muted)' }

const STEPS_LIST: React.CSSProperties = {
  marginTop: 4,
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
  maxHeight: 280,
  overflowY: 'auto',
  padding: 8,
  background: 'var(--bg-tertiary)',
  border: '1px solid var(--border-color, #eee)',
  borderRadius: 4,
}

const STEP_ROW: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  fontSize: 12,
  padding: '2px 4px',
  fontFamily: 'ui-monospace, monospace',
}

const STEP_PAYLOAD: React.CSSProperties = {
  color: 'var(--text-secondary)',
  flex: 1,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
}

const DELAY_BADGE: React.CSSProperties = {
  fontSize: 10,
  color: 'var(--text-muted)',
  background: 'var(--bg-tertiary)',
  padding: '0 4px',
  borderRadius: 2,
}

const ERR_INLINE: React.CSSProperties = {
  fontSize: 11,
  color: 'var(--accent-red)',
  fontFamily: 'ui-monospace, monospace',
}

const EMPTY_STYLE: React.CSSProperties = {
  marginTop: 8,
  padding: 12,
  fontSize: 12,
  color: 'var(--text-secondary)',
  background: 'var(--bg-tertiary)',
  border: '1px dashed var(--border-color)',
  borderRadius: 4,
  textAlign: 'center',
}
