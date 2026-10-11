import {
  useState,
  useRef,
  useEffect,
  useCallback,
  useMemo,
  type CSSProperties,
} from 'react'
import { createPortal } from 'react-dom'
import { ChevronDown } from 'lucide-react'
import {
  type Agent,
  type CollabChatEvent,
  type SceneInfo,
  type EditMessagePayload,
  dismissCollabScene,
  isCollabRuntimeEventType,
  submitAskAnswer,
  deleteSessionMessage,
  editSessionMessage,
  truncateSession,
  getSession,
} from '../api/chat'
import type { AskUserAnswer } from './AskUserQuestionCard'
import type { AskStateEntry, AskStatesByAskId } from './runtimeBlocks'
import { useConfirmDialog } from './ConfirmDialog'
import { useToast } from './ToastSystem'
import { useCopyFeedback } from './useCopyFeedback'
import CollabEmptyState from './CollabEmptyState'
import CollabHeaderBar from './CollabHeaderBar'
import CollabInputComposer from './CollabInputComposer'
import CollabMessageItem, { type CollabBubble } from './CollabMessageItem'
import {
  applyEventToCollabBubble,
  buildCollabTextSegment,
  hasCollabDetailedChain,
  normalizeCollabSegments,
} from './collabRuntime'
import {
  COLLAB_PHASE_STEPS,
  COLLAB_SCENE_CONFIGS,
  COLLAB_UI_TEXT,
  formatCollabEmptyDescription,
  formatCollabInputPlaceholder,
  formatCollabPhaseChangeNotice,
} from './collabUiText'
import { useCollabChainAutoCollapse } from './useCollabChainAutoCollapse'
import { useCollabHistoryLoader } from './useCollabHistoryLoader'
import { useCollabSubmitFlow } from './useCollabSubmitFlow'
import {
  useCollabStreamStore,
  abortStream,
  getCollapseTimers,
  clearAllCollapseTimers,
  rekeyResources,
} from '../stores/collabStreamStore'

interface CollabChatProps {
  scene?: SceneInfo
  agents: Agent[]
  onClose?: () => void
  existingSessionId?: string
  embedded?: boolean
  /** Sidebar 开关状态 + toggle 回调（embedded 模式下透传给 CollabHeaderBar 渲染 hamburger）。 */
  sidebarOpen?: boolean
  onToggleSidebar?: () => void
}

// 模块级稳定空数组：bubbles selector 在 bucket 缺失时 fallback。
// 同一引用避免 zustand 误判变更触发重渲染。
const EMPTY_BUBBLES: CollabBubble[] = []

/**
 * scene_update 流内回写的场景实时状态。
 *
 * App 传入的 scene prop 是打开会话时的一次性快照（getActiveScenes），每轮收尾时
 * 后端发的 scene_update 才是推进后的真相。header 的阶段/轮次以 liveScene 优先、
 * 快照兜底；切到别的场景（scene?.id 变化）即作废回落。
 */
interface LiveSceneState {
  phase?: string
  currentTurn: number
  isActive: boolean
}

/**
 * 计算 per-session bucket 的 key。
 *
 * 优先使用真实 session_id（已经持久化、跨重启稳定），
 * 退化场景：scene 已创建但还没拿到 session_id → 临时用 `scene::<sceneId>`，
 * 等 session_info 事件到达后由 handleCollabEvent 触发 rekey。
 *
 * 都没有 → '__pending__'：刚 mount 还没 scene 时的零值；不会跑流，安全。
 */
function deriveSessionKey(scene?: SceneInfo, existingSessionId?: string): string {
  if (existingSessionId) return existingSessionId
  if (scene?.id) return `scene::${scene.id}`
  return '__pending__'
}

