/**
 * Computer Use 设置面板（M4b）。
 *
 * 三段：
 * 1. 顶部：总开关 + 实时状态徽章（platform_supported / lock_holder / 最近活动）
 * 2. 中部：限流（per-second / per-minute）+ 黑名单（chip 多选）
 * 3. 底部：审计列表（最近 50 条）+ "立即停止" 按钮
 *
 * 数据流：
 * - mount 拉一次 policy + status
 * - 每 3 秒轮询 status（仅在 enabled 时；关掉就停轮询省电）
 * - 改任何字段 → 立即 PUT；后端会落盘
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  Eye,
  Loader2,
  Monitor,
  ShieldAlert,
  ShieldCheck,
  StopCircle,
  Terminal,
  X,
} from 'lucide-react'
import {
  type AuditRecord,
  type ComputerUsePolicy,
  type ComputerUseStatus,
  type PolicyResponse,
  getComputerUseAudit,
  getComputerUsePolicy,
  getComputerUseStatus,
  releaseComputerUseLock,
  updateComputerUsePolicy,
} from '../../api/computerUse'
import ComputerUseReplayPanel from './ComputerUseReplayPanel'
import { startPolling } from '../../lib/pollingLoop'

// ============================================================================
// 主组件
// ============================================================================
export default function ComputerUseSettings() {
  const [policyResp, setPolicyResp] = useState<PolicyResponse | null>(null)
  const [status, setStatus] = useState<ComputerUseStatus | null>(null)
  const [audit, setAudit] = useState<AuditRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saveErr, setSaveErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [releasing, setReleasing] = useState(false)

  // 初始拉
  const refresh = useCallback(async () => {
    setLoadErr(null)
    try {
      const [p, s, a] = await Promise.all([
        getComputerUsePolicy(),
        getComputerUseStatus(),
        getComputerUseAudit(50),
      ])
      setPolicyResp(p)
      setStatus(s)
      setAudit(a.items)
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // 每 3s 轮询 status（只在 enabled 时；省电省日志）
  const enabled = policyResp?.policy.enabled ?? false
  useEffect(() => {
    if (!enabled) return
    return startPolling(async () => {
      try {
        setStatus(await getComputerUseStatus())
      } catch { /* swallow，下次再试 */ }
    }, 3000, { immediate: false })
  }, [enabled])

  // 通用 PUT
  const patchPolicy = useCallback(async (patch: Partial<ComputerUsePolicy>) => {
    setSaving(true)
    setSaveErr(null)
    try {
      const out = await updateComputerUsePolicy(patch)
      setPolicyResp((prev) => (prev ? { ...prev, policy: out.policy } : prev))
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }, [])

  const handleRelease = useCallback(async () => {
    setReleasing(true)
    try {
      await releaseComputerUseLock()
      await refresh()
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e))
    } finally {
      setReleasing(false)
    }
  }, [refresh])

  if (loading) {
    return (
      <div style={LOADING_STYLE}>
        <Loader2 size={16} className="spin" /> <span>加载 Computer Use 配置…</span>
      </div>
    )
  }

  if (loadErr || !policyResp) {
    return (
      <div style={ERROR_STYLE}>
        <AlertCircle size={16} />
        <span>加载失败：{loadErr ?? '未知错误'}</span>
        <button type="button" onClick={() => void refresh()} style={BTN_GHOST}>
          重试
        </button>
      </div>
    )
  }

  const { policy, meta } = policyResp
  const platformOk = meta.platform_supported
  const lockHeld = !!status?.lock_holder
  const recentActions = status?.activity.actions_last_5s ?? 0

  return (
    <div style={ROOT_STYLE}>
      {/* ====== 顶部 Hero：标题 + 总开关 + 状态 ====== */}
      <header style={HERO_STYLE}>
        <div style={HERO_ROW_STYLE}>
          <div style={HERO_TITLE_STYLE}>
            <Monitor size={18} />
            <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>Computer Use</h2>
            <span style={SUBTITLE_STYLE}>让 AI 看屏幕、点鼠标、敲键盘</span>
          </div>
          <ToggleSwitch
            checked={policy.enabled}
            onChange={(v) => void patchPolicy({ enabled: v })}
            disabled={!platformOk || saving}
            label={policy.enabled ? '已启用' : '未启用'}
          />
        </div>

        <p style={HERO_DESC_STYLE}>
          一旦启用，模型可以截屏、点击、键入文字、操作剪贴板。每个动作都会被审计；锁住时同一时刻只允许一个会话控制。
          <strong> 默认关闭，需要你显式打开</strong>。
        </p>

        {/* 平台支持徽章 */}
        <div style={PILL_ROW_STYLE}>
          <Pill ok={platformOk} icon={<Monitor size={12} />}>
            {meta.platform} {platformOk ? '已支持' : '暂不支持（M3 加 macOS/Linux）'}
          </Pill>
          {policy.enabled && (
            <>
              <Pill ok={!lockHeld} icon={lockHeld ? <ShieldAlert size={12} /> : <ShieldCheck size={12} />}>
                {lockHeld
                  ? `控制中：${status?.lock_holder?.session_id ?? '—'} · ${
                      status?.lock_holder?.held_seconds.toFixed(0) ?? 0
                    }s`
                  : '空闲'}
              </Pill>
              <Pill ok icon={<Clock size={12} />}>
                近 5s {recentActions} 次动作
              </Pill>
            </>
          )}
        </div>

        {/* 立即停止按钮（仅锁被占时显示） */}
        {lockHeld && (
          <div style={{ marginTop: 12 }}>
            <button
              type="button"
              onClick={() => void handleRelease()}
              disabled={releasing}
              style={BTN_DANGER}
            >
              <StopCircle size={14} />
              {releasing ? '正在停止…' : '立即停止 / 强制释放锁'}
            </button>
          </div>
        )}
      </header>

      {/* ====== 限流 ====== */}
      <section style={SECTION_STYLE}>
        <h3 style={SECTION_TITLE_STYLE}>速率限制</h3>
        <div style={ROW_2COL_STYLE}>
          <NumberField
            label="每秒最多动作"
            hint="同一会话每秒不能超过这么多 click/type 等动作（防止失控刷屏）"
            value={policy.max_per_second}
            min={1}
            max={100}
            disabled={saving}
            onCommit={(v) => void patchPolicy({ max_per_second: v })}
          />
          <NumberField
            label="每分钟最多动作"
            hint="60 秒滑动窗口；防止长时间持续点击"
            value={policy.max_per_minute}
            min={1}
            max={10000}
            disabled={saving}
            onCommit={(v) => void patchPolicy({ max_per_minute: v })}
          />
        </div>
      </section>

      {/* ====== 视觉闭环（M5）====== */}
      <section style={SECTION_STYLE}>
        <div style={SECTION_HEADER_ROW}>
          <h3 style={{ ...SECTION_TITLE_STYLE, margin: 0, display: 'flex', alignItems: 'center', gap: 6 }}>
            <Eye size={14} /> 视觉闭环
          </h3>
          <ToggleSwitch
            checked={policy.auto_screenshot_after_mutate}
            onChange={(v) => void patchPolicy({ auto_screenshot_after_mutate: v })}
            disabled={saving}
            label={policy.auto_screenshot_after_mutate ? '已开启' : '已关闭'}
          />
        </div>
        <p style={HINT_STYLE}>
          每次 click / type / key / scroll 后自动再截一张，与上一张算差异区域，
          连同新图一起塞回 LLM 上下文 —— 让模型能"看到"自己的操作是否生效，自我校正失误。
          <strong> 相对原生 Anthropic computer_use 工具的核心差异化能力。</strong>
          关掉可省 token，但模型只能"瞎点"。
        </p>
      </section>

      {/* ====== 黑名单 ====== */}
      <section style={SECTION_STYLE}>
        <h3 style={SECTION_TITLE_STYLE}>禁用动作</h3>
        <p style={HINT_STYLE}>
          勾上的动作将被永远拒绝（即使总开关开着）。比如担心 AI 乱敲键盘可禁
          <code> type / key</code>，只让它截图和点鼠标。
        </p>
        <div style={CHIPS_STYLE}>
          {meta.all_actions.map((act) => {
            const blocked = policy.blocked_actions.includes(act)
            return (
              <button
                key={act}
                type="button"
                disabled={saving}
                onClick={() => {
                  const next = blocked
                    ? policy.blocked_actions.filter((a) => a !== act)
                    : [...policy.blocked_actions, act]
                  void patchPolicy({ blocked_actions: next })
                }}
                style={blocked ? CHIP_BLOCKED : CHIP_ALLOWED}
                title={blocked ? '点击移除黑名单' : '点击加入黑名单'}
              >
                {blocked ? <X size={11} /> : <CheckCircle2 size={11} />}
                {act}
              </button>
            )
          })}
        </div>
      </section>

      {/* ====== 审计 ====== */}
      <section style={SECTION_STYLE}>
        <div style={SECTION_HEADER_ROW}>
          <h3 style={{ ...SECTION_TITLE_STYLE, margin: 0 }}>最近活动</h3>
          <button
            type="button"
            onClick={() => void refresh()}
            style={BTN_GHOST}
          >
            刷新
          </button>
        </div>
        {audit.length === 0 ? (
          <div style={EMPTY_STYLE}>
            <Terminal size={14} /> 还没有任何 Computer Use 活动。
          </div>
        ) : (
          <div style={AUDIT_LIST_STYLE}>
            {audit.slice().reverse().map((rec, idx) => (
              <AuditRow key={idx} rec={rec} />
            ))}
          </div>
        )}
      </section>

      {/* ====== 回放（M7） ====== */}
      <ComputerUseReplayPanel />

      {saveErr && (
        <div style={ERROR_STYLE}>
          <AlertCircle size={14} /> 保存失败：{saveErr}
        </div>
      )}
    </div>
  )
}

