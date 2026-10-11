import { useState, useEffect, useRef, useMemo } from 'react'
import Chat, { type ChatHandle } from './components/Chat'
import { Suspense, lazy } from 'react'
import Sidebar from './components/Sidebar'
import PrimaryRail, { type PrimaryView } from './components/PrimaryRail'
import RoundtableList from './components/RoundtableList'
import CollabEmptyState from './components/CollabEmptyState'

import { getConfig, Session, Agent, syncAgentsFromBackend, ensureDefaultAgent, listSessions, getSession, getActiveScenes, type SceneInfo, getGameNotifications, type ConsultStreamEvent } from './api/chat'
import { API_BASE, waitForBackend } from './api/base'
import { startPolling } from './lib/pollingLoop'
import { SummonButton } from './components/SummonButton'
import { RelationPanel } from './components/RelationPanel'
import { JealousyIndicator } from './components/JealousyIndicator'
import { ToastProvider, useToast, gameNotificationToToast } from './components/ToastSystem'
import { useGlobalEventStream } from './components/useGlobalEventStream'
import { SessionTokenUsageBadge } from './components/SessionTokenUsageBadge'
import { IdeStatusBar } from './components/IdeStatusBar'
import {
  Sun, Moon, BookOpen, Wrench, Trophy, BarChart3, Clock,
  Minus, Square, Copy, X as CloseIcon, Settings, PanelLeftClose, PanelLeftOpen,
} from 'lucide-react'
import { getRuntime, type Runtime } from './runtime'
import type { WorkshopMode } from './components/CharacterWorkshop'
import { applyTheme, getStoredTheme, type Theme } from './theme'
import { AppLoading } from './components/AppLoading'
import { UpdaterProvider } from './context/UpdaterContext'
import { TopBarUpdateBadge } from './components/TopBarUpdateBadge'
import { UpdateModal } from './components/UpdateModal'

const MemoryPanel = lazy(() => import('./components/MemoryPanel'))
const ToolStatsPanel = lazy(() => import('./components/ToolStatsPanel'))
// 旧浮动 TasksPanel 已撤离顶栏，改由 Chat 内 InlineTasksPanel + Portal 双锚点呈现
// （plan goofy-cooking-breeze.md C5）。文件保留备查不 delete。
const CronJobsPanel = lazy(() => import('./components/CronJobsPanel'))
const CharacterWorkshop = lazy(() => import('./components/CharacterWorkshop'))
const ExportImportDialog = lazy(() => import('./components/ExportImportDialog'))
const AchievementPanel = lazy(() =>
  import('./components/AchievementPanel').then((module) => ({ default: module.AchievementPanel })),
)
const CollabChat = lazy(() => import('./components/CollabChat').then((module) => ({ default: module.CollabChat })))
const SettingsView = lazy(() => import('./components/settings/SettingsView'))
import type { SettingsTab } from './components/settings/SettingsView'

interface Config {
  identity: { name: string; identity: string; style: string }
  user: { title: string; role: string }
}


// ====== 全局事件 SSE 订阅（跨 session toast）======
// 接 v2 EventBus + 后端 /events/global 端点。当前处理 cron_fired。
// 接 agents 让 toast 能展示『角色名+头像』而不是冷冰冰的 agent_id。
function GlobalEventStream({ agents }: { agents: Agent[] }) {
  useGlobalEventStream(agents)
  return null
}

function isManagedChildWindowLabel(label: string): boolean {
  return label === 'memory-browser' || label === 'character-workshop-create' || label.startsWith('character-workshop-')
}

// ====== 通知轮询组件 ======
function NotificationPoller() {
  const { addToast } = useToast()

  useEffect(() => {
    const poll = async () => {
      try {
        const data = await getGameNotifications()
        if (data.notifications && data.notifications.length > 0) {
          for (const notif of data.notifications) {
            const toast = gameNotificationToToast(notif)
            if (toast) addToast(toast)
          }
        }
      } catch { /* 后端不在线，静默 */ }
    }

    // 首次立即拉取 + 每 30 秒轮询（不重叠：上一次没回来就跳过这一 tick）
    return startPolling(poll, 30000)
  }, [addToast])

  return null
}

// ====== 主 App ======
function LazyFeatureFallback({ label = '界面载入中...' }: { label?: string }) {
  return <AppLoading title="" subtitle={label} showProgress={false} logoSize={54} />
}

