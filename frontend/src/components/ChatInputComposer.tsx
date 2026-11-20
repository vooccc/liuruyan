import { useEffect, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent, type ReactNode, type RefObject } from 'react'

export function hasDraggedFiles(dt: DataTransfer | null | undefined): boolean {
  if (!dt) return false
  if (dt.files && dt.files.length > 0) return true
  if (dt.types && Array.from(dt.types).includes('Files')) return true
  if (dt.items) {
    for (let i = 0; i < dt.items.length; i++) {
      if (dt.items[i].kind === 'file') return true
    }
  }
  return false
}

export function extractDraggedFiles(dt: DataTransfer | null | undefined): File[] {
  if (!dt) return []
  if (dt.files && dt.files.length > 0) return Array.from(dt.files)
  const files: File[] = []
  if (dt.items) {
    for (let i = 0; i < dt.items.length; i++) {
      const item = dt.items[i]
      if (item.kind === 'file') {
        const f = item.getAsFile()
        if (f) files.push(f)
      }
    }
  }
  return files
}

interface ChatInputComposerProps {
  commandsPanel?: ReactNode
  topToolbar: ReactNode
  inputRef: RefObject<HTMLTextAreaElement>
  input: string
  onInputChange: (value: string) => void
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void
  placeholder: string
  bottomToolbar: ReactNode
  guardianDialog?: ReactNode
  /** InlineTasksPanel Portal 锚点（LLM 闲态形态）。常驻 DOM。 */
  tasksAnchorSlot?: ReactNode
  /** 附件 chip 列表（用户从 📎 上传的文件状态）。渲染在 topToolbar 和 textarea 之间。 */
  attachmentsSlot?: ReactNode
  /** 粘贴文件回调（剪贴板里有截图/文件时触发）。与 📎 走同一条 handleChooseFiles。 */
  onPasteFiles?: (files: File[]) => void
  /** 拖拽文件进输入框回调（支持音频/图片/文档等任意文件）。与 📎 走同一条 handleChooseFiles。 */
  onDropFiles?: (files: File[]) => void
  /** 工作区与 Pin 上下文药丸栏（与输入框紧密对齐） */
  pinnedBarSlot?: ReactNode
}

export default function ChatInputComposer({
  commandsPanel,
  topToolbar,
  inputRef,
  input,
  onInputChange,
  onKeyDown,
  placeholder,
  bottomToolbar,
  guardianDialog,
  tasksAnchorSlot,
  attachmentsSlot,
  onPasteFiles,
  onDropFiles,
  pinnedBarSlot,
}: ChatInputComposerProps) {
  const [isDragOver, setIsDragOver] = useState(false)
  const dragDepthRef = useRef(0)

  useEffect(() => {
    if (!isDragOver) return undefined
    const resetDragState = () => {
      dragDepthRef.current = 0
      setIsDragOver(false)
    }
    window.addEventListener('dragend', resetDragState)
    window.addEventListener('drop', resetDragState)
    return () => {
      window.removeEventListener('dragend', resetDragState)
      window.removeEventListener('drop', resetDragState)
    }
  }, [isDragOver])

  // 剪贴板里若有 file（截图复制 / 资源管理器复制文件），不打断文本粘贴。
  // 浏览器把图片/文件项放在 clipboardData.items 里 kind='file'；纯文本粘贴不含 file 项。
  const handlePaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    if (!onPasteFiles) return
    const items = event.clipboardData?.items
    if (!items || items.length === 0) return
    const files: File[] = []
    for (let i = 0; i < items.length; i++) {
      const item = items[i]
      if (item.kind === 'file') {
        const f = item.getAsFile()
        if (f) files.push(f)
      }
    }
    if (files.length === 0) return
    // 阻止 textarea 把图片的 dataURL / 文件路径作为 text 塞进来。
    event.preventDefault()
    onPasteFiles(files)
  }

  const handleDragEnter = (event: DragEvent<HTMLDivElement>) => {
    if (!onDropFiles || !hasDraggedFiles(event.dataTransfer)) return
    event.preventDefault()
    event.stopPropagation()
    dragDepthRef.current += 1
    setIsDragOver(true)
  }

  const handleDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (!onDropFiles || !hasDraggedFiles(event.dataTransfer)) return
    event.preventDefault()
    event.stopPropagation()
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'copy'
    }
    if (!isDragOver) setIsDragOver(true)
  }

  const handleDragLeave = (event: DragEvent<HTMLDivElement>) => {
    if (!onDropFiles) return
    if (dragDepthRef.current > 0) dragDepthRef.current -= 1
    const related = event.relatedTarget as Node | null
    if (dragDepthRef.current <= 0 || (related && !event.currentTarget.contains(related))) {
      dragDepthRef.current = 0
      setIsDragOver(false)
    }
  }

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    dragDepthRef.current = 0
    setIsDragOver(false)
    if (!onDropFiles || !hasDraggedFiles(event.dataTransfer)) return
    // 阻止 Chromium/WebView2 把文件名或路径当文本插进 textarea，或触发整页 file:// 跳转
    event.preventDefault()
    event.stopPropagation()
    const files = extractDraggedFiles(event.dataTransfer)
    if (files.length > 0) {
      onDropFiles(files)
    }
  }

  return (
    <div className="input-area">
      {tasksAnchorSlot}
      {commandsPanel}
      {pinnedBarSlot}

      <div
        className={`input-bar-modern${isDragOver ? ' is-drag-over' : ''}`}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        {topToolbar}
        {attachmentsSlot}

        <form className="input-form-modern" onSubmit={(event) => event.preventDefault()}>
          <textarea
            ref={inputRef}
            value={input}
            onChange={(event) => onInputChange(event.target.value)}
            onKeyDown={onKeyDown}
            onPaste={handlePaste}
            placeholder={placeholder}
            rows={1}
            className="modern-textarea"
          />
        </form>

        {bottomToolbar}
      </div>

      {guardianDialog}

    </div>
  )
}