// ============================================================================
// 子组件
// ============================================================================
function ToggleSwitch({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
  label?: string
}) {
  return (
    <label
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.6 : 1,
      }}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        style={{ display: 'none' }}
      />
      <span
        style={{
          width: 36,
          height: 20,
          borderRadius: 10,
          background: checked ? 'var(--primary-color)' : 'var(--border-color)',
          position: 'relative',
          transition: 'background 120ms',
        }}
      >
        <span
          style={{
            position: 'absolute',
            top: 2,
            left: checked ? 18 : 2,
            width: 16,
            height: 16,
            borderRadius: 8,
            background: '#fff',
            transition: 'left 120ms',
            boxShadow: '0 1px 2px rgba(0,0,0,0.2)',
          }}
        />
      </span>
      {label && <span style={{ fontSize: 13, fontWeight: 500 }}>{label}</span>}
    </label>
  )
}

function Pill({ ok, icon, children }: { ok: boolean; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        fontSize: 11,
        padding: '3px 8px',
        borderRadius: 999,
        background: ok ? 'var(--bg-tertiary)' : 'rgba(239, 68, 68, 0.12)',
        color: ok ? 'var(--text-secondary)' : 'var(--accent-red)',
        border: '1px solid var(--border-color)',
      }}
    >
      {icon} {children}
    </span>
  )
}

