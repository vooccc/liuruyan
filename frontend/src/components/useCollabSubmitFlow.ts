import { useCallback, type Dispatch, type MutableRefObject, type SetStateAction } from 'react'
import { type SceneInfo, collabChat, type ContentSegment, type CollabChatEvent } from '../api/chat'
import type { CollabBubble } from './CollabMessageItem'
import { COLLAB_UI_TEXT, formatCollabRequestErrorMessage } from './collabUiText'
import { setAbortController, abortStream } from '../stores/collabStreamStore'

interface UseCollabSubmitFlowParams {
  input: string
  isChatting: boolean
  scene?: SceneInfo
  /**
   * 用户在 SpeakerPicker 里指定的发言序列（粘性）。
   * 空数组 = 按 scene.turn_order 默认发言；非空 = 只让列表里的人按这个顺序发言。
   * 允许同 id 重复，由后端按顺序逐个跑。
   */
  speakers: string[]
  /** 当前 bucket key（CollabChat 计算）——所有 store 写入按这个 key 走 */
  sessionKey: string
  /**
   * 已有 session 的稳定 id（用于 collab API 请求体）。
   * 新建场景未拿到 session_id 时为 undefined，sessionKey 会是临时 'scene::xxx'，
   * 流首事件 session_info 到达后由 handleCollabEvent 触发 rekey。
   */
  existingSessionId?: string
  userScrolledUpRef: MutableRefObject<boolean>
  createBubbleId: (prefix: string) => string
  setInput: Dispatch<SetStateAction<string>>
  setIsChatting: (key: string, value: boolean) => void
  appendBubble: (key: string, bubble: CollabBubble) => void
  updateBubbles: (key: string, updater: (bubbles: CollabBubble[]) => CollabBubble[]) => void
  buildTextSegment: (content: string, agentId?: string) => ContentSegment
  handleCollabEvent: (event: CollabChatEvent) => void
}

export function useCollabSubmitFlow({
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
  buildTextSegment,
  handleCollabEvent,
}: UseCollabSubmitFlowParams) {
  /** 按给定文本发起一轮协作（2026-09-30 抽出：编辑重发走这里，不经过 input 状态） */
  const sendWithText = useCallback(async (rawText: string) => {
    const message = rawText.trim()
    if (!message || isChatting) return

    setIsChatting(sessionKey, true)
    userScrolledUpRef.current = false

    const controller = new AbortController()
    // 闭包捕获本次发送时的 sessionKey 作为 abort key——
    // 即使稍后 session_info 触发 rekey 把 bucket 迁到真实 session_id，
    // 我们也已通过 rekeyResources 把 controller 跟着迁过去了，
    // 后续 abortStream(realKey) 仍能 abort 这个 controller。
    setAbortController(sessionKey, controller)

    appendBubble(sessionKey, {
      id: createBubbleId('user'),
      agentId: '__user__',
      agentName: COLLAB_UI_TEXT.userName,
      avatar: COLLAB_UI_TEXT.userAvatar,
      content: message,
      isStreaming: false,
      segments: [buildTextSegment(message, '__user__')],
      chainExpanded: false,
    })

    let eventCount = 0
    try {
      const stream = collabChat(
        scene?.id || '',
        message,
        undefined,
        undefined,
        existingSessionId || undefined,
        controller.signal,
        speakers && speakers.length > 0 ? speakers : undefined,
      )

      for await (const event of stream) {
        eventCount += 1
        handleCollabEvent(event)
      }

      if (eventCount === 0) {
        appendBubble(sessionKey, {
          id: createBubbleId('system-empty'),
          agentId: '__system__',
          agentName: COLLAB_UI_TEXT.systemName,
          avatar: COLLAB_UI_TEXT.systemAvatar,
          content: COLLAB_UI_TEXT.noReplyMessage,
          isStreaming: false,
          segments: [buildTextSegment(COLLAB_UI_TEXT.noReplyMessage, '__system__')],
          chainExpanded: false,
        })
      }
    } catch (error: any) {
      if (error?.name === 'AbortError') {
        // 用户主动停止，静默
      } else if (eventCount > 0) {
        // 已经收到过事件 = 角色发言都已展示 → 流尾的 network error 属于
        // 服务端关连接 / 代理超时的良性抖动，不再向用户弹系统错误。
        console.warn('[CollabChat] 流结束后连接异常（已有回复，忽略）:', error)
      } else {
        const errorText = formatCollabRequestErrorMessage(error?.message)
        appendBubble(sessionKey, {
          id: createBubbleId('system-error'),
          agentId: '__system__',
          agentName: COLLAB_UI_TEXT.systemName,
          avatar: COLLAB_UI_TEXT.systemAvatar,
          content: errorText,
          isStreaming: false,
          segments: [buildTextSegment(errorText, '__system__')],
          chainExpanded: false,
        })
      }
    } finally {
      setIsChatting(sessionKey, false)
      // 注意不要直接清 abortController：rekey 后 sessionKey 可能已是临时 key，
      // controller 已迁走。这里安全做法是只清掉本 key 上残留的 entry。
      setAbortController(sessionKey, null)
    }
  }, [
    appendBubble,
    buildTextSegment,
    createBubbleId,
    existingSessionId,
    handleCollabEvent,
    isChatting,
    speakers,
    scene,
    sessionKey,
    setIsChatting,
    userScrolledUpRef,
  ])

  const handleSend = useCallback(async () => {
    const message = input.trim()
    if (!message || isChatting) return
    setInput('')
    await sendWithText(message)
  }, [input, isChatting, sendWithText, setInput])

  const handleStop = useCallback(() => {
    abortStream(sessionKey)
    setIsChatting(sessionKey, false)
    updateBubbles(sessionKey, (prev) => prev.map((bubble) => ({ ...bubble, isStreaming: false })))
  }, [sessionKey, setIsChatting, updateBubbles])

  return { handleSend, handleStop, sendWithText }
}
