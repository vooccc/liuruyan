import { useCallback, type Dispatch, type KeyboardEvent, type MutableRefObject, type SetStateAction } from 'react'
import { sendCorridorMessage } from '../api/chat'

interface UseChatInteractionsParams {
  input: string
  currentAgentId?: string | null
  inputRef: MutableRefObject<HTMLTextAreaElement | null>
  corridorTarget: string | null
  corridorContent: string
  handleSubmit: () => void | Promise<void>
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
  currentAgentId,
  inputRef,
  corridorTarget,
  corridorContent,
  handleSubmit,
  setInput,
  setShowCommands,
  setShowCorridorPanel,
  setCorridorTarget,
  setCorridorContent,
  setCorridorSending,
  setShowModelPicker,
}: UseChatInteractionsParams) {
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
    setShowCommands(value.startsWith('/') && !/\s/.test(value))
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
    handleKeyDown,
    selectCommand,
    handleInputChange,
    handleToggleModelPicker,
    handleToggleCorridorPanel,
    handleCancelCorridor,
    handleSendCorridor,
  }
}
