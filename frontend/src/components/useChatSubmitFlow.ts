import { useCallback, useRef, useState, type Dispatch, type FormEvent, type MutableRefObject, type SetStateAction } from 'react'
import type { AskStatesByAskId, AskStateEntry } from './runtimeBlocks'
import type { AskUserAnswer } from './AskUserQuestionCard'
import {
  createSession,
  getSession,
  sendMessage,
  submitAskAnswer,
  type Agent,
  type Message as MessageType,
  type Session,
} from '../api/chat'
import type { StreamEvent } from '../api/chat'
import type { ChatStreamLikeEvent } from './chatExternalHandle'
import type { ChatInputMode } from './chatInputModes'
import { buildSessionMessages } from './chatSessionMessages'
import {
  CHAT_SLASH_COMMANDS,
  CHAT_UI_TEXT,
  formatChatRequestErrorMessage,
  formatChatSessionTitle,
} from './chatUiText'
import { readSessionChatMessages, writeSessionChatMessages, readSessionMessageMeta } from './useChatSessionRuntime'
import { registerStream, abortStream, removeStream, hasInFlightStream } from '../lib/streamRegistry'
import { useAppStore } from '../stores/useAppStore'
import { extractSpeakableText } from '../api/tts'
import {
  feedTtsStreaming,
  flushTtsStreaming,
  resetTtsStreaming,
  ensureTtsStreamingConfig,
} from './ttsStreamingSpeaker'
import { getRuntime } from '../runtime'
import { buildIdeContextBlock } from '../utils/ideContext'
import { tryInjectLryWorkflow } from '../utils/lryWorkflowContext'
import { buildUserMessageWithAttachments } from './userAttachmentFooter'
import { finalizeLastThinkingSegment } from './chatStreamMessage'

/**
 * 安全生成 v4 UUID。
 *
 * 背景：crypto.randomUUID() 需要 secure context（HTTPS 或 localhost/127.0.0.1）。
 * 用户访问 localhost 时 OK，但跳上手机/平板走 Tailscale tailnet 以 HTTP 连后端
 * 时，非 Secure Context → crypto.randomUUID === undefined → TypeError。
 *
 * 这里随机 id 只是用来作 client_id 配对用 / assistant 消息，不需要密码学强度，
 * Math.random + RFC 4122 v4 位型填充完全够用（碰撞概率远低于业务量）。
 */
function safeRandomUUID(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  // Fallback: 遵守 RFC 4122 v4 的 8-4-4-4-12 hex 布局 + version=4 / variant=10xx
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16)
  })
}

interface UseChatSubmitFlowParams {
  input: string
  isLoading: boolean
  inputMode: ChatInputMode
  currentSession?: Session | null
  currentAgentId?: string | null
  currentAgent?: Agent | null
  selectedModel?: string | null
  /** 与 selectedModel 配对的 provider id（仅 override 时有值），让后端按
   *  (model, provider) 严格匹配，避免同名 model 跨多 provider 时静默走错家。 */
  submitProvider?: string | null
  inputRef: MutableRefObject<HTMLTextAreaElement | null>
  userScrolledUpRef: MutableRefObject<boolean>
  sessionIdRef: MutableRefObject<string>
  /**
   * P1.3（架构重构 2026-05-23）：sessionAbortMapRef 已退场，迁到 module-level
   * streamRegistry（`src/lib/streamRegistry.ts`）。 callsite 调 registerStream / abortStream /
   * removeStream，跨 Chat unmount 持久。设计参考标准中间件设计
   */
  isSendingRef: MutableRefObject<Record<string, boolean>>
  setInput: Dispatch<SetStateAction<string>>
  setShowCommands: Dispatch<SetStateAction<boolean>>
  setIsLoading: Dispatch<SetStateAction<boolean>>
  setExpandedThinking: Dispatch<SetStateAction<Set<string>>>
  setSessionLoading: (sid: string, loading: boolean) => void
  syncTimelineItems: (sid: string) => void
  applyStreamEventToMessage: (message: MessageType, event: ChatStreamLikeEvent) => MessageType
  onSessionCreated?: (session: Session) => void
  /** 用户从 📎 上传的附件路径 ref（仅包含已上传成功的绝对路径）。
   *  ref 模式：避免 attachments state 变动触发 handleSubmit useCallback 重建。 */
  attachmentsRef?: MutableRefObject<string[]>
  /** 发送被防重复/防抖分支拦下时告知用户。
   *  没有它这些分支只 console.warn，用户看到的是「点了发送什么都没发生」。 */
  onNotice?: (title: string, desc?: string) => void
  /** Goal 模式状态更新回调（当流中接收到 goal_state_update 时实时触发） */
  onGoalUpdate?: (goal: any) => void
  /** 目标状态刷新回调（会话流结束时触发同步） */
  onRefreshGoal?: () => void
  /** v6「修改并重新提问」延迟截断钩子：点消息上的修改按钮只做前端标记，
   *  真正的截断（删该消息之后所有对话）推迟到这里——即所有防抖 / 发送锁 /
   *  空文本 / slash 模板等 bail 分支都已通过、确定要真发这条消息的那一刻。
   *  抛错 → bail（文字送回输入框 + 释放锁），标记保留以便重试。 */
  onBeforeSend?: () => Promise<void>
}

