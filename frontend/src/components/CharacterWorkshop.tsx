/**
 * CharacterWorkshop — 角色工坊（统一人设编辑面板）
 * 
 * 支持两种模式：
 *   mode='create' — 新建角色：选模板 → 编辑 → 创建
 *   mode='edit'   — 编辑人设：加载全部数据 → 6个Tab编辑 → 保存
 * 
 * 6个Tab：
 *   基础 | 身份 | 灵魂 | 性格 | 基因(⚠️) | 属性(⚠️) — 图标统一为 lucide 线性 SVG
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import { X, Save, RotateCcw, AlertTriangle, Settings, ChevronRight, Sparkles, Loader2, BookmarkPlus, FileText, Contact, SlidersHorizontal, Volume2, BarChart3, Dna } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import MarkdownEditor from './workshop/MarkdownEditor'
import SpectrumSlider, { SPECTRUM_DIMENSIONS } from './workshop/SpectrumSlider'
import SaveAsTemplateDialog from './workshop/SaveAsTemplateDialog'
import {
  PersonaData,
  getGeneEditorFeatureStatus,
  loadAgentPersona,
  saveAgentPersona,
  loadTemplatePersona,
  unlockGeneEditorFeature,
  uploadAgentAvatar,
  removeAgentAvatar,
} from '../api/workshop'
import {
  Template,
  fetchTemplates,
  createAgentBackend,
  clearAgentSessionModelOverrides,
  listSessions,
  applyTemplateAvatarBackend,
  CLEAR_SESSION_MODEL_OVERRIDES_EVENT,
} from '../api/chat'
import {
  ActiveModelView,
  ProviderView,
  getActiveModel,
  listProviders,
  setActiveModel,
} from '../api/providers'
import { API_BASE } from '../api/base'
import AgentAvatar from './AgentAvatar'
import CharacterWorkshopVoiceTab from './CharacterWorkshopVoiceTab'
import { useConfirmDialog, useAlertDialog } from './ConfirmDialog'
import { AGENT_ACTIVE_MODEL_CHANGED_EVENT } from './useChatModelManager'

/** 计算模板的真实头像 URL（avatar_url 优先，fallback 到后端默认插槽） */
function templateAvatarSrc(tpl: Template | null | undefined): string | null {
  if (!tpl || tpl.template_id === 'custom') return null
  if (tpl.avatar_url) return `${API_BASE}${tpl.avatar_url}`
  return `${API_BASE}/agents/templates/${tpl.template_id}/avatar`
}

// ====== 类型 ======

export type WorkshopMode = 'create' | 'edit'

interface CharacterWorkshopProps {
  open: boolean
  onClose: () => void
  /** 模式：创建新角色 or 编辑已有 */
  mode: WorkshopMode
  /** 编辑模式时必传：角色ID */
  agentId?: string
  /** 独立窗口模式：使用系统窗口 chrome，不再渲染 DOM overlay/拖动浮窗 */
  standalone?: boolean
  /** 侧边栏的 agents 列表（用于创建后刷新） */
  onAgentCreated?: (agentId: string) => void
  onAgentUpdated?: () => void
}

// ====== Tab 定义 ======

type TabId = 'basic' | 'identity' | 'soul' | 'personality' | 'genome' | 'attributes' | 'voice'

type WorkshopTab = {
  id: TabId
  label: string
  advanced?: boolean
  /** 线性 SVG 图标组件（lucide），替换原 emoji 前缀 */
  Icon?: LucideIcon
}

const TABS: WorkshopTab[] = [
  { id: 'basic',       label: '基础', Icon: FileText },
  { id: 'identity',    label: '身份', Icon: Contact },
  { id: 'soul',        label: '灵魂', Icon: Sparkles },
  { id: 'personality', label: '性格', Icon: SlidersHorizontal },
  { id: 'voice',       label: '语音', Icon: Volume2 },
  { id: 'genome',      label: '基因', advanced: true, Icon: Dna },
  { id: 'attributes',  label: '属性', Icon: BarChart3 },
]

// ====== 组件 ======

