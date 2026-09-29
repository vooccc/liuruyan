import { type Dispatch, type SetStateAction } from 'react'
import { ShieldCheck, ShieldAlert, FileText, Globe, Crown } from 'lucide-react'
import {
  GUARDIAN_SWITCH_KEYS,
  GUARDIAN_SWITCH_LABELS,
  type GuardianAction,
  type GuardianConfig,
  type GuardianConfigResponse,
  type GuardianRuleSet,
  type GuardianSwitchKey,
} from '../api/guardian'
import { useConfirmDialog } from './ConfirmDialog'

export const GUARDIAN_DEFAULT_SCOPE = '__default__'

export interface GuardianConfigPanelProps {
  form: GuardianConfig
  setForm: Dispatch<SetStateAction<GuardianConfig>>
  serverState: GuardianConfigResponse | null
  /** null = 全局默认；string = agent id */
  activeAgentId: string | null
  onChangeActiveAgent: (next: string | null) => void
  /** dialog: 紧凑布局；page: 宽松，让卡片呼吸 */
  variant?: 'dialog' | 'page'
}

/**
 * Guardian 权限配置共享面板
 *
 * 视觉：左侧"全局默认 + 角色"两段式列表，右侧详情卡片化。
 * 规则用 3 段段控件（继承 / 禁止 / 允许）替代原 select，risky 项额外加红边警示。
 */
