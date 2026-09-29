import { useRef, type ChangeEvent } from 'react'
import { Paperclip, Plus, Waypoints } from 'lucide-react'
import type { Agent } from '../api/chat'
import ChatCorridorPanel from './ChatCorridorPanel'
import { CHAT_UI_TEXT } from './chatUiText'

interface ChatInputTopToolbarProps {
  currentAgentId?: string | null
  currentAgentName?: string | null
  agents: Agent[]
  showCorridorPanel: boolean
  corridorTarget: string | null
  corridorContent: string
  corridorSending: boolean
  onToggleCorridorPanel: () => void
  onTargetChange: (target: string | null) => void
  onContentChange: (content: string) => void
  onCancelCorridor: () => void
  onSendCorridor: () => void
  /** 点击 @ 按钮时调起 mention dropdown（在光标位置插 @ 触发） */
  onInsertMention?: () => void
  /** mention dropdown 当前是否已打开（控制按钮高亮） */
  mentionActive?: boolean
  /** 点击 📎 按钮选完文件后触发，files 是浏览器 FileList。
   *  上层（useChatAttachments）拿到 File 数组后异步上传到 /chat/upload。 */
  onChooseFiles?: (files: FileList | null) => void
}

export default function ChatInputTopToolbar({
  currentAgentId,
  currentAgentName,
  agents,
  showCorridorPanel,
  corridorTarget,
  corridorContent,
  corridorSending,
  onToggleCorridorPanel,
  onTargetChange,
  onContentChange,
  onCancelCorridor,
  onSendCorridor,
  onInsertMention,
  mentionActive,
  onChooseFiles,
}: ChatInputTopToolbarProps) {
  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleClickAttach = () => {
    fileInputRef.current?.click()
  }
  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    onChooseFiles?.(e.target.files)
    // 清空 input value：浏览器对同一文件 change 事件不会重复触发，必须重置才能连选同文件
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  return (
    <div className="input-top-toolbar">
      <button
        type="button"
        className={`input-tool-btn ${mentionActive ? 'is-active' : ''}`}
        title={CHAT_UI_TEXT.mentionTitle}
        aria-label={CHAT_UI_TEXT.mentionTitle}
        onMouseDown={(e) => e.preventDefault()}
        onClick={onInsertMention}
      >
        <Plus className="input-tool-icon" size={16} strokeWidth={1.9} />
      </button>
      <button
        type="button"
        className="input-tool-btn"
        title="上传附件（PDF / Office / 图片 / 任意文件，50 MB 上限）"
        aria-label="上传附件"
        onClick={handleClickAttach}
      >
        <Paperclip className="input-tool-icon" size={15} strokeWidth={1.9} />
      </button>
      <input
        ref={fileInputRef}
        type="file"
        multiple
        style={{ display: 'none' }}
        onChange={handleFileChange}
      />
      <button
        type="button"
        className={`input-tool-btn ${corridorTarget ? 'is-active' : ''}`}
        title={CHAT_UI_TEXT.corridorTitle}
        aria-label={CHAT_UI_TEXT.corridorTitle}
        onClick={onToggleCorridorPanel}
      >
        <Waypoints className="input-tool-icon" size={15} strokeWidth={1.9} />
      </button>
      {showCorridorPanel && (
        <ChatCorridorPanel
          currentAgentId={currentAgentId}
          currentAgentName={currentAgentName}
          agents={agents}
          corridorTarget={corridorTarget}
          corridorContent={corridorContent}
          corridorSending={corridorSending}
          onTargetChange={onTargetChange}
          onContentChange={onContentChange}
          onCancel={onCancelCorridor}
          onSend={onSendCorridor}
        />
      )}
    </div>
  )
}