function NumberField({
  label,
  hint,
  value,
  min,
  max,
  disabled,
  onCommit,
}: {
  label: string
  hint?: string
  value: number
  min: number
  max: number
  disabled?: boolean
  onCommit: (v: number) => void
}) {
  const [draft, setDraft] = useState(String(value))
  const lastValue = useRef(value)

  useEffect(() => {
    // 外部 value 变化时（保存成功）同步 draft
    if (value !== lastValue.current) {
      setDraft(String(value))
      lastValue.current = value
    }
  }, [value])

  const commit = () => {
    const num = Number(draft)
    if (!Number.isFinite(num)) {
      setDraft(String(value))
      return
    }
    const clamped = Math.max(min, Math.min(max, Math.round(num)))
    setDraft(String(clamped))
    if (clamped !== value) onCommit(clamped)
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <label style={{ fontSize: 12, fontWeight: 500 }}>{label}</label>
      {hint && (
        <span style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{hint}</span>
      )}
      <input
        type="number"
        min={min}
        max={max}
        value={draft}
        disabled={disabled}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
        }}
        style={{
          padding: '6px 10px',
          fontSize: 13,
          borderRadius: 6,
          border: '1px solid var(--border-color)',
          background: 'var(--bg-card)',
          color: 'var(--text-color)',
          width: 100,
        }}
      />
    </div>
  )
}