const REQUEST_DEBOUNCE_MS = 800
/** 发送锁被认定为陈旧的时限。
 *  锁在 handleSubmit 顶部获取，registerStream 要等 createSession / IDE 上下文
 *  这些 await 之后才发生，所以「持锁但 streamRegistry 里没有流」在正常情况下
 *  只会持续一瞬；超过这个时限还是这个状态，说明上一轮在 try/finally 之前就异常
 *  退出了，锁不会再有人释放。 */
const SEND_LOCK_STALE_MS = 15000

function countPersistedMessages(messages: MessageType[]) {
  return messages.filter((message) => !message.ephemeral).length
}

/** cache 条目是否已与后端 transcript 对账（buildSessionMessages 合并时回填的
 *  message_seq）。乐观插入的条目和幽灵条目都没有。 */
function hasReconciledMessageSeq(message: Pick<MessageType, 'message_seq'>): boolean {
  return Number.isInteger(message.message_seq) && Number(message.message_seq) > 0
}

export function useChatSubmitFlow({
  input,
  isLoading,
  inputMode: _inputMode,
  currentSession,
  currentAgentId,
  currentAgent,
  selectedModel,
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
  onNotice,
  onGoalUpdate,
  onRefreshGoal,
  onBeforeSend,
}: UseChatSubmitFlowParams) {
  // per-session 索引：A session 的节流不应阻塞 B session（用户实测切换会话点发送无反应即此问题）。
  const lastRequestIdRef = useRef<Record<string, string>>({})
  // 发送锁的获取时刻（per sid），用于识别永远不会被释放的陈旧锁。见 SEND_LOCK_STALE_MS。
  const sendLockAtRef = useRef<Record<string, number>>({})
  // AskUserQuestion 提交 override：handleSubmitAskAnswer 写入，handleSubmit 顶部消费
  const submitOverrideTextRef = useRef<string | null>(null)
  const [askStatesByAskId, setAskStatesByAskId] = useState<AskStatesByAskId>({})
  const setAskState = useCallback((askId: string, entry: AskStateEntry) => {
    setAskStatesByAskId(prev => ({ ...prev, [askId]: entry }))
  }, [])

  const refreshSessionState = useCallback(async (sid: string) => {
    const session = await getSession(sid)
    // P1.2（架构重构 2026-05-23）：meta 改走 store helper，不再依赖组件内 ref。
    const previousMeta = readSessionMessageMeta(sid)
    const previousMessages = readSessionChatMessages(sid)
    const nextMeta = {
      updatedAt: session.updatedAt,
      messageCount: session.messageCount,
      historyRevision: session.historyRevision,
      nextMessageSeq: session.nextMessageSeq,
    }
    const previousTail = previousMessages[previousMessages.length - 1]
    const serverMessages = session.messages || []
    const nextTail = serverMessages[serverMessages.length - 1]
    const hasMeaningfulChange = (
      previousMeta?.updatedAt !== nextMeta.updatedAt
      || previousMeta?.messageCount !== nextMeta.messageCount
      || previousMeta?.historyRevision !== nextMeta.historyRevision
      || previousMeta?.nextMessageSeq !== nextMeta.nextMessageSeq
      || previousMessages.length !== serverMessages.length
      || previousTail?.message_seq !== nextTail?.message_seq
      || previousTail?.history_revision !== nextTail?.history_revision
      || previousTail?.timestamp !== nextTail?.timestamp
      || previousTail?.content !== nextTail?.content
    )

    if (!hasMeaningfulChange) {
      return session
    }

    // server 主动收缩消息（truncate / admin 删除）时的孤儿防线（2026-09-28）：
    // 「从未对账（无 message_seq）」的非 ephemeral cache 条目，其对应的
    // transcript 行已被删掉（或该轮停止/出错根本没落盘），永远等不到
    // client_id / signature 配对——buildSessionMessages 会把它们当
    // in-flight/partial 原位保留成幽灵消息（用户编辑重发后看到两条一样的
    // 消息即此根因，b66a3a8 删掉 serverShrunk 空重建分支后暴露）。
    // 收缩时把这些孤儿滤掉再合并；已对账条目仍走原合并路径（seq 规则自然
    // 丢弃服务端已不存在的）。 ephemeral 尾巴不参与计数也不滤。
    // 注：不能退回 b66a3a8 之前「收缩即空缓存重建」的做法——backend 重启
    // 瞬间的半量 / 空 messages 快照会把整屏清空（该提交修的就是这个）。
    // 在途流（streamRegistry 有 entry）时不滤，避免误杀正在生成的乐观消息。
    const nonEphemeralCacheCount = countPersistedMessages(previousMessages)
    const mergeBasis = (!hasInFlightStream(sid) && serverMessages.length < nonEphemeralCacheCount)
      ? previousMessages.filter((m) => m.ephemeral || hasReconciledMessageSeq(m))
      : previousMessages
    const nextMessages = buildSessionMessages(session, mergeBasis)
    useAppStore.getState().hydrateSessionChatMessages(sid, nextMessages, nextMeta)
    syncTimelineItems(sid)
    return session
  }, [syncTimelineItems])

  const handleStop = useCallback(() => {
    const sid = currentSession?.id || ''
    // P1.3（架构重构 2026-05-23）：调 streamRegistry.abort——abort 同时从 registry 清掉。
    // 即使 Chat 中间 unmount过，registry 上仍有 entry，停止按钮付原。
    abortStream(sid, 'user_stop')
    // M2-Streaming：用户主动停 = 中断流 + 清流式 speaker 状态 + 停所有播放
    resetTtsStreaming(sid)
    setSessionLoading(sid, false)
    setIsLoading(false)

    const stopUpdater = (prev: MessageType[]) => prev
      .map((message) => (
        message.isStreaming ? { ...message, isStreaming: false } : message
      ))

    writeSessionChatMessages(sid, stopUpdater)
    syncTimelineItems(sid)
  }, [
    currentSession?.id,
    setIsLoading,
    setSessionLoading,
    syncTimelineItems,
  ])

  const handleSubmit = useCallback(async (event?: FormEvent, messageOverride?: string) => {
    if (event) event.preventDefault()

    // 附件 snapshot：必须在 handleSubmit 顶部同步抓一份独立拷贝。
    //
    // bug 复现（2026-05-12 用户报"点角色卡片→选图问图里是啥→LLM 说没看到图"）：
    //   Chat.tsx 的 wrap `handleSubmit` 调本函数后立刻 `clearAttachments()`，
    //   `setAttachments([])` 会触发 useChatAttachments 内 useEffect 把
    //   `attachmentsRef.current` 重置为 []。
    //   "无 activeSessionId 首条消息"路径要 `await createSession(...)`——这个
    //   真异步会把控制权交还 event loop，clearAttachments 的 re-render +
    //   useEffect 先跑完；等 createSession 返回，原代码读 ref 已是空数组，
    //   后端收到的 chat body 不含 attachments，LLM 完全感知不到有图。
    //   "已有 session" 路径同步执行到 `await sendMessage(...)` 前才 await，
    //   参数表达式同步求值，clear 来不及跑，所以不出 bug。
    //
    // 抓快照后续读 snapshot 不读 ref，clear 何时跑都不影响。
    const attachmentSnapshot: string[] = attachmentsRef?.current
      ? [...attachmentsRef.current]
      : []

    // session 还没创建时（首条消息），用占位 key '__no_session__'。
    // 创建成功后通过 sessionIdRef 接续，这里仅决定节流/锁的索引粒度。
    const submitSid = currentSession?.id || sessionIdRef.current || '__no_session__'

    const now = Date.now()
    const lastTs = lastRequestIdRef.current[submitSid]
    if (lastTs && (now - parseInt(lastTs, 10)) < REQUEST_DEBOUNCE_MS) {
      console.warn(`[Chat] Request throttled (sid=${submitSid}): ${now - parseInt(lastTs, 10)}ms < ${REQUEST_DEBOUNCE_MS}ms`)
      return
    }
    lastRequestIdRef.current[submitSid] = String(now)

    if (isSendingRef.current[submitSid]) {
      const lockedAt = sendLockAtRef.current[submitSid] || 0
      const heldMs = now - lockedAt
      // 真有 in-flight 流 → 是正常的「上一条还在跑」，明确告诉用户去点停止。
      if (hasInFlightStream(submitSid)) {
        console.warn(`[Chat] handleSubmit blocked by isSendingRef (sid=${submitSid}, in-flight stream)`)
        onNotice?.('上一条还在生成中', '想改内容请先点停止，再重新发送')
        return
      }
      // 没有流却还持锁：只可能是上一轮在拿到锁之后、进 try/finally 之前异常退出。
      // 锁没人释放 → 该会话此后所有发送都在这里静默返回（用户感知为「界面正常但发不出去」）。
      if (heldMs < SEND_LOCK_STALE_MS) {
        console.warn(`[Chat] handleSubmit blocked by isSendingRef (sid=${submitSid}, held=${heldMs}ms)`)
        return
      }
      console.warn(`[Chat] stale send lock cleared (sid=${submitSid}, held=${heldMs}ms)`)
    }
    isSendingRef.current[submitSid] = true
    sendLockAtRef.current[submitSid] = now
    const releaseLock = () => {
      isSendingRef.current[submitSid] = false
      delete sendLockAtRef.current[submitSid]
    }

    // 优先消费入参 messageOverride 或 AskUserQuestion 的 override 文本（由 handleSubmitAskAnswer 写入）
    const overrideText = messageOverride ?? submitOverrideTextRef.current
    submitOverrideTextRef.current = null
    let rawText = (overrideText ?? input).trim()
    if (!rawText && attachmentSnapshot.length === 0) {
      releaseLock()
      return
    }

    // 🎯 Goal 模式自动代理：如果当前处于 goal 模式且用户输入的不是以 / 开头的命令，
    // 自动作为 /goal 目标派发，省去手动键入 /goal
    if (_inputMode === 'goal' && rawText && !rawText.startsWith('/')) {
      rawText = `/goal ${rawText}`
    }

    // 预载 TTS 配置
    void ensureTtsStreamingConfig()

    if (isLoading) {
      handleStop()
      releaseLock()
      return
    }

    if (rawText.startsWith('/')) {
      const command = CHAT_SLASH_COMMANDS.find((item) => rawText === item.command || rawText.startsWith(item.command + ' '))
      // /goal 为可直接执行的系统命令，不拦截为纯模板填充，直接进入发送流让后端解析执行
      if (command && !rawText.startsWith('/goal')) {
        const argsText = rawText.slice(command.command.length).trim()
        setInput(command.prompt + ' ' + argsText)
        inputRef.current?.focus()
        releaseLock()
        return
      }
    }

    // LRY-1: `.lry/workflows/<slug>.md` slash 触发。
    // 用户输入首行 `/add-new-tool …` → 从后端取对应 workflow markdown，
    // 包进 `<details>` 折叠块注入到 user message 头部。fetch 失败（404 / 网络）静默降级：
    // `/xxx` 按原样作为普通 user 文本发送，不阻断。这里在 CHAT_SLASH_COMMANDS 之后，
    // 所以内置 slash 模板优先，跟历史行为兼容。
    let workflowBlock = ''
    let messageBody = rawText
    try {
      const injection = await tryInjectLryWorkflow(rawText)
      if (injection) {
        workflowBlock = injection.block
        // 如果剥完首行 body 为空，保留 rawText 语义，让 LLM 至少能看到 workflow +
        // "用户没写具体任务" 的事实（workflow 里通常会引导"请描述你的目标"）。
        messageBody = injection.rest || ''
      }
    } catch (err) {
      console.warn('[Chat] tryInjectLryWorkflow failed, sending as plain text:', err)
    }

    // C-1: VSCode webview 运行时 + 用户未关闭 toggle 时，发送前拼一块 IDE 上下文。
    // 拿不到 / 出错 都返空串 → 不会拼，不阻断发送。
    // 拼后的文本作为 user message 全程使用（前端 cache + 后端 transcript 一致），
    // 渲染靠 markdown <details> 天然折叠。
    let ideContextBlock = ''
    try {
      const runtime = await getRuntime()
      ideContextBlock = await buildIdeContextBlock(runtime)
    } catch (err) {
      console.warn('[Chat] buildIdeContextBlock failed, sending without IDE context:', err)
    }
    // 顺序：workflow 块（行为框架）→ IDE 上下文（环境提示）→ 用户剩余文本。
    // 三部分全空极端情况不发生：rawText 判空已在本函数顶部拦截。
    const text = `${workflowBlock}${ideContextBlock}${messageBody}`

    // 如果不是外部 override 文本发送，则清空用户当前输入框
    if (!overrideText) {
      setInput('')
    }
    setShowCommands(false)
    userScrolledUpRef.current = false
    // 输入框已经清空，后面任何一条防重复/建会话失败分支提前 return 都必须把文字
    // 送回去 —— 否则用户的输入既没发出去也没了（override 文本不属于输入框，不回填）。
    const restoreInput = () => {
      if (overrideText === null) setInput(input)
    }
    const bail = (title: string, desc?: string) => {
      restoreInput()
      releaseLock()
      onNotice?.(title, desc)
    }

    const effectiveAgentId = currentAgentId || 'default'

    let sessionId = currentSession?.id
    if (!sessionId) {
      try {
        const agentName = currentAgent?.name || CHAT_UI_TEXT.defaultAssistantName
        const title = formatChatSessionTitle(effectiveAgentId, agentName)

        const session = await createSession(title, effectiveAgentId)
        if (session.agent_id && session.agent_id !== effectiveAgentId) {
          console.warn(`[Chat] Session agent_id mismatch! Expected=${effectiveAgentId}, Got=${session.agent_id}`)
        }
        sessionId = session.id
        sessionIdRef.current = session.id
        onSessionCreated?.(session)
      } catch (error) {
        console.error('Failed to create session:', error)
        bail('新建会话失败', '后端没响应，消息已送回输入框，可直接重试')
        return
      }
    }

    const userMessageId = Date.now()
    const assistantMessageId = userMessageId + 1

    // v6「修改并重新提问」延迟截断：走到这里说明防抖 / 发送锁 / 空文本 /
    // slash 模板 / 建会话等所有 bail 分支都已通过，这条消息确定要发 ——
    // 才真正执行被标记消息之后的截断。onBeforeSend 抛错 → bail（文字送回
    // 输入框 + 释放锁），标记保留，用户可重试。必须在读 cache 之前 await，
    // 让下面 readSessionChatMessages 拿到的是截断后的消息视图。
    if (onBeforeSend) {
      try {
        await onBeforeSend()
      } catch (error) {
        console.error('[Chat] onBeforeSend (edit-mark truncation) failed:', error)
        bail('修改失败', error instanceof Error ? error.message : '截断会话失败，文字已送回输入框')
        return
      }
    }

    // 端到端 client_id：本轮 user / assistant 各发一个 UUID，写进 cache + 传 backend，
    // backend 持久化进 transcript + 响应带回，buildSessionMessages 用它一对一精确匹配。
    // 注：http Tailscale 跨设备访问是非 secure context，crypto.randomUUID 不存在，
    // 用 safeRandomUUID() 带 fallback（见文件顶部定义）。
    const userClientId = safeRandomUUID()
    const assistantClientId = safeRandomUUID()

    const sid = sessionId || ''
    const currentMessages = readSessionChatMessages(sid)
    const lastUserMessage = [...currentMessages].reverse().find((message) => message.role === 'user')

    if (lastUserMessage && lastUserMessage.content === text && lastUserMessage.timestamp) {
      const lastTime = new Date(lastUserMessage.timestamp).getTime()
      if (Date.now() - lastTime < 2000) {
        console.warn(`[Chat] Duplicate content blocked (within 2s): "${rawText.substring(0, 30)}..."`)
        bail('刚刚已发过相同内容', '2 秒内的重复发送被拦下，文字已送回输入框')
        return
      }
    }

    const userTimestamp = new Date().toISOString()
    const nextMessages = (() => {
      const currentLast = currentMessages[currentMessages.length - 1]
      if (currentLast?.role === 'user' && currentLast.content === text) {
        // 只把「刚入 cache」的同文本当成重复点击。不卡时间的话，「修改并重新提问」
        // （截断后原文原样回到输入框）会被永久判为重复 —— 发送静默丢弃，用户
        // 看到的就是「点了重发之后发不出去」。无 timestamp 时保守处理（当作重复）。
        const lastTs = currentLast.timestamp ? new Date(currentLast.timestamp).getTime() : 0
        if (!lastTs || Date.now() - lastTs < 2000) {
          console.warn('[Chat] Cache duplicate blocked')
          return null
        }
      }

      // 乐观渲染的 user message content 提前拼上 attachment footer，
      // 让 ChatMessageItem 的 parseUserMessageAttachments 能立刻拿到附件路径
      // → 用户气泡发完就显图，不等 backend 持久化 + session 刷新那一拍。
      // 注意：text 本身不能拼 footer——它是发往 /chat 的 wire body，backend
      // 自己会再拼一次，重复就 footer×2。所以这里只用于 cache content。
      const displayContent = buildUserMessageWithAttachments(text, attachmentSnapshot)
      return [
        ...currentMessages,
        {
          id: userMessageId,
          role: 'user' as const,
          content: displayContent,
          timestamp: userTimestamp,
          client_id: userClientId,
          segments: [{ id: 0, type: 'text' as const, content: displayContent }],
        },
        {
          id: assistantMessageId,
          role: 'assistant' as const,
          content: '',
          isStreaming: true,
          timestamp: userTimestamp,
          client_id: assistantClientId,
          agent_id: currentSession?.agent_id || effectiveAgentId,
          segments: undefined,
        },
      ]
    })()

    if (!nextMessages) {
      bail('这条刚刚发过', '文字已送回输入框，改点内容再发或稍等一下重试')
      return
    }

    // 流式开始前不预 push 任何占位 segment：backend 持久化时会丢掉所有空段，
    // 只要 cache 多一个空段，buildMessageSignature 就漂移，in-flight assistant 永远
    // 匹配不上 backend，每轮发消息都把 cache 那条接到末尾累积（"发 N 条回 N 条"）。
    // segments 保持 undefined，由 applyStreamEventToMessage 在第一个 chunk 到达时构造；
    // 空 thinking 段本来就被 runtimeBlocks 过滤掉（mergedContent.trim() 为空 return null），
    // 所以删除占位 0 UX 退化。Plan 模式 nextMessages 里已带真实 thinking 段，自然保留。
    useAppStore.getState().setSessionChatMessages(sid, nextMessages)
    syncTimelineItems(sid)
    setIsLoading(true)

    const controller = new AbortController()
    // P1.3（架构重构 2026-05-23）：注册到 module-level streamRegistry，跨 Chat unmount 持久。
    // 同时抓上 assistantMessageId 以便后续诊断 / 可能的多 session 并发识别。
    registerStream(sid, { controller, assistantMessageId })
    setSessionLoading(sid, true)

    // ── P3 抛光（2026-05-16）：SSE 事件 rAF micro-batch ──────────────────
    // 5 个 tool_call 同帧到达时原本要触发 5 次 writeSessionChatMessages + 5 次
    // syncTimelineItems → store subscribers 重渲染 5 次（React 18 自动 batching
    // 在 fetch reader 的 await chunk 边界会断）。改成 rAF 合并：16ms 窗口内累积
    // events → 一次 prev.map + 内层依次 reduce 应用。SSE 总线 publishStreamEvent
    // 仍然每事件同步发，其他订阅者不受影响；只合并 chat store 写入侧。
    //
    // ── P4 平滑流（2026-05-16）：smooth streaming / typewriter 节流 ───────
    // 上面 rAF batch 保证一帧一次 setState，但**一帧涌入 50 字符仍会一蹦上屏**，
    // 视觉不稳（用户报告"输出文字太快有点不稳定的感觉"）。
    // 借鉴 Vercel AI SDK smoothStream + opencode-dev typewriter：
    //   content/thinking 文本按 CHARS_PER_FRAME 切片匀速播报，剩余字符留下一帧；
    //   结构化事件（tool_call/result/notice）原样发出**不消耗预算**，且必须等
    //   前面 text/thinking buffer 播完才出——否则工具卡会跳出来但文字还在流，
    //   顺序错乱看着比不平滑更糟。
    // queue FIFO 自然保证顺序：head 是 text 时按 budget 切片，剩余留 head；
    //   head 是结构化事件时原样移出。
    // CHARS_PER_FRAME = 12 → 60fps × 12 ≈ 720 字/秒，比模型生成速度（120-400 字/秒）
    //   快、不积压，但单帧最多 12 字、人眼能感知到"流"的连续性。
    // 正文（content）+ 思考（thinking）：都走"句子边界 chunked"——累 buffer，遇句子
    // 边界（。！？!?\n 等）整块 flush；强切阈值兜底过长无标点段落；静默超时兜底
    // LLM 不带标点收尾。整块入 queue 走 'event' kind 不受字符 budget 限制 → 一帧
    // 跳变出现，对齐 Cascade "一句一句啪啪出"的节奏。
    // 用户反馈"字符匀速像机关枪收不住"——所以正文也走块状，只是参数比思考紧
    // （强切 50 vs 60，超时 180 vs 250），节奏稍快但仍是块状跳变。
    type StreamQueueItem = { kind: 'event'; raw: ChatStreamLikeEvent }
    interface SmoothConfig {
      type: 'content' | 'thinking'
      flushChars: number
      flushTimeoutMs: number
      // 2026-05-17：最小 flush 间隔节流，限制块状跳变最大频率。
      // 模型突发输出时（一帧涌入大量字符）原本会连续多帧密集 flush 让用户视觉
      // "跟不上"；加节流后高速输出被限流到稳定节奏（CONTENT 5次/秒、THINKING 4次/秒），
      // 慢速输出仍由 flushTimeoutMs 兜底（不影响）。
      // force=true 路径（结构化事件 boundary）bypass 此节流保证顺序。
      minIntervalMs: number
      boundaryRe: RegExp
    }
    interface SmoothBuffer {
      config: SmoothConfig
      text: string
      agentId?: string
      timeoutId: number | null
      lastFlushAt: number    // 0 表示尚未 flush 过；用于 minIntervalMs 节流判定
    }
    // 2026-05-17：迭代到 v2（小块 + 中等节奏）——v3 大块慢节奏（60 字 / 350ms）
    // 用户反馈"过于慢"；回到小块 + minIntervalMs 节流方案，让突发输出被限流到
    // 4-5 次/秒稳定节奏，慢速输出仍由 timeout 兜底。
    const CONTENT_CONFIG: SmoothConfig = {
      type: 'content',
      flushChars: 30,            // 小块单次跳变量小，视觉柔和
      flushTimeoutMs: 220,       // 无标点段落兜底
      minIntervalMs: 200,        // 5 次/秒上限，限制突发块状跳变
      // 正文：仅强句子边界（不含 ；; 因正文里 ; 多用在代码/列表场景）
      boundaryRe: /[。！？!?\n]/,
    }
    const THINKING_CONFIG: SmoothConfig = {
      type: 'thinking',
      flushChars: 22,            // 每次约半行到一行，避免攒成大段跳变
      flushTimeoutMs: 160,       // 慢吐兜底及时上屏
      minIntervalMs: 120,        // ~8 次/秒均匀节拍
      boundaryRe: /[。！？；!?;,\n，]/,
    }
    let streamQueue: StreamQueueItem[] = []
    let flushScheduled = false
    const contentBuf: SmoothBuffer = { config: CONTENT_CONFIG, text: '', timeoutId: null, lastFlushAt: 0 }
    const thinkingBuf: SmoothBuffer = { config: THINKING_CONFIG, text: '', timeoutId: null, lastFlushAt: 0 }

    const cancelBufTimeout = (buf: SmoothBuffer) => {
      if (buf.timeoutId !== null) {
        clearTimeout(buf.timeoutId)
        buf.timeoutId = null
      }
    }

    type FlushMode = 'throttle' | 'timeout' | 'force'

    // 把 buf 中"已成块"的部分推入 queue。force=全推（无视节流和步长）。
    // 返回是否实际推入了 chunk。
    const flushSmoothBufferToQueue = (buf: SmoothBuffer, mode: FlushMode): boolean => {
      if (!buf.text) return false
      if (mode === 'throttle' && buf.lastFlushAt > 0) {
        const elapsedMs = Date.now() - buf.lastFlushAt
        if (elapsedMs < buf.config.minIntervalMs) return false
      }
      let cutAt = -1
      if (mode !== 'force') {
        const isThinking = buf.config.type === 'thinking'
        const maxStep = isThinking ? Math.max(buf.config.flushChars + 14, 36) : buf.text.length
        const searchEnd = Math.min(buf.text.length, maxStep)
        for (let i = searchEnd - 1; i >= 0; i--) {
          if (buf.config.boundaryRe.test(buf.text[i])) {
            cutAt = i + 1
            break
          }
        }
        if (cutAt < 0) {
          if (mode === 'throttle' && buf.text.length < buf.config.flushChars) return false
          cutAt = searchEnd
        }
      } else {
        cutAt = buf.text.length
      }
      const chunk = buf.text.slice(0, cutAt)
      buf.text = buf.text.slice(cutAt)
      streamQueue.push({
        kind: 'event',
        raw: { type: buf.config.type, content: chunk, agent_id: buf.agentId } as ChatStreamLikeEvent,
      })
      buf.lastFlushAt = Date.now()
      return true
    }

    const scheduleFlushIfNeeded = () => {
      if (!flushScheduled) {
        flushScheduled = true
        requestAnimationFrame(flushPendingStreamEvents)
      }
    }

    const scheduleBufTimeout = (buf: SmoothBuffer, delayMs: number) => {
      if (!buf.text || buf.timeoutId !== null) return
      buf.timeoutId = window.setTimeout(() => {
        buf.timeoutId = null
        if (flushSmoothBufferToQueue(buf, 'timeout')) scheduleFlushIfNeeded()
        if (buf.text) scheduleBufTimeout(buf, buf.config.minIntervalMs)
      }, delayMs)
    }

    const handleSmoothEvent = (buf: SmoothBuffer, ev: ChatStreamLikeEvent) => {
      buf.text += ev.content || ''
      buf.agentId = ev.agent_id
      const flushed = flushSmoothBufferToQueue(buf, 'throttle')
      if (flushed && !buf.text) {
        cancelBufTimeout(buf)
      } else {
        // 已有定时器 ticking 时不往后推，确保节奏均匀稳定
        scheduleBufTimeout(buf, flushed ? buf.config.minIntervalMs : buf.config.flushTimeoutMs)
      }
    }

    const enqueueStreamEvent = (ev: ChatStreamLikeEvent) => {
      if (ev.type === 'content') {
        // 正文先 flush thinking buffer 保顺序（"思考完了再说正文"）
        cancelBufTimeout(thinkingBuf)
        flushSmoothBufferToQueue(thinkingBuf, 'force')
        handleSmoothEvent(contentBuf, ev)
        return
      }
      if (ev.type === 'thinking') {
        handleSmoothEvent(thinkingBuf, ev)
        return
      }
      // 结构化事件（tool_call/result/notice/error/...）：必须等所有 buffer 清空才能出，
      // 否则会出现"工具卡跳出来但前面文字还没收尾"的顺序错乱。
      cancelBufTimeout(thinkingBuf)
      cancelBufTimeout(contentBuf)
      flushSmoothBufferToQueue(thinkingBuf, 'force')
      flushSmoothBufferToQueue(contentBuf, 'force')
      streamQueue.push({ kind: 'event', raw: ev })
    }

    const applyEventsToMessage = (events: ChatStreamLikeEvent[]) => {
      if (events.length === 0) return
      let updatedMessage: MessageType | null = null
      writeSessionChatMessages(sid, (prev) => prev.map((message) => {
        if (message.id !== assistantMessageId) return message
        let next: MessageType = { ...message, isStreaming: true }
        for (const ev of events) {
          next = applyStreamEventToMessage(next, ev)
        }
        updatedMessage = next
        return next
      }))
      syncTimelineItems(sid)
      // M2-Streaming：每帧把"当前消息累积的可朗读文本"喂给流式 speaker，
      // 完整句子立刻被切出去合成，无需等整段完成。
      // 仅当 sessionIdRef 仍是 sid 时才 feed——切走会话不该继续给老消息合成。
      if (updatedMessage && sessionIdRef.current === sid) {
        try {
          const fullText = extractSpeakableText(updatedMessage)
          if (fullText) {
            feedTtsStreaming({
              sessionId: sid,
              messageId: assistantMessageId,
              fullText,
              agentId: effectiveAgentId || undefined,
            })
          }
        } catch (e) {
          console.warn('[chat] TTS streaming feed failed:', e)
        }
      }
    }

    // queue 现在只装 'event' kind（content/thinking 都在 enqueue 时已成块入队，
    // 不再需要按 budget 切片），所以一帧消费全部即可。rAF batch 仍然保留：同一帧
    // 涌入多个块（一次 fetch chunk 拆出 5 个事件）合并成一次 setState。
    const flushPendingStreamEvents = () => {
      flushScheduled = false
      if (streamQueue.length === 0) return
      const events = streamQueue.map((item) => item.raw)
      streamQueue = []
      applyEventsToMessage(events)
    }

    // 终态（complete/abort/error）：force flush 两个 buffer + 一次性 drain queue，
    // 否则末尾未成句的字符 + 终态 tool_result 会丢失。
    const drainAllPendingStreamEvents = () => {
      flushScheduled = false
      cancelBufTimeout(thinkingBuf)
      cancelBufTimeout(contentBuf)
      flushSmoothBufferToQueue(thinkingBuf, 'force')
      flushSmoothBufferToQueue(contentBuf, 'force')
      if (streamQueue.length === 0) return
      const events = streamQueue.map((item) => item.raw)
      streamQueue = []
      applyEventsToMessage(events)
    }

    try {
      await sendMessage(
        text,
        (streamEvent: StreamEvent) => {
          if ((streamEvent as any).type === 'goal_state_update') {
            const g = (streamEvent as any).payload
            if (g) {
              onGoalUpdate?.(g)
            }
            return
          }
          enqueueStreamEvent(streamEvent)
          if (!flushScheduled) {
            flushScheduled = true
            requestAnimationFrame(flushPendingStreamEvents)
          }
        },
        () => {
          // 终态前必须 drain（全量一次出），否则末尾几十字 + tool_result 会被
          // 节流在 buffer 里丢失——P4 后 flushPending 只出 CHARS_PER_FRAME 个字，
          // 原来的一句 flushPending() 收不干净。
          drainAllPendingStreamEvents()
          // P1.3：正常 [DONE] 路径，从 registry 移除（不 abort，流已自然结束）。
          removeStream(sid)
          setSessionLoading(sid, false)

          const cacheUpdater = (prev: MessageType[]) => prev.map((message) =>
            message.id === assistantMessageId
              ? finalizeLastThinkingSegment({ ...message, isStreaming: false })
              : message,
          )

          writeSessionChatMessages(sid, cacheUpdater)

          syncTimelineItems(sid)
          if (sessionIdRef.current === sid) {
            syncTimelineItems(sid)
            setIsLoading(false)
            setExpandedThinking((prev) => {
              const next = new Set(prev)
              next.add(`${assistantMessageId}-think`)
              return next
            })

            // M2-Streaming：流式 speaker 在 applyEventsToMessage 里逐句已喂出去合成；
            // 这里只 flush 尾巴（最后一句没标点收尾时）。
            try {
              flushTtsStreaming({
                sessionId: sid,
                messageId: assistantMessageId,
                agentId: effectiveAgentId || undefined,
              })
            } catch (e) {
              console.warn('[chat] TTS flush failed:', e)
            }
          }

          void refreshSessionState(sid).catch(() => null)
          onRefreshGoal?.()
          void userTimestamp
        },
        sessionId || undefined,
        selectedModel || undefined,
        submitProvider || undefined,
        effectiveAgentId,
        controller.signal,
        { userClientId, assistantClientId },
        attachmentSnapshot,
      )
    } catch (error: any) {
      // P3 micro-batch + P4 节流：错误/abort 前 drain 保证已收事件不丢
      drainAllPendingStreamEvents()
      // M2-Streaming：abort/error 都立刻停朗读 + 清状态
      // 防止半截消息留 cursor 状态污染下一次重发
      resetTtsStreaming(sid)
      if (error.name === 'AbortError') {
        // P1.3：AbortError 路径一般是 handleStop 触发的，abortStream 已经在那里 remove 过。
        // 这里幂等再打一次，防止额外路径（外部 abort signal）漏清。
        removeStream(sid)
        setSessionLoading(sid, false)

        const stopUpdater = (prev: MessageType[]) => prev
          .map((message) => (
            message.isStreaming ? finalizeLastThinkingSegment({ ...message, isStreaming: false }) : message
          ))

        writeSessionChatMessages(sid, stopUpdater)
        if (sessionIdRef.current === sid) {
          setIsLoading(false)
          syncTimelineItems(sid)
        }
        return
      }

      const errorMessage = error?.message || String(error)
      console.error('Chat error:', errorMessage)

      const errorUpdater = (prev: MessageType[]) => prev
        .map((message) => (
          message.id === assistantMessageId
            ? finalizeLastThinkingSegment({ ...message, content: formatChatRequestErrorMessage(errorMessage), isStreaming: false })
            : message
        ))

      writeSessionChatMessages(sid, errorUpdater)
      if (sessionIdRef.current === sid) {
        syncTimelineItems(sid)
        setIsLoading(false)
      }
    } finally {
      releaseLock()
    }
  }, [
    applyStreamEventToMessage,
    currentAgent?.name,
    currentAgentId,
    currentSession?.id,
    handleStop,
    input,
    inputRef,
    isLoading,
    isSendingRef,
    onSessionCreated,
    selectedModel,
    submitProvider,
    sessionIdRef,
    setExpandedThinking,
    setInput,
    setIsLoading,
    setSessionLoading,
    setShowCommands,
    syncTimelineItems,
    userScrolledUpRef,
    onRefreshGoal,
    onBeforeSend,
  ])

  const handleSubmitAskAnswer = useCallback(async (
    askId: string,
    toolUseId: string | undefined,
    answers: AskUserAnswer[],
  ) => {
    const sid = currentSession?.id || sessionIdRef.current
    if (!sid) {
      setAskState(askId, { state: 'failed', error: '会话未就绪，无法提交回答' })
      return
    }
    setAskState(askId, { state: 'submitting', error: null })
    // v2（2026-09-29，交互式阻塞语义）：提问后工具循环在后端挂起等待，
    // 提交直接 POST /chat/ask/answer 把答案投递回去 → 答案作为该 tool_call 的
    // tool_result 同轮回流，模型立即继续（不再走"答案当新消息"路径，也就
    // 不存在与在途流的发送锁冲突 / 假"已回答" / override 文本残留问题）。
    try {
      await submitAskAnswer({
        session_id: sid,
        ask_id: askId,
        tool_use_id: toolUseId ?? null,
        answers,
      })
      setAskState(askId, { state: 'submitted', error: null })
    } catch (err: any) {
      // 404 = pending ask 已不存在（页面刷新 / 后端重启 / 等待超时）。
      // 降级走旧路径：答案作为普通 user message 回流，模型从历史里接上。
      if (err?.status === 404) {
        submitOverrideTextRef.current =
          '[用户对 ask_user_question 的回答]\n\n' + JSON.stringify({
            kind: 'ask_user_answer' as const,
            ask_id: askId,
            tool_use_id: toolUseId ?? null,
            answers,
          }, null, 2)
        try {
          await handleSubmit()
          setAskState(askId, { state: 'submitted', error: null })
        } catch (err2: any) {
          setAskState(askId, {
            state: 'failed',
            error: String(err2?.message || err2 || '提交失败'),
          })
        }
        return
      }
      setAskState(askId, {
        state: 'failed',
        error: String(err?.message || err || '提交失败'),
      })
    }
  }, [currentSession?.id, handleSubmit, setAskState])

  return {
    handleStop,
    handleSubmit,
    askStatesByAskId,
    handleSubmitAskAnswer,
    refreshSessionState,
  }
}
