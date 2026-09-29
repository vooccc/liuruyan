/**
 * 模型发现 + 批量导入弹窗（P3 核心交互）。
 *
 * 设计参考 cherry-studio ManageModelsPopup：
 * - 顶部搜索 + tab 筛选（全部 / 免费 / 推理 / 视觉 / 函数调用 / embedding / rerank）
 * - 网格列表显示发现的所有模型（带 free 徽章 + 能力 chip）
 * - 已在 provider.models[] 里的标记"已添加"
 * - 批量全选/全不选 / 勾选导入
 * - 毛玻璃面板、Portal、Esc 关闭
 */
import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { createPortal } from 'react-dom'
import {
  AlertTriangle, Check, ChevronDown, ChevronRight, Filter, Globe, Loader2, Minus, Plus, Search, Sparkles, X,
} from 'lucide-react'

import {
  discoverModels, updateProvider,
  type DiscoveredModel, type ModelEntry, type ProviderView,
} from '../../api/providers'
import { ProviderAvatar } from '../ProviderAvatar'
import { groupModelsByDefault } from '../../utils/modelGroup'
import { BACKEND_TAG_LABELS, BACKEND_TAG_COLORS } from '../../utils/inferModelCapabilities'

/* ============================================================================
   类型
   ============================================================================ */

type FilterTab = 'all' | 'free' | 'vision' | 'websearch' | 'reasoning' | 'function_calling' | 'embedding' | 'rerank'

const TAB_DEFS: Array<{ id: FilterTab; label: string; hint?: string }> = [
  { id: 'all', label: '全部' },
  { id: 'free', label: '免费', hint: '仅显示免费模型' },
  { id: 'vision', label: '视觉' },
  { id: 'websearch', label: '联网', hint: '支持联网搜索的模型（如 *-online、perplexity sonar）' },
  { id: 'reasoning', label: '推理' },
  { id: 'function_calling', label: '工具', hint: '支持函数调用 / tools' },
  { id: 'embedding', label: '嵌入' },
  { id: 'rerank', label: '重排' },
]

function matchFilter(m: DiscoveredModel, tab: FilterTab): boolean {
  if (tab === 'all') return true
  if (tab === 'free') return m.is_free
  return (m.tags || []).includes(tab)
  // websearch / reasoning / vision / function_calling / embedding / rerank 统一走 m.tags
}

// tag 中文 label + 色板：已抽到 utils/inferModelCapabilities.ts BACKEND_TAG_LABELS /
// BACKEND_TAG_COLORS（与 ProviderSettings 已启用模型列表共用，保证 UI 视觉一致）。
const TAG_LABELS = BACKEND_TAG_LABELS
const TAG_COLOR_CLASS = BACKEND_TAG_COLORS

/* ============================================================================
   Props
   ============================================================================ */

interface ManageModelsPopupProps {
  provider: ProviderView
  /** 该 provider 已启用的 model id 集合，用来标记"已添加"状态 */
  existingIds: Set<string>
  onClose: () => void
  /** 成功导入若干模型后回调（用来让父组件刷新 providers 列表） */
  onImported: (addedCount: number) => void
}

/* ============================================================================
   主组件
   ============================================================================ */