function AppInner() {
  const { addToast } = useToast()
  const [, setConfig] = useState<Config | null>(null)
  const [loading, setLoading] = useState(true)
  const [theme, setTheme] = useState<Theme>(() => getStoredTheme())
  const [currentSession, setCurrentSession] = useState<Session | null>(null)
  const [currentAgentId, setCurrentAgentId] = useState<string | null>('default')

  // 持久化当前会话 ID，刷新后自动恢复
  const LAST_SESSION_KEY = 'liuruyan_last_session_id'
  // 持久化"议事厅里打开的协作会话"（2026-09-29）：刷新/重启后恢复深度协作。
  // 之前只记页签（primaryView），协作会话本身是内存状态刷新即失——刷新后停在
  // "议事厅空态"，得重新点一次列表。退出协作 / 解散时清除。
  const LAST_COLLAB_KEY = 'liuruyan_last_collab_session_id'
  // 同步 currentAgentId 到 session 绑定角色：初始化恢复 / 新建 session 后，工具栏选中
  // 角色（currentAgent）才能跟实际会话角色一致，避免停在 'default'（如烟）错位。
  // 无条件同步——即便 agents 列表暂未含该角色，currentAgentId 也应反映真实 session 角色，
  // 而不是保留旧值（onSelectSession 的 agents.some 守卫曾因此漏同步）。
  const persistAndSetSession = (session: Session | null) => {
    if (session) {
      setCurrentSession(session)
      setCurrentAgentId(session.agent_id || 'default')
      localStorage.setItem(LAST_SESSION_KEY, session.id)
    } else {
      setCurrentSession(null)
      localStorage.removeItem(LAST_SESSION_KEY)
    }
  }
  const [showMemory, setShowMemory] = useState(false)
  const [showToolStats, setShowToolStats] = useState(false)
  const [showCron, setShowCron] = useState(false)
  const [showAchievements, setShowAchievements] = useState(false)
  /**
   * Sidebar 开关（桌面 + mobile 统一 state）。
   *
   * 历史：之前桌面用 sidebarCollapsed 控制 280↔52px 头像栏切换，mobile 用 mobileSidebarOpen
   * 控制 drawer overlay；2026-05-19 用户决定 "桌面也要沉浸式"——折叠态头像栏没节省切角色
   * 步骤反而占空间，改为统一 "展开 ↔ 完全隐藏" 两态，桌面 mobile 共用一个 state。
   *
   * 行为：
   * - 桌面（>768px）：sidebar 占位 280px ↔ display:none 完全脱离 layout（main-area 占满）
   * - mobile（≤768px）：drawer overlay 滑入 ↔ 完全隐藏在屏幕外
   * - 切换入口：main-header 左上 hamburger（PanelLeftClose / PanelLeftOpen）
   * - 自动关闭：mobile 上选会话/选 agent 后 auto-close（drawer 惯例）；桌面保持展开
   * - 默认值：桌面 true（展开，保留旧 UX 习惯）/ mobile false（隐藏 = 沉浸式聊天）
   *
   * 跨产品惯例：ChatGPT / Claude / Cursor 桌面 mobile 都同一位置同一行为。
   */
  const [sidebarOpen, setSidebarOpen] = useState(() => {
    if (typeof window === 'undefined') return true  // SSR fallback
    // localStorage 优先：用户上次手动关 / 开的状态会被记住，跨刷新 / 重启保持。
    // 隐私模式 / iOS Safari 14 等可能禁用 localStorage，try/catch 兜底走默认值。
    try {
      const saved = window.localStorage.getItem('sidebarOpen')
      if (saved === 'true') return true
      if (saved === 'false') return false
    } catch {
      /* localStorage 禁用 → fallthrough 走默认值 */
    }
    return window.innerWidth > 768  // 首次访问：桌面 true / mobile false（768px 跟 CSS @media 断点对齐）
  })
  // 持久化 sidebarOpen 到 localStorage（仿 primaryView 模式）。
  // setSidebarOpen 任意点（hamburger 点击 / mobile auto-close 选会话）都会触发，统一 sink。
  useEffect(() => {
    try {
      localStorage.setItem('sidebarOpen', String(sidebarOpen))
    } catch {
      /* localStorage 禁用时不持久化（state 仍正常工作只是不跨刷新保留） */
    }
  }, [sidebarOpen])
  const [isMaximized, setIsMaximized] = useState(false)
  const [runtime, setRuntime] = useState<Runtime | null>(null)
  const [agents, setAgents] = useState<Agent[]>([])
  const chatRef = useRef<ChatHandle | null>(null)
  const consultSessionByStreamRef = useRef<Record<string, string>>({})
  const shownInvalidSessionWarningRef = useRef<string>('')
  const dragLastTimeRef = useRef(0)
  const dragLastPosRef = useRef({ x: 0, y: 0 })
  const closingMainWindowRef = useRef(false)

  // 角色工坊状态
  const [workshopOpen, setWorkshopOpen] = useState(false)
  const [workshopMode, setWorkshopMode] = useState<'create' | 'edit'>('edit')
  const [workshopAgentId, setWorkshopAgentId] = useState<string>()

  // 数据导入/导出弹窗
  const [exportImportOpen, setExportImportOpen] = useState(false)

  async function closeTauriChildWindows() {
    if (runtime?.kind !== 'tauri') return
    try {
      const { getAllWebviewWindows } = await import('@tauri-apps/api/webviewWindow')
      const windows = await getAllWebviewWindows()
      await Promise.all(
        windows
          .filter(win => isManagedChildWindowLabel(win.label))
          .map(win => win.close().catch(() => {}))
      )
    } catch (err) {
      console.warn('[App] close child windows failed:', err)
    }
  }

  async function closeMainWindowWithChildren() {
    if (!runtime || closingMainWindowRef.current) return
    closingMainWindowRef.current = true
    await closeTauriChildWindows()
    await runtime.window.close()
  }

  async function openMemoryBrowser(aid?: string) {
    if (runtime?.kind === 'tauri') {
      try {
        const { WebviewWindow } = await import('@tauri-apps/api/webviewWindow')
        const label = 'memory-browser'
        const existing = await WebviewWindow.getByLabel(label)
        if (existing) {
          await existing.setFocus()
          return
        }
        const params = new URLSearchParams()
        if (aid) params.set('agentId', aid)
        new WebviewWindow(label, {
          url: `/#memory-browser${params.toString() ? `?${params.toString()}` : ''}`,
          title: '记忆浏览器',
          width: 900,
          height: 680,
          minWidth: 640,
          minHeight: 500,
          center: true,
          resizable: true,
          decorations: false,
        })
        return
      } catch (err) {
        console.warn('[App] open standalone memory browser failed, fallback to in-app panel', err)
      }
    }
    setShowMemory(true)
    setShowToolStats(false)
  }

  async function openCharacterWorkshop(mode: WorkshopMode, aid?: string) {
    if (runtime?.kind === 'tauri') {
      try {
        const { WebviewWindow } = await import('@tauri-apps/api/webviewWindow')
        const label = aid ? `character-workshop-${aid}` : 'character-workshop-create'
        const existing = await WebviewWindow.getByLabel(label)
        if (existing) {
          await existing.setFocus()
          return
        }
        const params = new URLSearchParams({ mode })
        if (aid) params.set('agentId', aid)
        new WebviewWindow(label, {
          url: `/#character-workshop?${params.toString()}`,
          title: mode === 'create' ? '创建新角色' : '人设工坊',
          width: 760,
          height: 720,
          minWidth: 560,
          minHeight: 520,
          center: true,
          resizable: true,
          decorations: false,
        })
        return
      } catch (err) {
        console.warn('[App] open standalone character workshop failed, fallback to in-app panel', err)
      }
    }

    setWorkshopMode(mode)
    setWorkshopAgentId(aid)
    setWorkshopOpen(true)
  }

  // 叙事场景状态（从 Chat 提升上来）
  const [activeScene, setActiveScene] = useState<SceneInfo | null>(null)
  const [showCollabChat, setShowCollabChat] = useState(false)
  const [existingCollabSessionId, setExistingCollabSessionId] = useState<string | undefined>(undefined)

  // 主视图：伙伴（单聊） | 议事厅（协作）
  const [primaryView, setPrimaryView] = useState<PrimaryView>(() => {
    const stored = localStorage.getItem('primaryView')
    // 刷新时如果停留在设置页，自动切回聊天视图（避免看不到消息）
    if (stored === 'settings') return 'companions'
    // 'media' 是旧 key（内容视图早期命名），读到旧值平滑迁移成 'content'，不丢用户上次停留的 tab
    return stored === 'roundtable' ? 'roundtable' : 'companions'
  })
  useEffect(() => {
    localStorage.setItem('primaryView', primaryView)
  }, [primaryView])

  // 设置页跳转上下文：从主聊天点「权限」时定位到 permissions tab + 当前 agent。
  // 切回非 settings 后清空，避免下次开设置又被锚定。
  const [settingsInitialTab, setSettingsInitialTab] = useState<SettingsTab | undefined>(undefined)
  const [settingsFocusAgentId, setSettingsFocusAgentId] = useState<string | null>(null)
  const handleJumpToPermissions = (agentId: string | null) => {
    setSettingsInitialTab('permissions')
    setSettingsFocusAgentId(agentId)
    setPrimaryView('settings')
  }

  // 全局跳设置入口：任意组件 dispatch `lry:open-settings`（detail: {tab, agentId}）
  // 即可定位到设置页对应 tab。目前消费者：SessionTokenUsageBadge 的"工具定义偏高"提示。
  useEffect(() => {
    const onOpen = (e: Event) => {
      const detail = (e as CustomEvent<{ tab?: SettingsTab; agentId?: string | null }>).detail
      if (!detail?.tab) return
      setSettingsInitialTab(detail.tab)
      setSettingsFocusAgentId(detail.agentId ?? null)
      setPrimaryView('settings')
    }
    window.addEventListener('lry:open-settings', onOpen)
    return () => window.removeEventListener('lry:open-settings', onOpen)
  }, [])

  // 设置页"懒加载 + 不卸载"标志：第一次进入 settings 才 mount，之后切走只 display:none，
  // 保留 SettingsView 内部 tab/滚动/未保存草稿等本地状态。
  // 同理 Sidebar / main-area / Chat 也始终挂载，避免 SSE reader 被 GC、头像闪到 fallback、
  // sidebar useEffect 重新拉一遍 sessions/agents（用户反馈"切回来好像又重新加载了一遍"）。
  const [settingsEverMounted, setSettingsEverMounted] = useState(false)
  useEffect(() => {
    if (primaryView === 'settings') setSettingsEverMounted(true)
  }, [primaryView])

  // 主聊天页 ChatModelPicker 点"管理模型"时派发 `liuruyan:open-model-settings`，
  // 这里监听跳到设置页 providers tab。
  useEffect(() => {
    let unlisten: (() => void) | undefined
    if (runtime?.kind === 'tauri') {
      import('@tauri-apps/api/event')
        .then(({ listen }) => listen('liuruyan://agents-changed', () => {
          syncAgentsFromBackend().then(setAgents).catch(() => {})
        }))
        .then((fn) => { unlisten = fn })
        .catch(() => {})
    }
    return () => { unlisten?.() }
  }, [runtime?.kind])

  useEffect(() => {
    const onOpen = () => {
      setSettingsInitialTab('providers')
      setSettingsFocusAgentId(null)
      setShowMemory(false)
      setShowToolStats(false); setShowAchievements(false); setShowCron(false)
      setPrimaryView('settings')
    }
    window.addEventListener('liuruyan:open-model-settings', onOpen)
    return () => window.removeEventListener('liuruyan:open-model-settings', onOpen)
  }, [])

  // 追踪正在运行的会话ID（用于侧边栏转圈指示）
  const [runningSessionIds, setRunningSessionIds] = useState<Set<string>>(new Set())

  // 议事厅 badge 数据：所有 collab 会话 ID（轮询，不依赖 runningSessionIds 重启）
  const [collabSessionIds, setCollabSessionIds] = useState<Set<string>>(new Set())
  useEffect(() => {
    let cancelled = false
    async function refresh() {
      try {
        const resp = await listSessions()
        if (cancelled) return
        const ids = new Set<string>()
        for (const s of resp.sessions) {
          const ct = s.metadata?.collab_type
          if (ct === 'quick' || ct === 'scene') ids.add(s.id)
        }
        setCollabSessionIds(ids)
      } catch { /* silent */ }
    }
    const stop = startPolling(refresh, 15000)
    return () => { cancelled = true; stop() }
  }, [])
  const roundtableRunningCount = useMemo(() => {
    let n = 0
    runningSessionIds.forEach(id => { if (collabSessionIds.has(id)) n++ })
    return n
  }, [runningSessionIds, collabSessionIds])
  const handleLoadingChange = (sessionId: string | null, loading: boolean) => {
    if (!sessionId) return
    setRunningSessionIds(prev => {
      const next = new Set(prev)
      if (loading) next.add(sessionId)
      else next.delete(sessionId)
      return next
    })
  }

  const notifyInvalidSessions = (count?: number) => {
    if (!count) return
    const warningKey = String(count)
    if (shownInvalidSessionWarningRef.current === warningKey) return
    shownInvalidSessionWarningRef.current = warningKey
    addToast({
      type: 'info',
      title: '发现归属异常会话',
      desc: `发现 ${count} 个归属异常会话，已从侧栏隐藏，请删除或手动处理。`,
      icon: '⚠️',
      urgency: 'medium',
      color: '#f59e0b',
    })
  }

  // 加载配置 + 检测后端连通性 + 自动加载会话
  const [backendOnline, setBackendOnline] = useState(true)

  useEffect(() => {
    async function loadConfig() {
      // 等 sidecar 起来再发第一批请求，避免头像 <img> 打到空端口被 404 负缓存
      await waitForBackend()
      try {
        const data = await getConfig()
        setConfig(data)
        setBackendOnline(true)

        // 加载 agents 列表（首启兜底：后端为空时 ensureDefaultAgent 会从模板创建柳如烟，
        //   2026-05-24 状态提升：原本这步在 Sidebar mount 时做，现在搬到 App.tsx 单点）
        try {
          const agentList = await ensureDefaultAgent()
          setAgents(agentList)
        } catch { /* agents 加载失败不影响主流程 */ }

        // 自动加载会话列表并选中默认会话
        try {
          const response = await listSessions()
          notifyInvalidSessions(response.session_warnings?.invalid_session_count)
          const sessions = response.sessions
          // 2026-09-29：恢复目标跳过协作会话——协作会话只能从议事厅进（RoundtableList /
          // Sidebar 协作区 → handleOpenCollabSession）。主聊壳渲染协作数据会缺议事厅的
          // 编排 UI（阶段轨/指定发言/工具摘要），刷新后若最近会话恰好是协作，主聊会把
          // 它当普通单聊渲染成"半个议事厅"。
          const isCollabSession = (s?: Session) => s?.metadata?.collab_type === 'scene'
          // 优先恢复上次会话，回退到 default 或第一个非协作会话
          const savedId = localStorage.getItem(LAST_SESSION_KEY)
          const savedSess = savedId ? sessions.find(s => s.id === savedId) : undefined
          const targetSess =
            (savedSess && !isCollabSession(savedSess) ? savedSess : undefined) ||
            sessions.find(s => s.id === 'default' && !isCollabSession(s)) ||
            sessions.find(s => !isCollabSession(s))
          if (targetSess) {
            // 优先用完整对象（含 messages），失败时回退轻量版
            try {
              const fullSession = await getSession(targetSess.id)
              persistAndSetSession(fullSession)
            } catch {
              persistAndSetSession(targetSess)
            }
          }

          // 2026-09-29：恢复议事厅深度协作。刷新/重启后若上次打开过协作会话（且该
          // session 仍存在），直接回到协作界面——场景/消息都已持久化，历史加载走
          // useCollabHistoryLoader，无需重跑流。场景已 resolve 时 matchedScene 为
          // null，协作界面照常打开（与从列表点开已结束会话的行为一致）。
          // 注意 primaryView 读的是 useState 初始化时的 localStorage 快照。
          try {
            const savedCollabId = localStorage.getItem(LAST_COLLAB_KEY)
            if (savedCollabId) {
              const collabSess = sessions.find(s => s.id === savedCollabId)
              if (collabSess && collabSess.metadata?.collab_type === 'scene') {
                const sceneId = collabSess.metadata.scene_id as string | undefined
                let matchedScene: SceneInfo | null = null
                if (sceneId) {
                  try {
                    const { scenes } = await getActiveScenes()
                    matchedScene = scenes.find(s => s.id === sceneId) || null
                  } catch { /* 场景快照失败 → 无阶段信息打开，不影响历史 */ }
                }
                setExistingCollabSessionId(savedCollabId)
                setActiveScene(matchedScene)
                setShowCollabChat(true)
              } else {
                localStorage.removeItem(LAST_COLLAB_KEY)
              }
            }
          } catch { /* 协作恢复失败不影响主流程 */ }
        } catch { /* 会话列表加载失败不影响主流程 */ }
      } catch (err) {
        console.error('Failed to fetch config:', err)
        setBackendOnline(false)
      } finally {
        setLoading(false)
      }
    }
    loadConfig()

    // 定期检测后端是否在线（每10秒）。
    //
    // 不再轮询 agents 列表（2026-05-24 取消）：原本 Sidebar 内部 10s 轮询是为了刷新
    // emotion / emotion_intensity 字段（情感图标），但实际 UI 上情感图标早已不展示，
    // 轮询变成纯消耗。改为纯事件驱动：
    //   - 启动一次 ensureDefaultAgent（见上方）
    //   - Workshop onAgentCreated / onAgentUpdated 显式回调
    //   - Sidebar 内上传头像 / 删除 / 模板 / 改名等局部 setAgents 经 onAgentsChange 上推
    // 真要恢复情感图标实时性，未来走 SSE 推送（已有事件总线）而不是轮询。
    return startPolling(async () => {
      try {
        const res = await fetch(`${API_BASE}/health`)
        if (res.ok) setBackendOnline(true)
      } catch {
        setBackendOnline(false)
      }
    }, 10000, { immediate: false })
  }, [])
  // 应用主题
  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  // 窗口控制（走 runtime 抽象层，Tauri / VSCode webview / web 兜底；见 docs/vscode_extension_plan.md §6.2）
  const handleMinimize = () => { void runtime?.window.minimize() }
  const handleMaximize = async () => {
    if (!runtime) return
    await runtime.window.toggleMaximize()
    setIsMaximized(await runtime.window.isMaximized())
  }
  const handleClose = () => { void closeMainWindowWithChildren() }

  // 拖拽 + 双击最大化
  // 关键：mousedown 时不立刻 startDragging（否则 OS 进入模态拖拽循环会吞掉第二次 mousedown），
  // 而是等到鼠标真正移动 > 2px 才开始拖拽。纯双击（不移动）永不触发 startDragging，双击可正常识别。
  const handleDragAreaMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0) return
    if ((e.target as HTMLElement).closest('button, [data-no-drag]')) return

    const now = Date.now()
    const dx0 = Math.abs(e.clientX - dragLastPosRef.current.x)
    const dy0 = Math.abs(e.clientY - dragLastPosRef.current.y)
    if (now - dragLastTimeRef.current < 350 && dx0 < 6 && dy0 < 6) {
      dragLastTimeRef.current = 0
      handleMaximize()
      return
    }
    dragLastTimeRef.current = now
    dragLastPosRef.current = { x: e.clientX, y: e.clientY }

    const startX = e.clientX
    const startY = e.clientY
    const onMove = (me: MouseEvent) => {
      if (Math.abs(me.clientX - startX) > 2 || Math.abs(me.clientY - startY) > 2) {
        cleanup()
        void runtime?.window.startDragging()
      }
    }
    const onUp = () => cleanup()
    const cleanup = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  // 初始化 runtime（环境探测 + dynamic import 加载对应实现）+ 注册 resize 监听
  // 阶段 1 改造，见 docs/vscode_extension_plan.md §6.2
  useEffect(() => {
    let active = true
    let unlistenFn: (() => void) | null = null
    let closeUnlistenFn: (() => void) | null = null

    void getRuntime().then((r) => {
      if (!active) return
      setRuntime(r)
      // 立刻同步一次 maximized 状态（用户在 runtime 就绪前如果点过最大化按钮也能纠正回来）
      void r.window.isMaximized().then((v) => {
        if (active) setIsMaximized(v)
      })
      // 注册 resize 监听
      void r.window.onResized(() => {
        if (!active) return
        void r.window.isMaximized().then((v) => {
          if (active) setIsMaximized(v)
        })
      }).then((fn) => {
        if (!active) { fn(); return }
        unlistenFn = fn
      })
      if (r.kind === 'tauri') {
        void import('@tauri-apps/api/window')
          .then(({ getCurrentWindow }) => getCurrentWindow().onCloseRequested(async (event) => {
            if (closingMainWindowRef.current) return
            event.preventDefault()
            closingMainWindowRef.current = true
            try {
              const { getAllWebviewWindows } = await import('@tauri-apps/api/webviewWindow')
              const windows = await getAllWebviewWindows()
              await Promise.all(
                windows
                  .filter(win => isManagedChildWindowLabel(win.label))
                  .map(win => win.close().catch(() => {}))
              )
            } catch (err) {
              console.warn('[App] close child windows failed:', err)
            }
            await r.window.close()
          }))
          .then((fn) => {
            if (!active) { fn(); return }
            closeUnlistenFn = fn
          })
          .catch(() => {})
      }
    })

    return () => {
      active = false
      unlistenFn?.()
      closeUnlistenFn?.()
    }
  }, [])

  const currentAgent = agents.find((agent) => agent.id === currentAgentId) || null
  const resolveAgentName = (agentId?: string) => {
    if (!agentId) return '对方'
    return agents.find((agent) => agent.id === agentId)?.name || agentId
  }
  const ritualTimestamp = (base?: string, offsetMs = 0) => {
    const source = base ? new Date(base).getTime() : Date.now()
    return new Date(source + offsetMs).toISOString()
  }
  const currentWorkspaceName = currentSession?.title || currentAgent?.name || '主聊天工作台'
  const isDeepCollabMode = showCollabChat && Boolean(activeScene || existingCollabSessionId)
  // 2026-09-29：主聊面板可见性。停在议事厅页签但没打开协作时，主区域显示议事厅
  // 空态，不再漏出主聊残留——之前是"左边议事厅列表、右边主聊消息"的错位画面。
  // Chat 始终挂载（always-mount 纪律不变），只是 display:none。
  const showChatPanel = primaryView === 'companions'
    || (primaryView !== 'roundtable' && !isDeepCollabMode)

  const handleExitDeepCollab = () => {
    setShowCollabChat(false)
    setExistingCollabSessionId(undefined)
    setActiveScene(null)
    setPrimaryView('companions')
    // 用户显式退出协作：清掉持久化的协作会话，刷新后不再恢复
    localStorage.removeItem(LAST_COLLAB_KEY)
  }

  const handlePrimaryViewChange = (next: PrimaryView) => {
    if (next === 'companions') {
      // 切回伙伴：不退出协作，让 CollabChat 在后台保持挂载（SSE 流继续）
      // 显式退出协作走 handleExitDeepCollab（CollabHeaderBar 的关闭/解散按钮 / Sidebar 切会话）
      setPrimaryView('companions')
      // 离开 settings：清掉跳转上下文（下次手动开设置回到默认 tab）
      setSettingsInitialTab(undefined)
      setSettingsFocusAgentId(null)
      return
    }
    if (next === 'settings') {
      // 切设置页：不退出协作（切回时状态还在），并关闭任何底部面板
      setShowMemory(false)
      setShowToolStats(false); setShowAchievements(false); setShowCron(false)
      setPrimaryView('settings')
      return
    }

    // 切换到议事厅：仅切视图，不改变当前 collab 状态
    setPrimaryView('roundtable')
    setSettingsInitialTab(undefined)
    setSettingsFocusAgentId(null)
  }

  const handleConsultEvent = ({ streamId, event }: { streamId: string; event: ConsultStreamEvent }) => {
    const knownSessionId = consultSessionByStreamRef.current[streamId]
    const sessionId = event.session_id || knownSessionId
    if (!sessionId) return

    if (!knownSessionId) {
      consultSessionByStreamRef.current[streamId] = sessionId
    }

    if (event.type === 'consult_start') {
      const agentName = resolveAgentName(event.agent_id)
      consultSessionByStreamRef.current[streamId] = sessionId
      chatRef.current?.insertRitualMarker({
        sessionId,
        streamId,
        phase: 'consult_start',
        agentId: event.agent_id,
        agentName,
        content: `✦ 已召唤 ${agentName} ✦`,
        timestamp: ritualTimestamp(event.timestamp, -1),
      })
      chatRef.current?.startExternalStream({
        sessionId,
        streamId,
        agentId: event.agent_id || '',
        content: '',
        timestamp: event.timestamp,
      })
      return
    }

    if (event.type === 'content' || event.type === 'thinking' || event.type === 'tool_call' || event.type === 'tool_result' || event.type === 'system_notice') {
      // 如果还没有启动流式消息，先启动一个（兼容没有 consult_start 的场景）
      if (!knownSessionId) {
        chatRef.current?.startExternalStream({
          sessionId,
          streamId,
          agentId: event.agent_id || '',
          content: '',
          timestamp: event.timestamp,
        })
        consultSessionByStreamRef.current[streamId] = sessionId
      }

      chatRef.current?.pushExternalStreamEvent({
        sessionId,
        streamId,
        event: {
          type: event.type,
          content: event.content,
          kind: event.kind,
          name: event.name,
          input: event.input,
          result: event.result,
          error: event.error,
          id: event.id,
          tool_use_id: event.tool_use_id,
          agent_id: event.agent_id,
        },
      })
      return
    }

    if (event.type === 'consult_end') {
      const agentId = event.agent_id || event.message?.segments?.find((segment) => segment.agent_id)?.agent_id
      const agentName = resolveAgentName(agentId)
      chatRef.current?.finishExternalStream({
        sessionId,
        streamId,
        content: event.message?.content || event.response,
        timestamp: event.message?.timestamp || event.timestamp,
        segments: event.message?.segments,
      })
      chatRef.current?.insertRitualMarker({
        sessionId,
        streamId,
        phase: 'consult_end',
        agentId,
        agentName,
        content: '✦ 此次召唤已结束 ✦',
        timestamp: ritualTimestamp(event.message?.timestamp || event.timestamp, 1),
      })
      delete consultSessionByStreamRef.current[streamId]
      return
    }

    if (event.type === 'error') {
      const agentName = resolveAgentName(event.agent_id)
      chatRef.current?.failExternalStream({
        sessionId,
        streamId,
        agentId: event.agent_id,
        error: event.error,
      })
      chatRef.current?.insertRitualMarker({
        sessionId,
        streamId,
        phase: 'consult_error',
        agentId: event.agent_id,
        agentName,
        content: '✦ 此次召唤未能完成 ✦',
        timestamp: ritualTimestamp(undefined, 1),
      })
      delete consultSessionByStreamRef.current[streamId]
    }
  }

  /** 创建新协作后立即跳转进去（议事厅 CollabLauncher onCreated）
   *  后端 POST /social/scenes 同时建 session 并返回 session_id，前端直接用，无需 listSessions 反查
   */
  const handleSceneCreated = (scene: SceneInfo, sessionId?: string) => {
    setExistingCollabSessionId(sessionId)
    setActiveScene(scene)
    setShowCollabChat(true)
    setPrimaryView('roundtable')
    if (sessionId) localStorage.setItem(LAST_COLLAB_KEY, sessionId)
  }

  /** 从侧栏/议事厅/主聊重新打开协作会话 */
  const handleOpenCollabSession = async (sessionId: string, metadata: Record<string, unknown>) => {
    const sceneId = metadata.scene_id as string | undefined
    if (sceneId) {
      try {
        const { scenes } = await getActiveScenes()
        const matchedScene = scenes.find((item) => item.id === sceneId) || null
        setActiveScene(matchedScene)
      } catch {
        setActiveScene(null)
      }
    } else {
      setActiveScene(null)
    }
    setExistingCollabSessionId(sessionId)
    setShowCollabChat(true)
    setPrimaryView('roundtable')
    // 记住打开的协作会话：刷新/重启后自动回到协作界面（2026-09-29）
    localStorage.setItem(LAST_COLLAB_KEY, sessionId)
  }

  if (loading) {
    return <AppLoading />
  }

  return (
    <div className="app-layout">
      {/* 通知轮询器 */}
      <NotificationPoller />

      {/* 全局事件 SSE 订阅（cron_fired 等跨 session toast） */}
      <GlobalEventStream agents={agents} />

      {/* ===== Shell：Rail 贯通整高，右侧包 top-bar + app-body ===== */}
      <div className="app-shell">
        <PrimaryRail
          view={primaryView}
          onChange={handlePrimaryViewChange}
          roundtableRunningCount={roundtableRunningCount}
          onTopMouseDown={handleDragAreaMouseDown}
        />
        <div className="app-right">

      {/* ===== 第一层：全局顶栏（可拖拽 + 窗口控制）===== */}
      <header className="top-bar" onMouseDown={handleDragAreaMouseDown}>
        <div className="top-bar-left">
          {/* 拖拽区域（之前的 mobile hamburger 按钮已挪到 main-header 左上，桌面 mobile 同位置） */}
          <TopBarUpdateBadge />
        </div>

        <div className="top-bar-right">
          <div className="window-controls" data-tauri-drag-region={false}>
            {/* VSCode runtime 下显示 IDE 桥接状态 chip（其他 runtime 自动隐身）。
                inline 渲染（不用 portal），自然继承 .window-controls 的 flex 排版。 */}
            <IdeStatusBar />
            <button
              className={`window-ctrl-btn window-settings-btn ${primaryView === 'settings' ? 'active' : ''}`}
              onClick={() => handlePrimaryViewChange(primaryView === 'settings' ? 'companions' : 'settings')}
              title={primaryView === 'settings' ? '关闭设置 (Ctrl+,)' : '设置 (Ctrl+,)'}
            >
              <Settings size={14} strokeWidth={1.8} />
            </button>
            <button className="window-ctrl-btn" onClick={handleMinimize} title="最小化">
              <Minus size={14} strokeWidth={2} />
            </button>
            <button className="window-ctrl-btn" onClick={handleMaximize} title={isMaximized ? '还原' : '最大化'}>
              {isMaximized ? (
                <Copy size={13} strokeWidth={1.8} style={{ transform: 'scaleX(-1)' }} />
              ) : <Square size={13} strokeWidth={1.8} />}
            </button>
            <button className="window-ctrl-btn window-close-btn" onClick={handleClose} title="关闭">
              <CloseIcon size={14} strokeWidth={2} />
            </button>
          </div>
        </div>
      </header>

      {/* ===== 第二层：侧边栏 + 主内容区（并排）=====
       *  关键：三种视图（companions / roundtable / settings）都"始终挂载"，
       *  仅用 display:none 切换。原因（用户 2026-05-14 反馈）：
       *    - Chat unmount → useChatSessionRuntime / SSE reader 引用丢，但 useAppStore
       *      仍在跑（"消息还在走"但本地态错位）
       *    - Sidebar unmount/remount → useEffect 重新 listSessions/syncAgents
       *      （"侧边栏好像又重新加载了一遍"）
       *    - Chat 首帧 useMemo 找不到匹配 agent → fallback agents[0]
       *      （"头像变成别人的"）
       *  类比 isDeepCollabMode 下 CollabChat 的 display:none 处理（line 811 注释）。 */}
      <div className="app-body">
        {/* Drawer backdrop：仅 mobile + drawer open 时可见（CSS @media 控制）。
            桌面下 display: none（CSS），DOM 在但零渲染开销。点击关闭 drawer。
            命名跟 .sidebar-wrapper 配套，统一 sidebar-* 前缀。 */}
        <div
          className={`sidebar-drawer-backdrop ${sidebarOpen ? 'sidebar-open' : ''}`}
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />

        {/* companions 视图 sidebar：always mounted。
            wrapper 用 className 控制 mobile drawer 行为：
              - 桌面：display: contents（透明 wrapper，sidebar 直接作为 .app-body flex child）
              - mobile：display: block + position: fixed + transform，整个 wrapper 滑入滑出 */}
        <div
          className={`sidebar-wrapper ${sidebarOpen ? 'sidebar-open' : ''}`}
          data-view-active={primaryView === 'companions'}
          style={primaryView === 'companions' ? undefined : { display: 'none' }}
        >
          <Sidebar
            currentSession={currentSession}
            currentAgentId={currentAgentId}
            activeCollabSessionId={isDeepCollabMode ? existingCollabSessionId : undefined}
            agents={agents}
            onAgentsChange={setAgents}
            onSelectSession={(s) => {
              // 2026-05-22：不再调 handleExitDeepCollab——选单聊会话只是看不同会话，
              //   议事厅状态应保留（跟 line 484 切视图的语义对齐）。
              //   原逻辑会清 activeScene/existingCollabSessionId→切回议事厅丢 session。
              persistAndSetSession(s)
              // mobile auto-close drawer（drawer 惯例）；桌面保持展开方便继续操作
              if (typeof window !== 'undefined' && window.innerWidth <= 768) setSidebarOpen(false)
              if (!s) return
              if (s.agent_id === 'default') {
                setCurrentAgentId('default')
                return
              }
              if (s.agent_id && agents.some((agent) => agent.id === s.agent_id)) {
                setCurrentAgentId(s.agent_id)
              }
            }}
            onSelectAgent={(agentId) => {
              // 2026-05-22：不再调 handleExitDeepCollab——选 agent 只是看伙伴默认页，
              //   议事厅该隐藏但保持挂载（跟 onSelectSession 对齐）。
              setCurrentAgentId(agentId)
              persistAndSetSession(null)  // 切 agent 时清空会话记忆
              // mobile auto-close drawer；桌面保持展开
              if (typeof window !== 'undefined' && window.innerWidth <= 768) setSidebarOpen(false)
            }}
            runningSessionIds={runningSessionIds}
            onOpenWorkshop={(mode, aid) => {
              void openCharacterWorkshop(mode, aid)
            }}
            onOpenCollab={handleOpenCollabSession}
            onOpenExportImport={() => setExportImportOpen(true)}
          />
        </div>

        {/* roundtable 视图：always mounted。
            跟 companions wrapper 一样的 mobile drawer 行为——roundtable 列表也是
            "左侧导航"性质，mobile 上应该用 drawer 而非常驻。 */}
        <div
          className={`sidebar-wrapper ${sidebarOpen ? 'sidebar-open' : ''}`}
          data-view-active={primaryView === 'roundtable'}
          style={primaryView === 'roundtable' ? undefined : { display: 'none' }}
        >
          <RoundtableList
            agents={agents}
            activeCollabSessionId={isDeepCollabMode ? existingCollabSessionId : undefined}
            runningSessionIds={runningSessionIds}
            onOpenCollab={handleOpenCollabSession}
            onCollabCreated={handleSceneCreated}
          />
        </div>

        {/* settings 视图：首次进入才 lazy mount，之后保持挂载用 display 切换 */}
        {settingsEverMounted && (
          <Suspense fallback={primaryView === 'settings' ? <LazyFeatureFallback label="设置页载入中..." /> : null}>
            <div
              style={{
                display: primaryView === 'settings' ? 'contents' : 'none',
              }}
            >
              <SettingsView
                initialTab={settingsInitialTab}
                focusAgentId={settingsFocusAgentId}
                onCloseSettings={() => handlePrimaryViewChange('companions')}
              />
            </div>
          </Suspense>
        )}

        
        {/* 主内容区（Chat / CollabChat）：always mounted；settings/workflow 模式 display:none */}
        <div
          className="main-area"
          style={primaryView === 'settings' ? { display: 'none' } : undefined}
        >
          {/* 第三层：主内容区头栏（功能按钮）—— 协作模式不渲染，CollabHeaderBar 自带完整头部
           *  切换伙伴视图时即使处于 deep collab 也显示单聊主头，因为此时主可见面板是 Chat
           *  2026-09-29：议事厅页签 + 无活跃协作时也不渲染（主区域显示议事厅空态） */}
          {showChatPanel && (
          <div className="main-header">
            <div className="main-header-status">
              {/* Sidebar 切换按钮 — 桌面 mobile 同位置同行为：
                  桌面：sidebar 280px ↔ display:none（main-area 占满），
                  mobile：drawer overlay 滑入 ↔ 完全隐藏。
                  跟 ChatGPT / Claude / Cursor 一致放在 main-header 左上。
                  图标按当前状态切：展开时 Close 图标提示"点我收起"，反之亦然。 */}
              <button
                type="button"
                className="sidebar-toggle-btn"
                onClick={() => setSidebarOpen((v) => !v)}
                aria-label={sidebarOpen ? '收起侧边栏' : '展开侧边栏'}
                title={sidebarOpen ? '收起侧边栏' : '展开侧边栏'}
              >
                {sidebarOpen ? <PanelLeftClose size={17} strokeWidth={1.8} /> : <PanelLeftOpen size={17} strokeWidth={1.8} />}
              </button>
              <div className="workspace-pill workspace-pill-main">
                <span>{currentWorkspaceName}</span>
              </div>
              <SessionTokenUsageBadge sessionId={currentSession?.id} />
            </div>

            <div className="main-header-actions" data-tauri-drag-region={false}>
              <>
                  <SummonButton
                    currentAgentId={currentAgentId || ''}
                    currentSessionId={currentSession?.id || ''}
                    agents={agents}
                    onConsultEvent={handleConsultEvent}
                    onGoRoundtable={() => setPrimaryView('roundtable')}
                  />
                  <RelationPanel
                    currentAgentId={currentAgentId || ''}
                    agents={agents}
                  />
                  <JealousyIndicator
                    currentAgentId={currentAgentId || ''}
                    agents={agents}
                  />
                  <button
                    className={`icon-btn ${showAchievements ? 'active' : ''}`}
                    onClick={() => setShowAchievements(!showAchievements)}
                    title="成就之殿"
                  >
                    <Trophy size={17} />
                  </button>
                  <button
                    className={`icon-btn ${theme === 'light' ? 'active' : ''}`}
                    onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
                    title={theme === 'dark' ? '切换亮色模式' : '切换暗色模式'}
                  >
                    {theme === 'dark' ? <Sun size={17} /> : <Moon size={17} />}
                  </button>
                  <button
                    className={`icon-btn ${showMemory ? 'active' : ''}`}
                    onClick={() => { void openMemoryBrowser(currentAgentId || undefined) }}
                    title="记忆浏览器"
                  >
                    <BookOpen size={17} />
                  </button>
                  <button
                    className="icon-btn"
                    onClick={() => {
                      setSettingsInitialTab('skills')
                      setSettingsFocusAgentId(currentAgentId)
                      setShowMemory(false); setShowToolStats(false)
                      setShowAchievements(false); setShowCron(false)
                      setPrimaryView('settings')
                    }}
                    title="技能管理（跳到设置·技能）"
                  >
                    <Wrench size={17} />
                  </button>
                  <button
                    className={`icon-btn ${showToolStats ? 'active' : ''}`}
                    onClick={() => { setShowToolStats(!showToolStats); setShowMemory(false) }}
                    title="工具统计"
                  >
                    <BarChart3 size={17} />
                  </button>
                  <button
                    className={`icon-btn ${showCron ? 'active' : ''}`}
                    onClick={() => { setShowCron(!showCron); setShowMemory(false); setShowToolStats(false) }}
                    title="定时任务"
                  >
                    <Clock size={17} />
                  </button>
            </>
            </div>
          </div>
          )}

          {/* 聊天主内容
           *  关键：CollabChat 在 isDeepCollabMode 时始终挂载，以保证切到伙伴视图后
           *  SSE 流、bubbles、思考链继续在后台运行；切回议事厅能看到实时进度。
           *  只用 CSS 隐藏（display:none），不 unmount；否则 fetch reader 会被 GC，流断。 */}
          {isDeepCollabMode && (
            <Suspense fallback={<LazyFeatureFallback label="协作界面载入中..." />}>
              <div
                className="collab-main-shell"
                style={primaryView === 'roundtable' ? undefined : { display: 'none' }}
              >
                <CollabChat
                  scene={activeScene || undefined}
                  agents={agents}
                  onClose={handleExitDeepCollab}
                  existingSessionId={existingCollabSessionId}
                  embedded
                  sidebarOpen={sidebarOpen}
                  onToggleSidebar={() => setSidebarOpen((v) => !v)}
                />
              </div>
            </Suspense>
          )}
          {/* Chat 主聊天：always mounted；切到议事厅 + 深度协作模式时仅 display:none 隐藏。
           *  bug 修复（2026-05-23）：原写法用条件渲染（mount/unmount）切换 Chat，导致
           *  单聊正在跑时切去深度协作议事厅再切回，Chat 内部 isLoading / sessionAbortMapRef /
           *  sessionLoadingMapRef 全丢，停止按钮永久消失，无法终止当前任务。
           *  跟 CollabChat 在 isDeepCollabMode 模式下的 display:none 处理对称（line 923-940），
           *  并与 line 711-719 注释的"三种视图都始终挂载"纪律一致。
           *
           *  P1.6 决策（2026-05-23 架构重构后）：保留 always-mount。
           *  虽然 P1.1-1.4b 把 loading / messageMeta / streamRegistry / externalStreamMessageIds /
           *  timelineMarkers 全部迁到 useAppStore + module-level，理论上 Chat unmount/remount
           *  不再丢状态，可以恢复条件渲染。但仍保留 always-mount，原因：
           *    1) 切换更快（无 React reconciliation / re-mount 开销）
           *    2) useChatSubmitFlow 内部还有 routedProgress* / schedulerPanorama*Cache 等 ref
           *       未迁移（P1.4c 推迟），always-mount 保证它们也不会蒸发
           *    3) 多个长任务并发时这个开销差距更明显
           *  架构决策文档：docs/architecture_spec.md */}
          <div
            className="chat-main-shell"
            style={showChatPanel
              ? { flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, minWidth: 0, overflow: 'hidden' }
              : { display: 'none' }}
          >
            <Chat
              ref={chatRef}
              currentSession={currentSession}
              currentAgentId={currentAgentId}
              backendOnline={backendOnline}
              agents={agents}
              onSessionCreated={(session) => persistAndSetSession(session)}
              onLoadingChange={handleLoadingChange}
              onJumpToPermissions={handleJumpToPermissions}
            />
          </div>

          {/* 议事厅空态：停在议事厅页签但没打开协作时占住主区域，避免"左议事厅列表 +
              右主聊残留消息"的错位画面（2026-09-29）。发起入口在左侧列表头部。 */}
          {primaryView === 'roundtable' && !isDeepCollabMode && (
            <div className="roundtable-empty-main">
              <CollabEmptyState
                emoji="🏛️"
                title="议事厅"
                description="从左侧选择一个协作会话，或点击「发起协作」开一场新的"
                tip="协作由两名以上角色同台参与"
              />
            </div>
          )}
        </div>
      </div>
        </div>{/* /app-right */}
      </div>{/* /app-shell */}

      {/* 记忆面板 */}
      {showMemory && (
        <Suspense fallback={<LazyFeatureFallback label="记忆面板载入中..." />}>
          <MemoryPanel onClose={() => setShowMemory(false)} agentId={currentAgentId || undefined} agents={agents} />
        </Suspense>
      )}

      {/* 工具统计面板 */}
      {showToolStats && (
        <Suspense fallback={<LazyFeatureFallback label="工具统计载入中..." />}>
          <ToolStatsPanel onClose={() => setShowToolStats(false)} />
        </Suspense>
      )}

      {/* 定时任务面板（cron）— 优先用 session.agent_id（chat 主路径走的就是它），
          否则 fallback 到 currentAgentId state（用户主动选 agent 时同步）*/}
      {showCron && (
        <Suspense fallback={<LazyFeatureFallback label="定时任务面板载入中..." />}>
          <CronJobsPanel
            agentId={currentSession?.agent_id || currentAgentId || 'default'}
            onClose={() => setShowCron(false)}
          />
        </Suspense>
      )}

      {/* 成就面板 */}
      {showAchievements && (
        <Suspense fallback={<LazyFeatureFallback label="成就面板载入中..." />}>
          <AchievementPanel onClose={() => setShowAchievements(false)} />
        </Suspense>
      )}

      {/* 协作对话弹窗 */}
      {/* 角色工坊：Tauri 下默认走独立窗口；非 Tauri 环境使用内嵌浮动兜底 */}
      <Suspense fallback={null}>
      <CharacterWorkshop
        open={workshopOpen}
        onClose={() => setWorkshopOpen(false)}
        mode={workshopMode}
        agentId={workshopAgentId}
        onAgentCreated={(newId) => {
          // 刷新 agents 列表
          syncAgentsFromBackend().then(setAgents).catch(() => {})
          setCurrentAgentId(newId)
        }}
        onAgentUpdated={() => {
          // 刷新 agents 列表
          syncAgentsFromBackend().then(setAgents).catch(() => {})
        }}
      />
      </Suspense>

      {/* 数据导入/导出弹窗 */}
      <Suspense fallback={null}>
        <ExportImportDialog
          open={exportImportOpen}
          onClose={() => {
            setExportImportOpen(false)
            // 导入可能改了角色列表，刷新一下
            syncAgentsFromBackend().then(setAgents).catch(() => {})
          }}
        />
      </Suspense>

      {/* 全局版本升级提示弹窗 */}
      <UpdateModal />
    </div>
  )
}

// 用 ToastProvider 与 UpdaterProvider 包裹
export default function App() {
  return (
    <ToastProvider>
      <UpdaterProvider>
        <AppInner />
      </UpdaterProvider>
    </ToastProvider>
  )
}
