import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { Send } from 'lucide-react'
import type { Agent } from '../api/chat'
import CollabSpeakerPicker, { CollabSpeakerTrigger } from './CollabSpeakerPicker'

interface CollabInputComposerProps {
  input: string
  isChatting: boolean
  /** 场景已散会（scene_update is_active=false）：禁输入禁发送 */
  disabled?: boolean
  placeholder: string
  sendLabel: string
  stopLabel: string
  /** 当前场景的参与者 id（候选池） */
  participants: string[]
  /** 全角色目录（lookup name/avatar） */
  agents: Agent[]
  /** 已指定的发言序列（粘性，由 CollabChat 持有） */
  speakers: string[]
  onSpeakersChange: (next: string[]) => void
  onInputChange: (value: string) => void
  onSend: () => void
  onStop: () => void
}

const TEXTAREA_MAX_HEIGHT = 150

/**
 * 协作模式输入框（多人聊天）。
 *
 * 布局：
 *   ┌─────────────────────────────────┐
 *   │ <textarea, multi-line, autogrow> │
 *   ├─────────────────────────────────┤
 *   │ [@] ① 柳如烟 ② 苏晚      [发送] │
 *   └─────────────────────────────────┘
 *
 * 设计要点：
 * - textarea 取代单行 input：长消息体验更好；Enter 发送 / Shift+Enter 换行（与主聊天对齐）。
 * - speaker chip 在底栏左侧常驻可见，让"我现在是指定状态"一目了然。
 * - picker popover 锚定到 trigger 按钮上方，点外部 / Esc 关闭。
 */
export default function CollabInputComposer({
  input,
  isChatting,
  disabled = false,
  placeholder,
  sendLabel,
  stopLabel,
  participants,
  agents,
  speakers,
  onSpeakersChange,
  onInputChange,
  onSend,
  onStop,
}: CollabInputComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const [pickerOpen, setPickerOpen] = useState(false)

  // ── textarea 自适应高度（最高 ~180px 后启用滚动） ──
  useLayoutEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    const next = Math.min(el.scrollHeight, TEXTAREA_MAX_HEIGHT)
    el.style.height = `${next}px`
  }, [input])

  // 流式回复结束后回弹焦点到输入框（不打扰用户主动 blur 的场景：仅 isChatting 由 true → false 时触发）
  const wasChattingRef = useRef(isChatting)
  useEffect(() => {
    if (wasChattingRef.current && !isChatting) {
      textareaRef.current?.focus()
    }
    wasChattingRef.current = isChatting
  }, [isChatting])

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      if (!isChatting && !disabled && input.trim()) onSend()
    }
  }

  return (
    <div className="collab-input-shell">
      <div className="collab-input-card">
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(event) => onInputChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          disabled={isChatting || disabled}
          autoFocus
          rows={1}
          className="collab-input-textarea"
        />
        <div className="collab-input-toolbar">
          <div className="collab-input-toolbar-left">
            <CollabSpeakerTrigger
              value={speakers}
              agents={agents}
              isOpen={pickerOpen}
              onToggle={() => setPickerOpen((prev) => !prev)}
              buttonRef={triggerRef}
            />
            {pickerOpen && (
              <CollabSpeakerPicker
                participants={participants}
                agents={agents}
                value={speakers}
                onChange={onSpeakersChange}
                onClose={() => setPickerOpen(false)}
                anchorEl={triggerRef.current}
              />
            )}
          </div>
          <div className="collab-input-toolbar-right">
            {isChatting ? (
              <button
                type="button"
                className="collab-send-btn stop"
                onClick={onStop}
              >
                {stopLabel}
              </button>
            ) : (
              <button
                type="button"
                className="collab-send-btn"
                onClick={onSend}
                disabled={!input.trim() || disabled}
              >
                <Send size={13} />
                <span>{sendLabel}</span>
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
