/**
 * VoiceInputSlot：语音输入插槽——流式语音是项目里唯一的语音输入入口
 * （旧的 MicSpeechButton 批量转写已于 2026-11-15 整体拆除）。
 *
 * 渲染条件：GET /voice/stream/config 的 available === true（enabled + 引擎就绪）。
 * 刷新时机：挂载时一次 + 'voice-config-updated' 自定义事件（设置页保存后广播）。
 * 不可用（开关关了 / 本地模型缺失 / 后端未起）→ 不渲染任何东西，无回退。
 */
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'

import VoiceStreamButton from './VoiceStreamButton'
import { fetchVoiceStreamConfig, type VoiceStreamButtonRef, type VoiceStreamConfigResponse } from './types'

interface VoiceInputSlotProps {
  disabled?: boolean
  onTranscription: (text: string) => void
  onError?: (message: string) => void
  /** 草稿回调：partial 实时进输入框。 */
  onDraftChange?: (draft: string | null) => void
}

const VoiceInputSlot = forwardRef<VoiceStreamButtonRef, VoiceInputSlotProps>(
  ({ disabled, onTranscription, onError, onDraftChange }, ref) => {
    const [streamCfg, setStreamCfg] = useState<VoiceStreamConfigResponse | null>(null)
    const streamBtnRef = useRef<VoiceStreamButtonRef | null>(null)
    const useStream = streamCfg?.available === true

    useEffect(() => {
      let cancelled = false
      const refresh = () => {
        fetchVoiceStreamConfig().then((data) => {
          if (!cancelled) setStreamCfg(data)
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

    // ref 转发：暴露流式按钮（Ctrl+Shift+M 快捷键无需感知可用性）
    const toggleRecording = useCallback(() => {
      streamBtnRef.current?.toggleRecording()
    }, [])

    useImperativeHandle(
      ref,
      () => ({
        toggleRecording,
        isRecording: () => streamBtnRef.current?.isRecording() ?? false,
        isLoading: () => streamBtnRef.current?.isLoading() ?? false,
      }),
      [toggleRecording],
    )

    if (!useStream) return null

    return (
      <VoiceStreamButton
        ref={streamBtnRef}
        disabled={disabled}
        onTranscription={onTranscription}
        onError={onError}
        maxSeconds={streamCfg?.max_session_seconds ?? 600}
        onDraftChange={onDraftChange}
      />
    )
  },
)

VoiceInputSlot.displayName = 'VoiceInputSlot'

export default VoiceInputSlot