export default function GuardianConfigPanel({
  form,
  setForm,
  serverState,
  activeAgentId,
  onChangeActiveAgent,
  variant = 'page',
}: GuardianConfigPanelProps) {
  const { confirm: confirmDialog, dialog: confirmDialogNode } = useConfirmDialog()

  const agents = serverState?.agents ?? []
  const effectiveDefaults: GuardianRuleSet | null = serverState?.effective_defaults ?? null

  const isDefaultScope = activeAgentId === null
  const activeAgent = isDefaultScope ? null : agents.find((a) => a.id === activeAgentId)
  const activeAgentName = activeAgent?.name ?? activeAgentId ?? ''

  const currentDefaults: Partial<GuardianRuleSet> = form.defaults ?? {}
  const currentOverride: Partial<Pick<GuardianRuleSet, GuardianSwitchKey>> = isDefaultScope
    ? {}
    : form.overrides?.[activeAgentId!] ?? {}

  // —— 写入：默认 / override —— //
  const setDefaultSwitch = (key: GuardianSwitchKey, value: GuardianAction) => {
    setForm((prev) => ({
      ...prev,
      defaults: { ...(prev.defaults ?? {}), [key]: value },
    }))
  }

  const setOverrideSwitch = (
    agentId: string,
    key: GuardianSwitchKey,
    value: GuardianAction | null,
  ) => {
    setForm((prev) => {
      const overrides = { ...(prev.overrides ?? {}) }
      const current: Partial<Pick<GuardianRuleSet, GuardianSwitchKey>> = {
        ...(overrides[agentId] ?? {}),
      }
      if (value === null) {
        delete current[key]
      } else {
        current[key] = value
      }
      if (Object.keys(current).length === 0) {
        delete overrides[agentId]
      } else {
        overrides[agentId] = current
      }
      return { ...prev, overrides }
    })
  }

  const handleSegmentChange = async (key: GuardianSwitchKey, segValue: SegValue) => {
    const meta = GUARDIAN_SWITCH_LABELS[key]
    if (segValue === 'inherit') {
      if (!isDefaultScope) setOverrideSwitch(activeAgentId!, key, null)
      return
    }
    if (meta.risky && segValue === 'ALLOW') {
      const target = isDefaultScope ? '所有角色' : activeAgentName
      const ok = await confirmDialog(
        '允许修改后端业务代码',
        `允许 ${target} 修改 backend/* 业务代码。\n\n` +
          `Guardian 内核（6 个文件 + 配置 json）仍受红区硬保护，无法被改动。\n` +
          `每次写会自动备份到 data_dir/backups/、落审计 jsonl，并在右下角弹通知。`,
        'warning',
        '允许',
      )
      if (!ok) return
    }
    if (isDefaultScope) {
      setDefaultSwitch(key, segValue)
    } else {
      setOverrideSwitch(activeAgentId!, key, segValue)
    }
  }

  // —— 一个角色 override 的项数（小徽章用） —— //
  const overrideCountFor = (agentId: string): number => {
    const o = form.overrides?.[agentId]
    return o ? Object.keys(o).length : 0
  }
  const totalOverrideAgents = Object.keys(form.overrides ?? {}).filter((aid) => overrideCountFor(aid) > 0).length

  return (
    <div className={`guardian-panel guardian-panel-${variant}`}>
      {/* ========= 左：scope 列表 ========= */}
      <aside className="guardian-scope-list" aria-label="权限作用范围">
        <header className="guardian-scope-header">
          <span className="guardian-scope-header-title">作用范围</span>
          {totalOverrideAgents > 0 && (
            <span className="guardian-scope-header-count">{totalOverrideAgents} 个角色已自定义</span>
          )}
        </header>

        <div className="guardian-scope-items">
          <button
            type="button"
            className={`guardian-scope-item is-default ${isDefaultScope ? 'active' : ''}`}
            onClick={() => onChangeActiveAgent(null)}
          >
            <span className="guardian-scope-icon"><Crown size={14} /></span>
            <span className="guardian-scope-label">
              <span className="guardian-scope-name">全局默认</span>
              <span className="guardian-scope-sub">所有角色的基线</span>
            </span>
          </button>

          {agents.length === 0 && (
            <div className="guardian-scope-empty">无可配角色</div>
          )}

          {agents.map((agent) => {
            const aid = agent.id
            const overrideCount = overrideCountFor(aid)
            const active = activeAgentId === aid
            return (
              <button
                key={aid}
                type="button"
                className={`guardian-scope-item ${active ? 'active' : ''} ${overrideCount > 0 ? 'has-override' : ''}`}
                onClick={() => onChangeActiveAgent(aid)}
                title={`${agent.name} · ${aid}`}
              >
                <span className="guardian-scope-icon">
                  <span className={`guardian-scope-dot ${overrideCount > 0 ? 'on' : ''}`} aria-hidden="true" />
                </span>
                <span className="guardian-scope-label">
                  <span className="guardian-scope-name">{agent.name}</span>
                  <span className="guardian-scope-sub">
                    {overrideCount > 0 ? `已自定义 ${overrideCount} 项` : '继承全局默认'}
                  </span>
                </span>
              </button>
            )
          })}
        </div>
      </aside>

      {/* ========= 右：规则详情 ========= */}
      <main className="guardian-detail" aria-label="权限规则详情">
        <header className="guardian-detail-header">
          <div className="guardian-detail-title">
            <span className="guardian-detail-icon">
              {isDefaultScope ? <ShieldCheck size={18} /> : <ShieldCheck size={18} />}
            </span>
            <h2>{isDefaultScope ? '全局默认' : activeAgentName}</h2>
            {!isDefaultScope && (
              <span className="guardian-detail-tag">
                {Object.keys(currentOverride).length > 0
                  ? `已自定义 ${Object.keys(currentOverride).length} 项`
                  : '继承全局默认'}
              </span>
            )}
          </div>
          <p className="guardian-detail-sub">
            {isDefaultScope
              ? '在此设置的规则会作用于所有角色（除非该角色自己覆盖）。'
              : `仅作用于 ${activeAgentName}。任意一项选「继承默认」即沿用全局值。`}
          </p>
        </header>

        <div className="guardian-rule-list">
          {GUARDIAN_SWITCH_KEYS.map((key) => (
            <RuleCard
              key={key}
              ruleKey={key}
              isDefaultScope={isDefaultScope}
              currentDefaults={currentDefaults}
              currentOverride={currentOverride}
              effectiveDefaults={effectiveDefaults}
              onChange={handleSegmentChange}
            />
          ))}
        </div>

        {isDefaultScope && (
          <section className="guardian-files-section">
            <h3 className="guardian-section-title">隐私 / 公开文件名</h3>
            <p className="guardian-section-sub">
              这两份名单只在「全局默认」配置；按角色不区分。其他角色对你的文件做读判断时按这个走。
            </p>
            <FileListEditor
              kind="private_files"
              currentDefaults={currentDefaults}
              effectiveDefaults={effectiveDefaults}
              setForm={setForm}
            />
            <FileListEditor
              kind="public_files"
              currentDefaults={currentDefaults}
              effectiveDefaults={effectiveDefaults}
              setForm={setForm}
            />
          </section>
        )}
      </main>

      {confirmDialogNode}
    </div>
  )
}

/* ========================================================================
   单条规则卡片：图标 + label + 描述 + segmented control
   ======================================================================== */

type SegValue = 'inherit' | 'ALLOW' | 'DENY'

interface RuleCardProps {
  ruleKey: GuardianSwitchKey
  isDefaultScope: boolean
  currentDefaults: Partial<GuardianRuleSet>
  currentOverride: Partial<Pick<GuardianRuleSet, GuardianSwitchKey>>
  effectiveDefaults: GuardianRuleSet | null
  onChange: (key: GuardianSwitchKey, next: SegValue) => void
}