export default function ManageModelsPopup({
  provider, existingIds, onClose, onImported,
}: ManageModelsPopupProps) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [models, setModels] = useState<DiscoveredModel[]>([])
  const [discoverStats, setDiscoverStats] = useState<{ total: number; free: number }>({ total: 0, free: 0 })

  const [tab, setTab] = useState<FilterTab>('all')
  const [query, setQuery] = useState('')
  // selected 初始 = 已添加的（让用户能在弹窗里取消勾选 → 保存时会被移除）
  const [selected, setSelected] = useState<Set<string>>(() => new Set(existingIds))
  const [importing, setImporting] = useState(false)

  /* ---- 拉取 ---------------------------------------------------------- */

  const fetchOnce = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await discoverModels(provider.id)
      setModels(res.models)
      setDiscoverStats({ total: res.count, free: res.free_count })
    } catch (e) {
      setError(e instanceof Error ? e.message : '拉取失败')
    } finally {
      setLoading(false)
    }
  }, [provider.id])

  useEffect(() => { void fetchOnce() }, [fetchOnce])

  /* ---- Esc 关闭 ------------------------------------------------------ */

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  /* ---- 过滤 ---------------------------------------------------------- */

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return models.filter((m) => {
      if (!matchFilter(m, tab)) return false
      if (!q) return true
      return (m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
    })
  }, [models, tab, query])

  const counts = useMemo(() => {
    const out: Record<FilterTab, number> = {
      all: models.length,
      free: 0, reasoning: 0, vision: 0, websearch: 0, function_calling: 0, embedding: 0, rerank: 0,
    }
    for (const m of models) {
      if (m.is_free) out.free++
      for (const t of (m.tags || [])) if (t in out) (out as Record<string, number>)[t]++
    }
    return out
  }, [models])

  /* ---- 勾选操作 ------------------------------------------------------ */

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  // 全选筛选项：把当前 filtered 中的都勾上（保留未在 filtered 中的原选中项）
  const selectAllFiltered = () => {
    setSelected((prev) => {
      const next = new Set(prev)
      filtered.forEach((m) => next.add(m.id))
      return next
    })
  }
  // 只清空筛选项里的选中项（避免误清未显示的原已勾项）
  const clearFilteredSelection = () => {
    setSelected((prev) => {
      const next = new Set(prev)
      filtered.forEach((m) => next.delete(m.id))
      return next
    })
  }

  /* ---- 保存：diff 后一次性 updateProvider ----------------------- */

  /** 当前 diff：相对于弹窗打开时的 existingIds。
   *  - toAddIds：选中但原本不在 provider 里的
   *  - toRemoveIds：原本在 provider 里但现在未选中的 */
  const diff = useMemo(() => {
    const toAdd: string[] = []
    const toRemove: string[] = []
    selected.forEach((id) => { if (!existingIds.has(id)) toAdd.push(id) })
    existingIds.forEach((id) => { if (!selected.has(id)) toRemove.push(id) })
    return { toAdd, toRemove }
  }, [selected, existingIds])

  const hasChanges = diff.toAdd.length > 0 || diff.toRemove.length > 0

  const discoveredToEntry = (m: DiscoveredModel): ModelEntry => ({
    id: m.id,
    name: m.name || m.id,
    tags: [...(m.tags || [])],
    icon: '',
    added_by_user: true,
    pricing: m.pricing ?? null,
    context_length: m.context_length ?? null,
  })

  const doSave = async () => {
    if (!hasChanges) { onClose(); return }
    setImporting(true)
    try {
      // 拼出保存后的完整 models：
      // 1. provider.models 中仍在 selected 里的（保留原元数据如 icon/added_by_user）
      // 2. selected 中但本不在 provider 中的，从di发现列表拿
      const kept = provider.models.filter((m) => selected.has(m.id))
      const adding = diff.toAdd
        .map((id) => models.find((m) => m.id === id))
        .filter((m): m is DiscoveredModel => Boolean(m))
        .map(discoveredToEntry)
      const finalModels: ModelEntry[] = [...kept, ...adding]
      await updateProvider(provider.id, { models: finalModels })
      onImported(diff.toAdd.length)
      setTimeout(onClose, 200)
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败')
    } finally {
      setImporting(false)
    }
  }

  /* ---- 渲染 ---------------------------------------------------------- */

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
      <div className="lr-mm-panel">
        {/* 头部 */}
        <header className="lr-mm-header">
          <ProviderAvatar provider={provider} size={22} />
          <div className="lr-mm-header__text">
            <div className="lr-mm-header__title">{provider.name} · 模型管理</div>
            <div className="lr-mm-header__sub">
              {!loading && !error && (
                <>
                  共 <b>{discoverStats.total}</b> 个模型
                  {discoverStats.free > 0 && (
                    <>，其中 <b className="lr-mm-freecount">{discoverStats.free}</b> 个免费</>
                  )}
                </>
              )}
            </div>
          </div>
          <button type="button" className="lr-mm-close" onClick={onClose} title="关闭 (Esc)">
            <X size={16} />
          </button>
        </header>

        {/* 搜索 + 筛选 */}
        <div className="lr-mm-toolbar">
          <div className="lr-mm-search">
            <Search size={14} />
            <input
              type="text"
              autoFocus
              value={query}
              placeholder="搜索模型 ID / 显示名"
              onChange={(e) => setQuery(e.target.value)}
            />
            {query && (
              <button type="button" onClick={() => setQuery('')} title="清空">
                <X size={12} />
              </button>
            )}
          </div>
          <div className="lr-mm-tabs" role="tablist">
            {TAB_DEFS.map((def) => (
              <button
                key={def.id}
                type="button"
                role="tab"
                aria-selected={tab === def.id}
                className={`lr-mm-tab ${tab === def.id ? 'active' : ''}`}
                onClick={() => setTab(def.id)}
                title={def.hint}
              >
                {def.id === 'free' && <Sparkles size={11} />}
                {def.id === 'all' && <Filter size={11} />}
                <span>{def.label}</span>
                <span className="lr-mm-tab__count">{counts[def.id]}</span>
              </button>
            ))}
          </div>
        </div>

        {/* 批量操作条 */}
        <div className="lr-mm-bulk">
          <span className="lr-mm-bulk__meta">
            显示 {filtered.length}
            {selected.size > 0 && (
              <> · 已勾 <b>{selected.size}</b></>
            )}
            {hasChanges && (
              <span className="lr-mm-bulk__diff">
                {diff.toAdd.length > 0 && <> · 将加入 <b className="lr-mm-diff-add">+{diff.toAdd.length}</b></>}
                {diff.toRemove.length > 0 && <> · 将移除 <b className="lr-mm-diff-rm">-{diff.toRemove.length}</b></>}
              </span>
            )}
          </span>
          <div className="lr-mm-bulk__actions">
            <button type="button" className="lr-mm-btn ghost" onClick={selectAllFiltered}
              disabled={loading || filtered.length === 0}>全选筛选项</button>
            <button type="button" className="lr-mm-btn ghost" onClick={clearFilteredSelection}
              disabled={loading || filtered.length === 0}>取消筛选项</button>
          </div>
        </div>

        {/* 列表 */}
        <div className="lr-mm-list">
          {loading && (
            <div className="lr-mm-state">
              <Loader2 size={18} className="spin" />
              <span>正在从 {provider.name} 拉取模型…</span>
            </div>
          )}
          {error && !loading && (
            <div className="lr-mm-state error">
              <AlertTriangle size={16} />
              <span>{error}</span>
              <button type="button" className="lr-mm-btn primary" onClick={fetchOnce}>重试</button>
            </div>
          )}
          {!loading && !error && filtered.length === 0 && (
            <div className="lr-mm-state muted">
              <Globe size={16} />
              <span>没有匹配的模型</span>
            </div>
          )}
          {!loading && !error && (
            <ModelDiscoverList
              filtered={filtered}
              providerId={provider.id}
              existingIds={existingIds}
              selected={selected}
              onToggle={toggle}
              onSetSelected={setSelected}
            />
          )}
        </div>

        {/* 底部确认 */}
        <footer className="lr-mm-footer">
          <div className="lr-mm-footer__actions">
            <button type="button" className="lr-mm-btn ghost" onClick={onClose} disabled={importing}>取消</button>
            <button
              type="button"
              className="lr-mm-btn primary"
              onClick={doSave}
              disabled={importing || !hasChanges}
            >
              {importing ? <Loader2 size={13} className="spin" /> : <Check size={13} />}
              {hasChanges
                ? <>保存{diff.toAdd.length > 0 && ` +${diff.toAdd.length}`}{diff.toRemove.length > 0 && ` −${diff.toRemove.length}`}</>
                : '无修改'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )

  return createPortal(node, document.body)
}


