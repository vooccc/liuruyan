/**
 * MarkdownEditor — 工坊专用的可切换预览编辑器
 * 
 * 简约设计：textarea + 可选预览模式，无依赖
 */
import { useState } from 'react'

interface MarkdownEditorProps {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  /** 是否显示预览切换按钮（默认 true） */
  showPreviewToggle?: boolean
  /** 最小高度（默认 200px） */
  minHeight?: number
  /** 最大高度（默认 500px） */
  maxHeight?: number
  /** 额外 CSS 类名 */
  className?: string
  /** 只读模式 */
  readOnly?: boolean
}

export default function MarkdownEditor({
  value,
  onChange,
  placeholder,
  showPreviewToggle = true,
  className = '',
  readOnly = false,
}: MarkdownEditorProps) {
  const [showPreview, setShowPreview] = useState(false)

  return (
    <div className={`md-editor ${className}`}>
      {showPreviewToggle && !readOnly && (
        <div className="md-editor-toolbar">
          <button
            className={`md-editor-toggle ${!showPreview ? 'active' : ''}`}
            onClick={() => setShowPreview(false)}
            type="button"
          >
            编辑
          </button>
          <button
            className={`md-editor-toggle ${showPreview ? 'active' : ''}`}
            onClick={() => setShowPreview(true)}
            type="button"
          >
            预览
          </button>
        </div>
      )}
      {showPreview || readOnly ? (
        <div className="md-editor-preview">
          {value || (
            <span className="md-editor-placeholder">{placeholder || '暂无内容'}</span>
          )}
        </div>
      ) : (
        <textarea
          className="md-editor-textarea"
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder={placeholder}
          spellCheck={false}
        />
      )}
    </div>
  )
}
