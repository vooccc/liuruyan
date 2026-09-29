/**
 * ProviderLogoEditor —— 服务商头像编辑 popover。
 *
 * 三个动作（参考 cherry-studio AddProviderPopup）：
 *   1. 上传图片：本地文件 → 压缩到 256px → base64 存 IndexedDB
 *   2. 内置图库：从 13 个 BUILTIN_PROVIDERS 挑一个 brand logo（写到 provider.icon 字段）
 *   3. 重置：清 IndexedDB + 清 icon 字段，回到自动决策（智能识别 / 生成头像）
 *
 * 视觉：点击当前头像，弹出三按钮 row + 内置图库 grid。所有持久化 op 完成后
 * 调 `notifyProviderLogoChanged(providerId)`，全局 ProviderAvatar 自动刷新。
 */
import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { createPortal } from 'react-dom'
import { ImagePlus, RotateCcw, X } from 'lucide-react'

import { updateProvider, type ProviderView } from '../../api/providers'
import {
  compressImageToBase64,
  providerLogoStorage,
} from '../../services/providerLogoStorage'
import {
  ProviderAvatar,
  notifyProviderLogoChanged,
} from '../ProviderAvatar'
import { BrandIcon, type BrandId } from '../BrandIcon'

/** 内置图库可选项：用户可挑这些 brand logo 套到自己的 custom provider 上。 */
const LOGO_GALLERY: Array<{ brand: BrandId; label: string }> = [
  { brand: 'openrouter', label: 'OpenRouter' },
  { brand: 'openai', label: 'OpenAI' },
  { brand: 'anthropic', label: 'Anthropic' },
  { brand: 'deepseek', label: 'DeepSeek' },
  { brand: 'gemini', label: 'Gemini' },
  { brand: 'kimi', label: 'Kimi' },
  { brand: 'zhipu', label: '智谱' },
  { brand: 'siliconflow', label: '硅基流动' },
  { brand: 'dashscope', label: '通义千问' },
  { brand: 'modelscope', label: '魔搭' },
  { brand: 'minimax', label: 'MiniMax' },
  { brand: 'doubao', label: '豆包' },
  { brand: 'spark', label: '讯飞星火' },
  { brand: 'mistral', label: 'Mistral' },
  { brand: 'cohere', label: 'Cohere' },
  { brand: 'groq', label: 'Groq' },
  { brand: 'together', label: 'Together' },
  { brand: 'baidu', label: '百度' },
  { brand: 'qianfan', label: '百度千帆' },
  { brand: 'xai', label: 'xAI' },
  { brand: 'mimo', label: '小米 MiMo' },
  { brand: 'google', label: 'Google' },
  { brand: 'ollama', label: 'Ollama' },
  { brand: 'lmstudio', label: 'LM Studio' },
]

interface Props {
  provider: ProviderView
  onClose: () => void
  /** 头像变更后回调（用于刷新外层 provider state，例如 icon 字段）。 */
  onChanged?: (next: ProviderView) => void
}