/* ============================================================================
   ModelDiscoverList: 按 vendor 前缀分组（cherry 同款）。多组时折叠面板，
   单组时平铺。每组分组头带"全选/取消"，方便用户一键勾选整组（OpenRouter
   场景下"把整个 anthropic 组都加进来" 是高频操作）。
   ============================================================================ */

function ModelDiscoverList({
  filtered, providerId, existingIds, selected, onToggle, onSetSelected,
}: {
  filtered: DiscoveredModel[]
  providerId: string
  existingIds: Set<string>
  selected: Set<string>
  onToggle: (id: string) => void
  onSetSelected: (next: Set<string>) => void
}) {
  const groups = useMemo(() => groupModelsByDefault(filtered, providerId), [filtered, providerId])
  const groupKeys = useMemo(() => Object.keys(groups), [groups])

  // 折叠态：默认全展开
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const toggleGroup = (k: string) => {
    setCollapsed((s) => {
      const n = new Set(s)
      if (n.has(k)) n.delete(k); else n.add(k)
      return n
    })
  }

  const renderRow = (m: DiscoveredModel) => {
    const checked = selected.has(m.id)
    const wasExisting = existingIds.has(m.id)
    // 状态语义（用于侧边色条提示“这次会变化”）：
    //   wasExisting && !checked  → will-remove（原有但取消了）
    //   !wasExisting && checked  → will-add（原没但勾上了）
    //   wasExisting && checked   → unchanged-kept（原有仍保留）
    //   !wasExisting && !checked → unchanged-none（未动）
    const willRemove = wasExisting && !checked
    const willAdd = !wasExisting && checked
    const tagsShown = (m.tags || []).filter((t) => t !== 'free').slice(0, 4)
    return (
      <button
        key={m.id}
        type="button"
        className={
          'lr-mm-row' +
          (checked ? ' selected' : '') +
          (willRemove ? ' will-remove' : '') +
          (willAdd ? ' will-add' : '')
        }
        onClick={() => onToggle(m.id)}
        title={m.id}
      >
        {/* 左侧状态图标：选中→−（点击取消），未选→+（点击加入） */}
        <span className={'lr-mm-row__check' + (checked ? ' is-checked' : ' is-add')}>
          {checked ? <Minus size={14} /> : <Plus size={14} />}
        </span>
        <span className="lr-mm-row__name">{m.name || m.id}</span>
        <span className="lr-mm-row__id">{m.id}</span>
        <span className="lr-mm-row__tags">
          {m.is_free && (
            <span className="lr-chip lr-chip--free" title={`免费 (${m.free_reason})`}>
              <Sparkles size={9} strokeWidth={2.5} />免费
            </span>
          )}
          {tagsShown.map((t) => (
            <span key={t} className={`lr-chip lr-chip--cap ${TAG_COLOR_CLASS[t] ?? 'cap-gray'}`}>
              {TAG_LABELS[t] ?? t}
            </span>
          ))}
          {m.context_length && (
            <span className="lr-chip lr-chip--cap cap-gray">
              {m.context_length >= 1000000
                ? `${(m.context_length / 1000000).toFixed(0)}M`
                : `${Math.round(m.context_length / 1000)}K`}
            </span>
          )}
        </span>
        {willAdd && <span className="lr-mm-row__badge add">将加入</span>}
        {willRemove && <span className="lr-mm-row__badge rm">将移除</span>}
      </button>
    )
  }

  // 单组：直接平铺
  if (groupKeys.length <= 1) {
    return <>{filtered.map(renderRow)}</>
  }

  // 整组选中状态（组内所有模型都勾了=all，都未勾=none，部分=partial）
  const groupSelectionState = (gk: string): 'none' | 'partial' | 'all' => {
    const ms = groups[gk]
    if (ms.length === 0) return 'none'
    const picked = ms.filter((m) => selected.has(m.id)).length
    if (picked === 0) return 'none'
    if (picked === ms.length) return 'all'
    return 'partial'
  }
  const toggleGroupSelection = (gk: string, e: MouseEvent) => {
    e.stopPropagation()  // 不触发折叠
    const ms = groups[gk]
    const state = groupSelectionState(gk)
    const next = new Set(selected)
    if (state === 'all') {
      // 已全选 → 一键清除本组
      ms.forEach((m) => next.delete(m.id))
    } else {
      // 未全选 → 全部勾上
      ms.forEach((m) => next.add(m.id))
    }
    onSetSelected(next)
  }

  return (
    <>
      {groupKeys.map((gk) => {
        const ms = groups[gk]
        const isCollapsed = collapsed.has(gk)
        const state = groupSelectionState(gk)
        return (
          <div key={gk} className="lr-mm-group">
            <div
              className="lr-mm-group__head"
              onClick={() => toggleGroup(gk)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleGroup(gk) } }}
            >
              {isCollapsed
                ? <ChevronRight size={13} className="lr-mm-group__chev" />
                : <ChevronDown size={13} className="lr-mm-group__chev" />}
              <span className="lr-mm-group__name">{gk}</span>
              <span className="lr-mm-group__count">{ms.length}</span>
              <button
                type="button"
                className={`lr-mm-group__pickall ${state === 'all' ? 'is-all' : state === 'partial' ? 'is-partial' : ''}`}
                onClick={(e) => toggleGroupSelection(gk, e)}
                title={state === 'all' ? '取消选择本组' : '选择本组全部'}
              >
                {state === 'all' ? '取消本组' : state === 'partial' ? '全选本组' : '选本组'}
              </button>
            </div>
            {!isCollapsed && (
              <div className="lr-mm-group__body">{ms.map(renderRow)}</div>
            )}
          </div>
        )
      })}
    </>
  )
}