function RuleCard({
  ruleKey,
  isDefaultScope,
  currentDefaults,
  currentOverride,
  effectiveDefaults,
  onChange,
}: RuleCardProps) {
  const meta = GUARDIAN_SWITCH_LABELS[ruleKey]
  const value: GuardianAction | undefined = isDefaultScope
    ? (currentDefaults as Record<string, GuardianAction | undefined>)[ruleKey]
    : currentOverride[ruleKey]
  const inherited = !isDefaultScope && value === undefined
  const inheritedFrom = effectiveDefaults?.[ruleKey] as GuardianAction | undefined

  // 当前 effective 状态（用于状态点 + 标签颜色）
  const effective: GuardianAction = (value ?? inheritedFrom ?? 'DENY') as GuardianAction
  const stateClass = effective === 'ALLOW'
    ? (meta.risky ? 'state-allow-risky' : 'state-allow')
    : 'state-deny'

  const currentSeg: SegValue = inherited ? 'inherit' : (value as SegValue)

  return (
    <article className={`guardian-rule-card ${meta.risky ? 'is-risky' : ''} ${stateClass}`}>
      <header className="guardian-rule-head">
        <span className="guardian-rule-icon">
          {meta.risky ? <ShieldAlert size={15} /> : <ShieldCheck size={15} />}
        </span>
        <div className="guardian-rule-title-block">
          <h4 className="guardian-rule-title">{meta.label}</h4>
          <p className="guardian-rule-hint">{meta.hint}</p>
        </div>
        <span className={`guardian-rule-state ${stateClass}`}>
          {effective === 'ALLOW' ? '允许' : '禁止'}
          {inherited && <span className="guardian-rule-state-tag">继承</span>}
        </span>
      </header>

      <div className="guardian-segmented" role="radiogroup" aria-label={meta.label}>
        {!isDefaultScope && (
          <button
            type="button"
            role="radio"
            aria-checked={currentSeg === 'inherit'}
            className={`guardian-seg ${currentSeg === 'inherit' ? 'active' : ''}`}
            onClick={() => onChange(ruleKey, 'inherit')}
          >
            继承默认<span className="guardian-seg-sub">{inheritedFrom ?? '?'}</span>
          </button>
        )}
        <button
          type="button"
          role="radio"
          aria-checked={currentSeg === 'DENY'}
          className={`guardian-seg seg-deny ${currentSeg === 'DENY' ? 'active' : ''}`}
          onClick={() => onChange(ruleKey, 'DENY')}
        >
          禁止
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={currentSeg === 'ALLOW'}
          className={`guardian-seg seg-allow ${meta.risky ? 'is-risky' : ''} ${currentSeg === 'ALLOW' ? 'active' : ''}`}
          onClick={() => onChange(ruleKey, 'ALLOW')}
        >
          允许
        </button>
      </div>
    </article>
  )
}

/* ========================================================================
   文件列表编辑器（私有 / 公开）
   ======================================================================== */

interface FileListEditorProps {
  kind: 'private_files' | 'public_files'
  currentDefaults: Partial<GuardianRuleSet>
  effectiveDefaults: GuardianRuleSet | null
  setForm: Dispatch<SetStateAction<GuardianConfig>>
}

function FileListEditor({ kind, currentDefaults, effectiveDefaults, setForm }: FileListEditorProps) {
  const value = (currentDefaults[kind] ?? effectiveDefaults?.[kind] ?? []) as string[]
  const text = value.join(', ')
  const isCustomized = currentDefaults[kind] != null
  const isPrivate = kind === 'private_files'

  return (
    <div className="guardian-file-card">
      <div className="guardian-file-head">
        <span className="guardian-file-icon">
          {isPrivate ? <FileText size={14} /> : <Globe size={14} />}
        </span>
        <span className="guardian-file-title">
          {isPrivate ? '隐私文件名' : '公开文件名'}
        </span>
        <span className={`guardian-file-tag ${isCustomized ? 'customized' : 'inherited'}`}>
          {isCustomized ? '已自定义' : '默认'}
        </span>
      </div>
      <input
        type="text"
        value={text}
        className="guardian-file-input"
        placeholder={isPrivate ? '例如：SOUL.md, REFLECTIONS.md' : '例如：USER.md, public.md'}
        onChange={(e) => {
          const items = e.target.value
            .split(',')
            .map((s) => s.trim())
            .filter(Boolean)
          setForm((prev) => ({
            ...prev,
            defaults: { ...(prev.defaults ?? {}), [kind]: items },
          }))
        }}
      />
      <p className="guardian-file-hint">
        英文逗号分隔。{isPrivate ? '其他角色禁读这些文件名。' : '其他角色可只读这些文件名。'}
      </p>
    </div>
  )
}
