/**
 * 添加自定义服务商弹窗（对应 POST /providers）。
 *
 * 设计对标 cherry studio：用户只填 name + base_url + api_key，provider id 由前端自动生成
 * （`custom-<nanoid>`）。这样允许同名服务商并存、并且内置的 openai/anthropic 等短名不会变成
 * 用户不能用的"关键字"。内置 provider id 仍是固定英文短名（transport 映射需要）。
 * transport 默认 openai_chat，不暴露（真要用 Anthropic 的可走 API）。
 */
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AlertCircle, Check, Loader2, X } from 'lucide-react'

import { createProvider } from '../../api/providers'

interface AddProviderDialogProps {
  onClose: () => void
  onCreated: (providerId: string) => void
}

/** 生成 URL 安全的随机 id（不引入 nanoid 依赖，手写足够）。 */
function makeProviderId(): string {
  const alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
  let s = 'custom-'
  for (let i = 0; i < 10; i++) s += alphabet[Math.floor(Math.random() * alphabet.length)]
  return s
}

export default function AddProviderDialog({
  onClose, onCreated,
}: AddProviderDialogProps) {
  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [transport, setTransport] = useState<'openai_chat' | 'anthropic_messages'>('openai_chat')
  const [description, setDescription] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Esc 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const canSubmit = !!name.trim() && !!baseUrl.trim() && !saving

  const onSubmit = async () => {
    setSaving(true)
    setError(null)
    try {
      const newId = makeProviderId()
      await createProvider({
        id: newId,
        name: name.trim(),
        base_url: baseUrl.trim(),
        api_key: apiKey.trim() || undefined,
        transport,
        description: description.trim() || undefined,
      })
      onCreated(newId)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : '创建失败')
    } finally {
      setSaving(false)
    }
  }

  // 防"input 内 selection 拖到 overlay 释放误关闭"：用 mousedown 起点判断真外部点击。
  // 起点必须就在 overlay 自身（不是从 panel 内开始的拖选），mouseup 也在 overlay，才算真点外面。
  const downOnOverlay = useRef(false)
  const onOverlayMouseDown = (e: React.MouseEvent<HTMLDivElement>) => {
    downOnOverlay.current = e.target === e.currentTarget
  }
  const onOverlayMouseUp = (e: React.MouseEvent<HTMLDivElement>) => {
    if (downOnOverlay.current && e.target === e.currentTarget) onClose()
    downOnOverlay.current = false
  }

  const node = (
    <div className="lr-mm-overlay" onMouseDown={onOverlayMouseDown} onMouseUp={onOverlayMouseUp}>
      <div className="lr-mm-panel" style={{ width: 'min(560px, 94vw)' }}>
        <header className="lr-mm-header">
          <div className="lr-mm-header__text">
            <div className="lr-mm-header__title">添加服务商</div>
            <div className="lr-mm-header__sub">接入内置之外服务商</div>
          </div>
          <button type="button" className="lr-mm-close" onClick={onClose} title="关闭 (Esc)">
            <X size={16} />
          </button>
        </header>

        <div className="lr-addform">
          <Field label="名称" required hint="仅用于列表显示，可与内置服务商同名">
            <input
              type="text"
              value={name}
              placeholder="如OpenAI"
              onChange={(e) => setName(e.target.value)}
              autoFocus
            />
          </Field>

          <Field label="协议" required hint="选择服务商 API 协议">
            <select
              value={transport}
              onChange={(e) => setTransport(e.target.value as 'openai_chat' | 'anthropic_messages')}
              className="lr-addform__select"
            >
              <option value="openai_chat">OpenAI 兼容（大多数 /v1/chat/completions）</option>
              <option value="anthropic_messages">Anthropic Messages（Claude /v1/messages）</option>
            </select>
          </Field>

          <Field
            label="接口地址"
            required
            hint={transport === 'anthropic_messages'
              ? 'Anthropic 兼容端点，如 https://xxx.com/anthropic'
              : 'OpenAI 兼容 /v1 端点'}
          >
            <input
              type="text"
              value={baseUrl}
              placeholder={transport === 'anthropic_messages'
                ? 'https://api.example.com/anthropic'
                : 'https://api.example.com/v1'}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </Field>

          <Field label="API Key" hint="留空则稍后在详情页填">
            <input
              type="password"
              value={apiKey}
              placeholder="sk-..."
              onChange={(e) => setApiKey(e.target.value)}
            />
          </Field>

          <Field label="描述" hint="可选，会在左栏 provider 卡片显示">
            <input
              type="text"
              value={description}
              placeholder="如 中转代理 / 国内镜像"
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>

          {error && (
            <div className="lr-addform__err lr-addform__err--block">
              <AlertCircle size={13} /> {error}
            </div>
          )}
        </div>

        <footer className="lr-mm-footer">
          <div className="lr-mm-footer__actions">
            <button type="button" className="lr-mm-btn ghost" onClick={onClose} disabled={saving}>取消</button>
            <button
              type="button"
              className="lr-mm-btn primary"
              onClick={onSubmit}
              disabled={!canSubmit}
            >
              {saving ? <Loader2 size={13} className="spin" /> : <Check size={13} />}
              创建
            </button>
          </div>
        </footer>
      </div>
    </div>
  )

  return createPortal(node, document.body)
}

function Field({
  label, required, hint, children,
}: {
  label: string
  required?: boolean
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="lr-addform__field">
      <label className="lr-addform__label">
        {label} {required && <span className="lr-addform__req">*</span>}
      </label>
      {children}
      {hint && <div className="lr-addform__hint">{hint}</div>}
    </div>
  )
}
