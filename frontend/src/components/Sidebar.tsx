import { useState, useEffect, useMemo, useRef } from 'react'
import {
  Plus, MessageSquare, Trash2, Edit3,
  Search, MoreHorizontal,
  ChevronDown, ChevronRight, Smile, Upload, FolderOpen, Users, FileArchive,
  Terminal as TerminalIcon, Folder as FolderIcon,
} from 'lucide-react'
import { Session, listSessions, deleteSession, updateSessionTitle, Agent, Template, deleteAgent as apiDeleteAgent, updateAgent as apiUpdateAgent, updateAgentBackend, fetchTemplates, createSession, applyTemplateAvatarBackend, getSession } from '../api/chat'
import { API_BASE, waitForBackend } from '../api/base'
import { useConfirmDialog, useAlertDialog } from './ConfirmDialog'
import AgentAvatar from './AgentAvatar'
import { useResizableWidth, ResizeHandle } from './useResizableWidth'

interface SidebarProps {
  currentSession: Session | null
  currentAgentId: string | null
  activeCollabSessionId?: string
  /** 智能体列表（来自 App.tsx 单一来源，2026-05-24 状态提升）。
   *
   * 历史：Sidebar 曾自己 useState<Agent[]> + 10s 轮询 + 内部所有 setAgents 都只改自己；
   * App.tsx 又有一份独立 agents state 给 Chat/Welcome/RoundtableList 用。
   * 用户从 Sidebar 入口换头像 / 新建 / 删除时，App.tsx 那份永远拿不到，
   * 导致 Chat 漂浮头像 / 关系面板 / 议事厅卡片显示 stale mtime 与错位 agent。
   * 现在统一 source of truth 在 App.tsx，Sidebar 通过 onAgentsChange 把变更推上去。 */
  agents: Agent[]
  /** 同步 agents 变更给上层（接受新值或 prev=>next updater，签名与 React setState 一致）。 */
  onAgentsChange: React.Dispatch<React.SetStateAction<Agent[]>>
  onSelectSession: (session: Session | null) => void
  onSelectAgent: (agentId: string) => void
  runningSessionIds?: Set<string>
  /** 打开角色工坊 */
  onOpenWorkshop?: (mode: 'create' | 'edit', agentId?: string) => void
  /** 打开协作会话（CollabChat） */
  onOpenCollab?: (sessionId: string, metadata: Record<string, unknown>) => void
  /** 打开数据导入导出弹窗 */
  onOpenExportImport?: () => void
}

/** 会话列表折叠阈值 */
const MAX_VISIBLE_SESSIONS = 3

/** 判断头像是否为图片 URL */
function isImageAvatar(avatar: string): boolean {
  return avatar.startsWith('data:') || avatar.startsWith('http')
}

/** 获取智能体头像 src：avatar_url（模板/自定义上传）> base64 > emoji */
function getAvatarSrc(agent: { avatar?: string; avatar_url?: string }): string | null {
  if (agent.avatar_url) return `${API_BASE}${agent.avatar_url}`
  if (agent.avatar && isImageAvatar(agent.avatar)) return agent.avatar
  return null
}

/** 获取情感图标 */
const EMOTION_ICONS: Record<string, string> = {
  '平静': '😌', '开心': '😊', '兴奋': '🤩', '感动': '🥹',
  '思念': '🌙', '担忧': '😟', '委屈': '😢', '傲娇': '😤',
  '专注': '🎯', '疲惫': '😴',
}
function getEmotionIcon(emotion: string): string {
  return EMOTION_ICONS[emotion] || '😌'
}

