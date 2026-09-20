/**
 * TTS 自动朗读全局播放器（M2）。
 *
 * 设计目标
 * ========
 * - **跨会话单例**：整个 app 只有一个 audio 实例 + 一条队列，避免多条消息同时朗读冲突。
 * - **队列串行播放**：assistant 消息完成时把音频 Blob 推队列，按 FIFO 依次播。
 * - **切会话立刻停**：`stop()` 清队列 + 暂停当前播放。
 * - **零依赖**：不引用任何 chat / message / store 模块——只接收 Blob 播放。
 *
 * 不在这一层做的事
 * ================
 * - 文本提取：调用方传 Blob 进来，本模块不知道"原文"是什么。
 * - mode 过滤：后端 should_speak 会过滤；前端 synthesizeText 返 null 时调用方就不该
 *   往这里 enqueue。
 * - 设置：用户能不能听到，是后端配置（tts.json mode）+ 角色 override 决定的，本层透明。
 *
 * 浏览器自动播放策略
 * ==================
 * 用户主动发消息后才触发朗读（assistant 消息完成 → enqueue），属于"用户已交互"的
 * 上下文，浏览器一般允许 audio.play()。极端情况（页面被动接收 push 流且用户从未交互）
 * play() 可能被拒，这时只 console.warn 不抛错。
 *
 * 设计文档：``_docs/handover_tts_design_2026-05-29.md`` §M2
 */

type PlayerState = 'idle' | 'loading' | 'playing'

type Listener = (state: PlayerState) => void

class TTSAutoPlayer {
  private audio: HTMLAudioElement | null = null
  private queue: Array<{ blob: Blob; objectUrl: string }> = []
  private currentObjectUrl: string | null = null
  private state: PlayerState = 'idle'
  private listeners: Set<Listener> = new Set()

  /** 把音频 Blob 推入队列。如果 idle 立即开播；playing 则等当前播完再播。 */
  enqueue(blob: Blob): void {
    const objectUrl = URL.createObjectURL(blob)
    this.queue.push({ blob, objectUrl })
    if (this.state === 'idle') {
      void this._playNext()
    }
  }

  /** 立即停止：清队列 + 暂停当前 + 释放 ObjectURL。切会话/用户主动停时调。 */
  stop(): void {
    // 释放队列里所有 ObjectURL（防内存泄漏）
    for (const item of this.queue) {
      try {
        URL.revokeObjectURL(item.objectUrl)
      } catch {}
    }
    this.queue = []
    if (this.audio) {
      try {
        this.audio.pause()
        this.audio.currentTime = 0
      } catch {}
    }
    if (this.currentObjectUrl) {
      try {
        URL.revokeObjectURL(this.currentObjectUrl)
      } catch {}
      this.currentObjectUrl = null
    }
    this._setState('idle')
  }

  /** 当前是否在播 / 队列非空。 */
  getState(): PlayerState {
    return this.state
  }

  /** 注册状态变化监听。返回 unsubscribe。 */
  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    listener(this.state) // 立即推一次当前状态
    return () => {
      this.listeners.delete(listener)
    }
  }

  // ── 私有 ────────────────────────────────────────────────────

  private _ensureAudio(): HTMLAudioElement {
    if (this.audio) return this.audio
    const audio = new Audio()
    audio.preload = 'auto'
    audio.addEventListener('ended', () => {
      // 当前段播完，释放 url 后播下一段
      if (this.currentObjectUrl) {
        try {
          URL.revokeObjectURL(this.currentObjectUrl)
        } catch {}
        this.currentObjectUrl = null
      }
      void this._playNext()
    })
    audio.addEventListener('error', (e) => {
      console.warn('[TTSAutoPlayer] audio error', e)
      // 出错不让队列卡死——继续下一段
      if (this.currentObjectUrl) {
        try {
          URL.revokeObjectURL(this.currentObjectUrl)
        } catch {}
        this.currentObjectUrl = null
      }
      void this._playNext()
    })
    this.audio = audio
    return audio
  }

  private async _playNext(): Promise<void> {
    const next = this.queue.shift()
    if (!next) {
      this._setState('idle')
      return
    }
    this._setState('playing')
    this.currentObjectUrl = next.objectUrl
    const audio = this._ensureAudio()
    audio.src = next.objectUrl
    try {
      await audio.play()
    } catch (e) {
      // play() 被浏览器拒绝（autoplay policy）；不抛错，跳过这段
      console.warn('[TTSAutoPlayer] play() rejected:', e)
      if (this.currentObjectUrl) {
        try {
          URL.revokeObjectURL(this.currentObjectUrl)
        } catch {}
        this.currentObjectUrl = null
      }
      // 继续下一段
      void this._playNext()
    }
  }

  private _setState(next: PlayerState): void {
    if (this.state === next) return
    this.state = next
    for (const listener of this.listeners) {
      try {
        listener(next)
      } catch (e) {
        console.warn('[TTSAutoPlayer] listener error:', e)
      }
    }
  }
}

/** 全局单例。所有调用方共用一个队列。 */
export const ttsAutoPlayer = new TTSAutoPlayer()
