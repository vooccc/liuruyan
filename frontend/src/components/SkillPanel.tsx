import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowUpCircle,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleSlash,
  Edit3,
  Info,
  Minus,
  Package,
  Plus,
  RefreshCw,
  Search,
  Shield,
  ShieldAlert,
  Sparkles,
  Upload,
  User,
  Wrench,
  X,
} from 'lucide-react'
import {
  AgentSkillInfo,
  AgentSkillsResponse,
  DevModeStatus,
  Skill,
  SkillPromotionCandidate,
  applySkillPromotion,
  approveSkillPromotion,
  equipSkill,
  getAgentSkills,
  getDevStatus,
  getSkillContent,
  listSkillPromotions,
  listSkills,
  rejectSkillPromotion,
  reloadSkills,
  submitSkillPromotion,
  unequipSkill,
  updateSkillContent,
} from '../api/chat'
import { toggleDevMode } from '../api/toolsInventory'
import { getAgentFile } from '../api/workshop'
import { getAvatarSrc } from './avatarUtils'
import { useConfirmDialog } from './ConfirmDialog'
import SkillPackPanel from './SkillPackPanel'

// dev mode 手势与 ToolsInventory 保持一致：连点 5 下 / 1.5s 间隔重置
const DEV_GESTURE_CLICKS = 5
const DEV_GESTURE_TIMEOUT_MS = 1500
// SKILL.md body 上限（与后端 _SKILL_CONTENT_MAX_BYTES 对齐）
const SKILL_CONTENT_MAX_BYTES = 30_000

interface SkillPanelProps {
  onClose?: () => void
  currentAgentId?: string
}

/**
 * 工作模式专用徽章
 *
 * 显示在 category === 'discipline' 的 skill 卡片上。
 * 这类 skill 不通过 load_skill 工具按需加载——装备后会被 build_messages
 * 在工作模式触发时全文常驻 system prompt，作为做事时的行为约束。
 *
 * 未开 dev mode 时，点击 5 下手势进入开发者模式（调用方传 onClick）；
 * 已开 dev mode 时 badge 不再响应点击（用户走卡片上的·编辑·按钮）。
 */
function DisciplineBadge({
  category,
  onClick,
  isDev,
}: {
  category?: string
  onClick?: () => void
  isDev?: boolean
}) {
  if (category !== 'discipline') return null
  const clickable = !!onClick && !isDev
  return (
    <span
      className={`discipline-badge${clickable ? ' discipline-badge--clickable' : ''}`}
      onClick={clickable ? onClick : undefined}
      title={
        isDev
          ? '开发者模式已开启——点击右侧 ✎ 可编辑纪律全文'
          : clickable
            ? '连续点击 5 下进入开发者模式，可编辑纪律全文'
            : '装备后只在工作模式（写代码/排 bug 等）触发时生效'
      }
    >
      <Shield size={10} />
      {isDev ? '可编辑纪律' : '工作模式专用'}
    </span>
  )
}

interface AgentOption {
  id: string
  name: string
  avatar: string
  avatar_url?: string
}

type ViewMode = 'global' | 'agent' | 'marketplace'
type PromotionAction = 'approve' | 'reject' | 'apply'

