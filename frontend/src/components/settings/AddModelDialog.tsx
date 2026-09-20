/**
 * 手动添加/编辑单个模型弹窗。
 *
 * 用于：`/v1/models` 不返回但实际可用的小众模型 / ollama 本地拉的新模型 /
 * 用户想给模型起个别名 等场景。走 PUT /providers/{id}（models 整体替换）。
 */
import { useEffect, useRef, useState, type MouseEvent } from 'react'
import { createPortal } from 'react-dom'
import { AlertCircle, Check, Loader2, X } from 'lucide-react'

import {
  updateProvider, type ModelEntry, type ProviderView,
} from '../../api/providers'

interface AddModelDialogProps {
  provider: ProviderView
  /** 非空时为编辑模式，此 id 的 model 会被覆盖；为空时为新增 */
  editingModelId?: string | null
  onClose: () => void
  onSaved: () => void
}

const CONTEXT_PRESETS = new Set([32_000, 64_000, 128_000, 200_000, 400_000, 1_000_000])

function initialContextMode(model: ModelEntry | null | undefined): string {
  if (!model?.context_length || model.context_length_source === 'inferred') return 'auto'
  return CONTEXT_PRESETS.has(model.context_length) ? String(model.context_length) : 'custom'
}

export default function AddModelDialog({
  provider, editingModelId, onClose, onSaved,
}: AddModelDialogProps) {
  const editing = Boolean(editingModelId)
  const existing = editing ? provider.models.find((m) => m.id === editingModelId) : null

  const [id, setId] = useState(existing?.id ?? '')
  const [name, setName] = useState(existing?.name ?? '')
  const [contextMode, setContextMode] = useState(() => initialContextMode(existing))
  const [customContext, setCustomContext] = useState(() => String(existing?.context_length ?? ''))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const idTrim = id.trim()
  const existingIds = new Set(provider.models.map((m) => m.id))
  if (editing && editingModelId) existingIds.delete(editingModelId)
  const idConflict = idTrim && existingIds.has(idTrim)
  const customContextNumber = Number(customContext)
  const customContextValid = Number.isFinite(customContextNumber)
    && customContextNumber >= 1_000
    && customContextNumber <= 4_000_000
  const canSubmit = !!idTrim && !idConflict && !saving
    && (contextMode !== 'custom' || customContextValid)
  const resolvedContextLength = contextMode === 'auto'
    ? null
    : contextMode === 'custom'
      ? Math.round(customContextNumber)
      : Number(contextMode)

  const onSubmit = async () => {
    setSaving(true)
    setError(null)
    try {
      const nextModels: ModelEntry[] = editing
        ? provider.models.map((m) =>
            m.id === editingModelId
              ? {
                  ...m,
                  id: idTrim,
                  name: name.trim() || idTrim,
                  added_by_user: true,
                  context_length: resolvedContextLength,
                  context_length_source: undefined,
                }
              : m,
          )
        : [
            ...provider.models,
            {
              id: idTrim,
              name: name.trim() || idTrim,
              tags: [],
              icon: '',
              added_by_user: true,
              context_length: resolvedContextLength,
            },
          ]
      await updateProvider(provider.id, { models: nextModels })
      onSaved()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  // 防 input selection 拖到 overlay 释放误关闭：mousedown 起点 + mouseup 都在 overlay 才算外部点击
  const downOnOverlayRef = useRef(false)
  const onOverlayMouseDown = (e: MouseEvent<HTMLDivElement>) => {
    downOnOverlayRef.current = e.target === e.currentTarget
  }
  const onOverlayMouseUp = (e: MouseEvent<HTMLDivElement>) => {
    if (downOnOverlayRef.current && e.target === e.currentTarget) onClose()
    downOnOverlayRef.current = false
  }

  const node = (
    <div className="lr-mm-overlay" onMouseDown={onOverlayMouseDown} onMouseUp={onOverlayMouseUp}>
      <div className="lr-mm-panel" style={{ width: 'min(480px, 94vw)' }}>
        <header className="lr-mm-header">
          <div className="lr-mm-header__text">
            <div className="lr-mm-header__title">
              {editing ? '编辑模型' : '手动添加模型'}
            </div>
            <div className="lr-mm-header__sub">
              {provider.name}{editing && existing ? ` · ${existing.id}` : ''}
            </div>
          </div>
          <button type="button" className="lr-mm-close" onClick={onClose} title="关闭 (Esc)">
            <X size={16} />
          </button>
        </header>

        <div className="lr-addform">
          <div className="lr-addform__field">
            <label className="lr-addform__label">模型 ID <span className="lr-addform__req">*</span></label>
            <input
              type="text"
              value={id}
              placeholder="如 gpt-4o-mini / qwen2.5-coder:7b"
              onChange={(e) => setId(e.target.value)}
              autoFocus
            />
            {idConflict && <div className="lr-addform__err">该 ID 在 {provider.name} 下已存在</div>}
            <div className="lr-addform__hint">调用时传给 API 的实际模型名</div>
          </div>

          <div className="lr-addform__field">
            <label className="lr-addform__label">显示名</label>
            <input
              type="text"
              value={name}
              placeholder={id || '留空则使用 ID'}
              onChange={(e) => setName(e.target.value)}
            />
            <div className="lr-addform__hint">选择器里显示的名字，可以用中文别名</div>
          </div>

          <div className="lr-addform__field">
            <label className="lr-addform__label" htmlFor="lr-model-context-window">上下文窗口</label>
            <div className="lr-addform__context-row">
              <select
                id="lr-model-context-window"
                value={contextMode}
                onChange={(e) => setContextMode(e.target.value)}
              >
                <option value="auto">自动识别（推荐）</option>
                <option value="32000">32K</option>
                <option value="64000">64K</option>
                <option value="128000">128K</option>
                <option value="200000">200K</option>
                <option value="400000">400K</option>
                <option value="1000000">1M</option>
                <option value="custom">自定义</option>
              </select>
              {contextMode === 'custom' && (
                <input
                  type="number"
                  min={1000}
                  max={4000000}
                  step={1000}
                  required
                  inputMode="numeric"
                  aria-label="自定义上下文 token 数"
                  value={customContext}
                  placeholder="例如 1050000"
                  onChange={(e) => setCustomContext(e.target.value)}
                />
              )}
            </div>
            <div className="lr-addform__hint">
              自动模式读取服务商数据或官方模型目录；手动设置只影响客户端预算与压缩，不会改变上游能力。
            </div>
          </div>

          {error && (
            <div className="lr-addform__err lr-addform__err--block">
              <AlertCircle size={13} /> {error}
            </div>
          )}
        </div>

        <footer className="lr-mm-footer">
          <div className="lr-mm-footer__actions">
            <button type="button" className="lr-mm-btn ghost" onClick={onClose} disabled={saving}>取消</button>
            <button type="button" className="lr-mm-btn primary" onClick={onSubmit} disabled={!canSubmit}>
              {saving ? <Loader2 size={13} className="spin" /> : <Check size={13} />}
              {editing ? '保存' : '添加'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )

  return createPortal(node, document.body)
}