export function CollabChat({
  scene,
  agents,
  onClose,
  existingSessionId,
  embedded = false,
  sidebarOpen,
  onToggleSidebar,
}: CollabChatProps) {
  // ── per-session bucket key ──
  const sessionKey = deriveSessionKey(scene, existingSessionId)

  // ── store selectors（只读 bucket 的快照）──
  const bubbles = useCollabStreamStore((s) => s.buckets[sessionKey]?.bubbles ?? EMPTY_BUBBLES)
  const isChatting = useCollabStreamStore((s) => s.buckets[sessionKey]?.isChatting ?? false)
  const hasSession = useCollabStreamStore((s) => s.buckets[sessionKey]?.hasSession ?? false)
  const loadingHistory = useCollabStreamStore((s) => s.buckets[sessionKey]?.loadingHistory ?? false)

  // ── store actions（稳定引用，不需 useMemo）──
  const setBubbles = useCollabStreamStore.getState().setBubbles
  const updateBubbles = useCollabStreamStore.getState().updateBubbles
  const appendBubble = useCollabStreamStore.getState().appendBubble
  const setIsChatting = useCollabStreamStore.getState().setIsChatting
  const setHasSession = useCollabStreamStore.getState().setHasSession
  const setLoadingHistory = useCollabStreamStore.getState().setLoadingHistory
  const nextBubbleSeq = useCollabStreamStore.getState().nextBubbleSeq
  const ensureBucket = useCollabStreamStore.getState().ensureBucket
  const rekeyBucket = useCollabStreamStore.getState().rekeyBucket

  const [input, setInput] = useState('')
  // 发言序列（粘性）：用户在 SpeakerPicker 里指定的顺序。
  // 跨会话切换时需重置（不同场景参与者不同，老选择会 stale）——下面 useEffect 看 sceneKey 变化就清。
  const [speakers, setSpeakers] = useState<string[]>([])
  // 场景实时状态：scene_update 事件回写（ref 供 handler 同步读 prev，state 驱动渲染）
  const [liveScene, setLiveScene] = useState<LiveSceneState | undefined>(undefined)
  const liveSceneRef = useRef<LiveSceneState | undefined>(undefined)

  // 切到别的场景时丢弃上一场的流内状态，回落到新 scene 快照。
  // 挂 scene?.id 而非 sessionKey：流中 rekey（scene::xxx → 真实 session_id）
  // 并不换场景，不能把 live 状态洗掉。
  useEffect(() => {
    liveSceneRef.current = undefined
    setLiveScene(undefined)
  }, [scene?.id])

  const [showScrollBottom, setShowScrollBottom] = useState(false)
  // 编辑/删除后强制重拉历史（配合 setBubbles([]) 绕过加载器的防 clobber 规则）
  const [historyTick, setHistoryTick] = useState(0)
  const { copiedId: copiedBubbleId, copyContent: copyBubbleContent } = useCopyFeedback<string>()
  const { confirm: confirmAction, dialog: confirmDialog } = useConfirmDialog()
  const { addToast } = useToast()

  // ── ask_user 闭环状态（P1 2026-09-29）──
  // 卡片提交态按 ask_id 存（AskUserQuestionCard 等待/已答/失败）;真实 session_id
  // 从 session_info 事件捕获——sessionKey 在首轮流里还是 'scene::xxx' 临时值，
  // 后端 pending ask 注册表认的是 collab session 的真实 id。
  const [askStates, setAskStates] = useState<AskStatesByAskId>({})
  const realSessionIdRef = useRef<string | undefined>(undefined)
  const setAskState = useCallback((askId: string, entry: AskStateEntry) => {
    setAskStates((prev) => ({ ...prev, [askId]: entry }))
  }, [])

  const handleSubmitAskAnswer = useCallback(async (
    askId: string,
    toolUseId: string | undefined,
    answers: AskUserAnswer[],
  ) => {
    const sid = realSessionIdRef.current
      || (sessionKey.startsWith('scene::') || sessionKey === '__pending__' ? undefined : sessionKey)
    if (!sid) {
      setAskState(askId, { state: 'failed', error: '会话未就绪，无法提交回答' })
      return
    }
    setAskState(askId, { state: 'submitting', error: null })
    try {
      await submitAskAnswer({
        session_id: sid,
        ask_id: askId,
        tool_use_id: toolUseId ?? null,
        answers,
      })
      setAskState(askId, { state: 'submitted', error: null })
    } catch (err: any) {
      // 404 = pending ask 已失效（页面刷新 / 后端重启 / 超时）。不做单聊那套
      // override 重发（那是 Chat 提交机器山的专属），这里直接报错提示重发即可。
      setAskState(askId, {
        state: 'failed',
        error: String(err?.message || err || '提交失败'),
      })
    }
  }, [sessionKey, setAskState])

  const scrollRef = useRef<HTMLDivElement>(null)
  // 滚动锁定是 view-local 状态：用户切群时能在新群独立判断"是否被人为往上翻"，
  // 没必要持久化到 store。
  const userScrolledUpRef = useRef(false)

  const cfg = COLLAB_SCENE_CONFIGS[scene?.scene_type || 'free'] || COLLAB_SCENE_CONFIGS.free
  const canDismissSession = hasSession && Boolean(scene)
  const activeParticipants = scene?.participants || []
  // 阶段/轮次以 scene_update 流内回写优先，App 快照兜底（快照只在打开会话时取一次）
  const activePhaseKey = liveScene?.phase ?? (scene?.phase || '')
  const activeTurn = liveScene?.currentTurn ?? scene?.current_turn ?? 0
  const sceneEnded = liveScene !== undefined && !liveScene.isActive
  const phaseIdx = COLLAB_PHASE_STEPS.findIndex((step) => step.key === activePhaseKey)
  const currentPhase = COLLAB_PHASE_STEPS[Math.max(0, phaseIdx)]
  const participantNames = activeParticipants.map((agentId) => (
    agents.find((agent) => agent.id === agentId)?.name || agentId
  ))
  const title = participantNames.join('、') || scene?.title || cfg.label
  const subtitle = sceneEnded
    ? `${cfg.label} · ${COLLAB_UI_TEXT.sceneEndedLabel}`
    : `${cfg.label} · 第 ${activeTurn + 1} 轮`
  const inputPlaceholder = sceneEnded
    ? COLLAB_UI_TEXT.sceneEndedPlaceholder
    : formatCollabInputPlaceholder(currentPhase.label)
  const collabThemeStyle = useMemo<CSSProperties>(() => ({
    ['--collab-accent' as string]: cfg.color,
    ['--collab-accent-soft' as string]: `${cfg.color}14`,
    ['--collab-accent-soft-strong' as string]: `${cfg.color}10`,
    ['--collab-accent-border' as string]: `${cfg.color}35`,
    ['--collab-accent-border-soft' as string]: `${cfg.color}2a`,
    ['--collab-accent-avatar-border' as string]: `${cfg.color}20`,
    ['--collab-stream-caret' as string]: cfg.color,
  }), [cfg.color])

  // ── bucket 初始化 / sessionKey 同步 ──
  useEffect(() => {
    ensureBucket(sessionKey)
    if (existingSessionId) setHasSession(sessionKey, true)
  }, [sessionKey, existingSessionId, ensureBucket, setHasSession])

  const createBubbleId = useCallback((prefix: string) => {
    const seq = nextBubbleSeq(sessionKey)
    return `${prefix}-${seq}`
  }, [sessionKey, nextBubbleSeq])

  // 系统行（阶段推进 / 散会提示）：走 __system__ 通道，仅本场内存态，不落盘。
  const appendSystemNoticeBubble = useCallback((text: string) => {
    appendBubble(sessionKey, {
      id: createBubbleId('system-notice'),
      agentId: '__system__',
      agentName: COLLAB_UI_TEXT.systemName,
      avatar: COLLAB_UI_TEXT.systemAvatar,
      content: text,
      isStreaming: false,
      segments: [buildCollabTextSegment(text, '__system__')],
      chainExpanded: false,
    })
  }, [appendBubble, createBubbleId, sessionKey])

  useCollabHistoryLoader({
    existingSessionId,
    sessionKey,
    agents,
    createBubbleId,
    setBubbles,
    setLoadingHistory,
    buildTextSegment: buildCollabTextSegment,
    normalizeSegments: normalizeCollabSegments,
    reloadTick: historyTick,
  })

  const syncScrollLock = useCallback(() => {
    const element = scrollRef.current
    if (!element) return
    const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight
    userScrolledUpRef.current = distanceFromBottom > 72
    setShowScrollBottom(distanceFromBottom > 120)
  }, [])

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const element = scrollRef.current
    if (!element) return
    element.scrollTo({ top: element.scrollHeight, behavior })
    userScrolledUpRef.current = false
    setShowScrollBottom(false)
  }, [])

  useEffect(() => {
    setShowScrollBottom(false)
  }, [sessionKey])

  const clearAutoCollapseTimer = useCallback((bubbleId: string) => {
    const timers = getCollapseTimers(sessionKey)
    const timer = timers.get(bubbleId)
    if (timer) {
      clearTimeout(timer)
      timers.delete(bubbleId)
    }
  }, [sessionKey])

  const scheduleAutoCollapse = useCallback((bubbleId: string, delay = 1800) => {
    clearAutoCollapseTimer(bubbleId)
    const timers = getCollapseTimers(sessionKey)
    timers.set(bubbleId, setTimeout(() => {
      updateBubbles(sessionKey, (prev) => prev.map((bubble) => (
        bubble.id === bubbleId && hasCollabDetailedChain(bubble.segments)
          ? { ...bubble, chainExpanded: false }
          : bubble
      )))
      timers.delete(bubbleId)
    }, delay))
  }, [clearAutoCollapseTimer, sessionKey, updateBubbles])

  useCollabChainAutoCollapse({
    sessionKey,
    bubbles,
    clearAutoCollapseTimer,
    scheduleAutoCollapse,
    hasDetailedChain: hasCollabDetailedChain,
  })

  const updateLatestStreamingBubble = useCallback((
    agentId: string | undefined,
    updater: (bubble: CollabBubble) => CollabBubble,
  ) => {
    if (!agentId) return
    updateBubbles(sessionKey, (prev) => {
      const reverseIndex = [...prev].reverse().findIndex((bubble) => (
        bubble.agentId === agentId && bubble.isStreaming
      ))
      if (reverseIndex < 0) return prev
      const targetIndex = prev.length - 1 - reverseIndex
      return prev.map((bubble, index) => (index === targetIndex ? updater(bubble) : bubble))
    })
  }, [sessionKey, updateBubbles])

  const handleCollabEvent = useCallback((event: CollabChatEvent) => {
    if (event.type === 'session_info') {
      if (event.session_id) {
        // 后端首次创建 session 时回吐真实 session_id；
        // 我们把临时 key 'scene::xxx' 的 bucket 迁到真实 session_id 上。
        // resources（abort / timers）也跟着迁。
        realSessionIdRef.current = event.session_id
        if (sessionKey !== event.session_id) {
          rekeyBucket(sessionKey, event.session_id)
          rekeyResources(sessionKey, event.session_id)
        }
        setHasSession(event.session_id, true)
      }
      return
    }

    if (event.type === 'agent_start') {
      const agent = agents.find((item) => item.id === event.agent_id)
      appendBubble(sessionKey, {
        id: createBubbleId(event.agent_id || 'agent'),
        agentId: event.agent_id || '',
        agentName: event.agent_name || agent?.name || event.agent_id || COLLAB_UI_TEXT.agentName,
        avatar: agent?.avatar || COLLAB_UI_TEXT.agentAvatar,
        avatarUrl: agent?.avatar_url,
        content: '',
        isStreaming: true,
        segments: [buildCollabTextSegment('', event.agent_id)],
        chainExpanded: false,
      })
      return
    }

    if (event.type === 'agent_end') {
      updateLatestStreamingBubble(event.agent_id, (bubble) => ({ ...bubble, isStreaming: false }))
      return
    }

    if (event.type === 'agent_error') {
      const errMsg = event.error || COLLAB_UI_TEXT.unknownError
      updateBubbles(sessionKey, (prev) => {
        const reverseIndex = [...prev].reverse().findIndex((bubble) => (
          bubble.agentId === event.agent_id && bubble.isStreaming
        ))
        if (reverseIndex < 0) {
          const agent = agents.find((item) => item.id === event.agent_id)
          return [...prev, {
            id: createBubbleId(event.agent_id || 'error'),
            agentId: event.agent_id || '',
            agentName: agent?.name || event.agent_id || COLLAB_UI_TEXT.agentName,
            avatar: agent?.avatar || COLLAB_UI_TEXT.systemAvatar,
            avatarUrl: agent?.avatar_url,
            content: `${COLLAB_UI_TEXT.systemAvatar} ${errMsg}`,
            isStreaming: false,
            segments: [buildCollabTextSegment(`${COLLAB_UI_TEXT.systemAvatar} ${errMsg}`, event.agent_id)],
            chainExpanded: false,
          }]
        }
        const targetIndex = prev.length - 1 - reverseIndex
        return prev.map((bubble, index) => {
          if (index !== targetIndex) return bubble
          const errorText = `${bubble.content ? `${bubble.content}\n\n` : ''}${COLLAB_UI_TEXT.systemAvatar} ${errMsg}`
          return {
            ...bubble,
            content: errorText,
            isStreaming: false,
            segments: normalizeCollabSegments([
              ...bubble.segments,
              {
                id: bubble.segments.length,
                type: 'text',
                content: `\n\n${COLLAB_UI_TEXT.systemAvatar} ${errMsg}`,
                agent_id: bubble.agentId,
              },
            ], errorText, bubble.agentId),
          }
        })
      })
      return
    }

    if (event.type === 'scene_update') {
      const prev = liveSceneRef.current
      const nextIsActive = event.is_active !== false
      const next: LiveSceneState = {
        // 散会回包的 phase 是终态哨兵 'resolved'：不覆盖最后真实阶段，
        // 进度轨停格在散会前的位置。
        phase: nextIsActive
          ? (event.phase ?? prev?.phase)
          : (prev?.phase ?? scene?.phase),
        currentTurn: event.current_turn ?? prev?.currentTurn ?? scene?.current_turn ?? 0,
        isActive: nextIsActive,
      }
      liveSceneRef.current = next
      setLiveScene(next)

      // 阶段推进 / 散会：时间线插一条系统行，让"会开到哪了"在场内可见
      const prevPhaseKey = prev?.phase ?? scene?.phase
      if (nextIsActive && next.phase && next.phase !== prevPhaseKey) {
        appendSystemNoticeBubble(formatCollabPhaseChangeNotice(next.phase))
      }
      if (!nextIsActive) {
        appendSystemNoticeBubble(COLLAB_UI_TEXT.sceneEndedMessage)
      }
      return
    }

    if (isCollabRuntimeEventType(event.type)) {
      updateLatestStreamingBubble(event.agent_id, (bubble) => applyEventToCollabBubble(bubble, event))
    }
  }, [
    agents,
    appendBubble,
    appendSystemNoticeBubble,
    createBubbleId,
    rekeyBucket,
    scene?.phase,
    sessionKey,
    setHasSession,
    updateBubbles,
    updateLatestStreamingBubble,
  ])

  const { handleSend, handleStop, sendWithText } = useCollabSubmitFlow({
    input,
    isChatting,
    scene,
    speakers,
    sessionKey,
    existingSessionId,
    userScrolledUpRef,
    createBubbleId,
    setInput,
    setIsChatting,
    appendBubble,
    updateBubbles,
    buildTextSegment: buildCollabTextSegment,
    handleCollabEvent,
  })

  // ── 消息编辑 / 删除（2026-09-30）──
  // 落盘模型（_persist_collab_messages）：一轮 = 一条 user 消息 + 每个角色各一条
  // 助手消息（segments[].agent_id 区分说话人）。因此：
  //   - user 气泡删除 = 删整轮（user + 其后连续的全部助手行，之后的对话保留）
  //   - 角色气泡删除 = 只删该角色自己的那条助手消息（其他角色的发言保留）
  //   - 编辑：user 走 { content }；角色走 { block_patches }（GET 注入的 block_index）
  // 注意：必须放在 useCollabSubmitFlow 之后——handleEditSend 依赖它返回的 sendWithText。
  const resolveSid = useCallback(() => {
    return realSessionIdRef.current
      || (sessionKey.startsWith('scene::') || sessionKey === '__pending__' ? undefined : sessionKey)
  }, [sessionKey])

  const reloadCollabHistory = useCallback(() => {
    setBubbles(sessionKey, [])
    setHistoryTick((t) => t + 1)
  }, [sessionKey, setBubbles])

  const guardBusy = useCallback(() => {
    if (!isChatting) return false
    addToast({ type: 'info', title: '协作进行中', desc: '等本轮发言结束后再操作', icon: 'ⓘ', urgency: 'low', color: '#6b7280' })
    return true
  }, [isChatting, addToast])

  const handleDeleteCollabMessage = useCallback(async (bubble: CollabBubble) => {
    const sid = resolveSid()
    const idx = bubble.sessionMessageIndex
    if (!sid || idx === undefined || guardBusy()) return
    const isUser = bubble.agentId === '__user__'
    const confirmed = await confirmAction(
      '删除发言',
      isUser
        ? '将删除这一轮：你的这条消息和本轮全部角色的回复，之后的对话保留。确定继续？'
        : '将删除该角色在本轮的这条发言，其他角色与之后的对话保留。确定继续？',
      'danger',
      '确定删除',
    )
    if (!confirmed) return
    try {
      // 删除前重读会话校验行 role，防索引漂移
      const fresh = await getSession(sid)
      const lines = (fresh?.messages || []) as Array<{ role: string }>
      const line = lines[idx]
      if (!line || (isUser ? line.role !== 'user' : line.role !== 'assistant')) {
        addToast({ type: 'info', title: '未能删除', desc: '消息位置已变化，请刷新后重试', icon: '⚠️', urgency: 'medium', color: '#ef4444' })
        return
      }
      if (isUser) {
        // 该轮每个角色各有一条助手行：从后往前删，避免索引位移
        let end = idx + 1
        while (end < lines.length && lines[end].role === 'assistant') end += 1
        for (let i = end - 1; i > idx; i -= 1) {
          await deleteSessionMessage(sid, i)
        }
      }
      await deleteSessionMessage(sid, idx)
      reloadCollabHistory()
    } catch (err: any) {
      addToast({ type: 'info', title: '删除失败', desc: String(err?.message || err || '未知错误'), icon: '⚠️', urgency: 'medium', color: '#ef4444' })
    }
  }, [confirmAction, guardBusy, reloadCollabHistory, resolveSid])

  const handleEditSave = useCallback(async (messageIndex: number, payload: EditMessagePayload) => {
    const sid = resolveSid()
    if (!sid) throw new Error('会话未就绪')
    if (guardBusy()) throw new Error('协作进行中，暂不能编辑')
    await editSessionMessage(sid, messageIndex, payload)
    reloadCollabHistory()
  }, [guardBusy, reloadCollabHistory, resolveSid])

  const handleEditSend = useCallback(async (messageIndex: number, text: string) => {
    const sid = resolveSid()
    if (!sid) throw new Error('会话未就绪')
    if (guardBusy()) throw new Error('协作进行中，暂不能重新发起')
    // 截断该 user 消息及其后所有行（keep = 之前的行数）
    await truncateSession(sid, messageIndex)
    setBubbles(sessionKey, [])
    // 重跑一轮：先清桶再流式灌入；结束后强制重拉一次持久化真相
    //（不能先 tick 重拉——历史 fetch 会和新回合的流式写入赛跑，把乐观气泡冲掉）
    await sendWithText(text)
    setBubbles(sessionKey, [])
    setHistoryTick((t) => t + 1)
  }, [guardBusy, resolveSid, sendWithText, sessionKey, setBubbles])

  // 滚动追随 bubbles 增长（注意：bubbles 是 store 切片，切群时引用换成新桶的）
  useEffect(() => {
    if (!userScrolledUpRef.current) {
      scrollToBottom('auto')
    }
  }, [bubbles, scrollToBottom])

  // 切群时滚动锁重置（视图态 only，bucket 状态自己持久）
  useEffect(() => {
    userScrolledUpRef.current = false
  }, [sessionKey])

  // 切群时清发言序列：换场景后老选择里的参与者可能不在新场景里（stale）。
  // 后端会静默过滤非参与者，但前端竹签也会跟着 stale。门守在这里抢先重置。
  useEffect(() => {
    setSpeakers([])
  }, [sessionKey])

  // 参与者变动（召唤加人 / 离开）时，从序列里出入 stale id，但保留顺序与重复。
  useEffect(() => {
    if (speakers.length === 0) return
    const set = new Set(activeParticipants)
    const filtered = speakers.filter((id) => set.has(id))
    if (filtered.length !== speakers.length) setSpeakers(filtered)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeParticipants.join(',')])

  // 组件 unmount 时清掉所有桶的 timers（CollabChat 不会真 unmount，
  // 这里只是兜底以防 App 重建）
  useEffect(() => () => {
    // 不清 abort：流可能还要继续在后台跑直到事件流结束
    // 不清 bubbles：保留以便下次进入时可见历史
    void 0
  }, [])

  useEffect(() => {
    if (embedded) return

    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose?.()
    }

    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [embedded, onClose])

  const handleClose = useCallback(() => {
    abortStream(sessionKey)
    onClose?.()
  }, [onClose, sessionKey])

  const handleDismiss = useCallback(async () => {
    abortStream(sessionKey)
    clearAllCollapseTimers(sessionKey)
    if (scene) await dismissCollabScene(scene.id)
    onClose?.()
  }, [onClose, scene, sessionKey])


  const toggleBubbleChain = useCallback((bubbleId: string) => {
    clearAutoCollapseTimer(bubbleId)
    updateBubbles(sessionKey, (prev) => prev.map((bubble) => (
      bubble.id === bubbleId
        ? { ...bubble, chainExpanded: !bubble.chainExpanded }
        : bubble
    )))
  }, [clearAutoCollapseTimer, sessionKey, updateBubbles])

  const content = (
    <div
      className={embedded ? 'collab-embedded-shell' : 'collab-modal-card'}
      style={collabThemeStyle}
    >
      <CollabHeaderBar
        embedded={embedded}
        themeStyle={collabThemeStyle}
        emoji={cfg.emoji}
        currentPhaseLabel={currentPhase.label}
        phaseIndex={phaseIdx}
        title={title}
        subtitle={subtitle}
        activeParticipants={activeParticipants}
        agents={agents}
        canDismissSession={canDismissSession}
        dismissTitle={COLLAB_UI_TEXT.dismissTitle}
        closeTitle={COLLAB_UI_TEXT.closeTitle}
        onDismiss={handleDismiss}
        onClose={handleClose}
        sidebarOpen={sidebarOpen}
        onToggleSidebar={onToggleSidebar}
      />

      <div className="collab-timeline-wrapper">
        <div ref={scrollRef} className="collab-messages-container" onScroll={syncScrollLock}>
          {loadingHistory ? (
            <CollabEmptyState loading title={COLLAB_UI_TEXT.loadingHistory} />
          ) : bubbles.length === 0 ? (
            <CollabEmptyState
              emoji={cfg.emoji}
              title={COLLAB_UI_TEXT.emptySceneTitle}
              description={formatCollabEmptyDescription(participantNames)}
              tip={COLLAB_UI_TEXT.emptyTip}
            />
          ) : (
            <div className="collab-message-list">
              {bubbles.map((bubble, index) => {
                const isMe = bubble.agentId === '__user__'
                const isSystem = bubble.agentId === '__system__'

                return (
                  <CollabMessageItem
                    key={bubble.id || `${bubble.agentId}-${index}`}
                    bubble={bubble}
                    agents={agents}
                    isMe={isMe}
                    isSystem={isSystem}
                    copied={copiedBubbleId === bubble.id}
                    onCopy={() => copyBubbleContent(bubble.id, bubble.content)}
                    onToggleChain={() => toggleBubbleChain(bubble.id)}
                    askStatesByAskId={askStates}
                    onSubmitAskAnswer={handleSubmitAskAnswer}
                    onEditSave={handleEditSave}
                    onEditSend={handleEditSend}
                    onDelete={handleDeleteCollabMessage}
                    busy={isChatting}
                  />
                )
              })}
            </div>
          )}
        </div>

        <div className={`chat-scroll-bottom-container ${showScrollBottom ? 'visible' : ''}`}>
          <div className="chat-scroll-bottom-inner">
            <button
              type="button"
              className="chat-scroll-bottom-btn"
              onClick={() => scrollToBottom('smooth')}
              title="回到底部"
              aria-label="回到底部"
            >
              <ChevronDown size={18} strokeWidth={2.2} />
            </button>
          </div>
        </div>
      </div>

      <CollabInputComposer
        input={input}
        isChatting={isChatting}
        disabled={sceneEnded}
        placeholder={inputPlaceholder}
        sendLabel={COLLAB_UI_TEXT.sendLabel}
        stopLabel={COLLAB_UI_TEXT.stopLabel}
        participants={activeParticipants}
        agents={agents}
        speakers={speakers}
        onSpeakersChange={setSpeakers}
        onInputChange={setInput}
        onSend={handleSend}
        onStop={handleStop}
      />

      {/* 删除确认弹窗（useConfirmDialog 的节点必须渲染） */}
      {confirmDialog}
    </div>
  )

  if (embedded) return content

  return createPortal(
    <div
      className="collab-modal-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget) handleClose()
      }}
    >
      {content}
    </div>,
    document.body,
  )
}

