/**
 * VoiceStreamClient：与后端 /voice/stream/ws 的 WebSocket 客户端。
 * 协议见 backend/voice_stream/service.py 头注释（浏览器侧）。
 *
 * 生命周期：connect → sendStart → 等 started → sendAudio* → sendFinish →
 * 等 finished/failed → close。中途 close = 取消。
 */
import type { VoiceStreamClientControl, VoiceStreamServerEvent } from './types'
import { VOICE_STREAM_WS_URL } from './types'

export interface VoiceStreamClientCallbacks {
  onEvent: (ev: VoiceStreamServerEvent) => void
  /** 连接层错误（握手失败等），语义上等价 failed 事件。 */
  onError?: (message: string) => void
}

export class VoiceStreamClient {
  private _ws: WebSocket | null = null
  private _closed = false
  private _rejectConnect: ((error: Error) => void) | null = null

  constructor(
    private readonly callbacks: VoiceStreamClientCallbacks,
    private readonly url: string = VOICE_STREAM_WS_URL,
  ) {}

  /** 建立连接。resolve = ws.onopen；reject = 握手失败。 */
  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      if (this._closed) {
        reject(new Error('client already closed'))
        return
      }
      const ws = new WebSocket(this.url)
      ws.binaryType = 'arraybuffer'
      this._ws = ws

      const timer = setTimeout(() => {
        this.close()
      }, 10000)
      this._rejectConnect = (error) => {
        clearTimeout(timer)
        reject(error)
      }
      const onOpen = () => {
        clearTimeout(timer)
        this._rejectConnect = null
        ws.removeEventListener('error', onError)
        ws.removeEventListener('open', onOpen)
        resolve()
      }
      const onError = () => {
        ws.removeEventListener('error', onError)
        ws.removeEventListener('open', onOpen)
        this.close()
      }
      ws.addEventListener('open', onOpen, { once: false })
      ws.addEventListener('error', onError, { once: false })
      ws.addEventListener('message', this._onMessage)
      ws.addEventListener('close', () => {
        if (!this._closed) {
          this.close()
          this.callbacks.onError?.('voice stream connection closed')
        }
      })
    })
  }

  private _onMessage = (ev: MessageEvent) => {
    if (typeof ev.data !== 'string') return // 服务器只回 JSON
    try {
      const parsed = JSON.parse(ev.data) as VoiceStreamServerEvent
      if (parsed && typeof parsed.type === 'string') {
        this.callbacks.onEvent(parsed)
      }
    } catch {
      /* 非 JSON 忽略 */
    }
  }

  private _sendControl(control: VoiceStreamClientControl) {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._ws.send(JSON.stringify(control))
    }
  }

  sendStart() {
    this._sendControl({ type: 'start' })
  }

  sendAudio(pcm: Int16Array | ArrayBuffer) {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      if (this._ws.bufferedAmount > 1024 * 1024) {
        this.close()
        this.callbacks.onError?.('语音发送积压，已停止录音，请检查网络后重试')
        return
      }
      this._ws.send(pcm as BufferSource)
    }
  }

  sendFinish() {
    this._sendControl({ type: 'finish' })
  }

  sendCancel() {
    this._sendControl({ type: 'cancel' })
  }

  close() {
    this._closed = true
    this._rejectConnect?.(new Error('语音连接已关闭或连接超时'))
    this._rejectConnect = null
    const ws = this._ws
    this._ws = null
    if (ws) {
      ws.removeEventListener('message', this._onMessage)
      try {
        ws.close()
      } catch {
        /* ignore */
      }
    }
  }
}
