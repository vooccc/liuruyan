/**
 * useVoiceStream：流式语音输入状态机（idle → connecting → recording → finishing → idle）。
 *
 * 文本落地策略：
 * - partial（当前句中间结果）→ onDraftChange(text)：草稿实时写进输入框末尾（替换式）。
 * - 每收到一句 final → 先 onDraftChange(null) 撤掉草稿，再 onTranscription(text)
 *   复用 Chat.tsx 现有的 handleVoiceTranscription（追加 + 聚焦）落正式句子。
 * - finished / failed / cancel / 启动失败 → onDraftChange(null) 清草稿。
 * - Escape（录音中）= 取消：立即停麦断连，不追加后续文本（已落地的句子保留）。
 * - maxSeconds 到点自动 finish（防忘关）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { VoiceStreamClient } from './client'
import { startPcmCapture, type PcmStream } from './pcmCapture'
import type { VoiceStreamServerEvent } from './types'

export type VoiceStreamPhase = 'idle' | 'connecting' | 'recording' | 'finishing'

export interface UseVoiceStreamOptions {
  /** 一句最终结果回调（追加进输入框）。 */
  onTranscription: (text: string) => void
  /** 错误回调（toast 用）。 */
  onError?: (message: string) => void
  /** 录音上限秒数（来自 /voice/stream/config，默认 600）。 */
  maxSeconds?: number
  /**
   * 草稿回调：partial 实时进输入框用。
   * string = 把输入框末尾的旧草稿替换为该文本；null = 撤掉草稿（final 落框前 / 会话结束）。
   */
  onDraftChange?: (draft: string | null) => void
}

export interface VoiceStreamController {
  phase: VoiceStreamPhase
  /** 当前句中间结果（实时预览）。 */
  partial: string
  /** 实时电平 0..1（波形动画）。 */
  level: number
  /** 开始/停止切换（按钮 + Ctrl+Shift+M 共用）。 */
  toggle: () => void
  /** 取消（Escape）：停止且不追加后续文本。 */
  cancel: () => void
}

export function useVoiceStream(opts: UseVoiceStreamOptions): VoiceStreamController {
  const { onTranscription, onError, maxSeconds = 600, onDraftChange } = opts

  const [phase, setPhase] = useState<VoiceStreamPhase>('idle')
  const [partial, setPartial] = useState('')
  const [level, setLevel] = useState(0)

  // refs：闭包/异步竞态下用同步语义
  const phaseRef = useRef<VoiceStreamPhase>('idle')
  const clientRef = useRef<VoiceStreamClient | null>(null)
  const pcmRef = useRef<PcmStream | null>(null)
  const onTranscriptionRef = useRef(onTranscription)
  const onErrorRef = useRef(onError)
  const onDraftChangeRef = useRef(onDraftChange)
  const autoStopTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  onTranscriptionRef.current = onTranscription
  onErrorRef.current = onError
  onDraftChangeRef.current = onDraftChange

  const _setPhase = (p: VoiceStreamPhase) => {
    phaseRef.current = p
    setPhase(p)
  }

  const _cleanupTimer = () => {
    if (autoStopTimerRef.current) {
      clearTimeout(autoStopTimerRef.current)
      autoStopTimerRef.current = null
    }
  }

  const _handleEvent = useCallback((ev: VoiceStreamServerEvent) => {
    switch (ev.type) {
      case 'started':
        // 麦克风先开（ getUserMedia 已在 startSession 里完成），正式进入录音态
        break
      case 'partial':
        setPartial(ev.text)
        onDraftChangeRef.current?.(ev.text)
        break
      case 'final':
        // 先撤草稿再落正式句（顺序不能反，否则正式句会拼在草稿后面）
        setPartial('')
        onDraftChangeRef.current?.(null)
        onTranscriptionRef.current(ev.text)
        break
      case 'finished':
        setPartial('')
        onDraftChangeRef.current?.(null)
        _setPhase('idle')
        break
      case 'failed':
        setPartial('')
        onDraftChangeRef.current?.(null)
        _setPhase('idle')
        onErrorRef.current?.(`流式语音失败（${ev.code}）：${ev.message}`)
        break
    }
  }, [])

  const _teardown = useCallback(async () => {
    _cleanupTimer()
    const pcm = pcmRef.current
    pcmRef.current = null
    if (pcm) {
      await pcm.stop().catch(() => undefined)
    }
    const client = clientRef.current
    clientRef.current = null
    if (client) client.close()
    setLevel(0)
  }, [])

  const startSession = useCallback(async () => {
    _setPhase('connecting')
    setPartial('')

    const client = new VoiceStreamClient({
      onEvent: _handleEvent,
      onError: (msg) => {
        if (phaseRef.current !== 'idle') {
          _setPhase('idle')
          onDraftChangeRef.current?.(null)
          onErrorRef.current?.(msg)
        }
      },
    })
    clientRef.current = client

    try {
      // 1) 先开麦（权限拒绝在这里抛）
      pcmRef.current = null
      const clientInternal = client
      let started = false
      const pcm = await startPcmCapture((chunk) => {
        // 音频帧直接进 WS；服务端在 task-started 前会缓存
        clientInternal.sendAudio(chunk.pcm)
        setLevel(chunk.level)
      })
      pcmRef.current = pcm

      // 2) 连 WS + start
      await clientInternal.connect()
      clientInternal.sendStart()
      started = true

      _setPhase('recording')

      // 3) 上限自动停
      _cleanupTimer()
      autoStopTimerRef.current = setTimeout(() => {
        if (phaseRef.current === 'recording') {
          void stopSession()
        }
      }, maxSeconds * 1000)
      void started
    } catch (e) {
      const msg =
        e instanceof Error
          ? e.name === 'NotAllowedError'
            ? '已拒绝麦克风权限，请在浏览器/系统设置里允许后重试'
            : e.message
          : String(e)
      await _teardown()
      _setPhase('idle')
      onDraftChangeRef.current?.(null)
      onErrorRef.current?.(`无法启动流式录音：${msg}`)
    }
  }, [_handleEvent, _teardown, maxSeconds])

  const stopSession = useCallback(async () => {
    // 停麦 → finish → 等 finished 事件回 idle
    _cleanupTimer()
    const pcm = pcmRef.current
    if (pcm) {
      await pcm.stop().catch(() => undefined)
      pcmRef.current = null
    }
    if (phaseRef.current !== 'idle') _setPhase('finishing')
    const client = clientRef.current
    if (client) client.sendFinish()
    setLevel(0)
    // finished/failed 事件会把 phase 拉回 idle；
    // 兜底：若 10s 内没回（连接已断），onError close 回调会处理
  }, [])

  const cancelSession = useCallback(async () => {
    const client = clientRef.current
    if (client) client.sendCancel()
    await _teardown()
    setPartial('')
    onDraftChangeRef.current?.(null)
    _setPhase('idle')
  }, [_teardown])

  const toggle = useCallback(() => {
    const p = phaseRef.current
    if (p === 'recording') {
      void stopSession()
    } else if (p === 'idle') {
      void startSession()
    }
    // connecting / finising 中忽略（防抖）
  }, [startSession, stopSession])

  // Escape 取消（capture 阶段截获，避免触发 Chat 层的 Escape 逻辑）
  useEffect(() => {
    if (phase !== 'recording') return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        void cancelSession()
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('keydown', onKey, true)
    }
  }, [phase, cancelSession])

  // 卸载兜底
  useEffect(
    () => () => {
      void _teardown()
    },
    [_teardown],
  )

  return { phase, partial, level, toggle, cancel: cancelSession }
}
