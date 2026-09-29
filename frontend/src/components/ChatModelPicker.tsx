/**
 * 模型切换下拉选择器（主聊天页只读视图）。
 *
 * 行为：
 * - 列出 ProviderSettings 与已配置插件启用的所有模型，按 provider 分组
 * - 点行切换当前模型；点底部"管理模型"跳到设置页
 * - 具体开启/关闭哪些模型在"设置中心"与"算力账号插件配置"中自由勾选控制
 *
 * UI：Portal 渲染避免父级裁剪、毛玻璃面板、能力 chip、搜索 + 键盘导航。
 */
import {
  memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState,
  type KeyboardEvent as ReactKeyboardEvent, type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import { Check, ChevronDown, Search, Settings2, Sparkles } from 'lucide-react'
import type { Model } from '../api/chat'
import { CHAT_UI_TEXT } from './chatUiText'
import { getBrandName } from './BrandIcon'
import { ProviderAvatar, type ProviderLike } from './ProviderAvatar'
import {
  resolveCapabilities, CAPABILITY_COLORS, CAPABILITY_LABELS, type Capability,
} from '../utils/inferModelCapabilities'

/** 从 Model 构造 ProviderAvatar 需的 ProviderLike，让头像决策链走同一套逻辑。
 *  Model 没 base_url，只是 provider id + provider_name + transport；足够走智能识别 + 生成头像。 */
function modelToProviderLike(m: Model | null | undefined): ProviderLike {
  return {
    id: m?.provider || '',
    name: m?.provider_name || '',
    transport: m?.transport || undefined,
  }
}

/* ----------------------------------------------------------------------------
 *  helpers
 * ------------------------------------------------------------------------- */

function isFreeModel(m: Model): boolean {
  const blob = `${m.id} ${m.name ?? ''}`.toLowerCase()
  return blob.includes(':free') || /\bfree\b/.test(blob)
}

function formatContextLength(tokens?: number | null): string | null {
  if (!tokens || tokens <= 0) return null
  if (tokens >= 1_000_000) {
    const millions = tokens / 1_000_000
    return `${Number.isInteger(millions) ? millions : millions.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')}M`
  }
  return `${Math.round(tokens / 1_000)}K`
}

function splitByMatch(text: string, query: string): Array<{ s: string; hit: boolean }> {
  if (!query.trim()) return [{ s: text, hit: false }]
  const lc = text.toLowerCase()
  const q = query.toLowerCase()
  const out: Array<{ s: string; hit: boolean }> = []
  let i = 0
  while (i < text.length) {
    const idx = lc.indexOf(q, i)
    if (idx === -1) { out.push({ s: text.slice(i), hit: false }); break }
    if (idx > i) out.push({ s: text.slice(i, idx), hit: false })
    out.push({ s: text.slice(idx, idx + q.length), hit: true })
    i = idx + q.length
  }
  return out
}

const HighlightedText = memo(function HighlightedText({
  text, query, className, title,
}: { text: string; query: string; className?: string; title?: string }) {
  const parts = useMemo(() => splitByMatch(text, query), [text, query])
  return (
    <span className={className} title={title}>
      {parts.map((p, i) =>
        p.hit ? <mark key={i} className="lr-model-row__hl">{p.s}</mark> : <span key={i}>{p.s}</span>
      )}
    </span>
  )
})

/** 按 provider 分组（保持源顺序）。group 顺带记录一条 providerName，
 *  用于分组 header；优先取组内首个 Model.provider_name（用户填的服务商名），
 *  取不到则由调用方 fallback 到 getBrandName(provider_id)。 */
function groupByProvider(
  models: Model[],
): Array<{ provider: string; providerName: string; items: Model[] }> {
  const buckets = new Map<string, Model[]>()
  const names = new Map<string, string>()
  const order: string[] = []
  for (const m of models) {
    const key = m.provider || ''
    if (!buckets.has(key)) {
      buckets.set(key, [])
      order.push(key)
    }
    buckets.get(key)!.push(m)
    if (!names.has(key) && m.provider_name) names.set(key, m.provider_name)
  }
  return order.map((p) => ({
    provider: p,
    providerName: names.get(p) || '',
    items: buckets.get(p)!,
  }))
}

/* ----------------------------------------------------------------------------
 *  Props
 * ------------------------------------------------------------------------- */

interface ChatModelPickerProps {
  currentModelMeta: Model | null
  customModels: Model[]
  selectedModel: string
  selectedProvider: string
  showModelPicker: boolean
  modelBtnRef: RefObject<HTMLDivElement>
  pickerPanelRef: RefObject<HTMLDivElement>
  getModelProvider: (model?: { provider?: string | null }) => string
  onTogglePicker: () => void
  onSelectCustomModel: (model: Model) => void
  /** 跳转到 ProviderSettings 进行添加/编辑 */
  onOpenModelSettings: () => void
}

/* ----------------------------------------------------------------------------
 *  单行
 * ------------------------------------------------------------------------- */

interface ItemProps {
  model: Model
  idx: number
  isActive: boolean
  isHighlighted: boolean
  highlightQuery: string
  onSelect: (model: Model) => void
  onMouseEnter?: (idx: number) => void
}

const ModelRow = memo(function ModelRow({
  model, idx, isActive, isHighlighted, highlightQuery, onSelect, onMouseEnter,
}: ItemProps) {
  const providerId = model.provider || ''
  const providerLike = useMemo(() => modelToProviderLike(model), [model])
  const providerLabel = model.provider_name || getBrandName(providerId)
  const caps = useMemo<Capability[]>(() => resolveCapabilities(model), [model.id, model.tags])
  const free = useMemo(() => isFreeModel(model), [model])
  const contextLabel = formatContextLength(model.context_length)

  const handleSelect = useCallback(() => {
    onSelect(model)
  }, [model, onSelect])

  const handleMouseEnter = useCallback(() => {
    onMouseEnter?.(idx)
  }, [idx, onMouseEnter])

  return (
    <button
      type="button"
      role="option"
      aria-selected={isActive}
      className={
        'lr-model-row' +
        (isActive ? ' active' : '') +
        (isHighlighted ? ' highlighted' : '')
      }
      onMouseEnter={handleMouseEnter}
      onClick={handleSelect}
    >
      <ProviderAvatar provider={providerLike} size={22} className="lr-model-row__icon" />
      <div className="lr-model-row__body">
        <div className="lr-model-row__top">
          <HighlightedText
            text={model.name || model.id}
            query={highlightQuery}
            className="lr-model-row__name"
            title={model.id}
          />
          {contextLabel && (
            <span
              className="lr-chip lr-chip--context"
              title={`上下文窗口：${model.context_length?.toLocaleString()} tokens${model.context_length_source === 'inferred' ? '（自动识别）' : ''}`}
            >
              {contextLabel}
            </span>
          )}
          {free && (
            <span className="lr-chip lr-chip--free" title="免费模型">
              <Sparkles size={9} strokeWidth={2.5} />免费
            </span>
          )}
          {caps.slice(0, 3).map((c) => (
            <span key={c} className={`lr-chip lr-chip--cap ${CAPABILITY_COLORS[c]}`}>
              {CAPABILITY_LABELS[c]}
            </span>
          ))}
        </div>
        <div className="lr-model-row__sub">
          <span className="lr-model-row__provider">{providerLabel}</span>
          <span className="lr-model-row__id">{model.id}</span>
        </div>
      </div>
      {isActive && <Check size={14} className="lr-model-row__check" />}
    </button>
  )
})

/* ----------------------------------------------------------------------------
 *  主组件
 * ------------------------------------------------------------------------- */

export default function ChatModelPicker(props: ChatModelPickerProps) {
  const {
    currentModelMeta, customModels, selectedModel, selectedProvider,
    showModelPicker, modelBtnRef, pickerPanelRef,
    getModelProvider, onTogglePicker, onSelectCustomModel, onOpenModelSettings,
  } = props

  const currentProviderLike = useMemo(
    () => modelToProviderLike(currentModelMeta),
    [currentModelMeta],
  )
  const currentBrandName =
    currentModelMeta?.provider_name || getBrandName(currentModelMeta?.provider)
  const currentContextLabel = formatContextLength(currentModelMeta?.context_length)

  const [anchorRect, setAnchorRect] = useState<DOMRect | null>(null)

  useLayoutEffect(() => {
    if (!showModelPicker) {
      setAnchorRect(null)
      return
    }
    const update = (e?: Event) => {
      if (e?.target && pickerPanelRef.current?.contains(e.target as Node)) {
        return
      }
      const el = modelBtnRef.current
      if (el) {
        const nextRect = el.getBoundingClientRect()
        setAnchorRect((prev) => {
          if (
            prev &&
            prev.top === nextRect.top &&
            prev.left === nextRect.left &&
            prev.width === nextRect.width &&
            prev.height === nextRect.height
          ) {
            return prev
          }
          return nextRect
        })
      }
    }
    update()
    window.addEventListener('resize', update)
    window.addEventListener('scroll', update, true)
    return () => {
      window.removeEventListener('resize', update)
      window.removeEventListener('scroll', update, true)
    }
  }, [showModelPicker, modelBtnRef, pickerPanelRef])

  const [query, setQuery] = useState('')
  const [highlight, setHighlight] = useState(0)
  const searchRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (showModelPicker) {
      setQuery('')
      setHighlight(0)
      listRef.current?.scrollTo({ top: 0 })
      setTimeout(() => searchRef.current?.focus(), 10)
    }
  }, [showModelPicker])

  const filter = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return (_: Model) => true
    return (m: Model) =>
      (m.id || '').toLowerCase().includes(q) ||
      (m.name || '').toLowerCase().includes(q) ||
      (m.provider || '').toLowerCase().includes(q) ||
      (m.provider_name || '').toLowerCase().includes(q) ||
      getBrandName(m.provider).toLowerCase().includes(q)
  }, [query])

  const filteredCustom = useMemo(() => customModels.filter(filter), [customModels, filter])
  const customGrouped = useMemo(() => groupByProvider(filteredCustom), [filteredCustom])

  useEffect(() => {
    if (highlight >= filteredCustom.length) {
      setHighlight(Math.max(0, filteredCustom.length - 1))
    }
  }, [filteredCustom.length, highlight])

  const scrollToRow = useCallback((idx: number) => {
    const el = listRef.current?.querySelector<HTMLButtonElement>(`[data-row-idx="${idx}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [])

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHighlight((h) => {
        const next = Math.min(filteredCustom.length - 1, h + 1)
        scrollToRow(next)
        return next
      })
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHighlight((h) => {
        const next = Math.max(0, h - 1)
        scrollToRow(next)
        return next
      })
    } else if (e.key === 'Enter') {
      const m = filteredCustom[highlight]
      if (m) { e.preventDefault(); onSelectCustomModel(m) }
    } else if (e.key === 'Escape') {
      e.preventDefault()
      onTogglePicker()
    }
  }

  const handleSelectModel = useCallback((m: Model) => {
    onSelectCustomModel(m)
  }, [onSelectCustomModel])

  const handleMouseEnterRow = useCallback((idx: number) => {
    setHighlight(idx)
  }, [])

  const trigger = (
    <div className="model-selector-anchor">
      <div
        ref={modelBtnRef}
        className={`model-selector ${showModelPicker ? 'open' : ''}`}
        onClick={onTogglePicker}
        role="button"
        aria-expanded={showModelPicker}
        aria-haspopup="listbox"
      >
        <ProviderAvatar provider={currentProviderLike} size={16} badge={false} className="model-current-brand" />
        <span className="model-current-name">
          {currentModelMeta?.name || CHAT_UI_TEXT.defaultModelName}
        </span>
        {currentContextLabel && (
          <span
            className="model-current-context"
            title={`上下文窗口：${currentModelMeta?.context_length?.toLocaleString()} tokens`}
          >
            {currentContextLabel}
          </span>
        )}
        {currentModelMeta && (
          <span className="model-current-provider" title={`Provider: ${currentBrandName}`}>
            · {currentBrandName}
          </span>
        )}
        <ChevronDown size={12} />
      </div>
    </div>
  )

  const panel = showModelPicker && anchorRect && (() => {
    const panelWidth = Math.max(340, Math.min(420, anchorRect.width * 2.5))
    const spaceAbove = anchorRect.top
    const spaceBelow = window.innerHeight - anchorRect.bottom
    const placeAbove = spaceAbove > 280 || spaceAbove > spaceBelow
    const left = Math.max(12, Math.min(window.innerWidth - panelWidth - 12, anchorRect.left))
    const style: React.CSSProperties = {
      position: 'fixed',
      left,
      width: panelWidth,
      zIndex: 10000,
      ...(placeAbove
        ? { bottom: window.innerHeight - anchorRect.top + 8 }
        : { top: anchorRect.bottom + 8 }),
    }

    return createPortal(
      <div
        ref={pickerPanelRef}
        className={`lr-model-panel ${placeAbove ? 'place-above' : 'place-below'}`}
        style={style}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
        role="listbox"
        tabIndex={-1}
      >
        <div className="lr-model-panel__search">
          <Search size={14} />
          <input
            ref={searchRef}
            type="text"
            placeholder="搜索模型 / Provider · ↑↓ 选择 · Enter 确认"
            value={query}
            onChange={(e) => { setQuery(e.target.value); setHighlight(0) }}
            onKeyDown={onKeyDown}
          />
          <span className="lr-model-panel__kbd">/</span>
        </div>

        <div className="lr-model-panel__list" ref={listRef}>
          {filteredCustom.length === 0 && (
            <div className="lr-model-panel__empty">
              {customModels.length === 0
                ? '尚未配置任何模型，点下方进入模型设置开始添加'
                : '没有匹配的模型'}
            </div>
          )}

          {customGrouped.map((group) => (
            <div key={`g-${group.provider}`}>
              <div className="lr-model-panel__group">
                <ProviderAvatar provider={modelToProviderLike(group.items[0])} size={12} badge={false} />
                <span>{group.providerName || getBrandName(group.provider)}</span>
                <span className="lr-model-panel__group-count">{group.items.length}</span>
              </div>
              {group.items.map((m) => {
                const idx = filteredCustom.indexOf(m)
                const isActive =
                  selectedModel === m.id &&
                  (selectedProvider === getModelProvider(m) || !selectedProvider)
                return (
                  <div key={`c-${m.id}-${getModelProvider(m)}`} data-row-idx={idx}>
                    <ModelRow
                      model={m}
                      idx={idx}
                      isActive={isActive}
                      isHighlighted={idx === highlight}
                      highlightQuery={query}
                      onSelect={handleSelectModel}
                      onMouseEnter={handleMouseEnterRow}
                    />
                  </div>
                )
              })}
            </div>
          ))}
        </div>

        <button
          type="button"
          className="lr-model-panel__add"
          onClick={onOpenModelSettings}
          title="去模型设置页添加 / 编辑 / 删除模型"
        >
          <Settings2 size={14} />
          <span>管理模型…</span>
        </button>
      </div>,
      document.body,
    )
  })()

  return <>{trigger}{panel}</>
}
