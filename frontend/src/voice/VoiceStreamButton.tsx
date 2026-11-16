/**
 * VoiceStreamButton：流式语音输入按钮（边说边出字）——项目里唯一的语音输入入口。
 * 复用 mic-button / mic-waves 全套样式类。
 * 实时预览文字不在这里显示——通过 onDraftChange 直接写进输入框（草稿区替换式渲染）。
 *
 * ref 暴露 toggleRecording / isRecording / isLoading（Ctrl+Shift+M 快捷键用）。
 */
import { forwardRef, useCallback, useImperativeHandle, useRef } from 'react'
import { Loader2, Mic } from 'lucide-react'

import { useVoiceStream, type VoiceStreamPhase } from './useVoiceStream'
import type { VoiceStreamButtonRef } from './types'

interface VoiceStreamButtonProps {
  disabled?: boolean
  onTranscription: (text: string) => void
  onError?: (message: string) => void
  /** 录音上限秒数（/voice/stream/config 下发，默认 600）。 */
  maxSeconds?: number
  /** 草稿回调：partial 实时写进输入框（见 useVoiceStream 头注释）。 */
  onDraftChange?: (draft: string | null) => void
}

/** 录音中的波形动画（复用现有 mic-waves CSS）。 */
function RecordingWaves() {
  return (
    <span className="mic-waves" aria-hidden>
      {[0, 1, 2, 3].map((i) => (
        <span key={i} className="mic-wave-bar" style={{ animationDelay: `${i * 0.12}s` }} />
      ))}
    </span>
  )
}

const VoiceStreamButton = forwardRef<VoiceStreamButtonRef, VoiceStreamButtonProps>(
  ({ disabled, onTranscription, onError, maxSeconds = 600, onDraftChange }, ref) => {
    const { phase, level, toggle } = useVoiceStream({
      onTranscription,
      onError,
      maxSeconds,
      onDraftChange,
    })

    const phaseRef = useRef<VoiceStreamPhase>(phase)
    phaseRef.current = phase

    useImperativeHandle(
      ref,
      () => ({
        toggleRecording: () => toggle(),
        isRecording: () => phaseRef.current === 'recording',
        isLoading: () =>
          phaseRef.current === 'connecting' || phaseRef.current === 'finishing',
      }),
      [toggle],
    )

    const busy = phase === 'connecting' || phase === 'finishing'
    const isDisabled = disabled || busy
    const title =
      busy
        ? phase === 'connecting'
          ? '连接语音服务…'
          : '等待最终结果…'
        : phase === 'recording'
          ? '点击停止 · Esc 取消'
          : '流式语音输入 (Ctrl+Shift+M)'

    const toggleWithGuard = useCallback(() => {
      if (isDisabled) return
      toggle()
    }, [isDisabled, toggle])

    // 波形高度用电平调制（不用也行——CSS 动画本身有脉动，这里叠一层真实音量）
    const scale = 0.5 + Math.min(1, level * 3) * 0.9

    return (
      <span
        className="voice-stream-slot"
        style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
      >
        <button
          type="button"
          className={`mic-button ${phase === 'recording' ? 'is-recording' : ''} ${busy ? 'is-loading' : ''}`}
          onClick={toggleWithGuard}
          disabled={isDisabled}
          title={title}
          aria-label={title}
        >
          {busy ? (
            <Loader2 size={16} className="mic-spinner" />
          ) : phase === 'recording' ? (
            <span style={{ display: 'inline-flex', transform: `scale(${scale.toFixed(2)})` }}>
              <RecordingWaves />
            </span>
          ) : (
            <Mic size={16} />
          )}
        </button>
      </span>
    )
  },
)

VoiceStreamButton.displayName = 'VoiceStreamButton'

export default VoiceStreamButton
