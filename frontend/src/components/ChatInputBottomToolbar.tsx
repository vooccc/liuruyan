import { useEffect, useRef, useState, type Ref, type RefObject } from 'react'
import { ChevronDown, ShieldCheck, Square, Trash2 } from 'lucide-react'
import type { Model } from '../api/chat'
import ChatModelPicker from './ChatModelPicker'
// voice 孤岛模块：流式语音输入（项目里唯一的语音输入入口）。
// 撤除本功能：删掉本 import + 下方 <VoiceInputSlot> 渲染块，再删 frontend/src/voice/ 目录。
import { VoiceInputSlot, type VoiceStreamButtonRef } from '../voice'
import { SendGlyph } from './conversationUi'
import { CHAT_UI_TEXT } from './chatUiText'
import {
  CHAT_INPUT_MODE_OPTIONS,
  type ChatInputMode,
  getChatInputModeOption,
} from './chatInputModes'

interface ChatInputBottomToolbarProps {
  selectedInputMode: ChatInputMode
  currentModelMeta: Model | null
  customModels: Model[]
  selectedModel: string
  selectedProvider: string
  showModelPicker: boolean
  modelBtnRef: RefObject<HTMLDivElement>
  pickerPanelRef: RefObject<HTMLDivElement>
  getModelProvider: (model?: { provider?: string | null }) => string
  isLoading: boolean
  canSubmit: boolean
  onToggleModelPicker: () => void
  onSelectCustomModel: (model: Model) => void
  onOpenModelSettings: () => void
  onSelectInputMode: (mode: ChatInputMode) => void
  onClearHistory: () => void
  onStop: () => void
  onSubmit: () => void
  onOpenGuardianDialog: () => void
  /** 语音转写结果回调：调用方（Chat.tsx）拼接到 textarea。 */
  onVoiceTranscription?: (text: string) => void
  /** 错误回调：拒权限 / 转写失败等。建议接到 toast。 */
  onVoiceError?: (message: string) => void
  /** 流式语音草稿回调（partial 实时进 textarea，替换式）。仅流式档有，可缺省。 */
  onVoiceDraft?: (draft: string | null) => void
  /** 暴露 ref 给外层用 Ctrl+Shift+M 全局快捷键调用 toggleRecording。
   *  类型用 Ref<T> 而非 RefObject<T>，兼容 React 18 下 useRef<T | null>(null) 创建的 ref。 */
  micButtonRef?: Ref<VoiceStreamButtonRef>
}

export default function ChatInputBottomToolbar({
  selectedInputMode,
  currentModelMeta,
  customModels,
  selectedModel,
  selectedProvider,
  showModelPicker,
  modelBtnRef,
  pickerPanelRef,
  getModelProvider,
  isLoading,
  canSubmit,
  onToggleModelPicker,
  onSelectCustomModel,
  onOpenModelSettings,
  onSelectInputMode,
  onClearHistory,
  onStop,
  onSubmit,
  onOpenGuardianDialog,
  onVoiceTranscription,
  onVoiceError,
  onVoiceDraft,
  micButtonRef,
}: ChatInputBottomToolbarProps) {
  const [showModePicker, setShowModePicker] = useState(false)
  const modePanelRef = useRef<HTMLDivElement>(null)
  const selectedMode = getChatInputModeOption(selectedInputMode)
  const SelectedModeIcon = selectedMode.icon

  useEffect(() => {
    if (!showModePicker) return undefined

    const handlePointerDown = (event: MouseEvent) => {
      if (!modePanelRef.current?.contains(event.target as Node)) {
        setShowModePicker(false)
      }
    }

    document.addEventListener('mousedown', handlePointerDown)
    return () => document.removeEventListener('mousedown', handlePointerDown)
  }, [showModePicker])

  return (
    <div className="input-bottom-toolbar">
      <div className="mode-selector-anchor" ref={modePanelRef}>
        <button
          type="button"
          className={`mode-selector is-${selectedMode.accent} ${showModePicker ? 'open' : ''}`}
          onClick={() => setShowModePicker((previous) => !previous)}
          title={CHAT_UI_TEXT.modeSwitchTitle}
        >
          <SelectedModeIcon size={14} />
          <span>{selectedMode.label}</span>
          <ChevronDown size={12} />
        </button>

        {showModePicker && (
          <div className="mode-picker-panel">
            {CHAT_INPUT_MODE_OPTIONS.map((option) => {
              const Icon = option.icon
              return (
                <button
                  key={option.key}
                  type="button"
                  className={`mode-picker-item is-${option.accent} ${option.key === selectedInputMode ? 'is-active' : ''}`}
                  onClick={() => {
                    onSelectInputMode(option.key)
                    setShowModePicker(false)
                  }}
                >
                  <span className="mode-picker-icon">
                    <Icon size={16} />
                  </span>
                  <span className="mode-picker-copy">
                    <span className="mode-picker-label">{option.label}</span>
                    <span className="mode-picker-description">{option.description}</span>
                  </span>
                </button>
              )
            })}
          </div>
        )}
      </div>

      <ChatModelPicker
        currentModelMeta={currentModelMeta}
        customModels={customModels}
        selectedModel={selectedModel}
        selectedProvider={selectedProvider}
        showModelPicker={showModelPicker}
        modelBtnRef={modelBtnRef}
        pickerPanelRef={pickerPanelRef}
        getModelProvider={getModelProvider}
        onTogglePicker={onToggleModelPicker}
        onSelectCustomModel={onSelectCustomModel}
        onOpenModelSettings={onOpenModelSettings}
      />

      <button
        type="button"
        className="toolbar-action-btn"
        onClick={onOpenGuardianDialog}
        title="权限管理（按角色配置文件读写、命令执行等权限）"
      >
        <ShieldCheck size={14} />
        <span>权限</span>
      </button>

      <button type="button" className="toolbar-action-btn danger-btn" onClick={onClearHistory} title={CHAT_UI_TEXT.clearTitle}>
        <Trash2 size={14} />
        <span>{CHAT_UI_TEXT.clearLabel}</span>
      </button>

      <div className="input-right-actions">
        {onVoiceTranscription ? (
          <VoiceInputSlot
            ref={micButtonRef}
            disabled={isLoading}
            onTranscription={onVoiceTranscription}
            onError={onVoiceError}
            onDraftChange={onVoiceDraft}
          />
        ) : null}
        {isLoading ? (
          <button
            type="button"
            className="send-button-modern stop-button"
            onClick={onStop}
            title={CHAT_UI_TEXT.stopTitle}
          >
            <Square size={16} fill="currentColor" />
          </button>
        ) : (
          <button
            type="button"
            disabled={!canSubmit}
            className="send-button-modern"
            onClick={onSubmit}
            title={CHAT_UI_TEXT.sendTitle}
          >
            <SendGlyph />
          </button>
        )}
      </div>
    </div>
  )
}
