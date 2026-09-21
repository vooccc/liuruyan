import { useCallback, type Dispatch, type KeyboardEvent, type MutableRefObject, type SetStateAction } from 'react'
import { clearHistory, sendCorridorMessage, type Session } from '../api/chat'
import type { ChatTimelineItem } from './chatExternalHandle'
import { CHAT_UI_TEXT } from './chatUiText'
import type { ConfirmType } from './ConfirmDialog'
import { useAppStore } from '../stores/useAppStore'
import { writeSessionMessageMeta } from './useChatSessionRuntime'

interface UseChatInteractionsParams {
  input: string
  currentSession?: Session | null
  currentAgentId?: string | null
  inputRef: MutableRefObject<HTMLTextAreaElement | null>
  corridorTarget: string | null
  corridorContent: string
  confirmAction: (title: string, message: string, type?: ConfirmType, confirmText?: string) => Promise<boolean>
  handleSubmit: () => void | Promise<void>
  setTimelineItems: Dispatch<SetStateAction<ChatTimelineItem[]>>
  setInput: Dispatch<SetStateAction<string>>
  setShowCommands: Dispatch<SetStateAction<boolean>>
  setShowCorridorPanel: Dispatch<SetStateAction<boolean>>
  setCorridorTarget: Dispatch<SetStateAction<string | null>>
  setCorridorContent: Dispatch<SetStateAction<string>>
  setCorridorSending: Dispatch<SetStateAction<boolean>>
  setShowModelPicker: Dispatch<SetStateAction<boolean>>
}

export function useChatInteractions({
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
}: UseChatInteractionsParams) {
  const handleClearHistory = useCallback(async () => {
    const ok = await confirmAction(
      CHAT_UI_TEXT.clearDialogTitle,
      CHAT_UI_TEXT.clearDialogMessage,
      'warning',
      CHAT_UI_TEXT.clearAction,
    )
    if (!ok) return

    const sid = currentSession?.id || ''
    await clearHistory(sid || undefined)
    setTimelineItems([])
    if (sid) {
      useAppStore.getState().setSessionChatMessages(sid, [])
      // P1.2（架构重构 2026-05-23）：meta 走 store helper 清（传 null）。
      writeSessionMessageMeta(sid, null)
      // P1.4b（架构重构 2026-05-23）：timelineMarkers 走 store action 清（传 null）。
      useAppStore.getState().setSessionTimelineMarkers(sid, null)
    }
  }, [
    confirmAction,
    currentSession?.id,
    setTimelineItems,
  ])

  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      handleSubmit()
    }
    if (event.key === '/' && !input) {
      setShowCommands(true)
    }
    if (event.key === 'Escape') {
      setShowCommands(false)
    }
  }, [handleSubmit, input, setShowCommands])

  const selectCommand = useCallback((command: { command: string }) => {
    setShowCommands(false)
    setInput(command.command + ' ')
    inputRef.current?.focus()
  }, [inputRef, setInput, setShowCommands])

  const handleInputChange = useCallback((value: string) => {
    setInput(value)
    setShowCommands(value.startsWith('/'))
  }, [setInput, setShowCommands])

  const handleToggleModelPicker = useCallback(() => {
    setShowModelPicker((prev) => !prev)
  }, [setShowModelPicker])

  const handleToggleCorridorPanel = useCallback(() => {
    setShowCorridorPanel((prev) => !prev)
  }, [setShowCorridorPanel])

  const handleCancelCorridor = useCallback(() => {
    setShowCorridorPanel(false)
    setCorridorTarget(null)
    setCorridorContent('')
  }, [setCorridorContent, setCorridorTarget, setShowCorridorPanel])

  const handleSendCorridor = useCallback(async () => {
    if (!corridorTarget || !corridorContent.trim() || !currentAgentId) return

    setCorridorSending(true)
    try {
      await sendCorridorMessage({
        from_agent: currentAgentId,
        to_agent: corridorTarget,
        content: corridorContent.trim(),
      })
      handleCancelCorridor()
    } catch {
    } finally {
      setCorridorSending(false)
    }
  }, [
    corridorContent,
    corridorTarget,
    currentAgentId,
    handleCancelCorridor,
    setCorridorSending,
  ])

  return {
    handleClearHistory,
    handleKeyDown,
    selectCommand,
    handleInputChange,
    handleToggleModelPicker,
    handleToggleCorridorPanel,
    handleCancelCorridor,
    handleSendCorridor,
  }
}
