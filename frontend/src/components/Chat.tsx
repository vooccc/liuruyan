import { useState, useRef, useEffect, useLayoutEffect, useCallback, useMemo, forwardRef, useImperativeHandle, type KeyboardEvent, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { Message as MessageType, Session, getAgents, type Agent } from '../api/chat'
import { useAppStore } from '../stores/useAppStore'
import InlineTasksPanel from './InlineTasksPanel'
import { useConfirmDialog, useAlertDialog } from './ConfirmDialog'
import BackendModifiedToastContainer from './BackendModifiedToastContainer'
import { useChatModelManager } from './useChatModelManager'
import { resetTtsStreaming } from './ttsStreamingSpeaker'
import { useCopyFeedback } from './useCopyFeedback'
import ChatCommandsPanel from './ChatCommandsPanel'
import ChatInputTopToolbar from './ChatInputTopToolbar'
import ChatInputBottomToolbar from './ChatInputBottomToolbar'
import ChatInputComposer from './ChatInputComposer'
import ChatAttachmentChips from './ChatAttachmentChips'
import type { MicSpeechButtonRef } from './MicSpeechButton'
import { getVoiceConfig, type VoiceProviderType } from '../api/voice'
import { useToast } from './ToastSystem'
import { useChatAttachments } from './useChatAttachments'
import ChatTimeline from './ChatTimeline'
import { getChatInputPlaceholder, type ChatInputMode } from './chatInputModes'
import {
  createChatExternalHandle,
  type ChatHandle,
  type ChatTimelineItem,
} from './chatExternalHandle'
import {
  CHAT_SLASH_COMMANDS,
  CHAT_UI_TEXT,
} from './chatUiText'
import { listLryWorkflows, type LryWorkflowListItem } from '../api/lry'
import { useChatSessionLoader } from './useChatSessionLoader'
import { useChatSubmitFlow } from './useChatSubmitFlow'
import { useDispatchEventConsumer } from './useDispatchEventConsumer'
import { useChatInteractions } from './useChatInteractions'
import { applyStreamEventToMessage, extractTextFromSegments } from './chatStreamMessage'
import { useChatSessionRuntime, writeSessionChatMessages } from './useChatSessionRuntime'
import PinnedBar from './PinnedBar'
import { GoalBar } from './GoalBar'
import MentionDropdown, { useMentionController } from './MentionDropdown'
import { createSession, deleteSessionMessage, editSessionMessage, truncateSession, updateSessionPinnedScope, getSessionGoal, type GoalState, type EditMessagePayload, type ScopeItem as PinnedScopeItem } from '../api/chat'
export type {
  ChatHandle,
  ChatExternalMessageInput,
  ChatExternalStreamStartInput,
  ChatExternalStreamEventInput,
  ChatExternalStreamFinishInput,
  ChatExternalStreamFailInput,
  ChatRitualMarkerInput,
} from './chatExternalHandle'

// 模块级稳定空数组：store selector 在 sessions[sid].chatMessages 缺失时 fallback；
// 用同一引用避免每次渲染创建新空数组导致 zustand 误报变更。
const EMPTY_MESSAGES: MessageType[] = []

/** cache 条目是否已与后端 transcript 对账（buildSessionMessages 合并时回填的
 *  message_seq）。乐观插入 / 幽灵条目没有。 */
function hasReconciledMessageSeq(m: MessageType): boolean {
  return Number.isInteger(m.message_seq) && Number(m.message_seq) > 0
}

/** truncateSession keep 参数的正确口径：目标条目之前「已对账」的非 ephemeral
 *  条目数 = 该消息在服务端用户视角消息数组里的下标。cache 混有无 seq 的幽灵 /
 *  乐观条目时，直接拿 findIndex 原始下标当 keep 会把截断起点算大、漏删服务端
 *  行 → 重发后盘上真的出现两条一样的消息。 */
function countReconciledBefore(messages: MessageType[], rawIdx: number): number {
  let count = 0
  for (let i = 0; i < rawIdx; i += 1) {
    const m = messages[i]
    if (m && !m.ephemeral && hasReconciledMessageSeq(m)) count += 1
  }
  return count
}

interface ChatProps {
  currentSession?: Session | null
  currentAgentId?: string | null
  backendOnline?: boolean
  agents?: Agent[]
  onSessionCreated?: (session: Session) => void
  onLoadingChange?: (sessionId: string | null, loading: boolean) => void
  /** 跳到「设置 → 权限」并定位当前角色（toolbar「权限」按钮触发） */
  onJumpToPermissions?: (agentId: string | null) => void
}

const Chat = forwardRef<ChatHandle, ChatProps>(function Chat(
  { currentSession, currentAgentId, backendOnline = true, agents: agentsProp, onSessionCreated, onLoadingChange, onJumpToPermissions }: ChatProps,
  ref,
) {
  // Stage B-1 / C-2 / D：messages 走 store selector。store sessions[sid].chatMessages
  // 是真值（ref 层已删）；写入由 setSessionChatMessages action 完成。
  const messages = useAppStore((state) => state.sessions[currentSession?.id || '']?.chatMessages ?? EMPTY_MESSAGES)
  const [timelineItems, setTimelineItems] = useState<ChatTimelineItem[]>([])
  const [input, setInput] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [showCommands, setShowCommands] = useState(false)
  // `.lry/workflows/` 列表懒加载：首次打开指令面板时 fetch 一次，失败静默（面板只是不显示这组）。
  // 不做周期性轮询——workflow 改动频率低，用户重开会话即刷新。
  const [lryWorkflows, setLryWorkflows] = useState<LryWorkflowListItem[] | null>(null)
  const lryWorkflowsFetchedRef = useRef(false)
  const [selectedInputMode, setSelectedInputMode] = useState<ChatInputMode>('code')
  const [showCorridorPanel, setShowCorridorPanel] = useState(false)
  const [corridorTarget, setCorridorTarget] = useState<string | null>(null)
  const [corridorContent, setCorridorContent] = useState('')
  const [corridorSending, setCorridorSending] = useState(false)
  const [expandedToolCalls, setExpandedToolCalls] = useState<Set<string>>(new Set())
  const [expandedThinking, setExpandedThinking] = useState<Set<string>>(new Set())
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const messagesContainerRef = useRef<HTMLDivElement>(null)
  // InlineTasksPanel 的 Portal 锚点（始终输入框上方）。callback ref 用 useState 持有，
  // 让 ref ready 时能触发 Portal 重渲。anchor div 常驻 DOM（条件渲染会让 Portal target
  // 不存在 → 异常）。
  // 历史：之前是双锚点（LLM 工作中 → timeline 末尾 / 闲 → 输入框上方），但 isLoading
  // 切换时 createPortal container 变化会让 child unmount/remount，DOM 几何位置瞬间跳变
  // = 用户感知的"飘动"。改单锚点后视觉永远稳定贴底，跟 windsurf/Cursor 侧栏范式一致。
  const [tasksInputAnchor, setTasksInputAnchor] = useState<HTMLDivElement | null>(null)
  const userScrolledUpRef = useRef(false)
  // 语音输入：disabled 时按钮整体不渲染；其他状态都显示按钮（具体引擎走后端）
  const [voiceProviderType, setVoiceProviderType] = useState<VoiceProviderType>('disabled')
  const micButtonRef = useRef<MicSpeechButtonRef | null>(null)
  const { addToast } = useToast()
  // per-session 索引：A session 在发送时不能锁住 B session 的输入。
  // key 用 sessionId；新建 session 前临时用空串作为占位 key（同一时刻只会有一条 pending）。
  const isSendingRef = useRef<Record<string, boolean>>({})
  const previousSessionIdRef = useRef<string | null>(null)
  const forceScrollToBottomRef = useRef(false)
  // 注：不需要程序滚动锁——ChatTimeline 用 scrollTop 单调性判定用户上滑，
  // 我们程序滚动总是向下推（scrollTop 增加），天然不会被误认为上滑。
  // 启动时拉一次语音配置；后续设置页改完会触发自定义事件让我们重拉。
  useEffect(() => {
    let cancelled = false
    const refresh = () => {
      getVoiceConfig()
        .then((data) => {
          if (!cancelled) setVoiceProviderType(data.config.provider_type)
        })
        .catch(() => {
          /* backend 未启 / 路由未装载，静默保持 disabled */
        })
    }
    refresh()
    const onUpdate = () => refresh()
    window.addEventListener('voice-config-updated', onUpdate)
    return () => {
      cancelled = true
      window.removeEventListener('voice-config-updated', onUpdate)
    }
  }, [])

  // 语音转写结果：拼到 input 末尾，需要时补空格。不自动发送，让用户确认。
  const handleVoiceTranscription = useCallback((text: string) => {
    setInput((prev) => (prev ? (prev.endsWith(' ') ? prev + text : prev + ' ' + text) : text))
    requestAnimationFrame(() => inputRef.current?.focus())
  }, [])

  const handleVoiceError = useCallback(
    (message: string) => {
      addToast({
        type: 'info',
        title: '语音输入',
        desc: message,
        icon: '🎙️',
        urgency: 'medium',
        color: '#f59e0b',
      })
    },
    [addToast],
  )

  // Ctrl+Shift+M 全局快捷键：与麦克风按钮 toggleRecording 等价。
  // disabled 状态下按钮不渲染，micButtonRef.current 为 null，自然 noop，不需额外判断。
  // 顶部 import 的 KeyboardEvent 是 react 类型，这里用 globalThis 的 DOM 版本避免冲突。
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && (e.key === 'M' || e.key === 'm')) {
        e.preventDefault()
        micButtonRef.current?.toggleRecording()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const { confirm: confirmAction, dialog: confirmDialog } = useConfirmDialog()
  const { dialog: alertDialog } = useAlertDialog()
  const {
    sessionIdRef,
    setSessionLoading,
    updateSessionMessages,
    syncTimelineItems,
    updateSessionTimelineMarkers,
  } = useChatSessionRuntime({
    onLoadingChange,
    setTimelineItems,
  })

  // P1.1（架构重构 2026-05-23）：消息加载逻辑
  useChatSessionLoader({
    currentSession,
    sessionIdRef,
    setIsLoading,
    setTimelineItems,
    syncTimelineItems,
  })

  const localAgents = useMemo(() => getAgents(), [])
  const agents = agentsProp && agentsProp.length > 0 ? agentsProp : localAgents
  const { copiedId, copyContent } = useCopyFeedback<number>({ resetMs: 2000 })
  const currentAgent = useMemo(() => {
    return agents.find(a => a.id === (currentAgentId || 'default')) || agents[0] || null
  }, [currentAgentId, agents])
  const currentAgentName = useMemo(() => {
    return agentsProp?.find((agent) => agent.id === currentAgentId)?.name || currentAgent?.name || null
  }, [agentsProp, currentAgent, currentAgentId])
  const {
    customModels,
    selectedModel,
    selectedProvider,
    submitModel,
    submitProvider,
    currentModelMeta,
    getModelProvider,
    showModelPicker,
    setShowModelPicker,
    modelBtnRef,
    pickerPanelRef,
    switchCustomModel,
    openModelSettings,
  } = useChatModelManager({
    activeSessionId: currentSession?.id ?? null,
    activeAgentId: currentAgentId ?? null,
    activeSession: currentSession ?? null,
  })

  // 权限：toolbar「权限」按钮直接跳设置（详细面板在 设置 → 权限 tab）
  // 运行预算入口已迁到 设置 → 运行预算 tab
  const handleOpenPermissions = useCallback(() => {
    onJumpToPermissions?.(currentAgentId ?? null)
  }, [onJumpToPermissions, currentAgentId])

  const getMessageSourceAgent = useCallback((message: MessageType) => {
    if (message.role !== 'assistant') return null
    const sourceAgentId = message.segments?.find((segment) => segment.agent_id)?.agent_id
      || message.agent_id
    if (!sourceAgentId) return currentAgent
    return agents.find((agent) => agent.id === sourceAgentId) || currentAgent
  }, [agents, currentAgent])

  useImperativeHandle(ref, () => createChatExternalHandle({
    currentSessionId: currentSession?.id,
    sessionIdRef,
    updateSessionMessages,
    updateSessionTimelineMarkers,
    syncTimelineItems,
    applyStreamEventToMessage,
    extractTextFromSegments,
    setExpandedThinking,
  }), [
    currentSession?.id,
    syncTimelineItems,
    updateSessionMessages,
    updateSessionTimelineMarkers,
  ])

  useChatSessionLoader({
    currentSession,
    sessionIdRef,
    setIsLoading,
    setTimelineItems,
    syncTimelineItems,
  })

  // 记录上一次 timeline 的"签名"，用于判断是否 append
  const previousTimelineSignatureRef = useRef<{ count: number; tailId: string | number | null }>({
    count: 0,
    tailId: null,
  })

  useEffect(() => {
    const nextSessionId = currentSession?.id || null
    if (previousSessionIdRef.current !== nextSessionId) {
      // M2：切会话时清流式 speaker 状态 + 停所有朗读音频
      // 防止：(1) 上一条会话尾音溢到新会话；(2) 流式 speaker 残留 cursor 状态
      // 注意：必须用**旧** sessionId（previousSessionIdRef 还没被覆盖前），否则
      // 清的是新会话的状态，老会话的 cursor 还残留。
      resetTtsStreaming(previousSessionIdRef.current || undefined)
      previousSessionIdRef.current = nextSessionId
      userScrolledUpRef.current = false
      forceScrollToBottomRef.current = true
      previousTimelineSignatureRef.current = { count: 0, tailId: null }
    }
  }, [currentSession?.id])

  // 切会话后 800ms 内持续 rAF 贴底：markdown / 代码高亮 / 图片是异步渲染，
  // 单次贴底无法保证"最终高度"被覆盖。这个循环跟随异步内容增长保持锚定。
  // 用户主动上滑后立刻停止，不打扰阅读历史。
  useEffect(() => {
    if (!currentSession?.id) return
    const container = messagesContainerRef.current
    if (!container) return
    let cancelled = false
    const start = performance.now()
    const pin = () => {
      if (cancelled) return
      if (userScrolledUpRef.current) return
      container.scrollTop = container.scrollHeight
      if (performance.now() - start < 800) {
        requestAnimationFrame(pin)
      }
    }
    requestAnimationFrame(pin)
    return () => { cancelled = true }
  }, [currentSession?.id])

  useLayoutEffect(() => {
    if (!forceScrollToBottomRef.current) return
    if (timelineItems.length === 0) return
    const container = messagesContainerRef.current
    if (!container) return
    container.scrollTop = container.scrollHeight
  }, [timelineItems])

  useEffect(() => {
    if (timelineItems.length === 0) return

    const prevSignature = previousTimelineSignatureRef.current
    const tailItem = timelineItems[timelineItems.length - 1]
    const tailId = tailItem ? (tailItem as any).id ?? null : null
    const nextSignature = {
      count: timelineItems.length,
      tailId,
    }

    // 判断本次变更的"类型"
    const isCountGrew = nextSignature.count > prevSignature.count

    previousTimelineSignatureRef.current = nextSignature

    requestAnimationFrame(() => {
      const container = messagesContainerRef.current
      if (!container) return

      const distanceFromBottom = container.scrollHeight - container.clientHeight - container.scrollTop

      if (userScrolledUpRef.current && !isCountGrew) return
      if (userScrolledUpRef.current && distanceFromBottom > 200) return

      const shouldForceScroll = forceScrollToBottomRef.current
      if (shouldForceScroll) {
        forceScrollToBottomRef.current = false
      }
      const hasStreaming = messages.some(m => m.isStreaming)
      messagesEndRef.current?.scrollIntoView({
        behavior: shouldForceScroll || hasStreaming ? 'instant' : 'smooth',
      })
    })
  }, [timelineItems, messages])

  // 工具卡 expand 入视野改由 RuntimeToolBlock 自己用 scrollIntoView({block:'nearest'})
  // 处理（runtimeBlocks.tsx），直接定位"刚展开的卡"DOM 比 messagesEndRef 稳。

  // ── 第 5 层：DOM reflow 贴底安全网（2026-05-16）─────────────────────
  // 现象：流式结束后，react-syntax-highlighter 异步注入 spans / 图片加载 /
  //   表格 reflow / KaTeX 排版 → DOM 高度继续增长，但 React 不会重渲染，
  //   主 effect (#3) 不会再触发贴底，用户看到"差几十像素没贴底"。
  // 方案：MutationObserver 监听容器子树 DOM 变化，rAF 合并风暴，距底 ≤ 200px
  //   就 instant 跟进。完全脱离 React 时机，只看 DOM 真实状态。
  // 安全：与主 effect 同时贴底也无冲突——两边都把 scrollTop 推到 scrollHeight，
  //   幂等；用户主动上滑 (userScrolledUpRef.current) 时本机制不打扰。
  useEffect(() => {
    const container = messagesContainerRef.current
    if (!container) return
    let rafId = 0

    const reflowCheck = () => {
      rafId = 0
      // 用户主动上滑（onWheel/onScroll 已标记）→ 立即停手，不打扰阅读历史。
      // 这是唯一保护机制：不再叠加 distance 阈值——流式生成长 markdown 时
      // 单次 mutation 可能把距底从 0 撑到 300-500px，叠 200 阈值会直接锁死。
      // 跟主 effect (#3) 的 `userScrolledUp && > 200` 判定保持一致：
      // 没上滑就无脑跟到底，幂等贴底无副作用。
      if (userScrolledUpRef.current) return
      const distance = container.scrollHeight - container.clientHeight - container.scrollTop
      if (distance < 2) return
      container.scrollTop = container.scrollHeight
    }

    const schedule = () => {
      if (rafId) return
      rafId = requestAnimationFrame(reflowCheck)
    }

    const mo = new MutationObserver(schedule)
    mo.observe(container, {
      childList: true,
      subtree: true,
      characterData: true,
    })

    return () => {
      mo.disconnect()
      if (rafId) cancelAnimationFrame(rafId)
    }
  }, [])

  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = 'auto'
      inputRef.current.style.height = `${Math.min(inputRef.current.scrollHeight, 150)}px`
    }
  }, [input])

  // 输入框 📎 附件：用户选文件 → 前置 validate → 并发上传 → chip 状态机。
  // attachmentsRef 只含已上传成功的路径数组，传给 useChatSubmitFlow 在 sendMessage 时透传。
  //
  // validateAttachment：图片附件前置校验。
  //
  // 2026-06-02 放宽策略：允许无 vision 模型接收图片附件，理由：
  // 1. 后端 llm_client 层有兜底降级（非 vision 模型会把图片路径作为文本 footer 传给 LLM）
  // 2. image-gen skill 等工具可以让无 vision 模型通过路径转发实现图生图
  // 3. 前端硬拦截会误伤合法场景（用户装了 image-gen skill 但前端不知道）
  //
  // vision 判定优先用后端权威 `model.tags`（来自 /models endpoint，源头是
  // backend/providers/capabilities.py::infer_capabilities）。这样新模型加进 provider
  // 配置后只要后端正则识别就立即生效，前端零改动。拿不到 model 对象时退化到
  // 前端正则（边界场景）。
  const validateAttachment = useCallback((_file: File): string | null => {
    // 放宽：不再拦截图片上传，由后端和 LLM 决定如何处理
    return null
  }, [])
  const { attachments, attachmentsRef, handleChooseFiles, removeAttachment, clearAttachments } = useChatAttachments({
    validateFile: validateAttachment,
  })

  // 发送被防重复 / 防抖 / 发送锁拦下时的用户可见提示。
  // 这些分支以前只 console.warn 就 return，用户看到的是「点了发送什么都没发生」。
  const handleSubmitNotice = useCallback((title: string, desc?: string) => {
    addToast({
      type: 'info',
      title,
      desc: desc ?? '',
      icon: '⚠️',
      urgency: 'medium',
      color: '#f59e0b',
    })
  }, [addToast])

  // 指令面板首次展开时才拉 .lry/workflows 列表。
  // 用 ref 做 once 标记避免 StrictMode / 频繁开关 panel 时重复请求。
  useEffect(() => {
    if (!showCommands || lryWorkflowsFetchedRef.current) return
    lryWorkflowsFetchedRef.current = true
    listLryWorkflows()
      .then((data) => setLryWorkflows(data.items))
      .catch(() => {
        // .lry/ 不存在 / 后端未跑 / 网络错 → 静默，面板不显示项目工作流分组
        setLryWorkflows([])
      })
  }, [showCommands])

  const [currentGoal, setCurrentGoal] = useState<GoalState | null>(null)

  const fetchSessionGoal = useCallback(async () => {
    if (!currentSession?.id) {
      setCurrentGoal(null)
      return
    }
    try {
      const g = await getSessionGoal(currentSession.id)
      setCurrentGoal(g)
    } catch {
      setCurrentGoal(null)
    }
  }, [currentSession?.id])

  // v6 标记式修改的发送前钩子：performMarkedTruncate（见下方定义）依赖
  // useChatSubmitFlow 返回的 refreshSessionState，而钩子又要传进该 hook ——
  // 循环。用 latest-ref 代理解开：hook 拿到稳定引用，真函数在其后每渲染回填。
  const onBeforeSendRef = useRef<(() => Promise<void>) | null>(null)
  const onBeforeSendProxy = useCallback(() => {
    const fn = onBeforeSendRef.current
    return fn ? fn() : Promise.resolve()
  }, [])

  const {
    handleStop,
    handleSubmit: handleSubmitBase,
    askStatesByAskId,
    handleSubmitAskAnswer,
    refreshSessionState,
  } = useChatSubmitFlow({
    input,
    isLoading,
    inputMode: selectedInputMode,
    currentSession,
    currentAgentId,
    currentAgent,
    // submitModel：仅当用户在该会话主动 override 过才有值；为空时不传，
    // 让后端 resolve_call_model_config 走 agent.active_model / global fallback
    selectedModel: submitModel,
    // submitProvider：同 submitModel 语义，与其配对 → 后端按 (model, provider) 严格匹配。
    submitProvider,
    inputRef,
    userScrolledUpRef,
    sessionIdRef,
    isSendingRef,
    setInput,
    setShowCommands,
    setIsLoading,
    setExpandedThinking,
    setSessionLoading,
    syncTimelineItems,
    applyStreamEventToMessage,
    onSessionCreated,
    attachmentsRef,
    onNotice: handleSubmitNotice,
    onGoalUpdate: setCurrentGoal,
    onRefreshGoal: fetchSessionGoal,
    onBeforeSend: onBeforeSendProxy,
  })

  // ==== 编辑并重发（v6.1 内联编辑器「发送」，合并原「修改并重新提问」按钮）====
  // 位置约束：必须在 useChatSubmitFlow 之后（performMarkedTruncate 依赖其返回的
  //   refreshSessionState），又必须在 useChatInteractions 之前
  //   （handleClearHistoryWithMark 消费 clearEditMark）。
  // 交互：user 消息上的编辑按钮打开内联编辑器（取消/保存/发送）。「发送」=
  //   截断此条之后的所有对话 + 以编辑后的文本重发（原样发送 = 重新生成）。
  //   截断动作复用 onBeforeSend 钩子 —— 在发送流程所有防抖/锁/空文本等 bail
  //   分支通过后才执行，任何"没发出去"的情况都不会误删对话。
  // keep 参数 = messages 数组里被编辑消息的 index（messages[0..idx) 保留）
  // 后端会自动连带保留某 assistant 后的 tool_result/image artifact 行，避免下次
  // LLM 调用 tool_use 配对失败 400。详见 backend/transcript.py:truncate_after。
  const [editingMessage, setEditingMessage] = useState<MessageType | null>(null)
  const editingMessageRef = useRef<MessageType | null>(null)

  const clearEditMark = useCallback(() => {
    editingMessageRef.current = null
    setEditingMessage(null)
  }, [setEditingMessage])

  // 发送前钩子（useChatSubmitFlow 在所有 bail 分支通过后调用）：执行被标记
  // 消息的延迟截断。目标已不在 messages 里（被回滚 / 删除 / 清历史移除）→
  // 无需截断，直接放行发送。截断失败保留标记，由 handleInlineEditSend 统一
  // 识别失败并抛错给内联编辑器。
  const performMarkedTruncate = useCallback(async () => {
    const target = editingMessageRef.current
    if (!target) return
    const sid = currentSession?.id
    if (!sid) {
      clearEditMark()
      return
    }
    const idx = messages.findIndex((m) => m.id === target.id)
    if (idx < 0) {
      clearEditMark()
      return
    }
    await truncateSession(sid, countReconciledBefore(messages, idx))
    // 截断已删掉服务端 [keep..) 的行。cache 里从被编辑条目起所有「从未对账
    // （无 message_seq）」的非 ephemeral 条目，对应的行要么刚被删、要么该轮
    // 停止/出错从未落盘，永远等不到配对——不立即清掉会被 buildSessionMessages
    // 当 in-flight/partial 原位保留成幽灵消息，重发后界面出现两条一样的消息。
    writeSessionChatMessages(sid, (prev) => prev.filter((m, i) => (
      m.ephemeral || i < idx || hasReconciledMessageSeq(m)
    )))
    syncTimelineItems(sid)
    await refreshSessionState(sid)
    clearEditMark()
  }, [currentSession?.id, messages, clearEditMark, refreshSessionState, syncTimelineItems])

  // 内联编辑器「发送」：置标记 → 走正常发送流（override 文本，不碰输入框草稿）
  // → onBeforeSend 在 bail 分支全过后执行截断。await 返回后若标记还在，说明
  // 发送在 onBeforeSend 之前/之中被拦下（防抖/发送锁/命令模板/截断失败）——
  // 清标记并抛错，编辑器保持打开、错误上屏，用户可直接重试（截断没发生）。
  // 截断成功但后续发送失败的情况：标记已清，重试时 idx<0 自动跳过截断。
  const handleInlineEditSend = useCallback(async (message: MessageType, newText: string) => {
    if (!currentSession?.id || isLoading) throw new Error('当前会话繁忙，无法发送')
    editingMessageRef.current = message
    setEditingMessage(message)
    try {
      await handleSubmitBase(undefined, newText)
      if (editingMessageRef.current?.id === message.id) {
        clearEditMark()
        throw new Error('发送被拦下，请稍后重试')
      }
    } catch (e) {
      if (editingMessageRef.current?.id === message.id) clearEditMark()
      throw e
    }
  }, [currentSession?.id, isLoading, handleSubmitBase, clearEditMark])

  // 标记目标被回滚 / 删除 / 清历史等操作移走后，标记态残留会让高亮指向空——
  // 监听 messages 变化兜底清除（发送流程内的清除已在 performMarkedTruncate 做）。
  useEffect(() => {
    if (!editingMessage) return
    if (messages.length > 0 && !messages.some((m) => m.id === editingMessage.id)) {
      clearEditMark()
    }
  }, [editingMessage, messages, clearEditMark])

  // 切会话：标记不跨会话保留
  useEffect(() => {
    clearEditMark()
  }, [currentSession?.id, clearEditMark])

  // latest-ref 回填（见上方 onBeforeSendProxy 注释）
  onBeforeSendRef.current = performMarkedTruncate

  const handleSessionMessageAdded = useCallback((sid: string, event: { raw: Record<string, unknown> }) => {
    if (event.raw.source !== 'cron_subagent') return
    void refreshSessionState(sid).catch(() => null)
  }, [refreshSessionState])

  const handleSessionFullSnapshot = useCallback((sid: string) => (
    refreshSessionState(sid).then(() => undefined)
  ), [refreshSessionState])

  useDispatchEventConsumer(currentSession?.id, {
    onMessageAdded: handleSessionMessageAdded,
    onFullSnapshot: handleSessionFullSnapshot,
  })

  // wrap handleSubmit：依然同步起 fetch（内部读 attachmentsRef.current 时是最新值），
  // 随后立刻清空 UI 上的 chip 列表。不 await：sendMessage 是 streaming，await 会等整个流结束。
  const handleSubmit = useCallback((event?: FormEvent, messageOverride?: string) => {
    // 底部输入框发送时，若有上方残留的原地编辑状态，自动收起退出
    if (editingMessageRef.current) {
      clearEditMark()
    }
    const result = handleSubmitBase(event, messageOverride)
    clearAttachments()
    return result
  }, [handleSubmitBase, clearAttachments, clearEditMark])

  // canSubmit 策略：
  // - 有文字 OR 有至少一个已上传成功的附件 → 允许发送
  // - 任一附件正在 uploading 时 → 禁用（防止用户在 attachmentsRef 还是空数组时裸发，
  //   导致首次附带图片消息失败，只有重发才成功的已知复现场景）
  const hasOkAttachment = attachments.some((a) => a.status === 'ok')
  const hasUploadingAttachment = attachments.some((a) => a.status === 'uploading')
  const canSubmit = Boolean(
    (input.trim() || hasOkAttachment) && !hasUploadingAttachment
  )

  const [pinnedRefreshKey, setPinnedRefreshKey] = useState(0)

  useEffect(() => {
    void fetchSessionGoal()
  }, [fetchSessionGoal, pinnedRefreshKey])

  const mention = useMentionController({
    textareaRef: inputRef,
    input,
    onInputChange: setInput,
  })

  // 顶部工具栏 @ 按钮：在光标位置插 @ 触发 mention dropdown。
  // detectMention 要求 @ 前是空白或开头，所以前一字符非空白时自动补一个空格。
  const handleInsertMention = useCallback(() => {
    const ta = inputRef.current
    if (!ta) return
    const cursor = ta.selectionStart ?? input.length
    const before = input.slice(0, cursor)
    const after = input.slice(cursor)
    const needsSpace = before.length > 0 && !/\s$/.test(before)
    const inserted = needsSpace ? ' @' : '@'
    const next = `${before}${inserted}${after}`
    setInput(next)
    // 把光标放在 @ 之后；setInput 后下一帧 DOM 已更新
    requestAnimationFrame(() => {
      const node = inputRef.current
      if (!node) return
      node.focus()
      const pos = before.length + inserted.length
      node.setSelectionRange(pos, pos)
    })
  }, [input, inputRef])

  const handleEnsureSession = useCallback(async (): Promise<string> => {
    if (currentSession?.id) return currentSession.id
    const effectiveAid = currentSession?.agent_id || currentAgentId || 'default'
    const agentName = agents?.find((a) => a.id === effectiveAid)?.name || '助手'
    const title = effectiveAid === 'default' ? '新对话' : `与${agentName}的对话`
    const session = await createSession(title, effectiveAid)
    onSessionCreated?.(session)
    return session.id
  }, [currentSession?.id, currentSession?.agent_id, currentAgentId, agents, onSessionCreated])

  const handleMentionSelect = useCallback(async (item: PinnedScopeItem) => {
    mention.handleSelect(item)
    try {
      const sid = currentSession?.id || (await handleEnsureSession())
      if (!sid) return
      // 把当前 pinned scope 加上这一条（去重 by type+path）
      const { getSessionPinned } = await import('../api/chat')
      const cur = await getSessionPinned(sid)
      const exists = cur.scope.some((it) => it.type === item.type && it.path === item.path)
      const next = exists ? cur.scope : [...cur.scope, item]
      await updateSessionPinnedScope(sid, next)
      setPinnedRefreshKey((v) => v + 1)
    } catch {
      // 静默失败：用户能从 PinnedBar 编辑器手动补
    }
  }, [currentSession?.id, handleEnsureSession, mention])

  const {
    handleClearHistory,
    handleKeyDown: rawHandleKeyDown,
    selectCommand,
    handleInputChange,
    handleToggleModelPicker,
    handleToggleCorridorPanel,
    handleCancelCorridor,
    handleSendCorridor,
  } = useChatInteractions({
    input,
    currentSession,
    currentAgentId,
    inputRef,
    corridorTarget,
    corridorContent,
    confirmAction,
    handleSubmit,
    setTimelineItems,
    setInput,
    setShowCommands,
    setShowCorridorPanel,
    setCorridorTarget,
    setCorridorContent,
    setCorridorSending,
    setShowModelPicker,
  })

  // 清空历史会移除包括编辑重发标记目标在内的所有消息 → 一并清标记防高亮悬空
  const handleClearHistoryWithMark = useCallback(async () => {
    await handleClearHistory()
    clearEditMark()
  }, [handleClearHistory, clearEditMark])

  // 包一层：mention dropdown 打开时拦截方向键/回车交给它，否则放行原 handler。
  const handleKeyDown = useCallback((e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (mention.handleKeyDownIntercept(e)) return
    rawHandleKeyDown(e)
  }, [mention, rawHandleKeyDown])

  // v4 (2026-05-22) L2 原地编辑：调 editSessionMessage 修改磁盘 + 重拉 session 刷新前端。
  // 与 handleInlineEditSend（截断+重发）严格区分：本方法不动后续对话。
  // 入参 message 用来定位 visible_index（messages 数组里的 index）；payload 已由
  // InplaceMessageEditor 组装好（user → {content}；assistant → {block_patches}）。
  const handleInplaceEditMessage = useCallback(async (message: MessageType, payload: EditMessagePayload) => {
    const sid = currentSession?.id
    if (!sid || isLoading) {
      throw new Error('当前会话繁忙，无法编辑')
    }
    const idx = messages.findIndex((m) => m.id === message.id)
    if (idx < 0) {
      throw new Error('未找到该消息')
    }
    const result = await editSessionMessage(sid, idx, payload)
    // refreshSessionState 会重拉 messages 并刷新 store，保证编辑后立即可见
    await refreshSessionState(sid)
    if (result.status === 'noop') {
      // 后端认为没改（内容相同 / 越界等）—— 不当错，但给用户一个轻提示
      addToast({
        type: 'info',
        title: '未修改',
        desc: result.reason || '内容无变化',
        icon: '✏️',
        urgency: 'low',
        color: '#6b7280',
      })
    }
  }, [currentSession?.id, isLoading, messages, refreshSessionState, addToast])

  const handleRollbackToMessage = useCallback(async (message: MessageType) => {
    const sid = currentSession?.id
    if (!sid || isLoading) return
    const idx = messages.findIndex((m) => m.id === message.id)
    if (idx < 0) return
    const confirmed = await confirmAction(
      '回滚到此处',
      '该消息及之后的所有对话内容将被永久删除，无法恢复。确定要继续吗？',
      'danger',
      '确定回滚',
    )
    if (!confirmed) return
    try {
      await truncateSession(sid, countReconciledBefore(messages, idx))
      // 与 handleInlineEditSend 同款孤儿清理：截断后 [idx..) 的无 seq 条目
      // 对应的行已被删，不清会残留成幽灵消息。
      writeSessionChatMessages(sid, (prev) => prev.filter((m, i) => (
        m.ephemeral || i < idx || hasReconciledMessageSeq(m)
      )))
      syncTimelineItems(sid)
      await refreshSessionState(sid)
      // 被回滚的区间可能覆盖修改标记的目标条目 —— 直接清标记防高亮悬空
      if (editingMessageRef.current && editingMessageRef.current.id === message.id) {
        clearEditMark()
      }
    } catch (error: any) {
      addToast({
        type: 'info',
        title: '回滚失败',
        desc: error?.message || '截断会话失败',
        icon: '⚠️',
        urgency: 'medium',
        color: '#ef4444',
      })
    }
  }, [currentSession?.id, isLoading, messages, confirmAction, refreshSessionState, addToast, syncTimelineItems, clearEditMark])

  // v5 (2026-05-26) 单条删除：对齐 Cherry "选哪条删哪条"。
  // - user 普通消息：仅删该行
  // - assistant turn：连带删紧随其后的 tool_result/image artifact 行（避免孤儿 tool_use）
  // - 之后的对话原样保留，破坏性介于"原地编辑"（0）和"回滚"（删此条及之后所有）之间
  const handleDeleteMessage = useCallback(async (message: MessageType) => {
    const sid = currentSession?.id
    if (!sid || isLoading) return
    const idx = messages.findIndex((m) => m.id === message.id)
    if (idx < 0) return
    const isAssistant = message.role === 'assistant'
    const desc = isAssistant
      ? '将永久删除这条助手回复（含其工具调用结果），不影响之后的对话。确定继续？'
      : '将永久删除这条消息，不影响之后的对话。确定继续？'
    const confirmed = await confirmAction('删除此条消息', desc, 'danger', '确定删除')
    if (!confirmed) return
    try {
      const result = await deleteSessionMessage(sid, idx)
      await refreshSessionState(sid)
      if (result.status === 'noop') {
        addToast({
          type: 'info',
          title: '未删除',
          desc: result.reason || '后端未删除任何消息',
          icon: '⚠️',
          urgency: 'medium',
          color: '#ef4444',
        })
      } else if (editingMessageRef.current?.id === message.id) {
        // 删掉的正是编辑重发标记的目标 → 清标记防高亮悬空
        clearEditMark()
      }
    } catch (error: any) {
      addToast({
        type: 'info',
        title: '删除失败',
        desc: error?.message || '删除消息失败',
        icon: '⚠️',
        urgency: 'medium',
        color: '#ef4444',
      })
    }
  }, [currentSession?.id, isLoading, messages, confirmAction, refreshSessionState, addToast, clearEditMark])

  return (
    <div className="chat-container">
      <BackendModifiedToastContainer />
      {!backendOnline && (
        <div className="offline-banner">
          <span className="offline-dot"></span>
          {CHAT_UI_TEXT.offlineBanner}
        </div>
      )}

      <GoalBar
        sessionId={currentSession?.id || null}
        goalState={currentGoal}
        onRefreshGoal={fetchSessionGoal}
        onResumeGoal={() => {
          void handleSubmit(undefined, '/goal resume')
        }}
      />

      <ChatTimeline
        timelineItems={timelineItems}
        currentAgent={currentAgent}
        copiedId={copiedId}
        expandedThinking={expandedThinking}
        expandedToolCalls={expandedToolCalls}
        inputRef={inputRef}
        messagesContainerRef={messagesContainerRef}
        messagesEndRef={messagesEndRef}
        userScrolledUpRef={userScrolledUpRef}
        setInput={setInput}
        setExpandedThinking={setExpandedThinking}
        setExpandedToolCalls={setExpandedToolCalls}
        copyContent={copyContent}
        getMessageSourceAgent={getMessageSourceAgent}
        askStatesByAskId={askStatesByAskId}
        onSubmitAskAnswer={handleSubmitAskAnswer}
        onInplaceEditMessage={handleInplaceEditMessage}
        onSendEditMessage={handleInlineEditSend}
        editingMessageId={editingMessage?.id ?? null}
        onRollbackToMessage={handleRollbackToMessage}
        onDeleteMessage={handleDeleteMessage}
        isBusy={isLoading}
        sessionId={currentSession?.id || null}
      />

      {mention.state.open && (
        <MentionDropdown
          query={mention.state.query}
          anchor={mention.state.anchor}
          onSelect={handleMentionSelect}
          onClose={mention.handleClose}
        />
      )}

      <ChatInputComposer
        pinnedBarSlot={(
          <PinnedBar
            sessionId={currentSession?.id || null}
            refreshKey={pinnedRefreshKey}
            isBusy={isLoading}
            onEnsureSession={handleEnsureSession}
          />
        )}
        commandsPanel={showCommands ? (
            <ChatCommandsPanel
              commands={CHAT_SLASH_COMMANDS}
              filter={input}
              onClose={() => setShowCommands(false)}
              onSelect={selectCommand}
              lryWorkflows={(lryWorkflows ?? []).map((w) => ({
                name: w.name,
                description: w.description,
              }))}
              onSelectLryWorkflow={(name) => {
                // 跟内置 slash 选中逻辑一致：把 `/name ` 塞回输入框，用户续写任务。
                // 实际工作流加载在 handleSubmit 的 tryInjectLryWorkflow 里做，这里只改 UI。
                setInput(`/${name} `)
                setShowCommands(false)
                inputRef.current?.focus()
              }}
            />
          ) : null}
          topToolbar={(
            <ChatInputTopToolbar
              currentAgentId={currentAgentId}
            currentAgentName={currentAgentName}
            agents={agents}
            showCorridorPanel={showCorridorPanel}
            corridorTarget={corridorTarget}
            corridorContent={corridorContent}
            corridorSending={corridorSending}
            onToggleCorridorPanel={handleToggleCorridorPanel}
            onTargetChange={setCorridorTarget}
            onContentChange={setCorridorContent}
            onCancelCorridor={handleCancelCorridor}
            onSendCorridor={handleSendCorridor}
            onInsertMention={handleInsertMention}
            mentionActive={mention.state.open}
            onChooseFiles={handleChooseFiles}
          />
        )}
        attachmentsSlot={(
          <ChatAttachmentChips attachments={attachments} onRemove={removeAttachment} />
        )}
        onPasteFiles={handleChooseFiles}
        inputRef={inputRef}
        input={input}
        onInputChange={handleInputChange}
        onKeyDown={handleKeyDown}
        placeholder={getChatInputPlaceholder(selectedInputMode)}
        bottomToolbar={(
          <ChatInputBottomToolbar
            selectedInputMode={selectedInputMode}
            currentModelMeta={currentModelMeta}
            customModels={customModels}
            selectedModel={selectedModel}
            selectedProvider={selectedProvider}
            showModelPicker={showModelPicker}
            modelBtnRef={modelBtnRef}
            pickerPanelRef={pickerPanelRef}
            getModelProvider={getModelProvider}
            isLoading={isLoading}
            canSubmit={canSubmit}
            onToggleModelPicker={handleToggleModelPicker}
            onSelectCustomModel={switchCustomModel}
            onOpenModelSettings={openModelSettings}
            onSelectInputMode={setSelectedInputMode}
            onClearHistory={handleClearHistoryWithMark}
            onStop={handleStop}
            onSubmit={handleSubmit}
            onOpenGuardianDialog={handleOpenPermissions}
            voiceEnabled={voiceProviderType !== 'disabled'}
            onVoiceTranscription={handleVoiceTranscription}
            onVoiceError={handleVoiceError}
            micButtonRef={micButtonRef}
          />
        )}
        guardianDialog={null}
        tasksAnchorSlot={<div ref={setTasksInputAnchor} className="inline-tasks-anchor" />}
      />

      {confirmDialog}
      {alertDialog}

      {/* InlineTasksPanel 单实例 + Portal 单锚点（输入框上方）。
          见上方 tasksInputAnchor 注释里的"双锚点改单锚点"原因——杜绝 Portal container
          切换导致的 unmount/remount，列表始终贴输入框稳定渲染。 */}
      {currentSession?.id && tasksInputAnchor && createPortal(
        <InlineTasksPanel sid={currentSession.id} />,
        tasksInputAnchor,
      )}
    </div>
  )
})

export default Chat
