import type { ClipboardEvent, KeyboardEvent, ReactNode, RefObject } from 'react'

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
  pinnedBarSlot,
}: ChatInputComposerProps) {
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

  return (
    <div className="input-area">
      {tasksAnchorSlot}
      {commandsPanel}
      {pinnedBarSlot}

      <div className="input-bar-modern">
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
