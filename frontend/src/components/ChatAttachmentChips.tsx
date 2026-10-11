import { useState, type MouseEvent } from 'react'
import { AlertCircle, FileText, Loader2, X } from 'lucide-react'
import type { ChatAttachment } from './useChatAttachments'
import ImageLightbox from './ImageLightbox'

interface ChatAttachmentChipsProps {
  attachments: ChatAttachment[]
  onRemove: (id: string) => void
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/**
 * 图片附件缩略图卡片（粘贴截图 / 选图片文件时用）。微信/Cascade 风格：
 *  - 56×56 圆角方卡，直接显示图像
 *  - 上传中：半透明遮罩 + 旋转 spinner
 *  - 失败：红色边框 + 警告角标,title 显示 error
 *  - 右上角 X 按钮 hover 显形
 *  - 点击缩略图本体（非 X 按钮）打开大图预览（onPreview 回调）
 */
function ImageThumb({
  att,
  onRemove,
  onPreview,
}: {
  att: ChatAttachment
  onRemove: (id: string) => void
  onPreview: (att: ChatAttachment) => void
}) {
  const titleText = att.status === 'failed' ? (att.error || att.name) : (att.path || att.name)
  const previewable = !!att.thumbUrl
  const handleClick = () => {
    if (previewable) onPreview(att)
  }
  const handleRemoveClick = (e: MouseEvent) => {
    // 阻止冒泡到外层 thumb，避免点 X 同时打开预览
    e.stopPropagation()
    onRemove(att.id)
  }
  return (
    <div
      className={`chat-attachment-thumb is-${att.status}${previewable ? ' is-clickable' : ''}`}
      title={previewable ? `${titleText}（点击查看大图）` : titleText}
      role={previewable ? 'button' : undefined}
      tabIndex={previewable ? 0 : undefined}
      onClick={handleClick}
      onKeyDown={(e) => {
        if (!previewable) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onPreview(att)
        }
      }}
    >
      {att.thumbUrl ? (
        <img src={att.thumbUrl} alt={att.name} className="chat-attachment-thumb-img" />
      ) : (
        <div className="chat-attachment-thumb-fallback"><FileText size={20} /></div>
      )}
      {att.status === 'uploading' && (
        <div className="chat-attachment-thumb-overlay">
          <Loader2 size={18} className="chat-attachment-spinner" />
        </div>
      )}
      {att.status === 'failed' && (
        <div className="chat-attachment-thumb-overlay is-error">
          <AlertCircle size={18} />
        </div>
      )}
      <button
        type="button"
        className="chat-attachment-thumb-remove"
        aria-label="移除附件"
        title="移除附件"
        onClick={handleRemoveClick}
      >
        <X size={12} />
      </button>
    </div>
  )
}

export default function ChatAttachmentChips({ attachments, onRemove }: ChatAttachmentChipsProps) {
  const [previewing, setPreviewing] = useState<ChatAttachment | null>(null)
  if (attachments.length === 0) return null
  // previewing 可能因为 attachments 变化（如被 remove）而失效，渲染前同步一下
  const livePreview = previewing
    ? attachments.find(a => a.id === previewing.id) || null
    : null
  return (
    <div className="chat-attachment-chips">
      {attachments.map(att => {
        // 图片走缩略图卡片（含失败的图，让用户看到自己粘了啥）
        if (att.isImage) {
          return <ImageThumb key={att.id} att={att} onRemove={onRemove} onPreview={setPreviewing} />
        }
        // 非图片文件保留原文本 chip
        return (
          <div
            key={att.id}
            className={`chat-attachment-chip is-${att.status}`}
            title={att.status === 'failed' ? att.error : att.path || att.name}
          >
            <span className="chat-attachment-chip-icon">
              {att.status === 'uploading' && <Loader2 size={12} className="chat-attachment-spinner" />}
              {att.status === 'ok' && <FileText size={12} />}
              {att.status === 'failed' && <AlertCircle size={12} />}
            </span>
            <span className="chat-attachment-chip-name" title={att.name}>{att.name}</span>
            <span className="chat-attachment-chip-size">{formatSize(att.size)}</span>
            <button
              type="button"
              className="chat-attachment-chip-remove"
              aria-label="移除附件"
              title="移除附件"
              onClick={() => onRemove(att.id)}
            >
              <X size={12} />
            </button>
          </div>
        )
      })}
      {livePreview && livePreview.thumbUrl && (
        <ImageLightbox
          src={livePreview.thumbUrl}
          alt={livePreview.name}
          caption={livePreview.name}
          subCaption={`${formatSize(livePreview.size)}${livePreview.mime ? ` · ${livePreview.mime}` : ''}`}
          onClose={() => setPreviewing(null)}
        />
      )}
    </div>
  )
}
