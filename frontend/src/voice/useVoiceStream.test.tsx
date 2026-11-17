// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useVoiceStream, type VoiceStreamController } from './useVoiceStream'
import type { VoiceStreamServerEvent } from './types'
import { startPcmCapture } from './pcmCapture'

const mocks = vi.hoisted(() => ({ clients: [] as any[], stop: vi.fn().mockResolvedValue(undefined) }))
vi.mock('./client', () => ({ VoiceStreamClient: class {
  callbacks: any
  constructor(callbacks: any) { this.callbacks = callbacks; mocks.clients.push(this) }
  connect = vi.fn().mockResolvedValue(undefined)
  sendStart = vi.fn()
  sendAudio = vi.fn()
  sendFinish = vi.fn()
  close = vi.fn()
} }))
vi.mock('./pcmCapture', () => ({ startPcmCapture: vi.fn(async () => ({ stop: mocks.stop })) }))

vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
let dispose: (() => void) | undefined
afterEach(() => { dispose?.(); dispose = undefined; mocks.clients.length = 0; vi.clearAllMocks(); vi.useRealTimers() })

async function mount() {
  const onDraftChange = vi.fn()
  const onTranscription = vi.fn()
  const onPolished = vi.fn()
  let controller!: VoiceStreamController
  const container = document.createElement('div')
  const root = createRoot(container)
  function Harness() {
    controller = useVoiceStream({ onDraftChange, onTranscription, onPolished })
    return null
  }
  await act(async () => root.render(<Harness />))
  dispose = () => { act(() => root.unmount()) }
  const emit = (event: VoiceStreamServerEvent, client = mocks.clients.at(-1)) => {
    act(() => client.callbacks.onEvent(event))
  }
  return { get controller() { return controller }, onDraftChange, onTranscription, onPolished, emit }
}

describe('voice stream callbacks', () => {
  it('preserves the visible draft when the connection fails', async () => {
    const view = await mount()
    await act(async () => view.controller.toggle())
    view.emit({ type: 'partial', text: '尚未定稿的文字' })
    act(() => mocks.clients[0].callbacks.onError('连接断开'))
    expect(view.onDraftChange).toHaveBeenLastCalledWith(null, true)
    expect(view.onTranscription).not.toHaveBeenCalled()
    expect(view.controller.phase).toBe('idle')
  })

  it('preserves the visible draft when the final result times out', async () => {
    vi.useFakeTimers()
    const view = await mount()
    await act(async () => view.controller.toggle())
    view.emit({ type: 'started' })
    view.emit({ type: 'partial', text: '等待中的文字' })
    await act(async () => view.controller.toggle())
    await act(async () => vi.advanceTimersByTimeAsync(35000))
    expect(view.onDraftChange).toHaveBeenLastCalledWith(null, true)
    expect(view.controller.phase).toBe('idle')
  })

  it('releases a microphone granted after cancellation', async () => {
    let grant!: (value: Awaited<ReturnType<typeof startPcmCapture>>) => void
    vi.mocked(startPcmCapture).mockImplementationOnce(() => new Promise((resolve) => { grant = resolve }))
    const view = await mount()
    act(() => view.controller.toggle())
    act(() => view.controller.cancel())
    await act(async () => { grant({ stop: mocks.stop, onChunk: vi.fn() }) })
    expect(mocks.stop).toHaveBeenCalledTimes(1)
    expect(mocks.clients[0].connect).not.toHaveBeenCalled()
    expect(view.controller.phase).toBe('idle')
  })

  it('sends finish only after the service is ready when stopped early', async () => {
    const view = await mount()
    await act(async () => view.controller.toggle())
    await act(async () => view.controller.toggle())
    const client = mocks.clients[0]
    expect(mocks.stop).toHaveBeenCalledTimes(1)
    expect(client.sendFinish).not.toHaveBeenCalled()
    view.emit({ type: 'started' })
    expect(client.sendFinish).toHaveBeenCalledTimes(1)
    view.emit({ type: 'finished', text: '' })
    expect(view.controller.phase).toBe('idle')
  })

  it('keeps sentence finalization separate from session cleanup', async () => {
    const view = await mount()
    await act(async () => view.controller.toggle())
    view.emit({ type: 'started' })
    view.emit({ type: 'partial', text: '草稿' })
    view.emit({ type: 'final', text: '第一句', index: 0 })
    view.emit({ type: 'final', text: '第二句', index: 1 })
    view.emit({ type: 'polished', text: '第一句。', index: 0 })
    expect(view.onDraftChange.mock.calls).toEqual([['草稿']])
    expect(view.onTranscription.mock.calls).toEqual([['第一句', 0], ['第二句', 1]])
    expect(view.onPolished).toHaveBeenCalledWith(0, '第一句。')
    view.emit({ type: 'finished', text: '第一句第二句' })
    expect(view.onDraftChange).toHaveBeenLastCalledWith(null, false)
    expect(view.controller.phase).toBe('idle')
  })

  it('cleans a cancelled session and ignores its late events after restart', async () => {
    const view = await mount()
    await act(async () => view.controller.toggle())
    const old = mocks.clients[0]
    view.emit({ type: 'partial', text: '旧草稿' })
    act(() => view.controller.cancel())
    expect(view.onDraftChange).toHaveBeenLastCalledWith(null, false)
    await act(async () => view.controller.toggle())
    view.emit({ type: 'final', text: '迟到结果', index: 0 }, old)
    view.emit({ type: 'final', text: '新结果', index: 0 })
    expect(view.onTranscription.mock.calls).toEqual([['新结果', 0]])
    expect(old.close).toHaveBeenCalledTimes(1)
  })
})
