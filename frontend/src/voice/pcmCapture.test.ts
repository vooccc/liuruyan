import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { startPcmCapture } from './pcmCapture'

let workletSource = ''
let port: { onmessage: ((event: { data: unknown }) => void) | null; postMessage: ReturnType<typeof vi.fn> }
let stopTrack: ReturnType<typeof vi.fn>
let closeContext: ReturnType<typeof vi.fn>
let failNode = false
let disconnectNode: ReturnType<typeof vi.fn>
let disconnectSource: ReturnType<typeof vi.fn>

beforeEach(() => {
  stopTrack = vi.fn()
  closeContext = vi.fn().mockResolvedValue(undefined)
  failNode = false
  disconnectNode = vi.fn()
  disconnectSource = vi.fn()
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: stopTrack }] }) } })
  vi.stubGlobal('URL', { createObjectURL: () => 'blob:worklet' })
  vi.stubGlobal('Blob', class { constructor(parts: string[]) { workletSource = parts.join('') } })
  vi.stubGlobal('AudioContext', class {
    state = 'running'
    audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) }
    resume = vi.fn().mockResolvedValue(undefined)
    close = closeContext
    createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: disconnectSource })
  })
  vi.stubGlobal('AudioWorkletNode', class {
    port: typeof port
    disconnect = disconnectNode
    constructor() {
      if (failNode) throw new Error('node failed')
      port = { onmessage: null, postMessage: vi.fn(() => {
        port.onmessage?.({ data: { pcm: new Int16Array([7]), level: 0.1 } })
        port.onmessage?.({ data: { type: 'stopped' } })
      }) }
      this.port = port
    }
  })
})

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

describe('PCM capture lifecycle', () => {
  it('releases remaining resources when node disconnection fails', async () => {
    const stream = await startPcmCapture(vi.fn())
    disconnectNode.mockImplementation(() => { throw new Error('disconnect failed') })
    await expect(stream.stop()).rejects.toThrow('disconnect failed')
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(disconnectSource).toHaveBeenCalledTimes(1)
    expect(closeContext).toHaveBeenCalledTimes(1)
  })

  it('releases audio nodes and context when stopping a track fails', async () => {
    const stream = await startPcmCapture(vi.fn())
    stopTrack.mockImplementation(() => { throw new Error('track failed') })
    await expect(stream.stop()).rejects.toThrow('track failed')
    expect(disconnectNode).toHaveBeenCalledTimes(1)
    expect(disconnectSource).toHaveBeenCalledTimes(1)
    expect(closeContext).toHaveBeenCalledTimes(1)
  })

  it('delivers the tail before cleanup and shares repeated stop calls', async () => {
    const chunks: number[] = []
    const stream = await startPcmCapture(({ pcm }) => {
      expect(stopTrack).not.toHaveBeenCalled()
      chunks.push(pcm[0])
    })
    const stopping = stream.stop()
    expect(stream.stop()).toBe(stopping)
    await stopping
    expect(chunks).toEqual([7])
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(closeContext).toHaveBeenCalledTimes(1)
  })

  it('releases the microphone when node initialization fails', async () => {
    failNode = true
    await expect(startPcmCapture(vi.fn())).rejects.toThrow('node failed')
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(closeContext).toHaveBeenCalledTimes(1)
  })

  it('cleans up and reports an unresponsive worklet', async () => {
    vi.useFakeTimers()
    const stream = await startPcmCapture(vi.fn())
    port.postMessage.mockImplementation(() => undefined)
    const assertion = expect(stream.stop()).rejects.toThrow('语音尾帧收取超时')
    await vi.advanceTimersByTimeAsync(1000)
    await assertion
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(closeContext).toHaveBeenCalledTimes(1)
  })

  it('flushes a short processor frame before acknowledging stop', async () => {
    const stream = await startPcmCapture(vi.fn())
    const messages: Array<{ pcm?: Int16Array; type?: string }> = []
    let Processor: any
    class Base { port = { postMessage: (data: object) => messages.push(data), onmessage: null } }
    new Function('AudioWorkletProcessor', 'sampleRate', 'registerProcessor', workletSource)(
      Base, 16000, (_name: string, processor: unknown) => { Processor = processor },
    )
    const processor = new Processor()
    processor.process([[new Float32Array(128).fill(0.5)]])
    expect(messages).toHaveLength(0)
    processor.port.onmessage({ data: { type: 'stop' } })
    expect(messages[0].pcm).toHaveLength(128)
    expect(messages[1]).toEqual({ type: 'stopped' })
    expect(processor.process([])).toBe(false)
    await stream.stop()
  })
})