function AuditRow({ rec }: { rec: AuditRecord }) {
  const time = new Date(rec.ts * 1000).toLocaleTimeString()
  const ok = rec.ok
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '70px 1fr auto',
        gap: 8,
        padding: '6px 10px',
        fontSize: 12,
        borderBottom: '1px solid var(--border-color)',
        background: ok ? 'transparent' : 'rgba(239, 68, 68, 0.04)',
      }}
    >
      <span style={{ color: 'var(--text-secondary)', fontFamily: 'monospace' }}>{time}</span>
      <span>
        <code style={{ fontSize: 11, padding: '1px 5px', background: 'var(--bg-tertiary)', borderRadius: 3 }}>
          {rec.action}
        </code>
        {!ok && <span style={{ color: 'var(--accent-red)', marginLeft: 6 }}>· {rec.error_code ?? 'error'}</span>}
        <span style={{ color: 'var(--text-secondary)', marginLeft: 6 }}>
          {rec.session_id}
        </span>
      </span>
      <span style={{ color: 'var(--text-secondary)', fontFamily: 'monospace' }}>
        {rec.duration_ms ? `${rec.duration_ms.toFixed(0)}ms` : ''}
      </span>
    </div>
  )
}

// ============================================================================
// 内联样式（参考其他 settings 面板的简洁风）
// ============================================================================
const ROOT_STYLE: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 20,
  padding: '20px 24px',
  maxWidth: 880,
}
const HERO_STYLE: React.CSSProperties = {
  padding: 16,
  background: 'var(--bg-card)',
  border: '1px solid var(--border-color)',
  borderRadius: 8,
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
}
const HERO_ROW_STYLE: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 16,
}
const HERO_TITLE_STYLE: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
}
const SUBTITLE_STYLE: React.CSSProperties = {
  fontSize: 12,
  color: 'var(--text-secondary)',
}
const HERO_DESC_STYLE: React.CSSProperties = {
  margin: 0,
  fontSize: 13,
  color: 'var(--text-secondary)',
  lineHeight: 1.6,
}
const PILL_ROW_STYLE: React.CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: 6,
  marginTop: 6,
}
const SECTION_STYLE: React.CSSProperties = {
  padding: 16,
  background: 'var(--bg-card)',
  border: '1px solid var(--border-color)',
  borderRadius: 8,
}
const SECTION_TITLE_STYLE: React.CSSProperties = {
  margin: '0 0 8px 0',
  fontSize: 14,
  fontWeight: 600,
}
const SECTION_HEADER_ROW: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  marginBottom: 8,
}
const HINT_STYLE: React.CSSProperties = {
  margin: '0 0 10px 0',
  fontSize: 12,
  color: 'var(--text-secondary)',
  lineHeight: 1.5,
}
const ROW_2COL_STYLE: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: '1fr 1fr',
  gap: 16,
}
const CHIPS_STYLE: React.CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: 6,
}
const CHIP_BASE: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
  fontSize: 11,
  padding: '4px 8px',
  borderRadius: 4,
  border: '1px solid var(--border-color)',
  cursor: 'pointer',
  fontFamily: 'monospace',
}
const CHIP_ALLOWED: React.CSSProperties = {
  ...CHIP_BASE,
  background: 'var(--bg-tertiary)',
  color: 'var(--text-secondary)',
}
const CHIP_BLOCKED: React.CSSProperties = {
  ...CHIP_BASE,
  background: 'rgba(239, 68, 68, 0.12)',
  borderColor: 'rgba(239, 68, 68, 0.5)',
  color: 'var(--accent-red)',
}
const AUDIT_LIST_STYLE: React.CSSProperties = {
  maxHeight: 280,
  overflowY: 'auto',
  border: '1px solid var(--border-color)',
  borderRadius: 6,
}
const EMPTY_STYLE: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: 16,
  fontSize: 12,
  color: 'var(--text-secondary)',
  background: 'var(--bg-tertiary)',
  borderRadius: 6,
  border: '1px dashed var(--border-color)',
}
const LOADING_STYLE: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: 24,
  fontSize: 13,
  color: 'var(--text-secondary)',
}
const ERROR_STYLE: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: 12,
  fontSize: 13,
  color: 'var(--accent-red)',
  background: 'rgba(239, 68, 68, 0.08)',
  border: '1px solid rgba(239, 68, 68, 0.3)',
  borderRadius: 6,
}
const BTN_GHOST: React.CSSProperties = {
  padding: '4px 10px',
  fontSize: 12,
  borderRadius: 4,
  border: '1px solid var(--border-color)',
  background: 'transparent',
  color: 'var(--text-color)',
  cursor: 'pointer',
}
const BTN_DANGER: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  padding: '8px 14px',
  fontSize: 13,
  fontWeight: 500,
  borderRadius: 6,
  border: '1px solid var(--accent-red)',
  background: 'var(--accent-red)',
  color: '#fff',
  cursor: 'pointer',
}
