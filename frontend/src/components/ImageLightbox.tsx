import { useEffect, type MouseEvent } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

interface ImageLightboxProps {
  src: string
  alt?: string
  /** 顶部标题（一般是文件名）。可选。 */
  caption?: string
  /** 副标题（一般是大小 / 路径）。可选。 */
  subCaption?: string
  onClose: () => void
}

/**
 * 通用图片大图预览组件。微信 / Cascade 风格：
 *  - 全屏暗背景蒙层，图片 contain 居中
 *  - 点击蒙层 / Esc / 右上角 X 关闭
 *  - 点击图片本身不关（防误触）
 *  - 通过 React portal 挂到 document.body，规避祖先 stacking context
 *
 * 复用点：除输入框附件缩略图外，未来历史消息里的图片块、
 * computer_use 截图卡片等都可以套这个。
 */
export default function ImageLightbox({ src, alt, caption, subCaption, onClose }: ImageLightboxProps) {
  // Esc 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // 弹窗期间锁 body 滚动，避免背景滚走
  useEffect(() => {
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = prev }
  }, [])

  const stop = (e: MouseEvent) => e.stopPropagation()

  const node = (
    <div
      className="lr-image-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label={caption || alt || 'image preview'}
      onClick={onClose}
    >
      <figure className="lr-image-lightbox-figure" onClick={stop}>
        {/* img-wrap 是 inline-block + position:relative：宽高紧贴 <img> 实际尺寸
            （非 figure 整列宽，figure 列宽会被下方 figcaption 撑开），
            close 按钮 absolute 到 wrap 的右上角才能精准贴在图片角上。 */}
        <div className="lr-image-lightbox-img-wrap">
          <img src={src} alt={alt || caption || ''} className="lr-image-lightbox-img" />
          <button
            type="button"
            className="lr-image-lightbox-close"
            onClick={onClose}
            aria-label="关闭预览"
          >
            <X size={14} />
          </button>
        </div>
        {(caption || subCaption) && (
          <figcaption className="lr-image-lightbox-caption">
            {caption && <span className="lr-image-lightbox-caption-title">{caption}</span>}
            {subCaption && <span className="lr-image-lightbox-caption-sub">{subCaption}</span>}
          </figcaption>
        )}
      </figure>
    </div>
  )

  return createPortal(node, document.body)
}