export default function ProviderLogoEditor({ provider, onClose, onChanged }: Props) {
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const downOnOverlayRef = useRef(false)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return LOGO_GALLERY
    return LOGO_GALLERY.filter(
      (l) => l.label.toLowerCase().includes(q) || l.brand.toLowerCase().includes(q),
    )
  }, [search])

  const handleUpload = async (file: File) => {
    setBusy('upload')
    setError(null)
    try {
      if (file.size > 5 * 1024 * 1024) {
        throw new Error('图片不能超过 5MB')
      }
      const base64 = await compressImageToBase64(file, { maxSize: 256, quality: 0.85 })
      await providerLogoStorage.set(provider.id, base64)
      // 上传图片后，清空 icon 字段（图片优先级最高，省得纠缠）
      if (provider.icon) {
        const res = await updateProvider(provider.id, { icon: '' })
        onChanged?.(res.provider)
      }
      notifyProviderLogoChanged(provider.id)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : '上传失败')
    } finally {
      setBusy(null)
    }
  }

  const handlePickBuiltin = async (brand: BrandId) => {
    setBusy(brand)
    setError(null)
    try {
      // 选内置 logo 时，清掉用户上传的图片（不然图片优先级会盖住选择）
      await providerLogoStorage.remove(provider.id)
      const res = await updateProvider(provider.id, { icon: brand })
      onChanged?.(res.provider)
      notifyProviderLogoChanged(provider.id)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败')
    } finally {
      setBusy(null)
    }
  }

  const handleReset = async () => {
    setBusy('reset')
    setError(null)
    try {
      await providerLogoStorage.remove(provider.id)
      if (provider.icon) {
        const res = await updateProvider(provider.id, { icon: '' })
        onChanged?.(res.provider)
      }
      notifyProviderLogoChanged(provider.id)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : '重置失败')
    } finally {
      setBusy(null)
    }
  }

  // 防"input 内 selection 拖到 overlay 释放误关闭"：用 mousedown 起点判断
  const onOverlayMouseDown = (e: MouseEvent<HTMLDivElement>) => {
    downOnOverlayRef.current = e.target === e.currentTarget
  }
  const onOverlayMouseUp = (e: MouseEvent<HTMLDivElement>) => {
    if (downOnOverlayRef.current && e.target === e.currentTarget) onClose()
    downOnOverlayRef.current = false
  }

  const node = (
    <div className="lr-mm-overlay" onMouseDown={onOverlayMouseDown} onMouseUp={onOverlayMouseUp}>
      <div
        className="lr-mm-panel lr-logo-editor"
        style={{ width: 'min(420px, 94vw)' }}
      >
        <header className="lr-mm-header">
          <ProviderAvatar provider={provider} size={28} />
          <div className="lr-mm-header__text">
            <div className="lr-mm-header__title">设置图标 · {provider.name}</div>
            <div className="lr-mm-header__sub">上传图片、选择内置图标，或重置为自动生成</div>
          </div>
          <button type="button" className="lr-mm-close" onClick={onClose} title="关闭 (Esc)">
            <X size={16} />
          </button>
        </header>

        <div className="lr-logo-editor__actions">
          <button
            type="button"
            className="lr-mm-btn ghost"
            onClick={() => fileInputRef.current?.click()}
            disabled={busy === 'upload'}
          >
            <ImagePlus size={14} />
            上传图片
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif,image/svg+xml"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) void handleUpload(f)
              if (fileInputRef.current) fileInputRef.current.value = ''
            }}
          />
          <button
            type="button"
            className="lr-mm-btn ghost"
            onClick={handleReset}
            disabled={busy === 'reset'}
          >
            <RotateCcw size={14} />
            重置图标
          </button>
        </div>

        <div className="lr-logo-editor__search">
          <input
            type="text"
            placeholder="搜索内置图标"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>

        <div className="lr-logo-editor__grid">
          {filtered.map((it) => {
            const isCurrent = provider.icon === it.brand
            return (
              <button
                key={it.brand}
                type="button"
                className={
                  'lr-logo-editor__cell' + (isCurrent ? ' is-current' : '')
                }
                title={it.label}
                onClick={() => handlePickBuiltin(it.brand)}
                disabled={busy === it.brand}
              >
                <BrandIcon id={it.brand} size={26} badge />
                <span className="lr-logo-editor__cell-label">{it.label}</span>
              </button>
            )
          })}
          {filtered.length === 0 && (
            <div className="lr-logo-editor__empty">没有匹配的 logo</div>
          )}
        </div>

        {error && (
          <div className="lr-addform__err lr-addform__err--block">{error}</div>
        )}

        <footer className="lr-mm-footer">
          <div className="lr-mm-footer__hint">
            提示：图片在本地浏览器存储，不会同步到服务端
          </div>
          <div className="lr-mm-footer__actions">
            <button type="button" className="lr-mm-btn ghost" onClick={onClose}>关闭</button>
          </div>
        </footer>
      </div>
    </div>
  )

  return createPortal(node, document.body)
}
