import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { Loader2, Mic } from 'lucide-react'
import { transcribeAudio, TranscriptionError } from '../api/voice'

export interface MicSpeechButtonRef {
  /** 开始 / 停止 录音的切换。Ctrl+Shift+M 全局快捷键调它。 */
  toggleRecording: () => void
  isRecording: () => boolean
  isLoading: () => boolean
}

interface MicSpeechButtonProps {
  /** 整体禁用按钮（如 isLoading 时）。 */
  disabled?: boolean
  /** 转写结果回调：调用方拼接到 textarea。 */
  onTranscription: (text: string) => void
  /** 错误回调：用户拒权限 / 录音过大 / 转写失败。调用方可弹 toast。 */
  onError?: (message: string) => void
  /** 录音上限（毫秒）。默认 5 分钟，跟 backend max_duration_seconds 对齐。 */
  maxDurationMs?: number
  /** 音频文件大小上限（MB）。默认 25。 */
  maxAudioSizeMb?: number
}

/** 录音中的波形动画（4 条柱子高低脉动）。 */
function RecordingWaves() {
  return (
    <span className="mic-waves" aria-hidden>
      {[0, 1, 2, 3].map((i) => (
        <span key={i} className="mic-wave-bar" style={{ animationDelay: `${i * 0.12}s` }} />
      ))}
    </span>
  )
}

/**
 * 麦克风按钮：点一次开始录音、再点一次停止 + 转写。
 *
 * 设计取舍：
 * - 用 MediaRecorder + 后端 Whisper（不走 Web Speech API）。理由见
 *   `docs/architecture_spec.md` §1。
 * - push-to-talk 行为通过 ref 暴露 toggleRecording，外层 Chat.tsx 用 Ctrl+Shift+M
 *   绑全局快捷键。
 * - mimeType 优先 audio/webm（Chrome/Edge 默认），不支持时退到 audio/mp4（Safari）。
 *
 * 错误统一通过 onError 报告，本组件内部不弹 alert / toast，由 Chat 层决定 UI。
 */
const MicSpeechButton = forwardRef<MicSpeechButtonRef, MicSpeechButtonProps>(
  ({ disabled, onTranscription, onError, maxDurationMs = 5 * 60 * 1000, maxAudioSizeMb = 25 }, ref) => {
    const [recording, setRecording] = useState(false)
    const [loading, setLoading] = useState(false)
    const recorderRef = useRef<MediaRecorder | null>(null)
    const chunksRef = useRef<Blob[]>([])
    // ref 镜像 + state：state 触发渲染、ref 给闭包/快捷键回调用同步语义
    const isRecordingRef = useRef(false)
    const isLoadingRef = useRef(false)
    const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

    const cleanupTimer = () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }

    const stopRecording = useCallback(() => {
      if (recorderRef.current && isRecordingRef.current) {
        try {
          recorderRef.current.stop()
        } catch {
          /* 已 stop 状态再 stop 抛 InvalidStateError，忽略 */
        }
        isRecordingRef.current = false
        setRecording(false)
      }
    }, [])

    const startRecording = useCallback(async () => {
      if (isRecordingRef.current || isLoadingRef.current) return
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
        const mimeType = MediaRecorder.isTypeSupported('audio/webm')
          ? 'audio/webm'
          : MediaRecorder.isTypeSupported('audio/mp4')
            ? 'audio/mp4'
            : ''
        const recorder = mimeType
          ? new MediaRecorder(stream, { mimeType })
          : new MediaRecorder(stream)
        chunksRef.current = []

        recorder.ondataavailable = (e) => {
          if (e.data.size > 0) chunksRef.current.push(e.data)
        }

        recorder.onstop = async () => {
          stream.getTracks().forEach((t) => t.stop())
          cleanupTimer()
          const actualType = recorder.mimeType || mimeType || 'audio/webm'
          const blob = new Blob(chunksRef.current, { type: actualType })
          const sizeMb = blob.size / 1024 / 1024
          if (sizeMb > maxAudioSizeMb) {
            onError?.(`录音文件过大（${sizeMb.toFixed(1)}MB > ${maxAudioSizeMb}MB），请缩短录音`)
            return
          }
          if (blob.size === 0) {
            // 极短按下立即松开 → 0 字节，静默忽略不打扰用户
            return
          }
          isLoadingRef.current = true
          setLoading(true)
          try {
            const ext = actualType.includes('webm')
              ? 'webm'
              : actualType.includes('mp4')
                ? 'mp4'
                : actualType.includes('ogg')
                  ? 'ogg'
                  : 'webm'
            const result = await transcribeAudio(blob, `audio.${ext}`)
            if (result.text) onTranscription(result.text)
            else onError?.('转写返回空文本，请重试')
          } catch (e: unknown) {
            if (e instanceof TranscriptionError) {
              if (e.status === 403) onError?.('语音转写功能已禁用，请到设置 → 语音输入开启')
              else if (e.status === 413) onError?.(`录音过大：${e.detail}`)
              else onError?.(`转写失败：${e.detail}`)
            } else {
              const msg = e instanceof Error ? e.message : String(e)
              onError?.(`转写请求失败：${msg}`)
            }
          } finally {
            isLoadingRef.current = false
            setLoading(false)
          }
        }

        recorder.start()
        recorderRef.current = recorder
        isRecordingRef.current = true
        setRecording(true)

        timerRef.current = setTimeout(() => {
          if (isRecordingRef.current) {
            onError?.(`录音超过 ${Math.round(maxDurationMs / 1000)} 秒，已自动停止`)
            stopRecording()
          }
        }, maxDurationMs)
      } catch (err: unknown) {
        const msg =
          err instanceof Error
            ? err.name === 'NotAllowedError'
              ? '已拒绝麦克风权限，请在浏览器/系统设置里允许后重试'
              : err.message
            : String(err)
        onError?.(`无法启动录音：${msg}`)
      }
    }, [maxAudioSizeMb, maxDurationMs, onError, onTranscription, stopRecording])

    const toggleRecording = useCallback(() => {
      if (isLoadingRef.current) return
      if (isRecordingRef.current) stopRecording()
      else void startRecording()
    }, [startRecording, stopRecording])

    useImperativeHandle(
      ref,
      () => ({
        toggleRecording,
        isRecording: () => isRecordingRef.current,
        isLoading: () => isLoadingRef.current,
      }),
      [toggleRecording],
    )

    // 卸载兜底：组件 unmount 时若还在录，强制停 + 释放 stream
    useEffect(
      () => () => {
        cleanupTimer()
        const rec = recorderRef.current
        if (rec && isRecordingRef.current) {
          try {
            rec.stop()
          } catch {
            /* ignore */
          }
        }
      },
      [],
    )

    const isDisabled = disabled || loading
    const title = loading
      ? '转写中…'
      : recording
        ? '点击停止录音'
        : '语音输入 (Ctrl+Shift+M)'

    return (
      <button
        type="button"
        className={`mic-button ${recording ? 'is-recording' : ''} ${loading ? 'is-loading' : ''}`}
        onClick={toggleRecording}
        disabled={isDisabled}
        title={title}
        aria-label={title}
      >
        {loading ? <Loader2 size={16} className="mic-spinner" /> : recording ? <RecordingWaves /> : <Mic size={16} />}
      </button>
    )
  },
)

MicSpeechButton.displayName = 'MicSpeechButton'

export default MicSpeechButton
