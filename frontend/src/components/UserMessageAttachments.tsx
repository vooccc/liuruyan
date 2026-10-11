import { useState } from 'react'
import { FileText } from 'lucide-react'
import ImageLightbox from './ImageLightbox'
import { getUploadedFileUrl, type UserMessageAttachment } from './userAttachmentFooter'

interface UserMessageAttachmentsProps {
  attachments: UserMessageAttachment[]
}

/**
 * 用户消息气泡里的"已发送附件"展示带（粘在文本上方，跟微信/Cascade 习惯一致）：
 *  - 图片：72×72 圆角缩略图，点击开 ImageLightbox 看大图
 *  - 非图片：暂时显示一行带文件名 + 图标的 chip（fallback，避免完全丢失视觉信号）
 *
 * thumb URL 走 backend GET /chat/upload/file?path= 端点，后端校验路径必须在 uploads_dir 内。
 */
export default function UserMessageAttachments({ attachments }: UserMessageAttachmentsProps) {
  const [previewing, setPreviewing] = useState<UserMessageAttachment | null>(null)
  if (!attachments || attachments.length === 0) return null

  return (
    <div className="user-msg-attachments">
      {attachments.map((att, i) => {
        if (att.isImage) {
          const url = getUploadedFileUrl(att.path)
          return (
            <button
              type="button"
              key={`${att.path}-${i}`}
              className="user-msg-attachment-thumb"
              onClick={() => setPreviewing(att)}
              aria-label={`查看 ${att.name}`}
            >
              <img src={url} alt={att.name} loading="lazy" />
            </button>
          )
        }
        return (
          <div
            key={`${att.path}-${i}`}
            className="user-msg-attachment-file"
            title={att.path}
          >
            <FileText size={14} />
            <span className="user-msg-attachment-file-name">{att.name}</span>
          </div>
        )
      })}
      {previewing && (
        <ImageLightbox
          src={getUploadedFileUrl(previewing.path)}
          alt={previewing.name}
          caption={previewing.name}
          onClose={() => setPreviewing(null)}
        />
      )}
    </div>
  )
}
