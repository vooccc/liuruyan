import { useState } from 'react'
import { Copy, Download, ExternalLink, ImageIcon, Sparkles } from 'lucide-react'

/**
 * GeneratedImageCard — 在 chat 中渲染 generate_image 工具生成的图片。
 *
 * 协议：后端 generate_image 工具返回 result JSON 含 `_runtime_block: 'generated_image'`
 * sentinel + images[]（每项含 url 或 data_uri）+ prompt/model/provider 等元数据；
 * runtimeBlocks 识别后渲染本卡片（取代默认 ChainTool 的 tool_result UI）。
 *
 * 设计：
 * - 单图占满宽度；多图网格排列
 * - 每张图下方工具栏：新窗口打开 / 下载 / 复制链接
 * - 图加载失败（URL 过期 / 跨源）显示占位 + 提示
 * - 顶部显示 prompt + model 元信息
 */

export interface GeneratedImageItem {
  url?: string
  data_uri?: string
  display_type?: 'url' | 'b64'
  revised_prompt?: string
}

export interface GeneratedImagePayload {
  _runtime_block: 'generated_image'
  images: GeneratedImageItem[]
  prompt: string
  model: string
  provider: string
  size?: string
  count?: number
}

function imageSrc(item: GeneratedImageItem): string {
  return item.data_uri || item.url || ''
}

function SingleImage({
  item,
  index,
  prompt,
}: {
  item: GeneratedImageItem
  index: number
  prompt: string
}) {
  const [loadError, setLoadError] = useState(false)
  const [copied, setCopied] = useState(false)
  const src = imageSrc(item)
  const isUrl = !!item.url && !item.data_uri

  const onCopy = async () => {
    if (!item.url) return
    try {
      await navigator.clipboard.writeText(item.url)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // 剪贴板失败：兜底无动作
    }
  }

  const onDownload = () => {
    const a = document.createElement('a')
    a.href = src
    a.download = `generated-${Date.now()}-${index}.png`
    a.target = '_blank'
    a.rel = 'noopener noreferrer'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
  }

  return (
    <div className="gen-image-item">
      {loadError ? (
        <div className="gen-image-error">
          <ImageIcon size={20} />
          <div className="gen-image-error-text">
            图片加载失败
            {isUrl && '（链接可能已过期，重新生成或让模型用 response_format=b64_json）'}
          </div>
        </div>
      ) : (
        <img
          className="gen-image-img"
          src={src}
          alt={item.revised_prompt || prompt}
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={() => setLoadError(true)}
        />
      )}
      <div className="gen-image-actions">
        <button
          type="button"
          className="gen-image-btn"
          onClick={() => window.open(src, '_blank', 'noopener,noreferrer')}
          title="在新窗口打开"
        >
          <ExternalLink size={13} />
        </button>
        <button
          type="button"
          className="gen-image-btn"
          onClick={onDownload}
          title="下载图片"
        >
          <Download size={13} />
        </button>
        {item.url && (
          <button
            type="button"
            className="gen-image-btn"
            onClick={onCopy}
            title="复制链接"
          >
            <Copy size={13} />
            {copied && <span className="gen-image-copied">已复制</span>}
          </button>
        )}
      </div>
    </div>
  )
}

export function GeneratedImageCard({
  payload,
}: {
  payload: GeneratedImagePayload
}) {
  const images = Array.isArray(payload.images) ? payload.images : []
  const multi = images.length > 1

  return (
    <div className="gen-image-card">
      <div className="gen-image-header">
        <span className="gen-image-badge" title="AI 生成图片">
          <Sparkles size={11} />
          <span>生图</span>
        </span>
        <span className="gen-image-model" title={`${payload.provider} · ${payload.model}`}>
          {payload.model}
        </span>
        {payload.size && <span className="gen-image-size">{payload.size}</span>}
      </div>
      {payload.prompt && (
        <div className="gen-image-prompt" title={payload.prompt}>
          {payload.prompt}
        </div>
      )}
      <div className={`gen-image-grid ${multi ? 'multi' : 'single'}`}>
        {images.map((item, idx) => (
          <SingleImage
            key={`gen-img-${idx}-${imageSrc(item).slice(-24)}`}
            item={item}
            index={idx}
            prompt={payload.prompt}
          />
        ))}
      </div>
    </div>
  )
}

/** 尝试把 tool_result 解析成 GeneratedImage payload；不是则返 null。
 *
 * 协议：后端返 JSON 字符串含 `_runtime_block: 'generated_image'` sentinel。
 * 跟 BrowserPreviewCard 同款"工具结果 sentinel"模式。
 */
export function tryParseGeneratedImagePayload(raw: string | undefined | null):
  | GeneratedImagePayload
  | null {
  if (!raw || typeof raw !== 'string') return null
  // 快速排除：不含 sentinel
  if (!raw.includes('"_runtime_block"') || !raw.includes('"generated_image"')) return null
  try {
    const obj = JSON.parse(raw)
    if (!obj || typeof obj !== 'object') return null
    if (obj._runtime_block !== 'generated_image') return null
    if (!Array.isArray(obj.images) || obj.images.length === 0) return null
    return obj as GeneratedImagePayload
  } catch {
    return null
  }
}
