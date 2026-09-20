import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertCircle, CheckCircle2, Loader2, RotateCcw, ShieldCheck } from 'lucide-react'
import {
  getGuardianConfig,
  setGuardianConfig,
  type GuardianConfig,
  type GuardianConfigResponse,
} from '../../api/guardian'
import GuardianConfigPanel from '../GuardianConfigPanel'

interface PermissionsSettingsProps {
  /** 由外部传入：从主聊天跳过来时聚焦该 agent；为 null/undefined 则停在「全局默认」 */
  focusAgentId?: string | null
}

/**
 * 设置 → 权限 tab。
 *
 * 顶部 hero（介绍 + 摘要）→ 中部 panel（左 scope 列表 + 右规则详情）→ 底部 sticky action bar。
 * 表单与服务端值的 diff 用 JSON.stringify 浅比，足够（结构稳定 + 不大）。
 */
export default function PermissionsSettings({ focusAgentId }: PermissionsSettingsProps) {
  const [serverState, setServerState] = useState<GuardianConfigResponse | null>(null)
  const [form, setForm] = useState<GuardianConfig>({})
  const [activeAgentId, setActiveAgentId] = useState<string | null>(focusAgentId ?? null)

  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)
  const [savedToastVisible, setSavedToastVisible] = useState(false)

  // 读：进 tab 立刻拉一次
  const refresh = useCallback(async () => {
    setLoading(true)
    setLoadErr(null)
    try {
      const data = await getGuardianConfig()
      setServerState(data)
      setForm(data.config ?? {})
    } catch (exc) {
      setLoadErr(exc instanceof Error ? exc.message : '读取权限配置失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  // 外部 focusAgentId 变化时（用户重新点权限按钮）→ 切到对应 agent
  // 用 ref 避免初次 mount 时被覆盖（mount 已经在 state 初始值里处理过）
  const focusAppliedRef = useRef(false)
  useEffect(() => {
    if (!focusAgentId) return
    if (focusAppliedRef.current) {
      setActiveAgentId(focusAgentId)
    } else {
      focusAppliedRef.current = true
    }
  }, [focusAgentId])

  // 服务端拉到后，如 focusAgentId 不在 agents 列表里就回退到全局默认
  useEffect(() => {
    if (!serverState || !activeAgentId) return
    const exists = serverState.agents.some((a) => a.id === activeAgentId)
    if (!exists) setActiveAgentId(null)
  }, [serverState, activeAgentId])

  // 是否有未保存修改
  const hasDirty = useMemo(() => {
    if (!serverState) return false
    return JSON.stringify(form) !== JSON.stringify(serverState.config ?? {})
  }, [form, serverState])

  // 摘要：N 个角色已自定义
  const overrideAgentCount = useMemo(() => {
    const overrides = form.overrides ?? {}
    return Object.keys(overrides).filter((k) => Object.keys(overrides[k] ?? {}).length > 0).length
  }, [form])

  const handleSave = async () => {
    setSaving(true)
    setSaveErr(null)
    try {
      const data = await setGuardianConfig(form)
      setServerState(data)
      setForm(data.config ?? {})
      setSavedToastVisible(true)
      setTimeout(() => setSavedToastVisible(false), 2500)
    } catch (exc) {
      setSaveErr(exc instanceof Error ? exc.message : '保存权限配置失败')
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => {
    if (serverState) {
      setForm(serverState.config ?? {})
      setSaveErr(null)
    }
  }

  return (
    <div className="settings-permissions">
      {/* ======= 顶部 hero ======= */}
      <header className="settings-permissions-hero">
        <div className="settings-permissions-hero-icon">
          <ShieldCheck size={20} />
        </div>
        <div className="settings-permissions-hero-text">
          <h2>权限管理</h2>
          <p>
            为每个角色细化「读隐私 / 写其他角色 / 删除 / 执行命令 / 写后端代码」5 条规则。
            <br />
            每个角色自己的工作目录始终可读可写，规则只约束跨角色与系统级动作。
          </p>
        </div>
        <div className="settings-permissions-hero-stats">
          <Stat label="可配角色" value={serverState?.agents.length ?? '—'} />
          <Stat label="已自定义" value={overrideAgentCount > 0 ? `${overrideAgentCount} 个` : '0'} active={overrideAgentCount > 0} />
        </div>
      </header>

      {/* ======= 中部主体 ======= */}
      <section className="settings-permissions-body">
        {loading && (
          <div className="settings-permissions-loading">
            <Loader2 size={14} className="spin" /> 加载权限配置中…
          </div>
        )}
        {loadErr && (
          <div className="settings-permissions-load-err">
            <AlertCircle size={14} /> {loadErr}
            <button type="button" className="guardian-bar-btn" onClick={refresh}>重试</button>
          </div>
        )}
        {!loading && !loadErr && (
          <GuardianConfigPanel
            form={form}
            setForm={setForm}
            serverState={serverState}
            activeAgentId={activeAgentId}
            onChangeActiveAgent={setActiveAgentId}
            variant="page"
          />
        )}
      </section>

      {/* ======= 底部 sticky action bar ======= */}
      {!loading && !loadErr && (
        <footer className="settings-permissions-actions" data-dirty={hasDirty || undefined}>
          <div className="settings-permissions-actions-status">
            {saveErr ? (
              <span className="settings-permissions-status err">
                <AlertCircle size={14} /> {saveErr}
              </span>
            ) : hasDirty ? (
              <span className="settings-permissions-status dirty">
                有未保存的修改
              </span>
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
              onClick={handleSave}
              disabled={!hasDirty || saving}
            >
              {saving ? <Loader2 size={13} className="spin" /> : <CheckCircle2 size={13} />}
              {saving ? '保存中…' : '保存并立即生效'}
            </button>
          </div>
        </footer>
      )}

      {savedToastVisible && (
        <div className="settings-permissions-toast">
          <CheckCircle2 size={14} /> 已保存，下次工具调用立即生效
        </div>
      )}
    </div>
  )
}

function Stat({ label, value, active }: { label: string; value: number | string; active?: boolean }) {
  return (
    <div className={`settings-permissions-stat ${active ? 'active' : ''}`}>
      <span className="settings-permissions-stat-value">{value}</span>
      <span className="settings-permissions-stat-label">{label}</span>
    </div>
  )
}
