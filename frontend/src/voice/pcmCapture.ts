/**
 * 麦克风 PCM 采集：AudioContext（设备原生采样率）→ 线性插值重采样到 16k 单声道 PCM16，
 * 每 100ms 回调一帧（1600 样本 / 3200 字节），附 RMS 电平（0..1，驱动波形动画）。
 *
 * 用 Blob URL 内联 AudioWorklet（不落盘、不受构建管线影响，删除本模块无残留文件）。
 * 兼容 WebView2 / Chrome / Edge / Safari 14.1+。
 */

export interface PcmChunk {
  /** 16k 单声道 little-endian PCM16，一帧 100ms。 */
  pcm: Int16Array
  /** 本帧 RMS 电平（0..1）。 */
  level: number
}

export interface PcmStream {
  /** 每帧回调（100ms 一次）。 */
  onChunk: (chunk: PcmChunk) => void
  /** 停止采集并释放麦克风 + AudioContext（先发送不足 100ms 的尾帧）。 */
  stop: () => Promise<void>
}

const TARGET_RATE = 16000
const CHUNK_SAMPLES = TARGET_RATE / 10 // 100ms

const WORKLET_SRC = `
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this._ratio = sampleRate / ${TARGET_RATE}
    this._phase = 0      // 重采样游标（相对本帧 buf[0]，取值 [0, ratio)）
    this._prevTail = 0   // 上一帧最后一个样本（跨帧插值用）
    this._out = []       // 待打包的 16k 输出样本
    this._stopped = false
    this.port.onmessage = ({ data }) => {
      if (data.type !== 'stop') return
      this._stopped = true
      this._flush(true)
      this.port.postMessage({ type: 'stopped' })
    }
  }

  process(inputs) {
    if (this._stopped) return false
    const input = inputs[0]
    if (!input || input.length === 0 || !input[0]) return true
    const ch = input[0]
    if (ch.length === 0) return true

    // buf = [上帧尾样本, ...本帧样本]，线性插值可跨帧边界
    const buf = new Float32Array(ch.length + 1)
    buf[0] = this._prevTail
    for (let i = 0; i < ch.length; i++) buf[i + 1] = ch[i]
    this._prevTail = ch[ch.length - 1]

    const last = buf.length - 1
    let t = this._phase
    while (t < last) {
      const i = Math.floor(t)
      const frac = t - i
      const s = buf[i] + (buf[i + 1] - buf[i]) * frac
      this._emit(s)
      t += this._ratio
    }
    // 游标折到下帧坐标：下帧 buf'[0] == 本帧 buf[last]
    this._phase = t - last

    this._flush()
    return true
  }

  _emit(s) {
    this._out.push(s)
  }

  _flush(final = false) {
    const count = final ? this._out.length : ${CHUNK_SAMPLES}
    if (!count || this._out.length < count) return
    let sumSq = 0
    const pcm = new Int16Array(count)
    for (let i = 0; i < count; i++) {
      let s = this._out[i]
      if (s > 1) s = 1; else if (s < -1) s = -1
      sumSq += s * s
      pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff
    }
    this._out = this._out.slice(count)
    const rms = Math.sqrt(sumSq / count)
    this.port.postMessage({ pcm: pcm, level: rms }, [pcm.buffer])
  }
}
registerProcessor('voice-pcm-capture', PcmCaptureProcessor)
`

let _workletUrl: string | null = null
let _workletLoadedCtxs = new WeakSet<AudioContext>()

async function ensureWorklet(ctx: AudioContext): Promise<void> {
  if (_workletLoadedCtxs.has(ctx)) return
  if (!_workletUrl) {
    _workletUrl = URL.createObjectURL(
      new Blob([WORKLET_SRC], { type: 'application/javascript' }),
    )
  }
  await ctx.audioWorklet.addModule(_workletUrl)
  _workletLoadedCtxs.add(ctx)
}

/** 打开麦克风并开始 PCM 采集。拒绝权限时抛 NotAllowedError（调用方提示用户）。 */
export async function startPcmCapture(
  onChunk: (chunk: PcmChunk) => void,
): Promise<PcmStream> {
  const media = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  })

  let ctx: AudioContext | undefined
  let source: MediaStreamAudioSourceNode | undefined
  let node: AudioWorkletNode | undefined
  const cleanup = async () => {
    const errors: unknown[] = []
    for (const track of media.getTracks()) {
      try { track.stop() } catch (error) { errors.push(error) }
    }
    if (node) node.port.onmessage = null
    try { node?.disconnect() } catch (error) { errors.push(error) }
    try { source?.disconnect() } catch (error) { errors.push(error) }
    try {
      if (ctx && ctx.state !== 'closed') await ctx.close()
    } catch (error) { errors.push(error) }
    if (errors.length) throw errors[0]
  }

  try {
    ctx = new AudioContext()
    await ensureWorklet(ctx)
    await ctx.resume()
    source = ctx.createMediaStreamSource(media)
    const captureNode = new AudioWorkletNode(ctx, 'voice-pcm-capture')
    node = captureNode
    let acknowledgeStop: (() => void) | undefined
    captureNode.port.onmessage = (ev: MessageEvent<PcmChunk | { type: 'stopped' }>) => {
      if ('type' in ev.data) acknowledgeStop?.()
      else onChunk(ev.data)
    }
    source.connect(captureNode)

    let stopPromise: Promise<void> | undefined
    const stop = (): Promise<void> => {
      if (stopPromise) return stopPromise
      stopPromise = (async () => {
        try {
          await new Promise<void>((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('语音尾帧收取超时')), 1000)
            acknowledgeStop = () => {
              clearTimeout(timer)
              resolve()
            }
            try {
              captureNode.port.postMessage({ type: 'stop' })
            } catch (error) {
              clearTimeout(timer)
              reject(error)
            }
          })
        } finally {
          await cleanup()
        }
      })()
      return stopPromise
    }
    return { onChunk, stop }
  } catch (error) {
    await cleanup().catch((cleanupError) => console.warn('voice capture cleanup failed', cleanupError))
    throw error
  }
}