export default function CharacterWorkshop({
  open,
  onClose,
  mode,
  agentId,
  standalone = false,
  onAgentCreated,
  onAgentUpdated,
}: CharacterWorkshopProps) {
  const [activeTab, setActiveTab] = useState<TabId>('basic')
  const [advancedMode, setAdvancedMode] = useState(false) // 高级模式开关
  const [geneEditorEnabled, setGeneEditorEnabled] = useState(false)
  const [geneEditorDevRuntime, setGeneEditorDevRuntime] = useState(false)
  const geneEditorUnlockClicksRef = useRef(0)
  const geneEditorUnlockTimerRef = useRef<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null) // 错误提示
  
  // 数据
  const [data, setData] = useState<PersonaData>({
    name: '', description: '', avatar: '🤖',
    prototype: '', tags: [],
    spectrum: {}, charm_layers: {},
    identity_md: '', soul_md: '', personality_md: '', genome_md: '',
  })

  // 脏数据追踪：编辑模式加载完成后冻结初始快照，用于判断是否有变更
  const [initialData, setInitialData] = useState<PersonaData | null>(null)
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false)

  // per-agent 默认模型（对齐 Cherry AgentModal：与 name/instructions 统一表单 state，
  // 由顶部“保存”按钮统一提交 setActiveModel）
  const [providersList, setProvidersList] = useState<ProviderView[]>([])
  const [initialActiveModel, setInitialActiveModel] = useState<ActiveModelView>({ provider_id: '', model: '' })
  const [pendingActiveModel, setPendingActiveModel] = useState<ActiveModelView>({ provider_id: '', model: '' })

  // 模板选择（仅 create 模式）
  const [templates, setTemplates] = useState<Template[]>([])
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null)
  const [step, setStep] = useState<'select' | 'edit'>('select') // create 模式的两步

  const applyTemplatePreview = useCallback((tpl: Template) => {
    setSelectedTemplateId(tpl.template_id)
    setData(prev => ({
      ...prev,
      name: tpl.template_id === 'custom' ? '' : (tpl.name || ''),
      description: tpl.template_id === 'custom' ? '' : (tpl.description || ''),
      avatar: tpl.template_id === 'custom' ? '✨' : (tpl.avatar || '🤖'),
    }))
  }, [])

  const { confirm, dialog: confirmDialog } = useConfirmDialog()
  const { alert, dialog: alertDialog } = useAlertDialog()

  // 是否正在清重置会话模型（按钮 loading 用）
  const [resettingSessions, setResettingSessions] = useState(false)

  // "另存为模板"弹窗开关（仅 edit 模式可用）
  const [saveAsTemplateOpen, setSaveAsTemplateOpen] = useState(false)

  // 头像图片 cache buster：每次上传/应用/移除头像后 +1，让 <img> 真的重拉，
  // 不被浏览器或 Service Worker 用旧缓存糊弄。
  const [avatarVer, setAvatarVer] = useState(0)
  // 头像操作中（上传/应用/移除）：按钮置灰防双击。
  const [avatarBusy, setAvatarBusy] = useState(false)
  const [panelPosition, setPanelPosition] = useState<{ x: number; y: number } | null>(null)

  // 重置该角色下所有会话的 per-session model override。
  // 只 edit 模式 + 有 agentId 时才提供。confirm 后调后端清 session.model，
  // 发业内事件让 useChatModelManager 清 perSessionOverride，再走一趟
  // listSessions 让前端 store 里的 session.model 字段同步变 null。
  const handleResetSessionModels = useCallback(async () => {
    if (mode !== 'edit' || !agentId) return
    const ok = await confirm(
      '重置所有会话的模型选择',
      '将清除该角色下所有会话中“曾手动切换过的模型”（session.model）。\n未来这些会话会跟随当前默认模型。消息内容、Token 统计、人设一概不动。\n\n继续吗？',
      'danger',
      '重置所有会话',
    )
    if (!ok) return
    setResettingSessions(true)
    try {
      const res = await clearAgentSessionModelOverrides(agentId)
      // 发业内信号：useChatModelManager 清内存 perSessionOverride
      window.dispatchEvent(
        new CustomEvent(CLEAR_SESSION_MODEL_OVERRIDES_EVENT, { detail: { agentId } }),
      )
      // 同步拉 sessions，让 store 中 session.model 变 null（下次切会话不会重新种子）
      try { await listSessions() } catch { /* ignore */ }
      await alert(
        res.cleared > 0
          ? `已重置 ${res.cleared} 个会话的模型选择。`
          : '该角色下没有需要重置的会话。',
      )
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      await alert(`重置失败：${msg}`)
    } finally {
      setResettingSessions(false)
    }
  }, [mode, agentId, confirm, alert])

  // ====== 加载数据 ======

  useEffect(() => {
    if (!open) return
    setActiveTab('basic')
    setAdvancedMode(false)
    setGeneEditorEnabled(false)
    setGeneEditorDevRuntime(false)
    setError(null) // 清除上次错误
    const panelWidth = Math.min(720, Math.max(320, window.innerWidth - 96))
    setPanelPosition({ x: Math.max(10, window.innerWidth - panelWidth - 18), y: 52 })

    void getGeneEditorFeatureStatus()
      .then(status => {
        const enabled = !!status.enabled
        setGeneEditorEnabled(enabled)
        setGeneEditorDevRuntime(!!status.dev_runtime)
        if (mode === 'edit' && agentId) {
          void loadFullPersona(agentId, enabled)
        } else if (mode === 'create') {
          void loadTemplateList()
        }
      })
      .catch(() => {
        setGeneEditorEnabled(false)
        setGeneEditorDevRuntime(false)
        if (mode === 'edit' && agentId) {
          void loadFullPersona(agentId, false)
        } else if (mode === 'create') {
          void loadTemplateList()
        }
      })
  }, [open, mode, agentId])

  async function loadFullPersona(id: string, includeGenome: boolean) {
    setLoading(true)
    try {
      const [persona, listRes, active] = await Promise.all([
        loadAgentPersona(id, { includeGenome }),
        listProviders(),
        getActiveModel('agent', id),
      ])
      setData(persona)
      // 冻结初始快照（深拷贝，避免引用共享）
      setInitialData(JSON.parse(JSON.stringify(persona)))
      setProvidersList(listRes.providers.filter(p => p.enabled !== false))
      setInitialActiveModel(active)
      setPendingActiveModel(active)
      setHasUnsavedChanges(false) // 刚加载完，没有变更
      setStep('edit')
    } catch (err) {
      console.error('加载人设失败:', err)
    } finally {
      setLoading(false)
    }
  }

  async function loadTemplateList() {
    setLoading(true)
    try {
      const [tpls, listRes] = await Promise.all([fetchTemplates(), listProviders()])
      setTemplates(tpls)
      setProvidersList(listRes.providers.filter(p => p.enabled !== false))
      // create 模式默认跟随全局（空）
      setInitialActiveModel({ provider_id: '', model: '' })
      setPendingActiveModel({ provider_id: '', model: '' })
      // 默认选中第一个模板，按钮一开始就显示，避免点选后高度跳变。
      // 同步把模板的 name/description/avatar 灌进 data，否则用户看到第一张卡片
      // 高亮但表单里 name 还是空（"开始创建" 后 BasicTab name 字段空白）。
      if (tpls.length > 0) {
        applyTemplatePreview(tpls[0])
      }
      setStep('select')
    } catch (err) {
      console.error('加载模板失败:', err)
    } finally {
      setLoading(false)
    }
  }

  // ====== 数据更新（局部） ======

  const updateData = useCallback((patch: Partial<PersonaData>) => {
    setData(prev => ({ ...prev, ...patch }))
  }, [])

  // ====== 头像操作 ======
  // 设计：仅 edit 模式可操作头像图片（create 模式没有 agentId，
  // 只能编辑 emoji + 看选中模板的预览图）。三个动作通过同一个 busy 锁串行。

  const handleUploadAvatar = useCallback(async (file: File) => {
    if (mode !== 'edit' || !agentId) return
    if (!file.type.startsWith('image/')) {
      await alert('请选择图片文件（PNG / JPG / WebP / GIF）')
      return
    }
    setAvatarBusy(true)
    try {
      const { avatar_url } = await uploadAgentAvatar(agentId, file)
      // 三件事同步：
      //  1) data.avatar_url 让 BasicTab 立即看到图（含 cache buster）
      //  2) initialData 同步——avatar 改动不该让保存按钮误判脏（avatar 上传走独立写盘通道，
      //     不走表单保存事务）
      //  3) +1 cache buster 让 <img> 真重拉
      //  4) 通知外层刷一次 agents 列表，让 Sidebar / ChatHeader 等的头像立即跟新
      setData(prev => ({ ...prev, avatar_url }))
      setInitialData(prev => prev ? { ...prev, avatar_url } : prev)
      setAvatarVer(v => v + 1)
      onAgentUpdated?.()
    } catch (err) {
      console.error('[Workshop] 头像上传失败:', err)
      await alert(`上传失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setAvatarBusy(false)
    }
  }, [mode, agentId, alert, onAgentUpdated])

  const handleRemoveAvatar = useCallback(async () => {
    if (mode !== 'edit' || !agentId) return
    const ok = await confirm(
      '移除自定义头像',
      '将删除已上传的头像图片，回到 emoji 显示。可以稍后重新上传。',
      'warning', '移除',
    )
    if (!ok) return
    setAvatarBusy(true)
    try {
      await removeAgentAvatar(agentId)
      setData(prev => ({ ...prev, avatar_url: null }))
      setInitialData(prev => prev ? { ...prev, avatar_url: null } : prev)
      setAvatarVer(v => v + 1)
      onAgentUpdated?.()
    } catch (err) {
      console.error('[Workshop] 头像移除失败:', err)
      await alert(`移除失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setAvatarBusy(false)
    }
  }, [mode, agentId, confirm, alert, onAgentUpdated])

  const handleApplyTemplateAvatar = useCallback(async (templateId: string) => {
    if (mode !== 'edit' || !agentId) return
    setAvatarBusy(true)
    try {
      const updated = await applyTemplateAvatarBackend(agentId, templateId)
      const newUrl = updated.avatar_url || `/agents/${agentId}/avatar`
      setData(prev => ({ ...prev, avatar_url: newUrl }))
      setInitialData(prev => prev ? { ...prev, avatar_url: newUrl } : prev)
      setAvatarVer(v => v + 1)
      onAgentUpdated?.()
    } catch (err) {
      console.error('[Workshop] 应用模板头像失败:', err)
      await alert(`应用模板头像失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setAvatarBusy(false)
    }
  }, [mode, agentId, alert, onAgentUpdated])

  // edit 模式下首次打开"用模板"时按需拉模板列表（避免无谓 API 调用）
  const ensureTemplatesLoaded = useCallback(async () => {
    if (templates.length > 0) return
    try {
      const tpls = await fetchTemplates()
      setTemplates(tpls)
    } catch (err) {
      console.warn('[Workshop] 拉模板列表失败:', err)
    }
  }, [templates.length])

  // 脏数据检测：每次 data / pendingActiveModel 变化时与初始快照对比
  useEffect(() => {
    if (mode !== 'edit') return
    if (!initialData) return
    const dataChanged = JSON.stringify(data) !== JSON.stringify(initialData)
    const modelChanged = (
      pendingActiveModel.provider_id !== initialActiveModel.provider_id ||
      pendingActiveModel.model !== initialActiveModel.model
    )
    setHasUnsavedChanges(dataChanged || modelChanged)
  }, [data, initialData, pendingActiveModel, initialActiveModel, mode])

  // ====== 保存 ======

  const handleSave = async () => {
    if (!data.name.trim()) return

    if (mode === 'create') {
      await handleCreate()
    } else {
      await handleEditSave()
    }
  }

  async function handleCreate() {
    setSaving(true)
    setError(null)
    try {
      const templateId = selectedTemplateId && selectedTemplateId !== 'custom' ? selectedTemplateId : undefined
      const newAgent = await createAgentBackend(
        data.name.trim(),
        data.description.trim(),
        data.avatar,
        templateId || undefined
      )
      
      // 创建后，如果用户编辑了 md 文件，保存到新角色
      const skipFiles: string[] = []
      if (!data.identity_md) skipFiles.push('IDENTITY.md')
      if (!data.soul_md) skipFiles.push('SOUL.md')
      if (!data.personality_md) skipFiles.push('PERSONALITY.md')
      if (!geneEditorEnabled || !data.genome_md) skipFiles.push('GENOME.md')

      const hasMdChanges = data.identity_md || data.soul_md || data.personality_md || (geneEditorEnabled && data.genome_md)
      if (hasMdChanges) {
        try {
          await saveAgentPersona(newAgent.id, data, { skipFiles })
        } catch (e) {
          console.error('保存人设文件失败（角色已创建）:', e)
        }
      }

      // 用户在 create 阶段选了模型 → 写到新 agent 的 active model。失败不阻塞（角色已创建）。
      if (pendingActiveModel.model && pendingActiveModel.provider_id) {
        try {
          await setActiveModel({
            scope: 'agent',
            agent_id: newAgent.id,
            provider_id: pendingActiveModel.provider_id,
            model: pendingActiveModel.model,
          })
        } catch (e) {
          console.error('设置默认模型失败（角色已创建）:', e)
        }
      }

      onAgentCreated?.(newAgent.id)
      onClose()
    } catch (err: any) {
      console.error('创建角色失败:', err)
      setError(err.message || '创建失败，请重试')
    } finally {
      setSaving(false)
    }
  }

  async function handleEditSave() {
    if (!agentId) return
    setError(null)
    
    // 基因编辑确认
    if (data.genome_md && activeTab === 'genome') {
      const ok = await confirm(
        '⚠️ 确认修改基因？',
        'GENOME.md 是角色的底层行为逻辑，修改后可能影响角色行为一致性。确定保存吗？',
        'warning', '确认修改'
      )
      if (!ok) return
    }

    setSaving(true)
    try {
      await saveAgentPersona(agentId, data, { skipFiles: geneEditorEnabled ? [] : ['GENOME.md'] })
      // model 有变更 → 同步写 active_model（独立 meta.json，跟 md 文件两条路但在同一保存事务内）
      const modelChanged = (
        pendingActiveModel.provider_id !== initialActiveModel.provider_id ||
        pendingActiveModel.model !== initialActiveModel.model
      )
      if (modelChanged) {
        await setActiveModel({
          scope: 'agent',
          agent_id: agentId,
          provider_id: pendingActiveModel.provider_id,
          model: pendingActiveModel.model,
        })
        // 通知主页 useChatModelManager 立即重拉 effective —— 修 "改完角色模型
        // 输入框下方显示不更新，必须切走再切回" 的 bug。
        window.dispatchEvent(new CustomEvent(AGENT_ACTIVE_MODEL_CHANGED_EVENT, {
          detail: { agentId },
        }))
      }
      onAgentUpdated?.()
      onClose()
    } catch (err: any) {
      console.error('保存失败:', err)
      setError(err.message || '保存失败，请重试')
    } finally {
      setSaving(false)
    }
  }

  // ====== 模板选择（create Step 1）=====

  function handleSelectTemplate(tpl: Template) {
    applyTemplatePreview(tpl)
  }

  function handleConfirmTemplate() {
    if (!selectedTemplateId) return
    setStep('edit')
    if (selectedTemplateId === 'custom') {
      setData(prev => ({
        ...prev,
        identity_md: '',
        soul_md: '',
        personality_md: '',
        genome_md: '',
      }))
      return
    }
    setLoading(true)
    void loadTemplatePersona(selectedTemplateId, { includeGenome: geneEditorEnabled })
      .then(tplData => {
        setData(prev => ({
          ...prev,
          name: prev.name || tplData.name,
          description: prev.description || tplData.description,
          avatar: prev.avatar || tplData.avatar,
          prototype: tplData.prototype || prev.prototype,
          core: tplData.core || prev.core,
          tags: tplData.tags || prev.tags,
          spectrum: Object.keys(prev.spectrum || {}).length > 0 ? prev.spectrum : tplData.spectrum,
          charm_layers: Object.keys(prev.charm_layers || {}).length > 0 ? prev.charm_layers : tplData.charm_layers,
          identity_md: tplData.identity_md,
          soul_md: tplData.soul_md,
          personality_md: tplData.personality_md,
          genome_md: tplData.genome_md,
        }))
      })
      .catch(err => {
        console.warn('[Workshop] 预加载模板内容失败:', err)
      })
      .finally(() => {
        setLoading(false)
      })
  }

  // ====== 渲染 ======
  if (!open) return null

  const title = mode === 'create'
    ? (step === 'select' ? '创建新角色' : `创建「${data.name || '新角色'}」`)
    : `${data.name || '角色'}的人设工坊`

  async function handleTitleUnlockClick() {
    if (!geneEditorDevRuntime || geneEditorEnabled) return
    geneEditorUnlockClicksRef.current += 1
    if (geneEditorUnlockTimerRef.current !== null) {
      window.clearTimeout(geneEditorUnlockTimerRef.current)
    }
    geneEditorUnlockTimerRef.current = window.setTimeout(() => {
      geneEditorUnlockClicksRef.current = 0
      geneEditorUnlockTimerRef.current = null
    }, 1800)
    if (geneEditorUnlockClicksRef.current < 7) return
    geneEditorUnlockClicksRef.current = 0
    if (geneEditorUnlockTimerRef.current !== null) {
      window.clearTimeout(geneEditorUnlockTimerRef.current)
      geneEditorUnlockTimerRef.current = null
    }
    const status = await unlockGeneEditorFeature()
    if (status.enabled) {
      setGeneEditorEnabled(true)
      setGeneEditorDevRuntime(!!status.dev_runtime)
      if (mode === 'edit' && agentId && !data.genome_md) {
        try {
          const persona = await loadAgentPersona(agentId, { includeGenome: true })
          setData(prev => ({ ...prev, genome_md: persona.genome_md || '' }))
          setInitialData(prev => prev ? { ...prev, genome_md: persona.genome_md || '' } : prev)
        } catch (e) {
          console.warn('[Workshop] 解锁后加载 GENOME.md 失败:', e)
        }
      }
      setAdvancedMode(true)
      await alert('已解锁开发调试：基因编辑本次后端会话可用。')
    }
  }

  function handlePanelDragStart(e: React.MouseEvent<HTMLElement>) {
    const target = e.target as HTMLElement
    if (target.closest('button, input, select, textarea, a')) return
    const startX = e.clientX
    const startY = e.clientY

    if (standalone) {
      function cleanup() {
        window.removeEventListener('mousemove', onMove)
        window.removeEventListener('mouseup', cleanup)
      }
      function onMove(moveEvent: MouseEvent) {
        if (Math.abs(moveEvent.clientX - startX) <= 2 && Math.abs(moveEvent.clientY - startY) <= 2) return
        cleanup()
        moveEvent.preventDefault()
        void import('@tauri-apps/api/window')
          .then(({ getCurrentWindow }) => getCurrentWindow().startDragging())
          .catch(() => {})
      }
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', cleanup)
      return
    }

    const startPos = panelPosition || { x: Math.max(10, window.innerWidth - 738), y: 52 }
    const panel = target.closest('.workshop-panel') as HTMLElement | null
    const panelWidth = panel?.offsetWidth || 720
    const panelHeight = panel?.offsetHeight || Math.min(window.innerHeight - 70, 760)

    function onMove(moveEvent: MouseEvent) {
      const nextX = Math.min(
        Math.max(10, startPos.x + moveEvent.clientX - startX),
        Math.max(10, window.innerWidth - panelWidth - 10),
      )
      const nextY = Math.min(
        Math.max(10, startPos.y + moveEvent.clientY - startY),
        Math.max(10, window.innerHeight - panelHeight - 10),
      )
      setPanelPosition({ x: nextX, y: nextY })
    }

    function onUp() {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }

    e.preventDefault()
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  return (
    <div className={standalone ? 'workshop-window-root' : 'workshop-overlay'}>
      <div
        className={`workshop-panel ${standalone ? 'standalone' : ''}`}
        style={!standalone && panelPosition ? { left: panelPosition.x, top: panelPosition.y } : undefined}
      >
        {/* === Header === */}
        <header
          className="workshop-header"
          onMouseDown={handlePanelDragStart}
        >
          <div className="workshop-header-left">
            {mode === 'create' && step === 'edit' && (
              <button className="workshop-back-btn" onClick={() => setStep('select')} type="button">
                ← 返回选模板
              </button>
            )}
            <h2
              className="workshop-title"
              onClick={() => { void handleTitleUnlockClick() }}
            >
              {title}
            </h2>
            {mode === 'edit' && <span className="workshop-mode-badge">编辑模式</span>}
            {mode === 'create' && step === 'edit' && <span className="workshop-mode-badge create">新建模式</span>}
          </div>
          <div className="workshop-header-right" data-tauri-drag-region={false}>
            {/* 另存为模板（仅 edit 模式 + 有 agentId 时显示；hasUnsavedChanges 时禁用，避免把未保存的脏数据冻进模板） */}
            {mode === 'edit' && agentId && (
              <button
                className="btn-save-as-template"
                onClick={() => setSaveAsTemplateOpen(true)}
                disabled={saving || hasUnsavedChanges}
                title={hasUnsavedChanges ? '请先保存当前修改，再另存为模板' : '把当前角色冻结成一个新模板'}
                type="button"
              >
                <BookmarkPlus size={15} />
                另存为模板
              </button>
            )}
            <button
              className={`btn-save ${saving ? 'saving' : ''} ${!hasUnsavedChanges && mode === 'edit' ? 'disabled' : ''}`}
              onClick={handleSave}
              disabled={
                saving ||
                !data.name.trim() ||
                (mode === 'create' && step === 'select') || /* 模板选择阶段不可保存 */
                (mode === 'create' && !selectedTemplateId && step === 'edit') || /* 编辑阶段未选模板 */
                (mode === 'edit' && !hasUnsavedChanges) /* 编辑模式：无变更时置灰 */
              }
              type="button"
            >
              {saving ? '保存中...' : (
                <>
                  <Save size={15} />
                  保存
                </>
              )}
            </button>
            <button className="btn-close-workshop" onClick={onClose} type="button" title="关闭人设工坊">
              <X size={18} />
            </button>
          </div>
        </header>

        {/* === Body === */}
        <div className="workshop-body">
          {/* Step 1: 模板选择 */}
          {mode === 'create' && step === 'select' && (
            <div className="workshop-step-select">
              {loading ? (
                <div className="workshop-loading">加载模板中...</div>
              ) : (
                <>
                  <div className="workshop-step-intro">
                    <h3 className="workshop-step-title">选一个出身</h3>
                    <p className="workshop-step-hint">从已有角色模板开始，或从空白自由创建</p>
                  </div>
                  <div className="workshop-template-grid">
                    {templates.map(tpl => {
                      const avatarSrc = templateAvatarSrc(tpl)
                      const isSelected = selectedTemplateId === tpl.template_id
                      return (
                        <button
                          key={tpl.template_id}
                          className={`ws-tpl-card ${isSelected ? 'selected' : ''}`}
                          onClick={() => handleSelectTemplate(tpl)}
                          title={tpl.description || tpl.name}
                          type="button"
                        >
                          <span className="ws-tpl-avatar">
                            <AgentAvatar
                              name={tpl.name}
                              src={avatarSrc}
                              imageClassName="ws-tpl-avatar-img"
                              plainClassName="ws-tpl-avatar-emoji"
                            />
                          </span>
                          <span className="ws-tpl-name">{tpl.name || tpl.template_id}</span>
                        </button>
                      )
                    })}
                    <button
                      className={`ws-tpl-card custom ${selectedTemplateId === 'custom' ? 'selected' : ''}`}
                      onClick={() => handleSelectTemplate({ template_id: 'custom', name: '', avatar: '✨', prototype: '自由创作' } as Template)}
                      type="button"
                    >
                      <span className="ws-tpl-avatar">
                        <span className="ws-tpl-avatar-emoji ws-tpl-avatar-custom">
                          <Sparkles size={28} strokeWidth={1.5} />
                        </span>
                      </span>
                      <span className="ws-tpl-name">自定义</span>
                    </button>
                  </div>
                  <div className="workshop-template-confirm">
                    {selectedTemplateId && (() => {
                      const sel = selectedTemplateId === 'custom'
                        ? { name: '自定义角色' }
                        : templates.find(t => t.template_id === selectedTemplateId)
                      const label = sel?.name || ''
                      return (
                        <>
                          <span className="workshop-template-selected-label">
                            已选择 <strong>{label}</strong>
                          </span>
                          <button className="btn-primary" onClick={handleConfirmTemplate} type="button">
                            开始创建
                            <ChevronRight size={16} />
                          </button>
                        </>
                      )
                    })()}
                  </div>
                </>
              )}
            </div>
          )}

          {/* Step 2 / Edit Mode: Tab 编辑区 */}
          {(mode === 'edit' || (mode === 'create' && step === 'edit')) && (
            <>
              {/* Tab Bar */}
              <nav className="workshop-tabs">
                {TABS.map(tab => {
                  if (tab.id === 'genome' && (!advancedMode || !geneEditorEnabled)) return null
                  
                  const isAttrTab = tab.id === 'attributes'
                  return (
                    <button
                      key={tab.id}
                      className={`workshop-tab ${activeTab === tab.id ? 'active' : ''} ${tab.advanced ? 'advanced' : ''}`}
                      onClick={() => setActiveTab(tab.id)}
                      type="button"
                    >
                      {tab.Icon && <tab.Icon size={14} className="workshop-tab-icon" />}
                      {tab.label}
                      {tab.advanced && !isAttrTab && <AlertTriangle size={11} className="tab-warning-icon" />}
                      {isAttrTab && (
                        <Settings
                          size={12}
                          className={`tab-attr-icon ${advancedMode ? 'active' : ''}`}
                          onClick={(e) => {
                            e.stopPropagation()
                            if (geneEditorEnabled) setAdvancedMode(!advancedMode)
                          }}
                        />
                      )}
                    </button>
                  )
                })}
                
                {/* 高级模式切换（属性Tab旁边） */}
                {geneEditorEnabled && !advancedMode && (
                  <button
                    className="workshop-tab advanced-toggle-btn"
                    onClick={() => setAdvancedMode(true)}
                    title="开启高级模式以编辑基因和光谱参数"
                    type="button"
                  >
                    ⚙️ 高级
                  </button>
                )}
                {geneEditorEnabled && advancedMode && (
                  <button
                    className="workshop-tab advanced-toggle-btn active"
                    onClick={() => setAdvancedMode(false)}
                    title="关闭高级模式"
                    type="button"
                  >
                    ⚙️ 高级 ✓
                  </button>
                )}
              </nav>

              {/* Tab Content */}
              <div className="workshop-content">
                {error && (
                  <div className="workshop-error-banner" onClick={() => setError(null)}>
                    ⚠️ {error}（点击关闭）
                  </div>
                )}
                {loading ? (
                  <div className="workshop-loading">加载数据中...</div>
                ) : renderTabContent()}
              </div>
            </>
          )}
        </div>
      </div>
      {confirmDialog}
      {alertDialog}
      {mode === 'edit' && agentId && (
        <SaveAsTemplateDialog
          open={saveAsTemplateOpen}
          onClose={() => setSaveAsTemplateOpen(false)}
          agentId={agentId}
          agentName={data.name}
          onSaved={async (tmpl) => {
            // 通知外部（Sidebar 会刷模板列表），但本面板不关闭——用户可能想继续编辑
            try { await alert(`已保存为模板：${tmpl.name || tmpl.template_id}`) } catch {}
            onAgentUpdated?.()
          }}
        />
      )}
    </div>
  )

  // ====== Tab 渲染器 ======

  function renderTabContent() {
    switch (activeTab) {
      case 'basic': {
        // 头像图片解析：edit 模式优先用 data.avatar_url（实例上传/应用过的图）；
        // 没有时 edit 模式什么都不显示（让 emoji 兜底）；create 模式用选中模板的预览。
        let resolvedAvatarUrl: string | null = null
        if (mode === 'edit' && data.avatar_url) {
          // 拼 cache buster：后端已经在 avatar_url 里塞了 ?v=mtime（文件 mtime 一变就换），
          // 这里再叠一层本地计数器 vv=avatarVer 防 1 秒内重复上传（mtime 秒级精度不够）。
          // 用 & 而不是 ? 保证规范 URL。
          const sep = data.avatar_url.includes('?') ? '&' : '?'
          resolvedAvatarUrl = `${API_BASE}${data.avatar_url}${sep}vv=${avatarVer}`
        } else if (mode === 'create' && selectedTemplateId && selectedTemplateId !== 'custom') {
          resolvedAvatarUrl = templateAvatarSrc(templates.find(t => t.template_id === selectedTemplateId))
        }
        return (
          <BasicTab
            data={data}
            onChange={updateData}
            avatarUrl={resolvedAvatarUrl}
            mode={mode}
            agentId={agentId}
            providers={providersList}
            activeModel={pendingActiveModel}
            onActiveModelChange={setPendingActiveModel}
            onResetSessionModels={
              mode === 'edit' && agentId ? handleResetSessionModels : undefined
            }
            resettingSessionModels={resettingSessions}
            templates={templates}
            avatarBusy={avatarBusy}
            onUploadAvatar={mode === 'edit' && agentId ? handleUploadAvatar : undefined}
            onRemoveAvatar={
              mode === 'edit' && agentId && data.avatar_url ? handleRemoveAvatar : undefined
            }
            onApplyTemplateAvatar={
              mode === 'edit' && agentId ? handleApplyTemplateAvatar : undefined
            }
            onEnsureTemplatesLoaded={mode === 'edit' ? ensureTemplatesLoaded : undefined}
          />
        )
      }
      case 'identity':
        return <IdentityTab value={data.identity_md || ''} onChange={v => updateData({ identity_md: v })} />
      case 'soul':
        return <SoulTab value={data.soul_md || ''} onChange={v => updateData({ soul_md: v })} />
      case 'personality':
        return <PersonalityTab value={data.personality_md || ''} onChange={v => updateData({ personality_md: v })} />
      case 'genome':
        if (!geneEditorEnabled) return null
        return (
          <GenomeTab
            value={data.genome_md || ''}
            onChange={v => updateData({ genome_md: v })}
          />
        )
      case 'voice':
        // create 模式还没真正落地角色目录前没有 agentId；提示先去基础 tab 保存
        if (mode === 'create' || !agentId) {
          return (
            <div className="voice-settings" style={{ padding: 16 }}>
              <p className="voice-settings-hint" style={{ marginTop: 0 }}>
                请先在「基础」tab 保存角色，然后回来配置音色。
              </p>
            </div>
          )
        }
        return (
          <CharacterWorkshopVoiceTab
            agentId={agentId}
            agentName={data.name}
          />
        )
      case 'attributes':
        return (
          <AttributesTab
            spectrum={data.spectrum || {}}
            charmLayers={data.charm_layers || {}}
            prototype={data.prototype || ''}
            tags={data.tags || []}
            advancedMode={advancedMode}
            onSpectrumChange={(key, val) => {
              updateData({ spectrum: { ...(data.spectrum || {}), [key]: val } })
            }}
            onCharmLayerChange={(layer, tags) => {
              updateData({ charm_layers: { ...(data.charm_layers || {}), [layer]: tags } })
            }}
            onPrototypeChange={v => updateData({ prototype: v })}
            onTagsChange={tags => updateData({ tags })}
            onResetDefault={() => resetAttributes()}
          />
        )
      default:
        return null
    }
  }

  async function resetAttributes() {
    const ok = await confirm(
      '恢复默认值',
      '将所有光谱值重置为 0，清空魅力层。确定？',
      'warning', '恢复默认'
    )
    if (!ok) return
    updateData({
      spectrum: Object.fromEntries(SPECTRUM_DIMENSIONS.map(d => [d.key, 0])),
      charm_layers: {},
    })
  }
}

// ====== 子组件：基础Tab ======

function BasicTab({
  data,
  onChange,
  avatarUrl,
  mode,
  agentId,
  providers,
  activeModel,
  onActiveModelChange,
  onResetSessionModels,
  resettingSessionModels,
  templates,
  avatarBusy,
  onUploadAvatar,
  onRemoveAvatar,
  onApplyTemplateAvatar,
  onEnsureTemplatesLoaded,
}: {
  data: PersonaData
  onChange: (p: Partial<PersonaData>) => void
  avatarUrl?: string | null
  mode: WorkshopMode
  agentId?: string
  providers: ProviderView[]
  activeModel: ActiveModelView
  onActiveModelChange: (v: ActiveModelView) => void
  /** 仅 edit + 有 agentId 时有值；点了清该角色下所有 session 的 model 字段 */
  onResetSessionModels?: () => Promise<void> | void
  resettingSessionModels?: boolean
  /** 头像三件套（仅 edit 模式有值） */
  templates: Template[]
  avatarBusy: boolean
  onUploadAvatar?: (file: File) => Promise<void> | void
  onRemoveAvatar?: () => Promise<void> | void
  onApplyTemplateAvatar?: (templateId: string) => Promise<void> | void
  /** 用模板的弹层第一次打开时按需触发拉模板列表 */
  onEnsureTemplatesLoaded?: () => Promise<void> | void
}) {
  void agentId // 保留入参，以后可能需要按 agentId 差异化
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [tplPickerOpen, setTplPickerOpen] = useState(false)
  const [dragHover, setDragHover] = useState(false)

  const canUpload = !!onUploadAvatar
  const canApplyTpl = !!onApplyTemplateAvatar

  function handleFileSelect(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (file && onUploadAvatar) {
      void onUploadAvatar(file)
    }
    e.target.value = ''
  }

  function handleDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault()
    setDragHover(false)
    if (!canUpload) return
    const file = e.dataTransfer.files?.[0]
    if (file && onUploadAvatar) {
      void onUploadAvatar(file)
    }
  }

  const showCreateAvatarLinks = mode === 'create' && canApplyTpl

  return (
    <div className="tab-basic">
      {/* === 头像 + 姓名（Notion 风：横排，无边框）=== */}
      <div className="ws-identity-row">
        {/* 外层 wrapper：定位锚点。移除按钮挂在裁剪层之外，才能完整悬在圆角上 */}
        <div className="ws-avatar-wrap">
          <div
            className={`ws-avatar-display ${dragHover ? 'drag-hover' : ''} ${canUpload ? 'can-upload' : ''}`}
            onClick={canUpload ? () => fileInputRef.current?.click() : undefined}
            onDragOver={e => {
              if (!canUpload) return
              e.preventDefault()
              setDragHover(true)
            }}
            onDragLeave={() => setDragHover(false)}
            onDrop={handleDrop}
            title={canUpload
              ? '点击或拖拽图片上传'
              : (mode === 'create' ? '创建角色后可上传自定义头像' : undefined)}
          >
            <AgentAvatar
              name={data.name}
              src={avatarUrl || null}
              imageClassName="ws-avatar-display-img"
              plainClassName="ws-avatar-display-initial"
            />
            {/* 上传中 / hover 时的轻提示遮罩 */}
            {canUpload && (
              <div className={`ws-avatar-overlay ${avatarBusy ? 'busy' : ''}`}>
                {avatarBusy ? <Loader2 size={18} className="spin" /> : <span>更换</span>}
              </div>
            )}
          </div>
          {/* 已上传自定义图时，hover 右上角浮出小 ×（移除）。挂在 wrapper 上，不被圆形裁剪 */}
          {canUpload && onRemoveAvatar && data.avatar_url && (
            <button
              type="button"
              className="ws-avatar-remove"
              onClick={(e) => { e.stopPropagation(); void onRemoveAvatar() }}
              disabled={avatarBusy}
              title="移除自定义头像，回到默认显示"
              aria-label="移除自定义头像"
            >
              <X size={11} strokeWidth={2.5} />
            </button>
          )}
        </div>

        <div className="ws-identity-fields">
          <label className="form-label">姓名 <span className="ws-req">*</span></label>
          <input
            className="form-input"
            type="text"
            value={data.name}
            onChange={e => onChange({ name: e.target.value })}
            placeholder="角色名称"
            autoFocus
          />
          {/* 副线：仅创建模式提示一句"使用模板头像或自定义" */}
          {showCreateAvatarLinks && (
            <p className="ws-identity-hint">
              当前使用模板头像；
              <button
                type="button"
                className="ws-link-btn"
                onClick={() => {
                  setTplPickerOpen(s => !s)
                  if (!tplPickerOpen) void onEnsureTemplatesLoaded?.()
                }}
              >
                换一个模板头像
              </button>
            </p>
          )}
          {/* edit 模式不打扰：只在没图也没拖时静静一行小提示 */}
          {canUpload && !data.avatar_url && (
            <p className="ws-identity-hint">点击头像或拖拽图片即可上传</p>
          )}
        </div>

        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          style={{ display: 'none' }}
          onChange={handleFileSelect}
        />
      </div>

      {/* create 模式：用户主动点了"换一个模板头像"才浮出网格；不占常驻空间 */}
      {showCreateAvatarLinks && tplPickerOpen && (
        <div className="ws-avatar-tpl-picker inline">
          {templates.length === 0 ? (
            <div className="ws-avatar-tpl-empty">加载模板列表中…</div>
          ) : (
            <div className="ws-avatar-tpl-grid">
              {templates
                .filter(t => t.template_id !== 'custom')
                .map(tpl => {
                  const src = templateAvatarSrc(tpl)
                  return (
                    <button
                      key={tpl.template_id}
                      type="button"
                      className="ws-avatar-tpl-item"
                      onClick={() => {
                        void onApplyTemplateAvatar?.(tpl.template_id)
                        setTplPickerOpen(false)
                      }}
                      disabled={avatarBusy}
                      title={tpl.name}
                    >
                      <AgentAvatar
                        name={tpl.name}
                        src={src}
                        imageClassName="ws-avatar-tpl-img"
                        plainClassName="ws-avatar-tpl-initial"
                      />
                    </button>
                  )
                })}
            </div>
          )}
        </div>
      )}

      {/* 角色核心字（只读展示，由模板/meta.json 写死。后续若开放编辑再换 input） */}
      {data.core && (
        <div className="ws-core-display" title="角色核心字（一个汉字 + 解读，概括本质）">
          <span className="ws-core-label">核心</span>
          <span className="ws-core-text">{data.core}</span>
        </div>
      )}

      <div className="form-group">
        <label className="form-label">描述</label>
        <textarea
          className="form-input form-textarea-sm"
          value={data.description}
          onChange={e => onChange({ description: e.target.value })}
          placeholder="一句话描述这个角色..."
          rows={2}
        />
      </div>

      {/* create 和 edit 都显示默认模型选择（对齐 Cherry AgentModal）。
          providers 列表在外层统一拉取；onChange 只更新外层 state，不调 API，
          由顶部“保存”按钮统一 setActiveModel。 */}
      {providers.length > 0 && (
        <AgentModelSelector
          providers={providers}
          value={activeModel}
          onChange={onActiveModelChange}
          onResetSessionModels={onResetSessionModels}
          resetting={!!resettingSessionModels}
        />
      )}
    </div>
  )
}

// ====== 子组件：角色默认模型选择（受控，per-agent model routing）======
//
// 2026-05-10 重构：取消原本“self-managed state + onChange 立即调 API”设计。
// 原设计跟顶部“保存”按钮不是同一事务，造成用户认知割裂（“保存按钮置灰但
// 模型已写盘”），且 create 模式无 agentId 不能调 API，导致 create 没有模型选择 UI。
//
// 现设计（对齐 Cherry AgentModal.tsx::SelectAgentBaseModelButton）：
// - 纯受控组件，外层传 providers / value / onChange
// - 不调 API，onChange 只更新外层 pendingActiveModel state
// - create + edit 都能用；create 模式下创建拿到 agentId 后由外层调 setActiveModel
// - edit 模式下顶部“保存”点击 → 外层 handleEditSave 同步写 active_model + persona md

function AgentModelSelector({
  providers,
  value,
  onChange,
  onResetSessionModels,
  resetting,
}: {
  providers: ProviderView[]
  value: ActiveModelView
  onChange: (v: ActiveModelView) => void
  /** 仅在 edit 模式 + 有 agentId 传入；为 undefined 时不渲染重置按钮 */
  onResetSessionModels?: () => Promise<void> | void
  resetting?: boolean
}) {
  const currentValue = value.model
    ? `${value.provider_id}::${value.model}`
    : ''

  function handleChange(v: string) {
    if (!v) {
      onChange({ provider_id: '', model: '' })
      return
    }
    const sep = v.indexOf('::')
    onChange({
      provider_id: v.slice(0, sep),
      model: v.slice(sep + 2),
    })
  }

  return (
    <div className="form-group">
      <label className="form-label">
        默认模型
        <span className="form-label-hint">（不选则跟随全局默认）</span>
      </label>
      <div className="workshop-model-row">
        <select
          className="form-input workshop-model-select"
          value={currentValue}
          onChange={e => handleChange(e.target.value)}
        >
          <option value="">（使用全局默认）</option>
          {providers.map(p => (
            <optgroup key={p.id} label={p.name || p.id}>
              {p.models.map(m => (
                <option key={`${p.id}::${m.id}`} value={`${p.id}::${m.id}`}>
                  {m.name || m.id}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        {/* 仅在 edit + 有 agentId 时出现：点了清该角色下所有会话的 model override。
            独立于“保存”按钮不绑定事务—点了就马上生效（confirm 保证不误触）。 */}
        {onResetSessionModels && (
          <button
            type="button"
            className="workshop-model-reset-btn"
            onClick={() => { void onResetSessionModels() }}
            disabled={resetting}
            title="重置该角色下所有会话的模型选择—以后都跟随上面的默认模型"
          >
            {resetting ? <Loader2 size={13} className="spin" /> : <RotateCcw size={13} />}
            <span>重置会话</span>
          </button>
        )}
      </div>
    </div>
  )
}

// ====== 子组件：身份Tab ======

function IdentityTab({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="tab-md-section">
      <div className="tab-md-header">
        <h3><Contact size={14} className="tab-md-icon" /> 身份卡 (IDENTITY.md)</h3>
        <p className="tab-md-desc">名片信息、称呼风格、社交面具、身份定位</p>
      </div>
      <MarkdownEditor
        value={value}
        onChange={onChange}
        placeholder={`# 身份\n\n## 名片\n- 称呼：\n- 标题：\n\n## 称呼风格\n- 对老板：\n- 对陌生人：\n\n## 社交面具\n...`}
      />
    </div>
  )
}

// ====== 子组件：灵魂Tab ======

function SoulTab({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="tab-md-section">
      <div className="tab-md-header">
        <h3><Sparkles size={14} className="tab-md-icon" /> 灵魂 (SOUL.md)</h3>
        <p className="tab-md-desc">核心底色、情感规则、行为边界、触发条件</p>
      </div>
      <MarkdownEditor
        value={value}
        onChange={onChange}
        placeholder={`# 灵魂\n\n## 核心底色\n...\n\n## 行为边界\n- 绝对不做的事：\n- 会做的事：\n\n## 情感触发\n...`}
      />
    </div>
  )
}

// ====== 子组件：性格Tab ======

function PersonalityTab({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="tab-md-section">
      <div className="tab-md-header">
        <h3><SlidersHorizontal size={14} className="tab-md-icon" /> 性格 (PERSONALITY.md)</h3>
        <p className="tab-md-desc">性格特征、说话风格、价值观、口头禅、习惯动作</p>
      </div>
      <MarkdownEditor
        value={value}
        onChange={onChange}
        placeholder={`# 性格\n\n## 核心特征\n...\n\n## 说话风格\n- 口头禅：\n- 用词偏好：\n\n## 价值观\n...`}
      />
    </div>
  )
}

// ====== 子组件：基因Tab（高级）=====

function GenomeTab({
  value,
  onChange,
}: {
  value: string
  onChange: (v: string) => void
}) {
  return (
    <div className="tab-genome-section">
      <div className="genome-warning-banner">
        <AlertTriangle size={16} />
        <span>GENOME.md 是角色的底层行为逻辑，研发阶段可自由编辑</span>
      </div>
      <div className="tab-md-header">
        <h3><Dna size={14} className="tab-md-icon" /> 基因 (GENOME.md)</h3>
        <p className="tab-md-desc">冻结 DNA：出厂设定，定义角色最根本的行为范式</p>
      </div>

      <MarkdownEditor
        value={value}
        onChange={onChange}
        placeholder={`# GENOME - 角色基因\n\n## 核心范式\n...\n\n## 行为约束\n...\n\n## 反应模式\n...`}
      />
    </div>
  )
}

// ====== 子组件：属性Tab ======

function AttributesTab({
  spectrum, charmLayers, prototype, tags, advancedMode,
  onSpectrumChange, onCharmLayerChange, onPrototypeChange, onTagsChange, onResetDefault,
}: {
  spectrum: Record<string, number>
  charmLayers: Record<string, string[]>
  prototype: string
  tags: string[]
  advancedMode: boolean
  onSpectrumChange: (key: string, value: number) => void
  onCharmLayerChange: (layer: string, tags: string[]) => void
  onPrototypeChange: (v: string) => void
  onTagsChange: (t: string[]) => void
  onResetDefault: () => void
}) {
  const CHARM_LAYER_NAMES = ['L1_表层', 'L2_深层', 'L3_核心', 'L4_真实'] as const
  
  const [newTagInput, setNewTagInput] = useState('')

  return (
    <div className="tab-attributes">
      {/* 原型 + 标签（始终可编辑） */}
      <div className="attr-section attr-basic">
        <div className="form-group">
          <label className="form-label">原型</label>
          <input
            className="form-input"
            type="text"
            value={prototype}
            onChange={e => onPrototypeChange(e.target.value)}
            placeholder="如：守护者、谋士、恋人..."
          />
        </div>
        
        <div className="form-group">
          <label className="form-label">标签</label>
          <div className="tag-list-editor">
            {(tags || []).map((tag, i) => (
              <span key={i} className="tag-chip">
                #{tag}
                <button
                  className="tag-remove"
                  onClick={() => onTagsChange(tags.filter((_, j) => j !== i))}
                  type="button"
                >×</button>
              </span>
            ))}
            <input
              className="tag-input"
              type="text"
              value={newTagInput}
              onChange={e => setNewTagInput(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && newTagInput.trim()) {
                  e.preventDefault()
                  onTagsChange([...tags, newTagInput.trim()])
                  setNewTagInput('')
                }
              }}
              placeholder="+ 添加标签"
            />
          </div>
        </div>
      </div>

      {/* 高级区域分隔线 */}
      <div className="attr-divider">
        <span>{advancedMode ? '⚡ 高级参数（可编辑）' : '🔒 高级参数（只读）'}</span>
      </div>

      {/* 光谱 */}
      <SpectrumSlider
        values={spectrum}
        onChange={onSpectrumChange}
        disabled={!advancedMode}
      />

      {/* 魅力层 */}
      <div className="charm-layers-section">
        <div className="charm-header">
          <span className="charm-title">✨ 魅力层 (Charm Layers)</span>
          {advancedMode && (
            <button className="charm-reset-btn" onClick={onResetDefault} type="button">
              <RotateCcw size={13} /> 恢复默认
            </button>
          )}
        </div>
        {CHARM_LAYER_NAMES.map(layerKey => {
          const layerTags = charmLayers[layerKey] || []
          return (
            <div key={layerKey} className="charm-layer-item">
              <label className="charm-layer-label">{layerKey.replace('_', ' ')}</label>
              {advancedMode ? (
                <div className="charm-tags-editable">
                  {layerTags.map((tag, i) => (
                    <span key={i} className="charm-tag">
                      {tag}
                      <button
                        className="tag-remove"
                        onClick={() => onCharmLayerChange(layerKey, layerTags.filter((_, j) => j !== i))}
                        type="button"
                      >×</button>
                    </span>
                  ))}
                  <input
                    className="charm-tag-input"
                    type="text"
                    placeholder="+ 添加"
                    onKeyDown={e => {
                      if (e.key === 'Enter' && e.currentTarget.value.trim()) {
                        e.preventDefault()
                        onCharmLayerChange(layerKey, [...layerTags, e.currentTarget.value.trim()])
                        e.currentTarget.value = ''
                      }
                    }}
                  />
                </div>
              ) : (
                <div className="charm-tags-readonly">
                  {layerTags.length > 0
                    ? layerTags.map((t, i) => <span key={i} className="charm-tag readonly">{t}</span>)
                    : <span className="charm-empty">(未设置)</span>
                  }
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