export default function Sidebar({
  currentSession, currentAgentId, activeCollabSessionId,
  agents, onAgentsChange,
  onSelectSession, onSelectAgent,
  runningSessionIds, onOpenWorkshop, onOpenCollab,
  onOpenExportImport,
}: SidebarProps) {
  // agents 走 props（state 提升到 App.tsx，见 SidebarProps 注释）
  const setAgents = onAgentsChange
  const [sessions, setSessions] = useState<Session[]>([])
  const [searchQuery, setSearchQuery] = useState('')
  const [expandedAgents, setExpandedAgents] = useState<Set<string>>(new Set())
  const [showAllSessions, setShowAllSessions] = useState<Set<string>>(new Set())
  const [templates, setTemplates] = useState<Template[]>([])
  const [templateLoading, setTemplateLoading] = useState(false)
  const [templateError, setTemplateError] = useState('')
  // 编辑 Agent 状态
  const [editingAgentId, setEditingAgentId] = useState<string | null>(null)
  const [editAgentName, setEditAgentName] = useState('')
  const [editAgentDesc, setEditAgentDesc] = useState('')
  // 会话编辑
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null)
  const [editSessionTitle, setEditSessionTitle] = useState('')
  // 下拉菜单状态
  const [openMenuAgentId, setOpenMenuAgentId] = useState<string | null>(null)
  const [menuBtnRect, setMenuBtnRect] = useState<DOMRect | null>(null) // 点击⋯时记录按钮位置
  // 头像选择器
  const [showAvatarPicker, setShowAvatarPicker] = useState<string | null>(null) // agentId
  const [pickerPos, setPickerPos] = useState<{ top: number; left: number; bottomUp?: boolean } | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const pickerRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const sidebarRef = useRef<HTMLDivElement>(null)
  const { confirm: confirmAction, dialog: confirmDialog } = useConfirmDialog()
  const { alert: alertAction, dialog: alertDialog } = useAlertDialog()

  // 主聊天左侧栏宽度（展开态可拖拽调整，折叠态走 .sidebar-collapsed 定宽）
  const { width: chatSidebarWidth, handleProps: chatSidebarResizeProps } = useResizableWidth({
    storageKey: 'lr:chat-sidebar-width',
    defaultWidth: 280,
    min: 220,
    max: 480,
  })

  useEffect(() => {
    // 等 sidecar 起来再拉 sessions / templates（agents 的拉取与 10s 轮询都在 App.tsx 里做，
    // 不在这里重复，避免和 App.tsx 的 setAgents 写串）。
    ;(async () => {
      await waitForBackend()
      loadSessions()
      loadTemplates()
    })()
  }, [])

  // 监听后台标题自动生成 SSE 推送的 window 事件，实时刷新侧栏标题
  useEffect(() => {
    const handleTitleUpdated = (e: Event) => {
      const detail = (e as CustomEvent<{ session_id: string; title: string }>).detail
      if (!detail?.session_id || !detail?.title) return
      setSessions((prev) =>
        prev.map((s) => (s.id === detail.session_id ? { ...s, title: detail.title } : s)),
      )
    }
    window.addEventListener('session_title_updated', handleTitleUpdated)
    return () => window.removeEventListener('session_title_updated', handleTitleUpdated)
  }, [])

  // 只展开"新出现"的 agent（首次填充 / 新建角色），不覆盖用户已收起的旧 agent。
  //
  // 历史踩坑（2026-05-24）：上个版本写成无差别 add 全部 agent id，导致 props.agents 引用一变
  // （比如轮询 / Workshop 回调拉新 list）就把用户手动收起的会话列表又强制展开。
  // 修复方案：用 ref 记录见过的 id，每次 diff 出"新增"才 add，已存在的保留 prev。
  const seenAgentIdsRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    const newOnes = agents.filter(a => !seenAgentIdsRef.current.has(a.id))
    if (newOnes.length > 0) {
      setExpandedAgents(prev => {
        const next = new Set(prev)
        for (const a of newOnes) next.add(a.id)
        return next
      })
    }
    for (const a of agents) seenAgentIdsRef.current.add(a.id)
  }, [agents])

  // 当 currentSession 变化时刷新会话列表（新会话创建后会触发）
  useEffect(() => {
    if (currentSession?.id) {
      loadSessions()
    }
  }, [currentSession?.id])

  // 点击外部关闭菜单/选择器
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpenMenuAgentId(null)
      }
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        setShowAvatarPicker(null)
        setPickerPos(null)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  async function loadSessions() {
    try {
      const response = await listSessions()
      setSessions(response.sessions)
    } catch {}
  }

  async function handleSessionSelect(session: Session) {
    const collabType = session.metadata?.collab_type as string | undefined
    const isCollab = collabType === 'scene'

    if (isCollab && onOpenCollab) {
      onOpenCollab(session.id, session.metadata || {})
      return
    }

    try {
      const liveSession = await getSession(session.id)
      setSessions((prev) => prev.map((item) => (
        item.id === session.id
          ? {
              ...item,
              updatedAt: liveSession.updatedAt,
              messageCount: liveSession.messageCount,
              title: liveSession.title,
              agent_id: liveSession.agent_id,
              metadata: liveSession.metadata,
            }
          : item
      )))
      onSelectSession(liveSession)
    } catch (error) {
      console.warn('[Sidebar] failed to open session:', session.id, error)
      setSessions((prev) => prev.filter((item) => item.id !== session.id))
      if (currentSession?.id === session.id) {
        onSelectSession(null)
      }
      await alertAction('该会话已失效或无法打开，已从当前列表移除。')
      void loadSessions()
    }
  }

  async function loadTemplates() {
    setTemplateLoading(true)
    setTemplateError('')
    try {
      const data = await fetchTemplates()
      setTemplates(data)
    } catch {
      setTemplates([])
      setTemplateError('无法加载头像模板，请检查后端服务是否已启动')
    } finally {
      setTemplateLoading(false)
    }
  }

  const filteredAgents = useMemo(() => {
    // 先按 ID 去重（防止重复 key 警告）
    const uniqueMap = new Map<string, typeof agents[0]>()
    for (const a of agents) {
      if (!uniqueMap.has(a.id)) uniqueMap.set(a.id, a)
    }
    const uniqueAgents = Array.from(uniqueMap.values())

    if (!searchQuery.trim()) return uniqueAgents
    const q = searchQuery.toLowerCase()
    return uniqueAgents.filter(a =>
      a.name.toLowerCase().includes(q) ||
      a.description.toLowerCase().includes(q)
    )
  }, [agents, searchQuery])

  function isAcpSession(s: Session): boolean {
    // 外部编辑器通过 ACP server 模式接入建立的会话——
    // 用 metadata.source === 'acp' 区分，由 backend/acp/server/agent.py:new_session 打标。
    return (s.metadata as Record<string, unknown> | undefined)?.['source'] === 'acp'
  }

  /** 是否为协作场景会话——session.metadata.collab_type === 'scene' */
  function isCollabSceneSession(s: Session): boolean {
    return (s.metadata as Record<string, unknown> | undefined)?.['collab_type'] === 'scene'
  }

  function getAgentSessions(agentId: string): Session[] {
    // 单聊视图只列 1:1 会话；ACP 接入 / scene 协作各自有独立入口（议事厅 / ACP 分组），
    // 避免一条 scene session 被武断挂在 session.agent_id（owner）那个角色下导致重复展示。
    let filtered: Session[]

    if (agentId === 'default') {
      filtered = sessions.filter(s => s.agent_id === 'default' && !isAcpSession(s) && !isCollabSceneSession(s))
    } else {
      filtered = sessions.filter(s => s.agent_id === agentId && !isAcpSession(s) && !isCollabSceneSession(s))
    }

    return filtered.sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime())
  }

  /** 按 acp_cwd 把 ACP 会话分组，给侧栏底部"ACP 外部接入"分组用。 */
  const acpGroups = useMemo(() => {
    const groups = new Map<string, Session[]>()
    for (const s of sessions) {
      if (!isAcpSession(s)) continue
      const cwd = (s.metadata as Record<string, unknown> | undefined)?.['acp_cwd']
      const key = typeof cwd === 'string' && cwd ? cwd : '(unknown cwd)'
      const arr = groups.get(key) ?? []
      arr.push(s)
      groups.set(key, arr)
    }
    // 每组按 updatedAt 倒序
    const out: { cwd: string; sessions: Session[] }[] = []
    for (const [cwd, arr] of groups.entries()) {
      arr.sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime())
      out.push({ cwd, sessions: arr })
    }
    // 组间按"最近活跃"倒序：每组取 max(updatedAt)
    out.sort((a, b) => new Date(b.sessions[0].updatedAt).getTime() - new Date(a.sessions[0].updatedAt).getTime())
    return out
  }, [sessions])

  // ACP 顶级分组 + 各 cwd 子分组的展开状态
  const [acpRootExpanded, setAcpRootExpanded] = useState(false)
  const [expandedAcpCwds, setExpandedAcpCwds] = useState<Set<string>>(new Set())

  /** 点击展开按钮 → 切换展开/收起 */
  function handleExpandClick(agentId: string, e: React.MouseEvent) {
    e.stopPropagation()
    toggleExpand(agentId)
  }

  function toggleExpand(agentId: string) {
    setExpandedAgents(prev => {
      const next = new Set(prev)
      if (next.has(agentId)) next.delete(agentId)
      else next.add(agentId)
      return next
    })
  }

  /** 点击智能体行 → 切换到该智能体主界面并自动展开会话列表 */
  function handleAgentSelect(agent: Agent) {
    setExpandedAgents(prev => {
      if (prev.has(agent.id)) return prev
      const next = new Set(prev)
      next.add(agent.id)
      return next
    })
    onSelectAgent(agent.id)
  }

  /* ====== Agent 拖拽排序 ======
   * HTML5 原生拖拽：整行可拖，从按钮/输入控件上按下的不算。
   * 悬停时按鼠标相对行中点的位置算 before/after 插入线，drop 后按全量列表
   * 重排并归一化 order = index，只对 order 真正变化的 agent 发 PUT 持久化
   * （后端 list_agents 已按 order 排序，App.tsx 的 agents 数组顺序即后端返回顺序）。 */
  const [dragAgentId, setDragAgentId] = useState<string | null>(null)
  const [dropHint, setDropHint] = useState<{ agentId: string; position: 'before' | 'after' } | null>(null)
  // ref 镜像：dragstart 的 setState 异步生效，dragover/drop 要同步读到当前拖的是谁
  const dragAgentIdRef = useRef<string | null>(null)

  function handleAgentDragStart(e: React.DragEvent, agentId: string) {
    if ((e.target as HTMLElement).closest('button, input')) {
      e.preventDefault()
      return
    }
    dragAgentIdRef.current = agentId
    setDragAgentId(agentId)
    e.dataTransfer.effectAllowed = 'move'
    // Firefox 需要 setData 才会真正启动拖拽
    try { e.dataTransfer.setData('text/plain', agentId) } catch { /* 忽略 */ }
  }

  function handleAgentDragOver(e: React.DragEvent, agentId: string) {
    const dragId = dragAgentIdRef.current
    if (!dragId || dragId === agentId) return
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    const rect = e.currentTarget.getBoundingClientRect()
    const after = e.clientY > rect.top + rect.height / 2
    const position = after ? 'after' : 'before'
    setDropHint(prev => (prev?.agentId === agentId && prev.position === position ? prev : { agentId, position }))
  }

  function handleAgentDrop(e: React.DragEvent, targetAgentId: string) {
    e.preventDefault()
    const dragId = dragAgentIdRef.current
    const position = dropHint?.agentId === targetAgentId ? dropHint.position : 'before'
    dragAgentIdRef.current = null
    setDragAgentId(null)
    setDropHint(null)
    if (!dragId || dragId === targetAgentId) return

    // 与 filteredAgents 相同的去重口径，避免重复 id 干扰索引计算
    const unique = Array.from(new Map(agents.map(a => [a.id, a])).values())
    const from = unique.findIndex(a => a.id === dragId)
    if (from < 0 || !unique.some(a => a.id === targetAgentId)) return

    const next = [...unique]
    const [moved] = next.splice(from, 1)
    let insertAt = next.findIndex(a => a.id === targetAgentId)
    if (position === 'after') insertAt += 1
    next.splice(insertAt, 0, moved)

    // 先记旧 order 再归一化，持久化时才知道谁真的变了
    const prevOrders = new Map(unique.map(a => [a.id, a.order]))
    const renumbered = next.map((a, i) => ({ ...a, order: i }))
    setAgents(renumbered)
    void persistAgentOrder(renumbered, prevOrders)
  }

  function handleAgentDragEnd() {
    dragAgentIdRef.current = null
    setDragAgentId(null)
    setDropHint(null)
  }

  /** 把新顺序写回后端（PUT order）。只发有变化的，失败不回滚——下次同步会纠偏。 */
  async function persistAgentOrder(next: Agent[], prevOrders: Map<string, number>) {
    for (let i = 0; i < next.length; i++) {
      const a = next[i]
      if (prevOrders.get(a.id) === i) continue
      try {
        await updateAgentBackend(a.id, { order: i })
      } catch (err) {
        console.warn('[Sidebar] persist agent order failed:', a.id, err)
      }
    }
  }

  /** 压缩图片到指定大小以内（递减 quality） */
  function compressImage(file: File, maxSizeKB: number = 500): Promise<string> {
    return new Promise((resolve, reject) => {
      const img = new Image()
      img.onload = () => {
        // 限制最大尺寸为 256px，减少体积
        const MAX_DIM = 256
        let w = img.width, h = img.height
        if (w > MAX_DIM || h > MAX_DIM) {
          const scale = MAX_DIM / Math.max(w, h)
          w = Math.round(w * scale)
          h = Math.round(h * scale)
        }
        const canvas = document.createElement('canvas')
        canvas.width = w
        canvas.height = h
        const ctx = canvas.getContext('2d')!
        ctx.drawImage(img, 0, 0, w, h)

        // 逐步降 quality 直到满足大小
        const tryCompress = (quality: number): void => {
          const dataUrl = canvas.toDataURL('image/webp', quality)
          // 估算 base64 大小（去掉 data:image/... 前缀后的实际数据）
          const sizeKB = Math.round((dataUrl.length - dataUrl.indexOf(',') - 1) * 3 / 4 / 1024)
          if (sizeKB <= maxSizeKB || quality <= 0.1) {
            resolve(dataUrl)
          } else {
            tryCompress(Math.max(0.1, quality - 0.1))
          }
        }
        tryCompress(0.9)
      }
      img.onerror = () => reject(new Error('图片加载失败'))
      img.src = URL.createObjectURL(file)
    })
  }

  /** 处理上传自定义头像 */
  async function handleUploadAvatar(agentId: string, e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    try {
      const dataUrl = await compressImage(file, 500)
      // 上传到后端保存为文件（不污染 avatar 字段）
      const resp = await fetch(`${API_BASE}/agents/${agentId}/avatar`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image_data: dataUrl })
      })
      if (!resp.ok) throw new Error('Upload failed')
      // 只更新 avatar_url，保持 avatar 为 emoji 不变
      const data = await resp.json()
      if (data?.agent) {
        setAgents(prev => prev.map(a => a.id === agentId ? data.agent : a))
      }
      setShowAvatarPicker(null)
      setPickerPos(null)
      setOpenMenuAgentId(null)
    } catch (err) {
      console.error('Avatar upload failed:', err)
    }
    e.target.value = ''
  }

  /** 删除 Agent（本地 + 后端） */
  async function handleDeleteAgent(agentId: string) {
    const ok = await confirmAction('删除智能体', '确定删除此智能体？其下的会话不会被删除。', 'danger', '删除')
    if (!ok) return
    apiDeleteAgent(agentId)  // 本地删除 + 异步后端删除
    setAgents(prev => prev.filter(a => a.id !== agentId))
    setExpandedAgents(prev => { const n = new Set(prev); n.delete(agentId); return n })
    setOpenMenuAgentId(null)
  }

  /** 更换头像（用角色模板图片） */
  async function handleChangeAvatar(agentId: string, template: Template) {
    try {
      const updated = await applyTemplateAvatarBackend(agentId, template.template_id)
      setAgents(prev => prev.map(a => a.id === agentId ? updated : a))
      setShowAvatarPicker(null)
      setPickerPos(null)
      setOpenMenuAgentId(null)
    } catch (err) {
      console.error('Template avatar apply failed:', err)
    }
  }

  function confirmEditAgent(agentId: string) {
    if (!editAgentName.trim()) return
    const updated = apiUpdateAgent(agentId, { name: editAgentName.trim(), description: editAgentDesc.trim() })
    if (updated) setAgents(prev => prev.map(a => a.id === agentId ? updated : a))
    setEditingAgentId(null)
  }

  /** 为指定角色新建会话 */
  async function handleNewSessionForAgent(agentId: string) {
    try {
      const agent = agents.find(a => a.id === agentId)
      const agentName = agent?.name || '助手'
      const title = agentId === 'default' ? '新对话' : `与${agentName}的对话`
      const session = await createSession(title, agentId)
      setSessions(prev => [...prev, session])
      onSelectSession(session)
    } catch (err) {
      console.error('Failed to create session:', err)
    }
  }

  async function handleDeleteSession(id: string, e: React.MouseEvent) {
    e.stopPropagation()
    const ok = await confirmAction('删除对话', '确定删除这个对话？此操作不可恢复。', 'danger', '删除')
    if (!ok) return
    try {
      await deleteSession(id)
      setSessions(prev => prev.filter(s => s.id !== id))
      if (currentSession?.id === id) onSelectSession(null)
    } catch (err: any) {
      await alertAction(`删除失败：${err?.message || err || '未知错误'}`)
      try {
        const response = await listSessions()
        setSessions(response.sessions)
      } catch {}
    }
  }

  function startEditSession(id: string, title: string, e: React.MouseEvent) {
    e.stopPropagation()
    setEditingSessionId(id)
    setEditSessionTitle(title)
  }

  async function confirmEditSession(id: string) {
    if (!editSessionTitle.trim()) return
    try {
      const updated = await updateSessionTitle(id, editSessionTitle.trim())
      setSessions(prev => prev.map(s => s.id === id ? updated : s))
      if (currentSession?.id === id) onSelectSession(updated)
    } catch {}
    setEditingSessionId(null)
  }

  /* ====== 展开模式（唯一模式）======
   *
   * 2026-05-19 删 sidebar-collapsed 头像栏中间态：用户反馈 "点头像不是直接切会话，
   * 是展开 + 继续选" — 实际没节省步骤反而占 52px 带。改为 Sidebar 只有“展开态”，
   * "隐藏态" 由 .sidebar-wrapper display:none 控制（App.tsx 中的 sidebarOpen state）。
   */
  // 主聊天左侧栏可拖拽调宽（仅展开态）
  return (
    <div
      className="sidebar"
      ref={sidebarRef}
      style={{ ['--sidebar-width' as string]: `${chatSidebarWidth}px` } as React.CSSProperties}
    >
      {/* 搜索栏 */}
      <div className="sidebar-search">
        <Search size={16} className="search-icon" />
        <input
          type="text"
          placeholder="搜索"
          value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          className="search-input"
        />
      </div>

      {/* 新建 Agent 按钮 */}
      <button className="new-agent-btn" onClick={() => onOpenWorkshop?.('create')}>
        <Plus size={16} />
        <span>新建 Agent</span>
      </button>

      {/* 数据导入/导出 */}
      {onOpenExportImport && (
        <button
          className="new-agent-btn eid-trigger-btn"
          onClick={onOpenExportImport}
          title="导出角色 / 全局记忆 ZIP，或从 ZIP 合并回本机"
        >
          <FileArchive size={14} />
          <span>数据导入 / 导出</span>
        </button>
      )}

      {/* Agent + 会话列表 */}
      <div className="agent-session-list">
        {filteredAgents.map(agent => {
          const agentSessions = getAgentSessions(agent.id)
          const isExpanded = expandedAgents.has(agent.id)
          const isShowAll = showAllSessions.has(agent.id)
          const displaySessions = isShowAll ? agentSessions : agentSessions.slice(0, MAX_VISIBLE_SESSIONS)
          const hasMore = agentSessions.length > MAX_VISIBLE_SESSIONS

          return (
            <div key={agent.id} className="agent-group">
              {/* Agent 行：点击整行 = 切换到该智能体；整行可拖拽排序（按钮上按下除外） */}
              <div
                className={`agent-header ${currentAgentId === agent.id ? 'active' : ''} ${dragAgentId === agent.id ? 'dragging' : ''} ${dropHint?.agentId === agent.id ? (dropHint.position === 'after' ? 'drop-after' : 'drop-before') : ''}`}
                onClick={() => handleAgentSelect(agent)}
                draggable
                onDragStart={(e) => handleAgentDragStart(e, agent.id)}
                onDragOver={(e) => handleAgentDragOver(e, agent.id)}
                onDrop={(e) => handleAgentDrop(e, agent.id)}
                onDragEnd={handleAgentDragEnd}
                title="拖拽调整顺序"
              >
                {/* 头像：纯展示，无交互 */}
                <div className="agent-avatar-btn">
                  <AgentAvatar
                    name={agent.name}
                    src={getAvatarSrc(agent)}
                    imageClassName="agent-avatar-img"
                    plainClassName="agent-avatar"
                  />
                </div>

                {/* 名称 + 描述 + 情感 */}
                <div className="agent-info">
                  <span className="agent-name">
                    {agent.name}
                      {agent.emotion && agent.emotion !== '平静' && (
                        <span className="agent-emotion-badge" title={`情感: ${agent.emotion}`}>
                          {getEmotionIcon(agent.emotion)}
                        </span>
                      )}
                      {agentSessions.length > 1 && (
                        <span className="peer-awareness-badge" title={`${agentSessions.length} 个对话`}>
                          {agentSessions.length}
                        </span>
                      )}
                    </span>
                  <span className="agent-desc">{agent.description}</span>
                </div>

                {/* ⋯ 更多按钮（在展开按钮左边） */}
                <button
                  className="agent-more-btn"
                  onClick={(e) => {
                    e.stopPropagation()
                    const rect = e.currentTarget.getBoundingClientRect()
                    if (openMenuAgentId === agent.id) {
                      setOpenMenuAgentId(null)
                      setMenuBtnRect(null)
                    } else {
                      setMenuBtnRect(rect)
                      setOpenMenuAgentId(agent.id)
                      setShowAvatarPicker(null)
                    }
                  }}
                  title="更多操作"
                >
                  <MoreHorizontal size={16} />
                </button>

                {/* 展开/收起按钮（最右边） */}
                <button
                  className="agent-expand-btn"
                  onClick={(e) => handleExpandClick(agent.id, e)}
                  title={isExpanded ? '收起会话' : '展开会话'}
                >
                  {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </button>


              </div>

              {/* 会话列表（展开时显示） */}
              {isExpanded && agentSessions.length > 0 && (
                <div className="agent-sessions">
                  {displaySessions.map(session => {
                    const collabType = session.metadata?.collab_type as string | undefined
                    const isCollab = collabType === 'scene'
                    const handleClick = () => { void handleSessionSelect(session) }
                    return (
                    <div
                      key={session.id}
                      className={`session-item ${(currentSession?.id === session.id || activeCollabSessionId === session.id) ? 'active' : ''} ${isCollab ? `session-item-collab session-item-collab-scene` : ''}`}
                      onClick={handleClick}
                    >
                      {isCollab ? (
                        <Users size={14} className={`session-icon session-icon-collab session-icon-collab-scene`} />
                      ) : (
                        <MessageSquare size={14} className="session-icon" />
                      )}
                      {runningSessionIds?.has(session.id) && <span className="session-running-indicator" />}
                      {editingSessionId === session.id ? (
                        <input
                          className="session-edit-input"
                          value={editSessionTitle}
                          onChange={e => setEditSessionTitle(e.target.value)}
                          onBlur={() => confirmEditSession(session.id)}
                          onKeyDown={e => { if (e.key === 'Enter') confirmEditSession(session.id); if (e.key === 'Escape') setEditingSessionId(null) }}
                          autoFocus
                        />
                      ) : (
                        <>
                          <div className="session-main">
                            {isCollab && (
                              <div className="session-collab-meta">
                                <span className="session-collab-tag scene">协作</span>
                                <span className="session-collab-open">协作工作台</span>
                              </div>
                            )}
                            <span className={`session-title ${isCollab ? `session-title-collab session-title-collab-scene` : ''}`}>
                              {session.title}
                            </span>
                          </div>
                          <div className="session-actions" onClick={e => e.stopPropagation()}>
                            <button className="action-icon" onClick={(e) => startEditSession(session.id, session.title, e)} title="重命名">
                              <Edit3 size={12} />
                            </button>
                            <button className="action-icon danger" onClick={(e) => handleDeleteSession(session.id, e)} title="删除">
                              <Trash2 size={12} />
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                    )
                  })}
                  {/* 超过3条 → 展开/收起更多 */}
                  {hasMore && (
                    <button
                      className="show-more-btn"
                      onClick={(e) => {
                        e.stopPropagation()
                        setShowAllSessions(prev => {
                          const next = new Set(prev)
                          if (next.has(agent.id)) next.delete(agent.id)
                          else next.add(agent.id)
                          return next
                        })
                      }}
                    >
                      {isShowAll ? (
                        <><ChevronRight size={14} /><span>收起</span></>
                      ) : (
                        <><MoreHorizontal size={14} /><span>展开更多 ({agentSessions.length - MAX_VISIBLE_SESSIONS})</span></>
                      )}
                    </button>
                  )}
                </div>
              )}

              {/* 展开但无会话 */}
              {isExpanded && agentSessions.length === 0 && (
                <div className="agent-empty-hint">
                  <button className="new-session-inline-btn" onClick={(e) => { e.stopPropagation(); handleNewSessionForAgent(agent.id) }}>
                    <Plus size={14} /> 新建对话
                  </button>
                </div>
              )}

              {/* 展开时底部新建会话按钮 */}
              {isExpanded && agentSessions.length > 0 && (
                <button className="new-session-inline-btn" onClick={(e) => { e.stopPropagation(); handleNewSessionForAgent(agent.id) }}>
                  <Plus size={14} /> 新建对话
                </button>
              )}
            </div>
          )
        })}

        {filteredAgents.length === 0 && searchQuery.trim() && (
          <div className="agents-empty">
            <p>未找到匹配的智能体</p>
          </div>
        )}

        {/* ====== ACP 外部接入：跟角色同级的顶级分组 ======
            来源：外部兼容编辑器通过 ACP server 模式接入开启的会话
            后端打 metadata.source='acp' + metadata.acp_cwd；按 cwd 二级聚合。
            搜索词存在时整组隐藏（避免干扰搜索体验）。 */}
        {acpGroups.length > 0 && !searchQuery.trim() && (
          <div className="agent-group acp-group">
            <div
              className={`agent-header acp-header`}
              onClick={() => setAcpRootExpanded(v => !v)}
              title="外部编辑器通过 ACP 连接产生的会话"
            >
              <div className="agent-avatar-btn acp-icon-wrap">
                <TerminalIcon size={18} className="acp-icon" />
              </div>
              <div className="agent-info">
                <span className="agent-name">
                  ACP 外部接入
                  <span className="peer-awareness-badge" title="项目数 · 会话数">
                    {acpGroups.length}·{acpGroups.reduce((n, g) => n + g.sessions.length, 0)}
                  </span>
                </span>
                <span className="agent-desc">来自外部编辑器</span>
              </div>
              <button
                className="agent-expand-btn"
                onClick={(e) => { e.stopPropagation(); setAcpRootExpanded(v => !v) }}
                title={acpRootExpanded ? '收起' : '展开'}
              >
                {acpRootExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              </button>
            </div>

            {acpRootExpanded && acpGroups.map(group => {
              const isCwdExpanded = expandedAcpCwds.has(group.cwd)
              const shortCwd = group.cwd.length > 32
                ? '…' + group.cwd.slice(-32)
                : group.cwd
              return (
                <div key={group.cwd} className="acp-cwd-group">
                  <div
                    className="acp-cwd-header"
                    onClick={() => {
                      setExpandedAcpCwds(prev => {
                        const next = new Set(prev)
                        if (next.has(group.cwd)) next.delete(group.cwd)
                        else next.add(group.cwd)
                        return next
                      })
                    }}
                    title={group.cwd}
                  >
                    <FolderIcon size={13} className="acp-cwd-icon" />
                    <span className="acp-cwd-path">{shortCwd}</span>
                    <span className="acp-cwd-count">{group.sessions.length}</span>
                    {isCwdExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                  </div>

                  {isCwdExpanded && (
                    <div className="agent-sessions">
                      {group.sessions.map(session => {
                        const handleClick = () => { void handleSessionSelect(session) }
                        return (
                          <div
                            key={session.id}
                            className={`session-item ${currentSession?.id === session.id ? 'active' : ''}`}
                            onClick={handleClick}
                          >
                            <MessageSquare size={14} className="session-icon" />
                            {runningSessionIds?.has(session.id) && <span className="session-running-indicator" />}
                            {editingSessionId === session.id ? (
                              <input
                                className="session-edit-input"
                                value={editSessionTitle}
                                onChange={e => setEditSessionTitle(e.target.value)}
                                onBlur={() => confirmEditSession(session.id)}
                                onKeyDown={e => { if (e.key === 'Enter') confirmEditSession(session.id); if (e.key === 'Escape') setEditingSessionId(null) }}
                                autoFocus
                              />
                            ) : (
                              <>
                                <div className="session-main">
                                  <span className="session-title">{session.title}</span>
                                </div>
                                <div className="session-actions" onClick={e => e.stopPropagation()}>
                                  <button className="action-icon" onClick={(e) => startEditSession(session.id, session.title, e)} title="重命名">
                                    <Edit3 size={12} />
                                  </button>
                                  <button className="action-icon danger" onClick={(e) => handleDeleteSession(session.id, e)} title="删除">
                                    <Trash2 size={12} />
                                  </button>
                                </div>
                              </>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* 右边缘拖拽调宽手柄（双击重置）— sidebar 收起按钮已处理到 main-header 左上 hamburger，
          底部工具栏 .sidebar-footer 已删 */}
      <ResizeHandle {...chatSidebarResizeProps} />

      {/* ====== Agent 下拉菜单浮层（fixed 定位，脱离列表流） ====== */}
      {openMenuAgentId && menuBtnRect && (() => {
        const agent = agents.find(a => a.id === openMenuAgentId)
        if (!agent) return null
        return (
          <div className="agent-menu-overlay" onClick={() => { setOpenMenuAgentId(null); setMenuBtnRect(null) }}>
            <div
              className="agent-dropdown-fixed"
              ref={menuRef}
              style={{
                position: 'fixed',
                top: menuBtnRect.bottom + 4,
                left: Math.max(8, menuBtnRect.right - 150)  // 右对齐按钮，至少离左边 8px
              }}
              onClick={e => e.stopPropagation()}
            >
              <button className="dropdown-item" onClick={() => {
                // 用菜单按钮位置算头像选择器位置
                const sidebarRect = sidebarRef.current?.getBoundingClientRect()
                const left = sidebarRect?.left ?? menuBtnRect.left
                const bottomUp = menuBtnRect.bottom > window.innerHeight - 260
                const top = bottomUp ? menuBtnRect.top - 4 : menuBtnRect.bottom + 4
                setPickerPos({ top, left, bottomUp })
                setShowAvatarPicker(agent.id)
                setOpenMenuAgentId(null)
                setMenuBtnRect(null)
              }}>
                <Smile size={14} />
                <span>更换头像</span>
              </button>
              <button className="dropdown-item" onClick={() => { onOpenWorkshop?.('edit', agent.id); setOpenMenuAgentId(null); setMenuBtnRect(null) }}>
                <Edit3 size={14} />
                <span>人设工坊</span>
              </button>
              <button className="dropdown-item" onClick={async () => {
                try {
                  const res = await fetch(`${API_BASE}/agents/${agent.id}/open-folder`, { method: 'POST' })
                  const data = await res.json()
                  if (!data.success) console.error('打开文件夹失败:', data.error)
                } catch (e) { console.error('打开文件夹失败:', e) }
                setOpenMenuAgentId(null); setMenuBtnRect(null)
              }}>
                <FolderOpen size={14} />
                <span>打开文件夹</span>
              </button>
              {agents.length > 1 && (
                <button className="dropdown-item danger" onClick={() => handleDeleteAgent(agent.id)}>
                  <Trash2 size={14} />
                  <span>删除智能体</span>
                </button>
              )}
            </div>
          </div>
        )
      })()}

      {/* ====== 头像选择器浮层（脱离列表流） ====== */}
      {showAvatarPicker && pickerPos && (() => {
        const agent = agents.find(a => a.id === showAvatarPicker)
        if (!agent) return null
        return (
          <div
            className="avatar-picker-overlay"
            onClick={() => { setShowAvatarPicker(null); setPickerPos(null) }}
          >
            <div
              className="avatar-picker"
              ref={pickerRef}
              style={{
                position: 'fixed',
                top: pickerPos.bottomUp ? 'auto' : pickerPos.top,
                bottom: pickerPos.bottomUp ? (window.innerHeight - pickerPos.top) : 'auto',
                left: pickerPos.left,
              }}
              onClick={e => e.stopPropagation()}
            >
              {getAvatarSrc(agent) && (
                <div className="avatar-current-preview">
                  <img src={getAvatarSrc(agent)!} alt="" className="avatar-preview-img" />
                  <span className="avatar-preview-label">当前头像</span>
                </div>
              )}
              <div className="avatar-picker-grid">
                {templateLoading ? (
                  <div className="template-empty">加载头像模板中...</div>
                ) : templateError ? (
                  <button className="template-error" onClick={loadTemplates}>
                    {templateError}<br /><small>点击重试</small>
                  </button>
                ) : templates.length === 0 ? (
                  <div className="template-empty">暂无可用头像模板</div>
                ) : (
                  templates.map(tpl => (
                    <button
                      key={tpl.template_id}
                      className={`avatar-picker-item ${agent.avatar_template === tpl.template_id ? 'active' : ''}`}
                      onClick={() => handleChangeAvatar(agent.id, tpl)}
                      title={tpl.name}
                    >
                      <img
                        src={`${API_BASE}${tpl.avatar_url || `/agents/templates/${tpl.template_id}/avatar`}`}
                        alt=""
                        style={{ width: 32, height: 32, borderRadius: 8, objectFit: 'cover' }}
                      />
                    </button>
                  ))
                )}
              </div>
              <div className="avatar-upload-section">
                <button
                  className="avatar-upload-btn"
                  onClick={() => fileInputRef.current?.click()}
                >
                  <Upload size={14} />
                  <span>上传自定义头像</span>
                </button>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*"
                  style={{ display: 'none' }}
                  onChange={(e) => handleUploadAvatar(agent.id, e)}
                />
              </div>
            </div>
          </div>
        )
      })()}

      {/* ====== 编辑 Agent 对话框 ====== */}
      {editingAgentId && (() => {
        const agent = agents.find(a => a.id === editingAgentId)
        if (!agent) return null
        return (
          <div className="new-agent-overlay" onClick={() => setEditingAgentId(null)}>
            <div className="new-agent-dialog" onClick={e => e.stopPropagation()}>
              <h3 className="dialog-title">编辑智能体</h3>
              <div className="dialog-form">
                <label className="form-label">名称</label>
                <input
                  type="text"
                  className="form-input"
                  placeholder="智能体名称"
                  value={editAgentName}
                  onChange={e => setEditAgentName(e.target.value)}
                  autoFocus
                />
                <label className="form-label">描述</label>
                <input
                  type="text"
                  className="form-input"
                  placeholder="一句话描述"
                  value={editAgentDesc}
                  onChange={e => setEditAgentDesc(e.target.value)}
                />
              </div>
              <div className="dialog-buttons">
                <button className="btn-cancel" onClick={() => setEditingAgentId(null)}>取消</button>
                <button className="btn-save" onClick={() => confirmEditAgent(agent.id)} disabled={!editAgentName.trim()}>保存</button>
              </div>
            </div>
          </div>
        )
      })()}
      {confirmDialog}
      {alertDialog}
    </div>
  )
}
