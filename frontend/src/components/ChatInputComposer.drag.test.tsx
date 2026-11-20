// @vitest-environment jsdom
import { act, createRef } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ChatInputComposer, { extractDraggedFiles, hasDraggedFiles } from './ChatInputComposer'
import {
  buildUserMessageWithAttachments,
  isAudioAttachmentPath,
  parseUserMessageAttachments,
} from './userAttachmentFooter'

vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)

let dispose: (() => void) | undefined
afterEach(() => {
  dispose?.()
  dispose = undefined
  vi.clearAllMocks()
})

function createMockDragEvent(type: string, dataTransfer: Partial<DataTransfer>): Event {
  const ev = new Event(type, { bubbles: true, cancelable: true }) as Event & {
    dataTransfer: Partial<DataTransfer>
  }
  ev.dataTransfer = dataTransfer
  return ev
}

describe('ChatInputComposer drag-and-drop & audio attachments', () => {
  it('detects and extracts dragged files without intercepting plain text drags', () => {
    const audioFile = new File(['abc'], 'meeting.mp3', { type: 'audio/mpeg' })
    const fileDt = {
      types: ['Files'],
      files: [audioFile] as unknown as FileList,
    } as unknown as DataTransfer
    expect(hasDraggedFiles(fileDt)).toBe(true)
    expect(extractDraggedFiles(fileDt)).toEqual([audioFile])

    const textDt = {
      types: ['text/plain'],
      files: [] as unknown as FileList,
    } as unknown as DataTransfer
    expect(hasDraggedFiles(textDt)).toBe(false)
    expect(extractDraggedFiles(textDt)).toEqual([])
  })

  it('accepts dropped audio file on input box, cancels default text insertion, and clears highlight', async () => {
    const onDropFiles = vi.fn()
    const inputRef = createRef<HTMLTextAreaElement>()
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)

    await act(async () => {
      root.render(
        <ChatInputComposer
          topToolbar={<div />}
          inputRef={inputRef}
          input=""
          onInputChange={vi.fn()}
          onKeyDown={vi.fn()}
          placeholder="输入..."
          bottomToolbar={<div />}
          onDropFiles={onDropFiles}
        />,
      )
    })
    dispose = () => {
      act(() => root.unmount())
      container.remove()
    }

    const bar = container.querySelector('.input-bar-modern') as HTMLDivElement
    const textarea = container.querySelector('textarea') as HTMLTextAreaElement
    expect(bar).toBeTruthy()
    expect(textarea).toBeTruthy()

    const audioFile = new File(['RIFF'], 'voice_memo.wav', { type: 'audio/wav' })
    const dragOverDt: Partial<DataTransfer> = {
      types: ['Files'],
      files: [] as unknown as FileList,
      dropEffect: 'none',
    }
    const dragOverEv = createMockDragEvent('dragover', dragOverDt)
    act(() => {
      textarea.dispatchEvent(dragOverEv)
    })
    expect(dragOverEv.defaultPrevented).toBe(true)
    expect(dragOverDt.dropEffect).toBe('copy')
    expect(bar.classList.contains('is-drag-over')).toBe(true)

    const dropEv = createMockDragEvent('drop', {
      types: ['Files'],
      files: [audioFile] as unknown as FileList,
    })
    act(() => {
      textarea.dispatchEvent(dropEv)
    })
    expect(dropEv.defaultPrevented).toBe(true)
    expect(onDropFiles).toHaveBeenCalledTimes(1)
    expect(onDropFiles).toHaveBeenCalledWith([audioFile])
    expect(bar.classList.contains('is-drag-over')).toBe(false)
  })

  it('builds and parses audio-only and mixed attachment footers cleanly', () => {
    expect(isAudioAttachmentPath('C:\\uploads\\123_demo.m4a')).toBe(true)
    expect(isAudioAttachmentPath('C:\\uploads\\123_pic.png')).toBe(false)

    const audioOnlyMsg = buildUserMessageWithAttachments('', ['C:\\uploads\\123_demo.mp3'])
    expect(audioOnlyMsg).toContain('[附件 / 用户上传，请用 read_file 工具读取]')
    const parsedAudio = parseUserMessageAttachments(audioOnlyMsg)
    expect(parsedAudio.cleanedText).toBe('')
    expect(parsedAudio.attachments).toEqual([
      {
        path: 'C:\\uploads\\123_demo.mp3',
        name: '123_demo.mp3',
        isImage: false,
        isAudio: true,
      },
    ])

    // 历史消息中旧格式 [附件 / 用户上传音频，请用 transcribe_audio 工具转写] 仍需兼容解析
    const legacyAudioMsg =
      '整理下录音\n\n[附件 / 用户上传音频，请用 transcribe_audio 工具转写]\n- D:/old.mp3'
    const parsedLegacyAudio = parseUserMessageAttachments(legacyAudioMsg)
    expect(parsedLegacyAudio.cleanedText).toBe('整理下录音')
    expect(parsedLegacyAudio.attachments).toEqual([
      { path: 'D:/old.mp3', name: 'old.mp3', isImage: false, isAudio: true },
    ])

    const mixedBackendMsg =
      '帮我整理一下\n\n[附件 / 用户上传，请用 read_file 工具读取]\n- D:/a.wav\n- D:/b.png'
    const parsedMixed = parseUserMessageAttachments(mixedBackendMsg)
    expect(parsedMixed.cleanedText).toBe('帮我整理一下')
    expect(parsedMixed.attachments).toEqual([
      { path: 'D:/a.wav', name: 'a.wav', isImage: false, isAudio: true },
      { path: 'D:/b.png', name: 'b.png', isImage: true, isAudio: false },
    ])
  })
})
