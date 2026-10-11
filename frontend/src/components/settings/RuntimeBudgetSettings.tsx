import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertCircle, CheckCircle2, Gauge, Loader2, RotateCcw } from 'lucide-react'
import {
  getRuntimeBudget,
  setRuntimeBudget,
  RUNTIME_BUDGET_PRESETS,
  type RuntimeBudgetFields,
  type RuntimeBudgetResponse,
} from '../../api/runtime'

/**
 * 设置 → 运行预算 tab。
 *
 * 全局护栏（工具次数 / 结果长度 / 墙钟时间 / 输出 token / 流空闲超时），
 * 原挂在聊天输入框底栏，现迁到设置。下次发送对话生效。
 */
export default function RuntimeBudgetSettings() {
  const [serverState, setServerState] = useState<RuntimeBudgetResponse | null>(null)
  const [form, setForm] = useState<RuntimeBudgetFields>(RUNTIME_BUDGET_PRESETS.balanced.values)

  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)
  const [savedToastVisible, setSavedToastVisible] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    setLoadErr(null)
    try {
      const data = await getRuntimeBudget()
      setServerState(data)
      setForm({ ...data.effective })
    } catch (exc) {
      setLoadErr(exc instanceof Error ? exc.message : '读取运行预算失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const hasDirty = useMemo(() => {
    if (!serverState) return false
    return JSON.stringify(form) !== JSON.stringify(serverState.effective)
  }, [form, serverState])

  const activePreset = useMemo(() => {
    for (const key of Object.keys(RUNTIME_BUDGET_PRESETS) as Array<keyof typeof RUNTIME_BUDGET_PRESETS>) {
      const v = RUNTIME_BUDGET_PRESETS[key].values
      if (
        form.max_tool_calls === v.max_tool_calls &&
        form.max_tool_rounds === v.max_tool_rounds &&
        form.max_tool_output_chars === v.max_tool_output_chars &&
        form.max_wall_clock_seconds === v.max_wall_clock_seconds &&
        form.max_output_tokens === v.max_output_tokens &&
        form.stream_idle_timeout_ms === v.stream_idle_timeout_ms &&
        Math.abs(form.warn_threshold_ratio - v.warn_threshold_ratio) < 1e-6
      ) {
        return key
      }
    }
    return null
  }, [form])

  const handleSave = async () => {
    setSaving(true)
    setSaveErr(null)
    try {
      const data = await setRuntimeBudget(form)
      setServerState(data)
      setForm({ ...data.effective })
      setSavedToastVisible(true)
      setTimeout(() => setSavedToastVisible(false), 2500)
    } catch (exc) {
      setSaveErr(exc instanceof Error ? exc.message : '保存运行预算失败')
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => {
    if (serverState) {
      setForm({ ...serverState.effective })
      setSaveErr(null)
    }
  }

  const applyPreset = (preset: keyof typeof RUNTIME_BUDGET_PRESETS) => {
    setForm({ ...RUNTIME_BUDGET_PRESETS[preset].values })
  }

  const userConfig = serverState?.config ?? {}
  const fieldHint = (key: keyof RuntimeBudgetFields) => {
    if (userConfig[key] != null) return null
    return <span className="label-hint">未配置 · 走 env / 默认</span>
  }

  const setNumber = (key: keyof RuntimeBudgetFields, value: number, min = 1) => {
    setForm((f) => ({ ...f, [key]: Math.max(min, Math.floor(value)) }))
  }

  return (
    <div className="settings-permissions">
      <header className="settings-permissions-hero">
        <div className="settings-permissions-hero-icon">
          <Gauge size={20} />
        </div>
        <div className="settings-permissions-hero-text">
          <h2>运行预算</h2>
          <p>
            限制单次对话的工具调用次数、结果总长度、墙钟时间与输出 token。
            <br />
            全局生效（不分会话 / 角色）。保存后<strong>下次发送</strong>对话生效。
          </p>
        </div>
        <div className="settings-permissions-hero-stats">
          <Stat
            label="当前档位"
            value={activePreset ? RUNTIME_BUDGET_PRESETS[activePreset].label : '自定义'}
            active={activePreset === 'deep'}
          />
          <Stat label="工具次数" value={form.max_tool_calls} />
          <Stat label="总用时" value={`${form.max_wall_clock_seconds}s`} />
        </div>
      </header>

      <section className="settings-permissions-body">
        {loading && (
          <div className="settings-permissions-loading">
            <Loader2 size={14} className="spin" /> 加载运行预算中…
          </div>
        )}
        {loadErr && (
          <div className="settings-permissions-load-err">
            <AlertCircle size={14} /> {loadErr}
            <button type="button" className="guardian-bar-btn" onClick={() => void refresh()}>
              重试
            </button>
          </div>
        )}
        {!loading && !loadErr && (
          <div className="settings-runtime-budget-form custom-form">
            <div className="form-group">
              <label style={{ textAlign: 'center' }}>预设档位</label>
              <div className="dialog-actions" style={{ justifyContent: 'center', paddingTop: 0 }}>
                {(Object.keys(RUNTIME_BUDGET_PRESETS) as Array<keyof typeof RUNTIME_BUDGET_PRESETS>).map((key) => {
                  const p = RUNTIME_BUDGET_PRESETS[key]
                  const active = activePreset === key
                  return (
                    <button
                      key={key}
                      type="button"
                      className="btn-test"
                      onClick={() => applyPreset(key)}
                      title={p.description}
                      style={
                        active
                          ? {
                              background: 'var(--primary-color)',
                              borderColor: 'var(--primary-color)',
                              color: '#fff',
                            }
                          : undefined
                      }
                    >
                      {p.label}
                    </button>
                  )
                })}
              </div>
            </div>

            <div className="form-group">
              <label style={{ opacity: 0.6, fontSize: '1em', marginTop: 8 }}>── 输入控制 ──</label>
            </div>

            <div className="form-group">
              <label>
                单次工具调用次数 <span className="label-hint">一次回复总共能用几次工具</span>{' '}
                {fieldHint('max_tool_calls')}
              </label>
              <input
                type="number"
                min={1}
                step={1}
                value={form.max_tool_calls}
                onChange={(e) => {
                  const v = Number(e.target.value)
                  if (Number.isFinite(v)) setNumber('max_tool_calls', v)
                }}
              />
            </div>

            <div className="form-group">
              <label>
                单次工具循环轮数 <span className="label-hint">反复「调工具→看结果」几次</span>{' '}
                {fieldHint('max_tool_rounds')}
              </label>
              <input
                type="number"
                min={1}
                step={1}
                value={form.max_tool_rounds}
                onChange={(e) => {
                  const v = Number(e.target.value)
                  if (Number.isFinite(v)) setNumber('max_tool_rounds', v)
                }}
              />
            </div>

            <div className="form-group">
              <label>
                工具结果总长度（字符） <span className="label-hint">工具读到的内容总共多长</span>{' '}
                {fieldHint('max_tool_output_chars')}
              </label>
              <input
                type="number"
                min={1}
                step={10000}
                value={form.max_tool_output_chars}
                onChange={(e) => {
                  const v = Number(e.target.value)
                  if (Number.isFinite(v)) setNumber('max_tool_output_chars', v)
                }}
              />
            </div>

            <div className="form-group">
              <label style={{ opacity: 0.6, fontSize: '1em', marginTop: 8 }}>── 输出控制 ──</label>
            </div>

            <div className="form-group">
              <label>
                单轮输出上限（token）{' '}
                <span className="label-hint">常见模型输出上限 32k+，默认已给足；模型报 max_tokens 超限时再下调</span>{' '}
                {fieldHint('max_output_tokens')}
              </label>
              <input
                type="number"
                min={1}
                step={1024}
                value={form.max_output_tokens}
                onChange={(e) => {
                  const v = Number(e.target.value)
                  if (Number.isFinite(v)) setNumber('max_output_tokens', v)
                }}
              />
            </div>

            <div className="form-group">
              <label style={{ opacity: 0.6, fontSize: '1em', marginTop: 8 }}>── 时间限制 ──</label>
            </div>

            <div className="form-group">
              <label>
                总用时上限（秒） <span className="label-hint">回复到结束最多用时</span>{' '}
                {fieldHint('max_wall_clock_seconds')}
              </label>
              <input
                type="number"
                min={1}
                step={60}
                value={form.max_wall_clock_seconds}
                onChange={(e) => {
                  const v = Number(e.target.value)
                  if (Number.isFinite(v)) setNumber('max_wall_clock_seconds', v)
                }}
              />
            </div>

            <div className="form-group">
              <label>
                流式间隔上限（秒）{' '}
                <span className="label-hint">模型沉默时间，默认 600 秒，思考更久的模型可再调高</span>{' '}
                {fieldHint('stream_idle_timeout_ms')}
              </label>
              <input
                type="number"
                min={1}
                step={30}
                value={Math.round(form.stream_idle_timeout_ms / 1000)}
                onChange={(e) => {
                  const v = Number(e.target.value)
                  if (Number.isFinite(v)) {
                    setForm((f) => ({
                      ...f,
                      stream_idle_timeout_ms: Math.max(1, Math.floor(v)) * 1000,
                    }))
                  }
                }}
              />
            </div>

            <div className="form-group">
              <label>
                告警阈值（{Math.round(form.warn_threshold_ratio * 100)}%）{' '}
                <span className="label-hint">工具次数 / 工具结果 / 总用时 任一用到比例就提醒收尾</span>{' '}
                {fieldHint('warn_threshold_ratio')}
              </label>
              <input
                type="range"
                min={0.5}
                max={0.98}
                step={0.05}
                value={form.warn_threshold_ratio}
                onChange={(e) =>
                  setForm((f) => ({ ...f, warn_threshold_ratio: Number(e.target.value) }))
                }
                style={{ width: '100%' }}
              />
            </div>
          </div>
        )}
      </section>

      {!loading && !loadErr && (
        <footer className="settings-permissions-actions" data-dirty={hasDirty || undefined}>
          <div className="settings-permissions-actions-status">
            {saveErr ? (
              <span className="settings-permissions-status err">
                <AlertCircle size={14} /> {saveErr}
              </span>
            ) : hasDirty ? (
              <span className="settings-permissions-status dirty">有未保存的修改</span>
            ) : (
              <span className="settings-permissions-status idle">
                <CheckCircle2 size={14} /> 已与服务端同步
              </span>
            )}
          </div>
          <div className="settings-permissions-actions-btns">
            <button
              type="button"
              className="guardian-bar-btn"
              onClick={handleReset}
              disabled={!hasDirty || saving}
            >
              <RotateCcw size={13} /> 撤销修改
            </button>
            <button
              type="button"
              className="guardian-bar-btn primary"
              onClick={() => void handleSave()}
              disabled={!hasDirty || saving}
            >
              {saving ? <Loader2 size={13} className="spin" /> : <CheckCircle2 size={13} />}
              {saving ? '保存中…' : '保存 · 下次发送生效'}
            </button>
          </div>
        </footer>
      )}

      {savedToastVisible && (
        <div className="settings-permissions-toast">
          <CheckCircle2 size={14} /> 已保存，下次发送对话生效
        </div>
      )}
    </div>
  )
}

function Stat({
  label,
  value,
  active,
}: {
  label: string
  value: number | string
  active?: boolean
}) {
  return (
    <div className={`settings-permissions-stat ${active ? 'active' : ''}`}>
      <span className="settings-permissions-stat-value">{value}</span>
      <span className="settings-permissions-stat-label">{label}</span>
    </div>
  )
}
