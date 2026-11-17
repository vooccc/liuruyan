import { useCallback, useEffect, useRef, useState } from 'react'

import { VoiceStreamClient } from './client'
import { startPcmCapture, type PcmStream } from './pcmCapture'
import type { VoiceStreamServerEvent } from './types'

export type VoiceStreamPhase = 'idle' | 'connecting' | 'recording' | 'finishing'

export interface UseVoiceStreamOptions {
  onTranscription: (text: string, index?: number) => void
  onError?: (message: string) => void
  maxSeconds?: number
  onDraftChange?: (draft: string | null, preserve?: boolean) => void
  onPolished?: (index: number, text: string) => void
}

export interface VoiceStreamController {
  phase: VoiceStreamPhase
  partial: string
  level: number
  toggle: () => void
  cancel: () => void
}

type Recording = {
  client: VoiceStreamClient
  pcm: PcmStream | null
  started: boolean
  finishPending: boolean
  pendingAudio: Int16Array[]
  timer: ReturnType<typeof setTimeout> | null
  readyTimer: ReturnType<typeof setTimeout> | null
}

export function useVoiceStream(opts: UseVoiceStreamOptions): VoiceStreamController {
  const { maxSeconds = 600 } = opts
  const callbacks = useRef(opts)
  callbacks.current = opts
  const [phase, setPhase] = useState<VoiceStreamPhase>('idle')
  const [partial, setPartial] = useState('')
  const [level, setLevel] = useState(0)
  const phaseRef = useRef<VoiceStreamPhase>('idle')
  const active = useRef<Recording | null>(null)

  const changePhase = useCallback((next: VoiceStreamPhase) => {
    phaseRef.current = next
    setPhase(next)
  }, [])

  const release = useCallback((recording: Recording) => {
    if (recording.timer !== null) clearTimeout(recording.timer)
    recording.timer = null
    if (recording.readyTimer !== null) clearTimeout(recording.readyTimer)
    recording.readyTimer = null
    recording.pendingAudio = []
    recording.client.close()
    const pcm = recording.pcm
    recording.pcm = null
    if (pcm) void pcm.stop().catch((error) => console.warn('voice capture cleanup failed', error))
  }, [])

  const complete = useCallback((recording: Recording, error?: string) => {
    if (active.current !== recording) return
    active.current = null
    release(recording)
    setPartial('')
    setLevel(0)
    callbacks.current.onDraftChange?.(null, Boolean(error))
    changePhase('idle')
    if (error) callbacks.current.onError?.(error)
  }, [changePhase, release])

  const stopSession = useCallback(async () => {
    const recording = active.current
    if (!recording || phaseRef.current !== 'recording') return
    changePhase('finishing')
    if (recording.timer !== null) clearTimeout(recording.timer)
    recording.timer = setTimeout(() => {
      complete(recording, '等待语音最终结果超时，已停止录音；已识别的文字保留')
    }, 35000)
    const pcm = recording.pcm
    recording.pcm = null
    try {
      await pcm?.stop()
      if (active.current !== recording) return
      setLevel(0)
      if (recording.started) recording.client.sendFinish()
      else recording.finishPending = true
    } catch (error) {
      complete(recording, `停止录音失败：${String(error)}`)
    }
  }, [changePhase, complete])

  const startSession = useCallback(async () => {
    changePhase('connecting')
    setPartial('')
    const recording: Recording = {
      client: new VoiceStreamClient({
        onEvent: (event: VoiceStreamServerEvent) => {
          if (active.current !== recording) return
          switch (event.type) {
            case 'started':
              if (recording.readyTimer !== null) clearTimeout(recording.readyTimer)
              recording.readyTimer = null
              recording.started = true
              for (const pcm of recording.pendingAudio) {
                if (active.current !== recording) break
                recording.client.sendAudio(pcm)
              }
              recording.pendingAudio = []
              if (active.current === recording && recording.finishPending) recording.client.sendFinish()
              break
            case 'partial':
              setPartial(event.text)
              callbacks.current.onDraftChange?.(event.text)
              break
            case 'final':
              setPartial('')
              callbacks.current.onTranscription(event.text, event.index)
              break
            case 'polished':
              callbacks.current.onPolished?.(event.index, event.text)
              break
            case 'finished':
              complete(recording)
              break
            case 'failed':
              complete(recording, `流式语音失败（${event.code}）：${event.message}`)
              break
          }
        },
        onError: (message) => complete(recording, message),
      }),
      pcm: null,
      started: false,
      finishPending: false,
      pendingAudio: [],
      timer: null,
      readyTimer: null,
    }
    active.current = recording
    recording.timer = setTimeout(() => {
      complete(recording, '启动语音输入超时，请检查麦克风权限或语音服务后重试')
    }, 30000)
    try {
      // 权限等待无法中止；过期调用拿到麦克风后必须立即释放。
      const pcm = await startPcmCapture((chunk) => {
        if (active.current !== recording) return
        if (recording.started) recording.client.sendAudio(chunk.pcm)
        else {
          if (recording.pendingAudio.length >= 300) {
            complete(recording, '语音启动等待过久，请重试')
            return
          }
          recording.pendingAudio.push(chunk.pcm)
        }
        if (active.current === recording) setLevel(chunk.level)
      })
      if (active.current !== recording) {
        await pcm.stop()
        return
      }
      recording.pcm = pcm
      await recording.client.connect()
      if (active.current !== recording) return
      recording.client.sendStart()
      changePhase('recording')
      if (recording.timer !== null) clearTimeout(recording.timer)
      recording.readyTimer = setTimeout(() => {
        complete(recording, '语音服务启动超时，请重试')
      }, 15000)
      recording.timer = setTimeout(() => {
        if (active.current === recording) void stopSession()
      }, Math.max(1, maxSeconds) * 1000)
    } catch (error) {
      const message = error instanceof Error
        ? error.name === 'NotAllowedError'
          ? '已拒绝麦克风权限，请在浏览器/系统设置里允许后重试'
          : error.message
        : String(error)
      complete(recording, `无法启动流式录音：${message}`)
    }
  }, [changePhase, complete, maxSeconds, stopSession])

  const cancelSession = useCallback(() => {
    const recording = active.current
    if (!recording) return
    complete(recording)
  }, [complete])

  const toggle = useCallback(() => {
    if (phaseRef.current === 'recording') void stopSession()
    else if (phaseRef.current === 'idle') void startSession()
    else cancelSession()
  }, [cancelSession, startSession, stopSession])

  useEffect(() => {
    if (phase === 'idle') return
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopPropagation()
      cancelSession()
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [phase, cancelSession])

  useEffect(() => () => {
    const recording = active.current
    active.current = null
    phaseRef.current = 'idle'
    if (recording) release(recording)
  }, [release])

  return { phase, partial, level, toggle, cancel: cancelSession }
}