export default function SkillPanel({ onClose, currentAgentId }: SkillPanelProps) {
  const [viewMode, setViewMode] = useState<ViewMode>(currentAgentId ? 'agent' : 'global')
  const [selectedAgentId, setSelectedAgentId] = useState<string>(currentAgentId || '')
  const [agents, setAgents] = useState<AgentOption[]>([])
  // Tauri webview 不支持 window.confirm
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog()

  const [globalSkills, setGlobalSkills] = useState<Skill[]>([])
  const [globalLoading, setGlobalLoading] = useState(false)
  const [selectedSkillDetail, setSelectedSkillDetail] = useState<Skill | AgentSkillInfo | null>(null)

  const [agentSkillData, setAgentSkillData] = useState<AgentSkillsResponse | null>(null)
  const [agentLoading, setAgentLoading] = useState(false)
  const [equippingId, setEquippingId] = useState<string | null>(null)

  const [promotionCandidates, setPromotionCandidates] = useState<SkillPromotionCandidate[]>([])
  const [promotionLoading, setPromotionLoading] = useState(false)
  const [promotionActionId, setPromotionActionId] = useState<string | null>(null)
  const [promotionError, setPromotionError] = useState('')

  const [searchQuery, setSearchQuery] = useState('')
  const [agentDropdownOpen, setAgentDropdownOpen] = useState(false)
  const agentSelectorRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleMouseDown(e: MouseEvent) {
      if (
        agentDropdownOpen &&
        agentSelectorRef.current &&
        !agentSelectorRef.current.contains(e.target as Node)
      ) {
        setAgentDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handleMouseDown)
    return () => document.removeEventListener('mousedown', handleMouseDown)
  }, [agentDropdownOpen])

  const [showExperienceLog, setShowExperienceLog] = useState(false)
  const [experienceLoading, setExperienceLoading] = useState(false)
  const [experienceContent, setExperienceContent] = useState('')

  // ====== 开发者模式 + 纪律编辑器（与 ToolsInventory 共用同一全局开关） ======
  const [devStatus, setDevStatus] = useState<DevModeStatus | null>(null)
  const [devClickCount, setDevClickCount] = useState(0)
  const [devToggleErr, setDevToggleErr] = useState<string | null>(null)
  const [devFlash, setDevFlash] = useState<string | null>(null)
  const gestureTimerRef = useRef<number | null>(null)

  const [editingSkill, setEditingSkill] = useState<string | null>(null)
  const [editContent, setEditContent] = useState('')
  const [editLoading, setEditLoading] = useState(false)
  const [editError, setEditError] = useState<string | null>(null)

  const isDev = !!devStatus?.enabled
  const canToggleOff = !!devStatus?.can_toggle_off

  useEffect(() => {
    void refreshDevStatus()
    return () => {
      if (gestureTimerRef.current !== null) {
        window.clearTimeout(gestureTimerRef.current)
        gestureTimerRef.current = null
      }
    }
  }, [])

  async function refreshDevStatus() {
    try {
      setDevStatus(await getDevStatus())
    } catch (err) {
      // 后端版本不匹配/网络抖动时给默认 closed state，让 5 次手势仍可生效。
      // toggleDevMode 真失败时会进 devToggleErr 状态条，用户能看到错误。
      console.warn('[SkillPanel] getDevStatus failed:', err)
      setDevStatus({
        enabled: false,
        via_env: false,
        via_runtime_toggle: false,
        can_toggle_off: true,
      })
    }
  }

  async function handleDisciplineClick(_skillName: string) {
    // devStatus 为 null（mount 中尚未 resolve）也允许累计点击，避免早返回吞掉手势
    if (devStatus?.via_env) return // env 强开后无须手势
    if (devStatus?.enabled) return // 已开 dev——点击 badge 不再触发，用户应走编辑按钮
    setDevToggleErr(null)
    if (gestureTimerRef.current !== null) {
      window.clearTimeout(gestureTimerRef.current)
      gestureTimerRef.current = null
    }
    const next = devClickCount + 1
    setDevClickCount(next)
    console.log(`[SkillPanel] discipline click ${next}/${DEV_GESTURE_CLICKS}`)
    if (next >= DEV_GESTURE_CLICKS) {
      setDevClickCount(0)
      try {
        await toggleDevMode(true)
        await refreshDevStatus()
        setDevFlash('已进入开发者模式，可编辑纪律')
        window.setTimeout(() => setDevFlash(null), 2500)
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err)
        console.error('[SkillPanel] toggleDevMode(true) failed:', err)
        setDevToggleErr(msg)
      }
      return
    }
    gestureTimerRef.current = window.setTimeout(() => {
      setDevClickCount(0)
      gestureTimerRef.current = null
    }, DEV_GESTURE_TIMEOUT_MS)
  }

  async function handleExitDev() {
    if (!canToggleOff) return
    const ok = await askConfirm(
      '退出开发者模式',
      '退出开发者模式？已保存的纪律编辑仍然生效。',
      'warning',
      '退出',
    )
    if (!ok) return
    setDevToggleErr(null)
    setEditingSkill(null) // 退 dev 顺便取消正在进行的编辑
    setEditContent('')
    setEditError(null)
    try {
      await toggleDevMode(false)
      await refreshDevStatus()
      setDevFlash('已退出开发者模式')
      window.setTimeout(() => setDevFlash(null), 2500)
    } catch (err) {
      setDevToggleErr(err instanceof Error ? err.message : String(err))
    }
  }

  async function handleStartEdit(skillName: string) {
    setEditError(null)
    setEditingSkill(skillName)
    setEditContent('')
    setEditLoading(true)
    try {
      const data = await getSkillContent(skillName)
      setEditContent(data.content || '')
    } catch (err) {
      setEditError(err instanceof Error ? err.message : String(err))
      setEditingSkill(null)
    } finally {
      setEditLoading(false)
    }
  }

  function handleCancelEdit() {
    if (editLoading) return
    setEditingSkill(null)
    setEditContent('')
    setEditError(null)
  }

  async function handleSaveEdit() {
    if (!editingSkill) return
    setEditLoading(true)
    setEditError(null)
    try {
      await updateSkillContent(editingSkill, editContent)
      await loadGlobalSkills()
      const savedName = editingSkill
      setEditingSkill(null)
      setEditContent('')
      setDevFlash(`已保存 ${savedName}`)
      window.setTimeout(() => setDevFlash(null), 2500)
    } catch (err) {
      setEditError(err instanceof Error ? err.message : String(err))
    } finally {
      setEditLoading(false)
    }
  }

  useEffect(() => {
    try {
      const raw = localStorage.getItem('liuryan_agents') || '[]'
      const parsed = JSON.parse(raw)
      setAgents(
        parsed.map((agent: any) => ({
          id: agent.id,
          name: agent.name,
          avatar: agent.avatar || '🤖',
          avatar_url: agent.avatar_url || undefined,
        })),
      )
    } catch {
      setAgents([])
    }
  }, [])

  useEffect(() => {
    if (viewMode === 'global') {
      void loadGlobalSkills()
      return
    }
    if (selectedAgentId) {
      void refreshAgentView(selectedAgentId)
    }
  }, [viewMode, selectedAgentId])

  const selectedAgent = agents.find((agent) => agent.id === selectedAgentId)

  const latestPromotionBySkill = useMemo(() => {
    const result = new Map<string, SkillPromotionCandidate>()
    for (const candidate of promotionCandidates) {
      if (!result.has(candidate.skill_name)) {
        result.set(candidate.skill_name, candidate)
      }
    }
    return result
  }, [promotionCandidates])

  async function loadGlobalSkills() {
    setGlobalLoading(true)
    try {
      setGlobalSkills(await listSkills())
    } finally {
      setGlobalLoading(false)
    }
  }

  async function loadAgentSkills(agentId: string) {
    setAgentLoading(true)
    try {
      setAgentSkillData(await getAgentSkills(agentId))
    } finally {
      setAgentLoading(false)
    }
  }

  async function loadPromotionCandidates(agentId: string) {
    setPromotionLoading(true)
    setPromotionError('')
    try {
      const data = await listSkillPromotions({ agentId })
      setPromotionCandidates(data.candidates || [])
    } catch (error) {
      console.error('[SkillPanel] failed to load promotions', error)
      setPromotionCandidates([])
      setPromotionError('晋升候选加载失败')
    } finally {
      setPromotionLoading(false)
    }
  }

  async function refreshAgentView(agentId: string) {
    await Promise.all([
      loadAgentSkills(agentId),
      loadPromotionCandidates(agentId),
    ])
  }

  async function handleReload() {
    await reloadSkills()
    if (viewMode === 'global') {
      await loadGlobalSkills()
      return
    }
    if (selectedAgentId) {
      await refreshAgentView(selectedAgentId)
    }
  }

  async function handleEquip(skillName: string) {
    if (!selectedAgentId) return
    setEquippingId(skillName)
    try {
      await equipSkill(selectedAgentId, skillName)
      await loadAgentSkills(selectedAgentId)
    } finally {
      setEquippingId(null)
    }
  }

  async function handleUnequip(skillName: string) {
    if (!selectedAgentId) return
    setEquippingId(skillName)
    try {
      await unequipSkill(selectedAgentId, skillName)
      await loadAgentSkills(selectedAgentId)
    } finally {
      setEquippingId(null)
    }
  }

  async function handleSubmitPromotion(skillName: string) {
    if (!selectedAgentId) return
    setPromotionActionId(`submit:${skillName}`)
    setPromotionError('')
    try {
      await submitSkillPromotion(selectedAgentId, skillName)
      await refreshAgentView(selectedAgentId)
    } catch (error: any) {
      console.error('[SkillPanel] failed to submit promotion', error)
      setPromotionError(error?.message || '提交晋升失败')
    } finally {
      setPromotionActionId(null)
    }
  }

  async function handlePromotionAction(candidateId: string, action: PromotionAction) {
    if (!selectedAgentId) return
    setPromotionActionId(`${action}:${candidateId}`)
    setPromotionError('')
    try {
      if (action === 'approve') {
        await approveSkillPromotion(candidateId, 'Approved from skill panel')
      } else if (action === 'reject') {
        await rejectSkillPromotion(candidateId, 'Rejected from skill panel')
      } else {
        await applySkillPromotion(candidateId, 'Applied from skill panel')
      }
      await refreshAgentView(selectedAgentId)
    } catch (error: any) {
      console.error(`[SkillPanel] failed to ${action} promotion`, error)
      setPromotionError(error?.message || `操作失败：${action}`)
    } finally {
      setPromotionActionId(null)
    }
  }

  async function openExperienceLog() {
    if (!selectedAgentId) return
    setShowExperienceLog(true)
    setExperienceLoading(true)
    try {
      const content = await getAgentFile(selectedAgentId, 'SKILL_EXPERIENCE.md')
      setExperienceContent(content || '# SKILL_EXPERIENCE.md\n\n暂无成长记录')
    } catch {
      setExperienceContent('# SKILL_EXPERIENCE.md\n\n暂无成长记录')
    } finally {
      setExperienceLoading(false)
    }
  }

  function getPromotionStatusLabel(status: SkillPromotionCandidate['status']) {
    if (status === 'approved') return '已批准'
    if (status === 'rejected') return '已驳回'
    if (status === 'applied') return '已应用'
    return '待审核'
  }

  function renderSkillMastery(skill: AgentSkillInfo, compact = false, candidate?: SkillPromotionCandidate) {
    const progressText =
      skill.level >= 4 ? '已达到当前最高等级' : `距离下一等级还差 ${skill.xp_to_next} XP`
    const width = skill.level >= 4 ? 100 : Math.max(4, skill.progress_percent)

    return (
      <div className={`skill-mastery ${compact ? 'compact' : ''}`}>
        <div className="skill-mastery-top">
          <span className="skill-level-badge">Lv{skill.level} {skill.level_label}</span>
          <span className="skill-xp-text">XP {skill.xp}</span>
          {candidate ? (
            <span className={`skill-promotion-status-badge ${candidate.status}`}>
              {getPromotionStatusLabel(candidate.status)}
            </span>
          ) : skill.promotable ? (
            <span className="skill-promotable-badge">可晋升</span>
          ) : null}
        </div>
        <div className="skill-progress-track">
          <div className="skill-progress-fill" style={{ width: `${width}%` }} />
        </div>
        <div className="skill-progress-meta">
          <span>{progressText}</span>
          <span>实战 {skill.total_uses} 次 · 成功 {skill.successful_uses} · 失败 {skill.failed_uses}</span>
        </div>
        {!compact && skill.last_summary && (
          <p className="skill-last-summary">{skill.last_summary}</p>
        )}
      </div>
    )
  }

  function renderPromotionButton(skill: AgentSkillInfo) {
    if (!skill.promotable || !selectedAgentId) return null

    const candidate = latestPromotionBySkill.get(skill.name)
    const submitKey = `submit:${skill.name}`
    const isSubmitting = promotionActionId === submitKey

    if (candidate?.status === 'pending') {
      return (
        <button className="promotion-btn pending" disabled title="候选已提交，等待审核">
          <ArrowUpCircle size={14} />
          待审核
        </button>
      )
    }

    if (candidate?.status === 'approved') {
      return (
        <button className="promotion-btn approved" disabled title="候选已批准，等待应用">
          <CheckCircle2 size={14} />
          待应用
        </button>
      )
    }

    if (candidate?.status === 'applied') {
      return (
        <button className="promotion-btn applied" disabled title="最近一轮经验已应用到系统技能">
          <Upload size={14} />
          已应用
        </button>
      )
    }

    return (
      <button
        className="promotion-btn"
        onClick={() => void handleSubmitPromotion(skill.name)}
        disabled={isSubmitting}
        title="把该角色的实战经验提交为系统级技能改进候选"
      >
        {isSubmitting ? <RefreshCw size={14} className="spin" /> : <ArrowUpCircle size={14} />}
        提交晋升
      </button>
    )
  }

  function renderGlobalView() {
    const filtered = globalSkills.filter((skill) =>
      skill.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      skill.description.toLowerCase().includes(searchQuery.toLowerCase()),
    )
    return (
      <>
        <div className="skill-section-header">
          <span>全局技能池</span>
          <span className="skill-count">共 {globalSkills.length} 项</span>
        </div>

        {filtered.map((skill) => {
          const isDiscipline = skill.category === 'discipline'
          const isEditing = editingSkill === skill.name
          const showEditTrigger = isDev && isDiscipline && !isEditing
          const overBudget = editContent.length > SKILL_CONTENT_MAX_BYTES

          return (
            <div key={skill.name} className={`skill-card-wrapper${isEditing ? ' editing' : ''}`}>
              <div className={`skill-card ${skill.disabled ? 'disabled' : ''}${isEditing ? ' editing' : ''}`}>
                <div className="skill-info">
                  <h4 className="skill-name">
                    {skill.display_name || skill.name}
                    <DisciplineBadge
                      category={skill.category}
                      onClick={() => void handleDisciplineClick(skill.name)}
                      isDev={isDev}
                    />
                  </h4>
                  <p className="skill-desc">{skill.description || '暂无描述'}</p>
                </div>
                {/* 右侧按钮区：编辑中为 ✓ 保存 / ✗ 取消；否则（dev+discipline）为 ✎ 编辑 + 启用/禁用；
                    普通状态只有启用/禁用 —— 按用户要求：保存/取消原位 swap 编辑按钮。 */}
                <div className="skill-card-actions-row">
                  {isEditing ? (
                    <>
                      <button
                        className="icon-btn skill-edit-save-btn"
                        onClick={() => void handleSaveEdit()}
                        disabled={editLoading || overBudget}
                        title={overBudget ? '超过字节上限' : '保存'}
                      >
                        {editLoading ? <RefreshCw size={14} className="spin" /> : <Check size={14} />}
                      </button>
                      <button
                        className="icon-btn skill-edit-cancel-btn"
                        onClick={handleCancelEdit}
                        disabled={editLoading}
                        title="取消"
                      >
                        <X size={14} />
                      </button>
                    </>
                  ) : (
                    <>
                      {showEditTrigger && (
                        <button
                          className="icon-btn skill-edit-trigger-btn"
                          onClick={() => void handleStartEdit(skill.name)}
                          title="编辑此纪律全文"
                        >
                          <Edit3 size={14} />
                        </button>
                      )}
                      <button
                        className="detail-btn"
                        onClick={() => setSelectedSkillDetail(skill)}
                        title="查看详情"
                      >
                        <Info size={14} />
                        <span>详情</span>
                      </button>
                    </>
                  )}
                </div>
              </div>

              {/* 编辑区：在卡片下方嵌入展开，textarea 占满宽度 */}
              {isEditing && (
                <div className="skill-edit-zone">
                  {editError && (
                    <div className="skill-edit-error">
                      <X size={12} /> {editError}
                    </div>
                  )}
                  {editLoading && !editContent && (
                    <div className="skill-edit-loading">
                      <RefreshCw size={14} className="spin" /> 载入中…
                    </div>
                  )}
                  <textarea
                    className="skill-edit-textarea"
                    value={editContent}
                    onChange={(e) => setEditContent(e.target.value)}
                    placeholder="纪律全文（Markdown）…"
                    spellCheck={false}
                    disabled={editLoading && !editContent}
                    rows={18}
                  />
                  <div className="skill-edit-meta">
                    <span className={overBudget ? 'over-budget' : undefined}>
                      {editContent.length.toLocaleString()} 字 · 上限 {SKILL_CONTENT_MAX_BYTES.toLocaleString()} 字节
                    </span>
                    <span className="skill-edit-warn">
                      ⚠️ 保存后纪律全文会被注入所有装备此纪律的角色 system prompt
                    </span>
                  </div>
                </div>
              )}
            </div>
          )
        })}

        {filtered.length === 0 && (
          <div className="empty-state">
            <Search size={32} />
            <p>没有匹配的技能</p>
          </div>
        )}
      </>
    )
  }

  function renderPromotionCandidates() {
    if (promotionLoading) {
      return (
        <div className="empty-state">
          <RefreshCw size={24} className="spin" />
          <p>正在读取晋升候选…</p>
        </div>
      )
    }

    if (promotionCandidates.length === 0) {
      return (
        <div className="promotion-empty">
          当前还没有提交过系统级晋升候选。角色达到 Lv3 且至少成功使用 3 次后，就可以把实战经验上推到共享技能层。
        </div>
      )
    }

    return (
      <div className="promotion-list">
        {promotionCandidates.map((candidate) => {
          const actionPrefix = candidate.id
          const approveLoading = promotionActionId === `approve:${actionPrefix}`
          const rejectLoading = promotionActionId === `reject:${actionPrefix}`
          const applyLoading = promotionActionId === `apply:${actionPrefix}`
          return (
            <div key={candidate.id} className="promotion-card">
              <div className="promotion-card-header">
                <div>
                  <div className="promotion-card-title">
                    <span>{candidate.skill_name}</span>
                    <span className={`skill-promotion-status-badge ${candidate.status}`}>
                      {getPromotionStatusLabel(candidate.status)}
                    </span>
                  </div>
                  <div className="promotion-card-meta">
                    来源角色 {candidate.agent_name} · Lv{candidate.source_level} {candidate.source_level_label} · XP {candidate.source_xp}
                  </div>
                </div>
                <div className="promotion-card-stats">
                  <span>总计 {candidate.total_uses}</span>
                  <span>成功 {candidate.successful_uses}</span>
                  <span>失败 {candidate.failed_uses}</span>
                </div>
              </div>

              {candidate.last_summary && (
                <p className="promotion-summary">{candidate.last_summary}</p>
              )}

              {candidate.heuristics.length > 0 && (
                <div className="promotion-lines">
                  <span className="promotion-label">有效手感</span>
                  <div className="promotion-chip-list">
                    {candidate.heuristics.map((item) => (
                      <span key={item} className="promotion-chip good">{item}</span>
                    ))}
                  </div>
                </div>
              )}

              {candidate.pitfalls.length > 0 && (
                <div className="promotion-lines">
                  <span className="promotion-label">踩坑提醒</span>
                  <div className="promotion-chip-list">
                    {candidate.pitfalls.map((item) => (
                      <span key={item} className="promotion-chip risk">{item}</span>
                    ))}
                  </div>
                </div>
              )}

              {candidate.review_notes && (
                <div className="promotion-review-note">
                  审核备注：{candidate.review_notes}
                </div>
              )}

              <div className="promotion-actions">
                {candidate.status === 'pending' && (
                  <>
                    <button
                      className="promotion-action approve"
                      onClick={() => void handlePromotionAction(candidate.id, 'approve')}
                      disabled={approveLoading || !!promotionActionId}
                    >
                      {approveLoading ? <RefreshCw size={14} className="spin" /> : <CheckCircle2 size={14} />}
                      批准
                    </button>
                    <button
                      className="promotion-action reject"
                      onClick={() => void handlePromotionAction(candidate.id, 'reject')}
                      disabled={rejectLoading || !!promotionActionId}
                    >
                      {rejectLoading ? <RefreshCw size={14} className="spin" /> : <CircleSlash size={14} />}
                      驳回
                    </button>
                  </>
                )}

                {candidate.status === 'approved' && (
                  <button
                    className="promotion-action apply"
                    onClick={() => void handlePromotionAction(candidate.id, 'apply')}
                    disabled={applyLoading || !!promotionActionId}
                  >
                    {applyLoading ? <RefreshCw size={14} className="spin" /> : <Upload size={14} />}
                    应用到系统技能
                  </button>
                )}

                {candidate.status === 'applied' && candidate.applied_at && (
                  <span className="promotion-applied-text">已在 {new Date(candidate.applied_at).toLocaleString()} 写入共享技能文件</span>
                )}
              </div>
            </div>
          )
        })}
      </div>
    )
  }

  function renderAgentView() {
    if (!selectedAgentId) {
      return (
        <div className="empty-state">
          <User size={32} />
          <p>请先选择一个角色</p>
        </div>
      )
    }

    if (agentLoading && !agentSkillData) {
      return (
        <div className="empty-state">
          <RefreshCw size={24} className="spin" />
          <p>正在加载角色技能…</p>
        </div>
      )
    }

    if (!agentSkillData) return null

    const { equipped, equipped_details, recommended, by_category } = agentSkillData
    const equippedCards =
      equipped_details.length > 0
        ? equipped_details
        : agentSkillData.all_skills.filter((skill) => skill.equipped)

    const filteredCategories: Record<string, AgentSkillInfo[]> = {}
    for (const [category, skills] of Object.entries(by_category)) {
      const filtered = skills.filter((skill) =>
        skill.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        skill.description.toLowerCase().includes(searchQuery.toLowerCase()),
      )
      if (filtered.length > 0) filteredCategories[category] = filtered
    }

    return (
      <>
        <div className="skill-section-header equipped-header">
          <span>{selectedAgent?.avatar} 当前装备</span>
          <span className="skill-count equipped-count">已装备 {equipped.length}</span>
        </div>

        {equipped.length > 0 ? (
          <div className="equipped-skills-grid">
            {equippedCards.map((skill) => (
              <div key={skill.name} className="equipped-skill-card">
                <div className="equipped-skill-card-head">
                  <div className="equipped-skill-card-title">
                    <span className="chip-name">{skill.display_name || skill.name}</span>
                    <span className="equipped-badge">已装备</span>
                    <DisciplineBadge category={skill.category} isDev={isDev} />
                  </div>
                  <button
                    className="chip-remove"
                    onClick={() => void handleUnequip(skill.name)}
                    disabled={equippingId === skill.name}
                    title="卸载技能"
                  >
                    {equippingId === skill.name ? <RefreshCw size={12} className="spin" /> : <X size={12} />}
                  </button>
                </div>

                {renderSkillMastery(skill, false, latestPromotionBySkill.get(skill.name))}

                <div className="equipped-skill-actions">
                  {renderPromotionButton(skill)}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="equipped-empty">
            当前还没有装备专属技能，角色会继续使用共享技能池。
          </div>
        )}

        <div className="skill-section-header promotion-header">
          <span><ArrowUpCircle size={14} /> 晋升候选</span>
          <span className="skill-count">{promotionCandidates.length} 条记录</span>
        </div>

        {promotionError && <div className="promotion-error">{promotionError}</div>}
        {renderPromotionCandidates()}

        {recommended.length > 0 && (
          <>
            <div className="skill-section-header recommended-header">
              <span><Sparkles size={14} /> 推荐装备</span>
            </div>
            <div className="recommended-skills">
              {recommended.map((skill) => (
                <div key={skill.name} className="recommended-skill-card">
                  <div className="skill-info">
                    <h4 className="skill-name">
                      {skill.display_name || skill.name}
                      <DisciplineBadge category={skill.category} isDev={isDev} />
                    </h4>
                    <p className="skill-desc">{skill.description || '暂无描述'}</p>
                    {renderSkillMastery(skill, true, latestPromotionBySkill.get(skill.name))}
                  </div>
                  <button
                    className="equip-btn"
                    onClick={() => void handleEquip(skill.name)}
                    disabled={equippingId === skill.name}
                  >
                    {equippingId === skill.name ? (
                      <RefreshCw size={14} className="spin" />
                    ) : (
                      <>
                        <Plus size={14} />
                        装备
                      </>
                    )}
                  </button>
                </div>
              ))}
            </div>
          </>
        )}

        <div className="skill-section-header">
          <span>技能仓库</span>
          <span className="skill-count">{agentSkillData.total_available} 项可用</span>
        </div>

        {Object.entries(filteredCategories).map(([category, skills]) => (
          <div key={category} className="skill-category">
            <h5 className="category-title">{category}</h5>
            {skills.map((skill) => (
              <div key={skill.name} className={`skill-card ${skill.equipped ? 'equipped' : ''}`}>
                <div className="skill-info">
                  <h4 className="skill-name">
                    {skill.display_name || skill.name}
                    {skill.equipped && <span className="equipped-badge">已装备</span>}
                    <DisciplineBadge category={skill.category} isDev={isDev} />
                  </h4>
                  <p className="skill-desc">{skill.description || '暂无描述'}</p>
                  {renderSkillMastery(skill, true, latestPromotionBySkill.get(skill.name))}
                </div>

                <div className="skill-card-actions">
                  {renderPromotionButton(skill)}
                  {viewMode === 'global' ? (
                    <button
                      className="detail-btn"
                      onClick={() => setSelectedSkillDetail(skill)}
                      title="查看详情"
                    >
                      <Info size={14} />
                      详情
                    </button>
                  ) : skill.equipped ? (
                    <button
                      className="unequip-btn"
                      onClick={() => void handleUnequip(skill.name)}
                      disabled={equippingId === skill.name}
                      title="卸载技能"
                    >
                      {equippingId === skill.name ? (
                        <RefreshCw size={14} className="spin" />
                      ) : (
                        <>
                          <Minus size={14} />
                          卸载
                        </>
                      )}
                    </button>
                  ) : (
                    <button
                      className="equip-btn"
                      onClick={() => void handleEquip(skill.name)}
                      disabled={equippingId === skill.name}
                    >
                      {equippingId === skill.name ? (
                        <RefreshCw size={14} className="spin" />
                      ) : (
                        <>
                          <Plus size={14} />
                          装备
                        </>
                      )}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        ))}

        {Object.keys(filteredCategories).length === 0 && (
          <div className="empty-state">
            <Search size={32} />
            <p>没有匹配的技能</p>
          </div>
        )}
      </>
    )
  }

  return (
    <div className="panel skill-panel skill-panel--embedded">
      <div className="panel-header">
        <h2><Wrench size={18} /> 技能管理</h2>
        <div className="panel-header-actions">
          <button
            className="icon-btn"
            onClick={() => void handleReload()}
            title="重新加载技能"
            disabled={globalLoading || agentLoading || promotionLoading}
          >
            <RefreshCw size={16} className={(globalLoading || agentLoading || promotionLoading) ? 'spin' : ''} />
          </button>
          {onClose && (
            <button className="icon-btn" onClick={onClose} title="关闭">
              <X size={18} />
            </button>
          )}
        </div>
      </div>

      {/* Dev mode 状态条：未开 → 不渲染（DisciplineBadge 自带 5 次手势提示）；
          已开 → 显示警告条 + 退出按钮（与 ToolsInventory 配色一致） */}
      {(isDev || devToggleErr || devFlash) && (
        <div className="skill-dev-bar">
          {devToggleErr && (
            <div className="skill-dev-bar-err">
              <X size={13} /> {devToggleErr}
            </div>
          )}
          {devFlash && (
            <div className="skill-dev-bar-flash">
              <Check size={13} /> {devFlash}
            </div>
          )}
          {isDev && (
            <div className="skill-dev-bar-active">
              <ShieldAlert size={13} />
              <span>
                <strong>开发者模式已开启</strong>
                {devStatus?.via_env ? '（env LIURUYAN_DEV_MODE）' : '（手势激活）'}
                ：可编辑 discipline 类纪律全文。
              </span>
              {canToggleOff && (
                <button type="button" className="skill-dev-exit-btn" onClick={() => void handleExitDev()}>
                  退出开发者模式
                </button>
              )}
            </div>
          )}
        </div>
      )}

      <div className="panel-toolbar" style={{ flexWrap: 'wrap', gap: '8px' }}>
        <div className="view-mode-switch">
          <button className={`mode-btn ${viewMode === 'global' ? 'active' : ''}`} onClick={() => setViewMode('global')}>
            <Shield size={14} /> 全局技能池
          </button>
          <button className={`mode-btn ${viewMode === 'agent' ? 'active' : ''}`} onClick={() => setViewMode('agent')}>
            <User size={14} /> 角色成长
          </button>
          <button className={`mode-btn ${viewMode === 'marketplace' ? 'active' : ''}`} onClick={() => setViewMode('marketplace')}>
            <Package size={14} /> 技能市场
          </button>
        </div>

        {viewMode === 'agent' && (
          <>
            <div className="agent-selector" ref={agentSelectorRef}>
              <button className="agent-selector-btn" onClick={() => setAgentDropdownOpen((open) => !open)}>
                {selectedAgent ? (
                  <>
                    {(() => {
                      // 走项目标准 getAvatarSrc：相对路径自动拼 API_BASE，否则 emoji fallback
                      const src = getAvatarSrc(selectedAgent)
                      return src ? (
                        <img className="agent-avatar-sm" src={src} alt="" style={{ width: 18, height: 18, borderRadius: '50%', objectFit: 'cover' }} />
                      ) : (
                        <span className="agent-avatar-sm">{selectedAgent.avatar}</span>
                      )
                    })()}
                    {selectedAgent.name}
                  </>
                ) : (
                  '选择角色'
                )}
                <ChevronDown size={14} />
              </button>

              {agentDropdownOpen && (
                <div className="agent-dropdown">
                  {agents.map((agent) => {
                    // 走项目标准 getAvatarSrc，避免相对路径直接喂给前端 dev server 取 404
                    const src = getAvatarSrc(agent)
                    return (
                      <button
                        key={agent.id}
                        className={`agent-dropdown-item ${agent.id === selectedAgentId ? 'selected' : ''}`}
                        onClick={() => {
                          setSelectedAgentId(agent.id)
                          setAgentDropdownOpen(false)
                        }}
                      >
                        {src ? (
                          <img className="agent-avatar-sm" src={src} alt="" style={{ width: 18, height: 18, borderRadius: '50%', objectFit: 'cover' }} />
                        ) : (
                          <span className="agent-avatar-sm">{agent.avatar}</span>
                        )}
                        {agent.name}
                      </button>
                    )
                  })}
                </div>
              )}
            </div>

            <button
              className="agent-selector-btn"
              onClick={() => void openExperienceLog()}
              disabled={!selectedAgentId || experienceLoading}
              title="查看角色技能成长日志"
            >
              <BookOpen size={14} />
              成长记录
            </button>
          </>
        )}

        <div className="search-box" style={{ marginLeft: 'auto' }}>
          <Search size={14} />
          <input
            type="text"
            placeholder="搜索技能…"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
          />
        </div>
      </div>

      <div className="panel-content skill-list">
        {viewMode === 'global' && renderGlobalView()}
        {viewMode === 'agent' && renderAgentView()}
        {viewMode === 'marketplace' && <SkillPackPanel />}
      </div>

      {selectedSkillDetail && (
        <div className="skill-detail-overlay" onClick={() => setSelectedSkillDetail(null)}>
          <div className="skill-detail-card" onClick={(event) => event.stopPropagation()}>
            <div className="skill-detail-header">
              <div className="skill-detail-icon"><Wrench size={20} /></div>
              <div className="skill-detail-heading">
                <div className="skill-detail-kicker">技能详情</div>
                <div className="skill-detail-title">{selectedSkillDetail.display_name || selectedSkillDetail.name}</div>
              </div>
              <button className="icon-btn" onClick={() => setSelectedSkillDetail(null)} title="关闭">
                <X size={16} />
              </button>
            </div>
            <div className="skill-detail-body">
              <p className="skill-detail-desc">{selectedSkillDetail.description || '暂无描述'}</p>
              <div className="skill-detail-meta-grid">
                <div className="skill-detail-meta-item">
                  <span>分类</span>
                  <strong>{selectedSkillDetail.category || '未分类'}</strong>
                </div>
                <div className="skill-detail-meta-item">
                  <span>状态</span>
                  <strong>{'equipped' in selectedSkillDetail ? (selectedSkillDetail.equipped ? '已装备' : '未装备') : (selectedSkillDetail.disabled ? '已禁用' : '可装备')}</strong>
                </div>
                {'path' in selectedSkillDetail && selectedSkillDetail.path && (
                  <div className="skill-detail-meta-item skill-detail-meta-wide">
                    <span>路径</span>
                    <strong>{selectedSkillDetail.path}</strong>
                  </div>
                )}
                {'level_label' in selectedSkillDetail && (
                  <>
                    <div className="skill-detail-meta-item">
                      <span>等级</span>
                      <strong>{selectedSkillDetail.level_label}</strong>
                    </div>
                    <div className="skill-detail-meta-item">
                      <span>使用次数</span>
                      <strong>{selectedSkillDetail.total_uses}</strong>
                    </div>
                  </>
                )}
              </div>
              {selectedSkillDetail.tags && selectedSkillDetail.tags.length > 0 && (
                <div className="skill-detail-tags">
                  {selectedSkillDetail.tags.map((tag) => <span key={tag}>{tag}</span>)}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {showExperienceLog && (
        <div className="skill-log-overlay" onClick={() => setShowExperienceLog(false)}>
          <div className="skill-log-card" onClick={(event) => event.stopPropagation()}>
            <div className="skill-log-header">
              <div>
                <div className="skill-log-title">技能成长记录</div>
                <div className="skill-log-subtitle">{selectedAgent?.name || '当前角色'} · SKILL_EXPERIENCE.md</div>
              </div>
              <button className="icon-btn" onClick={() => setShowExperienceLog(false)} title="关闭">
                <X size={16} />
              </button>
            </div>
            <div className="skill-log-body">
              {experienceLoading ? (
                <div className="empty-state">
                  <RefreshCw size={24} className="spin" />
                  <p>正在加载成长日志…</p>
                </div>
              ) : (
                <pre className="skill-log-pre">{experienceContent}</pre>
              )}
            </div>
          </div>
        </div>
      )}
      {confirmDialog}
    </div>
  )
}
