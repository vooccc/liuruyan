/**
 * 新 Provider 设置页（对应新 /providers API）。
 *
 * 跟老 ModelsSettings.tsx 并存：老页留着兼容，新页是 P3 的主要交付。
 *
 * 结构：
 *   左栏：provider 列表（内置 + 自定义），带搜索
 *   右栏：详情
 *     - 连接配置：base_url（freeze 时锁）/ api_key / extra_headers
 *     - 能力入口：打开 ManageModelsPopup 做模型发现 + 筛选 + 批量导入
 *     - 已启用模型列表：每项可单独删除
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle, AlertTriangle, Check, ChevronDown, ChevronRight, Copy, ExternalLink, Eye, EyeOff, GripVertical, Loader2, Lock,
  Minus, Pencil, Plus, RefreshCw, Search, Settings2, Star, Trash2, Unlock, X,
} from 'lucide-react'

import {
  deleteProvider, listProviders, reorderProviders, setActiveModel, updateProvider,
  type ProviderView,
} from '../../api/providers'
import {
  testModelConnection, testModelConnectionDeep,
  MODELS_CATALOG_CHANGED_EVENT,
  type RelayDiagnostics, type RelaySignal,
} from '../../api/chat'
import { getBrandColor } from '../BrandIcon'
import { ProviderAvatar } from '../ProviderAvatar'
import { groupModelsByDefault } from '../../utils/modelGroup'
import { BACKEND_TAG_LABELS, BACKEND_TAG_COLORS } from '../../utils/inferModelCapabilities'
import { useConfirmDialog, useAlertDialog } from '../ConfirmDialog'
import AddModelDialog from './AddModelDialog'
import AddProviderDialog from './AddProviderDialog'
import ExtraHeadersDialog from './ExtraHeadersDialog'
import ManageModelsPopup from './ManageModelsPopup'
import ProviderLogoEditor from './ProviderLogoEditor'
import TransportDropdown from './TransportDropdown'
import { useResizableWidth, ResizeHandle } from '../useResizableWidth'
import { useCopyFeedback } from '../useCopyFeedback'

/* ============================================================================
   主组件
   ============================================================================ */

export default function ProviderSettings() {
  const [providers, setProviders] = useState<ProviderView[]>([])
  const [current, setCurrent] = useState<{ provider: string; model: string }>({ provider: '', model: '' })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string>('')
  const [showAddProvider, setShowAddProvider] = useState(false)
  // 连通性缓存：跨详情切换、跨组件 mount 都保留检测结果。
  // localStorage 持久化（key='lr.providerConnCache'），用户改 key/host 时由 detail
  // 主动 invalidate，避免显示过期的绿点。
  const CONN_CACHE_KEY = 'lr.providerConnCache'
  const [connCache, setConnCache] = useState<Record<string, 'ok' | 'failed'>>(() => {
    try {
      const raw = localStorage.getItem(CONN_CACHE_KEY)
      return raw ? JSON.parse(raw) : {}
    } catch {
      return {}
    }
  })
  const persistCache = useCallback((m: Record<string, 'ok' | 'failed'>) => {
    try { localStorage.setItem(CONN_CACHE_KEY, JSON.stringify(m)) } catch { /* quota / disabled — 内存态仍生效 */ }
  }, [])
  const setConnResult = useCallback((id: string, r: 'ok' | 'failed') => {
    setConnCache((m) => {
      if (m[id] === r) return m
      const next = { ...m, [id]: r }
      persistCache(next)
      return next
    })
  }, [persistCache])
  const clearConnResult = useCallback((id: string) => {
    setConnCache((m) => {
      if (!(id in m)) return m
      const next = { ...m }
      delete next[id]
      persistCache(next)
      return next
    })
  }, [persistCache])

  const refresh = useCallback(async () => {
    try {
      const res = await listProviders()
      setProviders(res.providers)
      setCurrent({ provider: res.current_provider, model: res.current_model })
      setError(null)
      // 通知 useChatModelManager 重拉 listModels()，让聊天界面下拉列表立即同步
      // 新增/编辑/删除/导入的 provider 与 model，无需重启应用。
      // 这里 dispatch 会覆盖到所有 onChanged 调用点（add provider / add model /
      // import models / delete model / delete provider / 改 transport / 改名 /
      // 切默认模型 / logo 编辑 等），是单一收口。
      try {
        window.dispatchEvent(new CustomEvent(MODELS_CATALOG_CHANGED_EVENT))
      } catch { /* SSR / 测试环境 window 不存在时静默 */ }
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载 providers 失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  // 默认选中：当前 current_provider，否则第一个启用项
  useEffect(() => {
    if (providers.length === 0) return
    if (selectedId && providers.some((p) => p.id === selectedId)) return
    const target =
      providers.find((p) => p.id === current.provider)?.id
      ?? providers.find((p) => p.enabled)?.id
      ?? providers[0].id
    setSelectedId(target)
  }, [providers, current.provider, selectedId])

  const selected = useMemo(
    () => providers.find((p) => p.id === selectedId) ?? null,
    [providers, selectedId],
  )

  if (loading) {
    return (
      <div className="settings-models settings-loading">
        <Loader2 size={20} className="spin" />
        <span>正在加载 providers…</span>
      </div>
    )
  }

  if (error) {
    return (
      <div className="settings-models settings-loading">
        <AlertCircle size={20} />
        <span>{error}</span>
        <button type="button" onClick={() => { setLoading(true); void refresh() }}>重试</button>
      </div>
    )
  }

  return (
    <div className="settings-models lr-ps">
      <ProviderSidebar
        providers={providers}
        setProviders={setProviders}
        selectedId={selectedId}
        currentProvider={current.provider}
        connCache={connCache}
        onSelect={setSelectedId}
        onAddProvider={() => setShowAddProvider(true)}
      />
      <main className="settings-models-detail">
        {selected ? (
          <ProviderDetail
            provider={selected}
            currentModel={current.model}
            currentProvider={current.provider}
            onConnResult={setConnResult}
            onConnInvalidate={clearConnResult}
            onChanged={refresh}
          />
        ) : (
          <div className="settings-empty-detail">选一个 provider 开始配置</div>
        )}
      </main>
      {showAddProvider && (
        <AddProviderDialog
          onClose={() => setShowAddProvider(false)}
          onCreated={async (pid) => {
            await refresh()
            setSelectedId(pid)
          }}
        />
      )}
    </div>
  )
}

/* 注：「辅助模型」区段已于 2026-05-27 迁出到「辅助功能」面板（原「语音输入」），
   对应组件 `@/components/settings/AuxModelsFooter.tsx`。这里不再渲染。 */

/* ============================================================================
   左栏
   ============================================================================ */

function ProviderSidebar({
  providers, setProviders, selectedId, currentProvider, connCache, onSelect, onAddProvider,
}: {
  providers: ProviderView[]
  setProviders: React.Dispatch<React.SetStateAction<ProviderView[]>>
  selectedId: string
  currentProvider: string
  connCache: Record<string, 'ok' | 'failed'>
  onSelect: (id: string) => void
  onAddProvider: () => void
}) {
  const [query, setQuery] = useState('')
  // 拖拽视觉状态。搜索时禁拖（过滤后顺序跟后端全量不一致）。
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const [dragOverId, setDragOverId] = useState<string | null>(null)
  // 鼠标落在目标 item 的上半 → before；下半 → after；列表尾部空白 → 'tail'
  const [dragOverPos, setDragOverPos] = useState<'before' | 'after' | 'tail' | null>(null)
  // reorder 失败回显
  const [reorderErr, setReorderErr] = useState<string | null>(null)
  const dragEnabled = !query.trim()

  // providers ref：回调需要实时读到最新 providers。
  const providersRef = useRef(providers)
  providersRef.current = providers
  const dragEnabledRef = useRef(dragEnabled)
  dragEnabledRef.current = dragEnabled

  // 统一的 reorder 提交：失败回滚并提示
  const commitReorder = useCallback((next: ProviderView[], prev: ProviderView[]) => {
    setProviders(next)
    reorderProviders(next.map((x) => x.id))
      .then(() => setReorderErr(null))
      .catch((err) => {
        console.error('[ProviderSettings] reorder 失败', err)
        setProviders(prev)
        setReorderErr(err instanceof Error ? err.message : '排序保存失败，已回滚')
        window.setTimeout(() => setReorderErr(null), 4000)
      })
  }, [setProviders])

  // React 合成事件处理非冒泡的 dragover/drop 事件。
  const idFromTarget = (target: Element | null): string | null => {
    if (!target) return null
    const item = target.closest('[data-provider-id]')
    return item ? (item.getAttribute('data-provider-id') || null) : null
  }

  // 按鼠标 Y 在 item 中的位置判定 before/after
  const posInItem = (clientY: number, rect: DOMRect): 'before' | 'after' =>
    clientY < rect.top + rect.height / 2 ? 'before' : 'after'

  const onItemDragStart = useCallback((e: React.DragEvent) => {
    if (!dragEnabledRef.current) return
    const id = idFromTarget(e.target as Element)
    if (!id) return
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', id)
    setDraggingId(id)
    document.body.classList.add('lr-ps-dragging')
  }, [])

  const onItemDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    if (!dragEnabledRef.current) return
    const item = (e.target as Element).closest('[data-provider-id]') as HTMLElement | null
    if (!item) return
    const id = item.getAttribute('data-provider-id')
    if (!id) return
    const pos = posInItem(e.clientY, item.getBoundingClientRect())
    setDragOverId((prev) => (prev === id ? prev : id))
    setDragOverPos((prev) => (prev === pos ? prev : pos))
  }, [])

  const onItemDragEnter = useCallback((e: React.DragEvent) => {
    if (!dragEnabledRef.current) return
    e.preventDefault()
  }, [])

  const onItemDragLeave = useCallback((e: React.DragEvent) => {
    if (!dragEnabledRef.current) return
    const related = e.relatedTarget as Element | null
    const item = e.currentTarget as Element
    if (!related || !item.contains(related)) {
      // 交给 list-level onDragLeave 兜底清理
    }
  }, [])

  const onItemDrop = useCallback((e: React.DragEvent) => {
    if (!dragEnabledRef.current) return
    e.preventDefault()
    e.stopPropagation()
    const fromId = e.dataTransfer.getData('text/plain') || null
    const item = (e.target as Element).closest('[data-provider-id]') as HTMLElement | null
    const toId = item?.getAttribute('data-provider-id') || null
    const pos = item ? posInItem(e.clientY, item.getBoundingClientRect()) : 'before'
    setDraggingId(null)
    setDragOverId(null)
    setDragOverPos(null)
    document.body.classList.remove('lr-ps-dragging')
    if (!fromId || !toId || fromId === toId) return
    const prev = providersRef.current
    const fromIdx = prev.findIndex((x) => x.id === fromId)
    const toIdx = prev.findIndex((x) => x.id === toId)
    if (fromIdx < 0 || toIdx < 0) return
    const next = prev.slice()
    const [moved] = next.splice(fromIdx, 1)
    // 目标位置在移除后 idx 可能左移 1
    const adjustedTo = pos === 'after' ? toIdx + (fromIdx < toIdx ? 0 : 1) : toIdx - (fromIdx < toIdx ? 1 : 0)
    const insertAt = Math.max(0, Math.min(next.length, adjustedTo))
    next.splice(insertAt, 0, moved)
    commitReorder(next, prev)
  }, [commitReorder])

  const onItemDragEnd = useCallback(() => {
    setDraggingId(null)
    setDragOverId(null)
    setDragOverPos(null)
    document.body.classList.remove('lr-ps-dragging')
  }, [])

  // List 容器：接管尾部空白区 drop → 追加到末尾
  const onListDragOver = useCallback((e: React.DragEvent) => {
    if (!dragEnabledRef.current || !draggingId) return
    const item = (e.target as Element).closest('[data-provider-id]')
    if (item) return  // item 自己的处理器已接管
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    setDragOverId(null)
    setDragOverPos('tail')
  }, [draggingId])

  const onListDrop = useCallback((e: React.DragEvent) => {
    if (!dragEnabledRef.current) return
    const item = (e.target as Element).closest('[data-provider-id]')
    if (item) return  // item 接管
    e.preventDefault()
    const fromId = e.dataTransfer.getData('text/plain') || null
    setDraggingId(null)
    setDragOverId(null)
    setDragOverPos(null)
    document.body.classList.remove('lr-ps-dragging')
    if (!fromId) return
    const prev = providersRef.current
    const fromIdx = prev.findIndex((x) => x.id === fromId)
    if (fromIdx < 0 || fromIdx === prev.length - 1) return
    const next = prev.slice()
    const [moved] = next.splice(fromIdx, 1)
    next.push(moved)
    commitReorder(next, prev)
  }, [commitReorder])

  const onListDragLeave = useCallback((e: React.DragEvent) => {
    // 鼠标离开整个 list 区域（relatedTarget 不在 list 内）才清除
    const list = e.currentTarget as Element
    const related = e.relatedTarget as Element | null
    if (!related || !list.contains(related)) {
      setDragOverId(null)
      setDragOverPos(null)
    }
  }, [])

  // 侧栏宽度可调（通用 hook）
  const { width: sidebarWidth, handleProps: resizeHandleProps } = useResizableWidth({
    storageKey: 'lr-ps:sidebar-width',
    defaultWidth: 260,
    min: 200,
    max: 480,
  })

  // Alt+↑/↓ 键盘排序（仅非搜索态）
  const moveByKeyboard = useCallback((id: string, delta: -1 | 1) => {
    if (!dragEnabledRef.current) return
    const prev = providersRef.current
    const idx = prev.findIndex((x) => x.id === id)
    if (idx < 0) return
    const target = idx + delta
    if (target < 0 || target >= prev.length) return
    const next = prev.slice()
    ;[next[idx], next[target]] = [next[target], next[idx]]
    commitReorder(next, prev)
    // 保持焦点在 moved item
    window.requestAnimationFrame(() => {
      const el = document.querySelector<HTMLElement>(`[data-provider-id="${id}"]`)
      el?.focus()
    })
  }, [commitReorder])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return providers
    return providers.filter(
      (p) =>
        p.id.toLowerCase().includes(q) ||
        p.name.toLowerCase().includes(q) ||
        p.description.toLowerCase().includes(q),
    )
  }, [providers, query])

  return (
    <aside
      className="settings-models-providers lr-ps__sidebar"
      aria-label="providers"
      style={{ ['--provider-pane-width' as string]: `${sidebarWidth}px` } as React.CSSProperties}
    >
      <div className="settings-providers-header">
        <div className="settings-providers-search">
          <Search size={13} />
          <input
            type="text"
            value={query}
            placeholder="搜索服务商"
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button type="button" className="settings-providers-search-clear"
              onClick={() => setQuery('')} title="清空">
              <X size={11} />
            </button>
          )}
        </div>
        {/* + 添加服务商：搜索框右边的小方按钮，跟搜索图标/X 形成对称节奏；
            对标 cherry studio，避免独占一整行抢主视觉。 */}
        <button
          type="button"
          className="settings-providers-add-btn"
          onClick={onAddProvider}
          title="添加服务商（内置之外的 OpenAI / Anthropic 兼容服务）"
          aria-label="添加服务商"
        >
          <Plus size={15} />
        </button>
      </div>
      {!dragEnabled && (
        <div className="settings-providers-hint" aria-live="polite">
          搜索中，清空后可拖动排序
        </div>
      )}
      {reorderErr && (
        <div className="settings-providers-error" role="alert">
          <AlertCircle size={12} /> {reorderErr}
        </div>
      )}
      <div
        className="settings-providers-list"
        onDragOver={onListDragOver}
        onDrop={onListDrop}
        onDragLeave={onListDragLeave}
      >
        {filtered.length === 0 && (
          <div className="settings-providers-empty">无匹配项</div>
        )}
        {filtered.map((p) => {
          const isCurrent = p.id === currentProvider
          // 状态点逻辑（区分"已配置"和"已检测通过"）：
          //   红点 (failed)     → 检测失败，需要修
          //   绿点 (ok)         → 真实检测通过
          //   灰点 (configured) → 填了 api_key 但还没点过检测（"已配置但未验证"）
          //   不渲染            → 啥都没配
          // 本地服务（ollama/lmstudio）跟其他 provider 同一规则，不再硬绿
          const connResult = connCache[p.id]
          const dotKind: 'ok' | 'failed' | 'configured' | null =
            connResult === 'failed' ? 'failed'
              : connResult === 'ok' ? 'ok'
                : p.has_api_key ? 'configured'
                  : null
          const accent = getBrandColor(p.id)
          const isDragOverHere = dragOverId === p.id && draggingId !== null && draggingId !== p.id
          const isLast = filtered[filtered.length - 1]?.id === p.id
          const isTail = dragOverPos === 'tail' && isLast && draggingId !== null && draggingId !== p.id
          return (
              <div
                key={p.id}
                role="button"
                tabIndex={0}
                draggable={dragEnabled}
                data-provider-id={p.id}
                className={
                  `settings-provider-item ${selectedId === p.id ? 'active' : ''}`
                  + (draggingId === p.id ? ' dragging' : '')
                  + (isDragOverHere && dragOverPos === 'before' ? ' drag-over-before' : '')
                  + ((isDragOverHere && dragOverPos === 'after') || isTail ? ' drag-over-after' : '')
                }
                onClick={() => onSelect(p.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onSelect(p.id)
                    return
                  }
                  // Alt+↑/↓ 排序（不走拖拽也能调序，满足可访问性）
                  if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
                    e.preventDefault()
                    moveByKeyboard(p.id, e.key === 'ArrowUp' ? -1 : 1)
                  }
                }}
                onDragStart={onItemDragStart}
                onDragOver={onItemDragOver}
                onDragEnter={onItemDragEnter}
                onDragLeave={onItemDragLeave}
                onDrop={onItemDrop}
                onDragEnd={onItemDragEnd}
                style={accent ? { ['--provider-accent' as string]: accent } as React.CSSProperties : undefined}
              >
                {/* 布局（cherry studio 风格，紧凑左对齐）：
                    grip(手柄) · logo · name(占最多空间) · count · status_dot · current_dot
                    状态点从左边挪到右边，让服务商名字拿到主位、最大化完整展示。
                    grip always 渲染（visibility 控制）保持列对齐。 */}
                <span
                  className="settings-provider-grip"
                  style={{ visibility: dragEnabled ? 'visible' : 'hidden' }}
                  title="拖动调整顺序"
                  aria-hidden={!dragEnabled}
                >
                  <GripVertical size={11} />
                </span>
                <span className="settings-provider-logo">
                  <ProviderAvatar provider={p} size={18} />
                </span>
                <span className="settings-provider-name">
                  {p.name}
                  {!p.is_builtin && <span className="settings-provider-suffix">· 自定义</span>}
                </span>
                <span className="settings-provider-count">{p.models.length}</span>
                {/* status slot：双 9px 固定槽（dot 槽 + badge 槽）+ 4px gap。
                    缺位用 spacer 占住，让所有行的 dot/badge 都在同一垂直线。 */}
                <span className="lr-ps__status-slot">
                  {dotKind ? (
                    <span
                      className={`settings-provider-dot ${dotKind}`}
                      title={
                        dotKind === 'ok' ? '检测通过'
                          : dotKind === 'failed' ? '检测失败，请检查 Key/地址'
                            : '已配置 API Key，未检测过连通性（点开详情可检测）'
                      }
                    />
                  ) : (
                    <span className="lr-ps__status-spacer lr-ps__status-spacer--dot" aria-hidden="true" />
                  )}
                  {isCurrent ? (
                    <span className="lr-ps__current-badge" title="全局默认">
                      <Check size={9} strokeWidth={3.2} />
                    </span>
                  ) : (
                    <span className="lr-ps__status-spacer lr-ps__status-spacer--badge" aria-hidden="true" />
                  )}
                </span>
              </div>
          )
        })}
      </div>
      {/* 右边缘拖拽条：拖动调宽、双击重置 */}
      <ResizeHandle {...resizeHandleProps} />
    </aside>
  )
}

/* ============================================================================
   右栏：详情
   ============================================================================ */

/* ============================================================================
   ModelListGrouped: 按 cherry 同款启发式 (utils/modelGroup) 把模型按 vendor
   分组，多组时折叠面板展示，单组时平铺（避免不必要的 chrome）。
   ============================================================================ */

function ModelListGrouped({
  provider, currentModel, currentProvider, onEdit, onDelete, onSetDefault,
}: {
  provider: ProviderView
  currentModel: string
  currentProvider: string
  onEdit: (id: string) => void
  onDelete: (id: string) => void
  onSetDefault: (id: string) => void
}) {
  // 分组结果稳定：依赖 provider.id + models 引用即可
  const groups = useMemo(
    () => groupModelsByDefault(provider.models || [], provider.id),
    [provider.id, provider.models],
  )
  const groupKeys = useMemo(() => Object.keys(groups), [groups])

  // 折叠态：默认全展开。切 provider 后用 provider.id 作 key 重置。
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const lastProviderIdRef = useRef(provider.id)
  if (lastProviderIdRef.current !== provider.id) {
    lastProviderIdRef.current = provider.id
    if (collapsed.size > 0) setCollapsed(new Set())
  }
  const toggle = (k: string) => {
    setCollapsed((s) => {
      const n = new Set(s)
      if (n.has(k)) n.delete(k); else n.add(k)
      return n
    })
  }

  const renderRow = (m: ProviderView['models'][number]) => {
    const isCurrent = m.id === currentModel && provider.id === currentProvider
    return (
      <div key={m.id} className={`lr-ps__modelrow ${isCurrent ? 'current' : ''}`}>
        <ChevronRight size={12} className="lr-ps__modelrow-chevron" />
        <span className="lr-ps__modelrow-name" title={m.name || m.id}>{m.name || m.id}</span>
        <code className="lr-ps__modelrow-id" title={m.id}>{m.id}</code>
        <span className="lr-ps__modelrow-tags">
          {/* 能力 chip 用后端权威 tags + 共用色板（与 ChatModelPicker / ManageModelsPopup 视觉对齐）。
              free 走专用 .lr-chip--free 样式（带 Sparkles 风格），其他走 .lr-chip--cap + 色 class。
              新 tag 类目只要后端 capabilities.py 加一条 + utils BACKEND_TAG_* 加一行即可。 */}
          {(m.tags || []).slice(0, 4).map((t) => (
            t === 'free' ? (
              <span key={t} className="lr-chip lr-chip--free" title="免费模型">免费</span>
            ) : (
              <span key={t} className={`lr-chip lr-chip--cap ${BACKEND_TAG_COLORS[t] ?? 'cap-gray'}`}>
                {BACKEND_TAG_LABELS[t] ?? t}
              </span>
            )
          ))}
        </span>
        {isCurrent && <span className="lr-ps__modelrow-current" title="全局默认模型">默认</span>}
        {/* 方案 B：非默认行 hover 浮出"设为全局默认"按钮（默认那行不需要，已有徽章）。
            CSS .lr-ps__modelrow-setdefault 默认 opacity:0，.lr-ps__modelrow:hover 时浮出。 */}
        {!isCurrent && (
          <button
            type="button"
            className="lr-ps__modelrow-setdefault"
            onClick={() => onSetDefault(m.id)}
            title="设为全局默认（角色未配置模型时默认使用）"
          >
            <Star size={12} />
          </button>
        )}
        <button type="button" className="lr-ps__modelrow-edit" onClick={() => onEdit(m.id)} title="编辑">
          <Pencil size={12} />
        </button>
        <button type="button" className="lr-ps__modelrow-del" onClick={() => onDelete(m.id)} title="移除">
          <Minus size={12} />
        </button>
      </div>
    )
  }

  // 单组：平铺（无分组头）；多组：折叠面板
  if (groupKeys.length <= 1) {
    return <div className="lr-ps__modellist">{(provider.models || []).map(renderRow)}</div>
  }

  return (
    <div className="lr-ps__modellist lr-ps__modellist--grouped">
      {groupKeys.map((gk) => {
        const ms = groups[gk]
        const isCollapsed = collapsed.has(gk)
        return (
          <div key={gk} className="lr-ps__modelgroup">
            <button
              type="button"
              className="lr-ps__modelgroup-head"
              onClick={() => toggle(gk)}
              aria-expanded={!isCollapsed}
            >
              {isCollapsed
                ? <ChevronRight size={13} className="lr-ps__modelgroup-chev" />
                : <ChevronDown size={13} className="lr-ps__modelgroup-chev" />}
              <span className="lr-ps__modelgroup-name">{gk}</span>
              <span className="lr-ps__modelgroup-count">{ms.length}</span>
            </button>
            {!isCollapsed && (
              <div className="lr-ps__modelgroup-body">{ms.map(renderRow)}</div>
            )}
          </div>
        )
      })}
    </div>
  )
}

/** 连通性检测状态机（比照 cherry：idle → checking → success/failed → idle）。 */
type ConnStatus =
  | { kind: 'idle' }
  | { kind: 'checking' }
  | { kind: 'success' }
  | { kind: 'failed'; message: string }

function getConnectionFailureMessage(message: string): string {
  const detail = message.replace(/^连接失败[:：]\s*/u, '').trim()
  if (!detail || /^(检测失败|检测异常)$/u.test(detail)) {
    return '请检查接口地址、API Key 与网络设置后重试。'
  }
  if (/^connection error\.?$/iu.test(detail)) {
    return '无法访问服务，请检查接口地址、网络或代理设置后重试。'
  }
  if (/(api\s*key|unauthori[sz]ed|\b401\b|\b403\b)/iu.test(detail)) {
    return 'API Key 无效、已过期，或没有访问该模型的权限。'
  }
  return detail
}

function ProviderDetail({
  provider, currentModel, currentProvider, onConnResult, onConnInvalidate, onChanged,
}: {
  provider: ProviderView
  currentModel: string
  currentProvider: string
  /** 检测结果上报顶层缓存（用于列表状态点着色） */
  onConnResult: (id: string, r: 'ok' | 'failed') => void
  /** 用户改 key/host 时清除该 provider 旧检测结果（避免过期绿点） */
  onConnInvalidate: (id: string) => void
  onChanged: () => Promise<void>
}) {
  // 前端直接持有明文 key（同 cherry）。用户输入时走 localKey，debounce 700ms PUT 到后端。
  const [localKey, setLocalKey] = useState(provider.api_key)
  const [keyVisible, setKeyVisible] = useState(false)  // 眼睛按钮：input type 本地切换
  const [hostDraft, setHostDraft] = useState(provider.base_url)
  const { copiedId, copyContent } = useCopyFeedback<'baseUrl' | 'apiKey'>()
  const [hostUnlocked, setHostUnlocked] = useState(false)
  const [conn, setConn] = useState<ConnStatus>({ kind: 'idle' })
  const [detectedModel, setDetectedModel] = useState<string | null>(null)
  // 中继转接探测：轻检测结果（A+B）+ 深度检测状态机（C，按需触发）
  const [diagnostics, setDiagnostics] = useState<RelayDiagnostics | null>(null)
  const [deepProbe, setDeepProbe] = useState<
    | { kind: 'idle' }
    | { kind: 'running' }
    | {
        kind: 'done'
        verdict: 'native' | 'likely_relay' | 'unknown'
        confidence: 'high' | 'medium' | 'low'
        message: string
        cacheCreate: number | null
        cacheRead: number | null
        signals: RelaySignal[]
      }
    | { kind: 'error'; message: string }
  >({ kind: 'idle' })
  // dirty flag：仅当用户**主动**改 key/host 时为 true。
  // 用于把 "用户输入" 跟 "切 provider 时 localState 残留旧值" 区分开，
  // 避免切到新 provider 时误把残留差当成 "用户改了" 而清掉旧检测缓存。
  const isDirtyRef = useRef(false)
  const [showManage, setShowManage] = useState(false)
  const [showAddModel, setShowAddModel] = useState(false)
  const [editingModelId, setEditingModelId] = useState<string | null>(null)
  const [showLogoEditor, setShowLogoEditor] = useState(false)
  const [showHeadersEditor, setShowHeadersEditor] = useState(false)
  const [transportSwitching, setTransportSwitching] = useState(false)
  // inline rename / description：仅自定义 provider 可改
  const [editingName, setEditingName] = useState(false)
  const [nameDraft, setNameDraft] = useState(provider.name)
  const [editingDesc, setEditingDesc] = useState(false)
  const [descDraft, setDescDraft] = useState(provider.description || '')
  // 自定义确认对话框（原生 confirm()/alert() 在 Tauri webview 不弹，会造成"点了直接执行"的误判）
  const { confirm: showConfirm, dialog: confirmDialog } = useConfirmDialog()
  const { alert: showAlert, dialog: alertDialog } = useAlertDialog()

  /* ── 自定义 provider 协议切换：openai_chat ↔ anthropic_messages ── */
  const onChangeTransport = async (next: 'openai_chat' | 'anthropic_messages') => {
    if (provider.is_builtin || transportSwitching) return
    if (next === provider.transport) return
    const nextLabel = next === 'anthropic_messages' ? 'Anthropic Messages' : 'OpenAI 兼容'
    const ok = await showConfirm(
      '切换协议',
      `切换协议到「${nextLabel}」？\n切换后请确认 base_url 与 API Key 仍匹配新协议。`,
      'warning',
      '切换',
    )
    if (!ok) return
    setTransportSwitching(true)
    try {
      // 切换协议时，若存有旧协议的专有请求头（如 codex 或 anthropic 专属头），自动清空避免覆盖新协议伪装
      const currHeaders = provider.extra_headers || {}
      const keys = Object.keys(currHeaders)
      const vals = Object.values(currHeaders)
      const isCodex = keys.some(k => k.toLowerCase().includes('codex')) || vals.some(v => typeof v === 'string' && v.toLowerCase().includes('codex'))
      const isAnthropic = keys.some(k => k.toLowerCase().includes('anthropic')) || vals.some(v => typeof v === 'string' && v.toLowerCase().includes('claude'))
      const shouldClearHeaders = (next === 'anthropic_messages' && isCodex) || (next === 'openai_chat' && isAnthropic)

      await updateProvider(provider.id, {
        transport: next,
        ...(shouldClearHeaders ? { extra_headers: {} } : {}),
      })
      await onChanged()
    } catch (e) {
      await showAlert(e instanceof Error ? e.message : '切换协议失败')
    } finally {
      setTransportSwitching(false)
    }
  }

  // 切换 provider 时重置本地状态
  useEffect(() => {
    setLocalKey(provider.api_key)
    setKeyVisible(false)
    setHostDraft(provider.base_url)
    setHostUnlocked(false)
    setConn({ kind: 'idle' })
    setDetectedModel(null)
    setDiagnostics(null)
    setDeepProbe({ kind: 'idle' })
    isDirtyRef.current = false  // 重置：新 provider 还没被用户动过
    setEditingName(false)
    setNameDraft(provider.name)
    setEditingDesc(false)
    setDescDraft(provider.description || '')
  }, [provider.id, provider.api_key, provider.base_url, provider.name, provider.description])

  const baseUrlLocked = provider.freeze_url && provider.is_builtin && !hostUnlocked

  /* ── debounced auto-save：key / host 改动 700ms 后自动 PUT（同 cherry） ── */
  const saveTimerRef = useRef<number | null>(null)
  useEffect(() => {
    const payload: Record<string, unknown> = {}
    if (localKey !== provider.api_key) payload.api_key = localKey
    if (!baseUrlLocked && hostDraft !== provider.base_url) payload.base_url = hostDraft
    if (Object.keys(payload).length === 0) return
    // 仅当用户**主动**改过（dirty）才认为旧检测过期。
    // 切换 provider 时 localKey/hostDraft 跟 provider.api_key/base_url 暂时不一致
    // 不算用户改动 → 不 invalidate。
    if (!isDirtyRef.current) return

    // 立刻 invalidate：用户开始改 key/host 时旧检测结果就不可信了，
    // 不必等 700ms debounce 落地（避免短暂残留过期绿点）
    onConnInvalidate(provider.id)
    if (saveTimerRef.current != null) window.clearTimeout(saveTimerRef.current)
    saveTimerRef.current = window.setTimeout(() => {
      updateProvider(provider.id, payload)
        .then(() => onChanged())
        .catch((e) => {
          console.error('[ProviderSettings] auto-save 失败', e)
        })
    }, 700)
    return () => {
      if (saveTimerRef.current != null) window.clearTimeout(saveTimerRef.current)
    }
    // onChanged 稳定引用（refresh），不纳入依赖避免连环
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [localKey, hostDraft, provider.id, provider.api_key, provider.base_url, baseUrlLocked])

  /* ── 检测按钮：挑一个模型发小请求（同 cherry onCheckApi） ── */
  const onCheckApi = async () => {
    // 防御：没模型不该能点（按钮已 disabled），但套 ContextVar 上锁以防调用方绕过
    const models = provider.models || []
    if (models.length === 0) return
    // 优先用当前默认模型（如果属于本 provider），否则用第一个
    const preferred = provider.id === currentProvider
      ? models.find((m) => m.id === currentModel) ?? models[0]
      : models[0]

    setConn({ kind: 'checking' })
    setDiagnostics(null)
    setDeepProbe({ kind: 'idle' })
    try {
      const r = await testModelConnection(
        provider.transport === 'anthropic_messages' ? 'anthropic' : provider.id,
        localKey,
        preferred.id,
        hostDraft,
        provider.extra_headers,
        provider.transport,
      )
      if (r.success) {
        setConn({ kind: 'success' })
        setDetectedModel(r.identified_model || null)
        setDiagnostics(r.relay_diagnostics || null)
        onConnResult(provider.id, 'ok')
        // 不再 3 秒后自动 reset：用户需要时间看 diagnostics 警告
      } else {
        setConn({ kind: 'failed', message: r.message || '检测失败' })
        setDetectedModel(null)
        setDiagnostics(null)
        onConnResult(provider.id, 'failed')
      }
    } catch (e) {
      setConn({ kind: 'failed', message: e instanceof Error ? e.message : '检测异常' })
      setDetectedModel(null)
      setDiagnostics(null)
      onConnResult(provider.id, 'failed')
    }
  }

  /* ── 深度检测：Anthropic prompt cache 探针（决定性识别反代） ── */
  const onDeepCheck = async () => {
    if (provider.models.length === 0) return
    if (provider.transport !== 'anthropic_messages') return  // 仅 Anthropic 协议适用
    const preferred = provider.id === currentProvider
      ? provider.models.find((m) => m.id === currentModel) ?? provider.models[0]
      : provider.models[0]

    setDeepProbe({ kind: 'running' })
    try {
      const r = await testModelConnectionDeep(
        provider.transport === 'anthropic_messages' ? 'anthropic' : provider.id,
        localKey,
        preferred.id,
        hostDraft,
        provider.extra_headers,
        provider.transport,
      )
      if (r.success && r.applicable !== false) {
        setDeepProbe({
          kind: 'done',
          verdict: r.verdict || 'unknown',
          confidence: r.confidence || 'low',
          message: r.message || '',
          cacheCreate: r.cache_creation_input_tokens ?? null,
          cacheRead: r.cache_read_input_tokens ?? null,
          signals: r.signals || [],
        })
      } else {
        setDeepProbe({ kind: 'error', message: r.message || '深度检测失败' })
      }
    } catch (e) {
      setDeepProbe({ kind: 'error', message: e instanceof Error ? e.message : '深度检测异常' })
    }
  }

  /* ── inline rename：自定义 provider 改名 ── */
  const commitNameEdit = async () => {
    const next = nameDraft.trim()
    // 空名 / 没改 → 静默回退，不发请求
    if (!next || next === provider.name) {
      setNameDraft(provider.name)
      setEditingName(false)
      return
    }
    try {
      await updateProvider(provider.id, { name: next })
      await onChanged()
    } catch (e) {
      console.error('[ProviderSettings] 改名失败', e)
      await showAlert(e instanceof Error ? e.message : '改名失败')
      setNameDraft(provider.name)
    } finally {
      setEditingName(false)
    }
  }

  /* ── inline description：自定义 provider 改描述 ── */
  const commitDescEdit = async () => {
    const next = descDraft.trim()
    const prev = (provider.description || '').trim()
    // 没改 → 静默回退；空串允许（清描述）
    if (next === prev) {
      setDescDraft(provider.description || '')
      setEditingDesc(false)
      return
    }
    try {
      await updateProvider(provider.id, { description: next })
      await onChanged()
    } catch (e) {
      console.error('[ProviderSettings] 改描述失败', e)
      await showAlert(e instanceof Error ? e.message : '改描述失败')
      setDescDraft(provider.description || '')
    } finally {
      setEditingDesc(false)
    }
  }

  const onDeleteModel = async (id: string) => {
    const ok = await showConfirm(
      '移除模型',
      `从 ${provider.name} 中移除模型「${id}」？`,
      'danger',
      '移除',
    )
    if (!ok) return
    try {
      const nextModels = provider.models.filter((m) => m.id !== id)
      await updateProvider(provider.id, { models: nextModels })
      await onChanged()
    } catch (e) {
      await showAlert(e instanceof Error ? e.message : '删除失败')
    }
  }

  const onDeleteProvider = async () => {
    const ok = await showConfirm(
      `删除 provider「${provider.name}」`,
      `该 provider 下的所有模型都会一并移除，此操作不可撤销。确定删除吗？`,
      'danger',
      '删除',
    )
    if (!ok) return
    try {
      await deleteProvider(provider.id)
      await onChanged()
    } catch (e) {
      await showAlert(e instanceof Error ? e.message : '删除失败')
    }
  }

  const existingIds = useMemo(
    () => new Set(provider.models.map((m) => m.id)),
    [provider.models],
  )

  return (
    <>
      {/* 顶部 header */}
      <header className="settings-detail-header">
        <div className="settings-detail-title">
          {/* 头像：内置只展示，自定义可点编辑（后端 builtin 不允许改 icon，前端也禁掉入口） */}
          {provider.is_builtin ? (
            <div className="settings-detail-logo">
              <ProviderAvatar provider={provider} size={28} />
            </div>
          ) : (
            <button
              type="button"
              className="settings-detail-logo settings-detail-logo--btn"
              onClick={() => setShowLogoEditor(true)}
              title="点击设置头像"
            >
              <ProviderAvatar provider={provider} size={28} />
            </button>
          )}
          {provider.is_builtin ? (
            <h2 className="lr-ps__name">{provider.name}</h2>
          ) : editingName ? (
            <input
              className="lr-ps__name-input"
              value={nameDraft}
              autoFocus
              onFocus={(e) => e.currentTarget.select()}
              onChange={(e) => setNameDraft(e.target.value)}
              onBlur={commitNameEdit}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  e.currentTarget.blur()
                } else if (e.key === 'Escape') {
                  e.preventDefault()
                  setNameDraft(provider.name)
                  setEditingName(false)
                }
              }}
              maxLength={64}
              spellCheck={false}
              style={{ width: `${Math.max(nameDraft.length, 4) + 1}ch` }}
            />
          ) : (
            <h2
              className="lr-ps__name lr-ps__name--editable"
              title="点击重命名（Enter 保存 / Esc 取消）"
              onClick={() => { setNameDraft(provider.name); setEditingName(true) }}
            >
              {provider.name}
              <Pencil size={12} className="lr-ps__name-pen" />
            </h2>
          )}
          {/* 协议：内置只读徽章，自定义用 TransportDropdown（自绘 popover，保 a11y + 视觉精致） */}
          {provider.is_builtin ? (
            <span className="settings-detail-kind">
              {provider.transport === 'anthropic_messages' ? 'Anthropic 协议' : 'OpenAI 兼容'}
            </span>
          ) : (
            <TransportDropdown
              ariaLabel="API 协议"
              title="切换 API 协议"
              value={provider.transport === 'anthropic_messages' ? 'anthropic_messages' : 'openai_chat'}
              busy={transportSwitching}
              options={[
                { value: 'openai_chat',        label: 'OpenAI 兼容' },
                { value: 'anthropic_messages', label: 'Anthropic 协议' },
              ]}
              onChange={(v) => onChangeTransport(v)}
            />
          )}
          {provider.is_aggregator && (
            <span className="lr-ps__aggregator-badge">聚合平台</span>
          )}
          {/* 右上角删除按钮：仅自定义 provider；onClick 内部有 confirm() 二次确认 */}
          {!provider.is_builtin && (
            <button
              type="button"
              className="lr-ps__delete-btn"
              onClick={onDeleteProvider}
              title={`删除这个自定义 provider（该 provider 下所有模型会一并移除）`}
              aria-label="删除这个 provider"
            >
              <Trash2 size={14} />
            </button>
          )}
        </div>
        {/* 描述：自定义可 inline 编辑（与名称同交互）；内置只读。
            空描述时自定义显示淡色占位，避免"没字就没法点"的死角。
            官网 / 获取 Key 链接独立一行，不参与编辑态。 */}
        <div className="lr-ps__desc-row">
          {provider.is_builtin ? (
            provider.description ? (
              <p className="settings-detail-sub lr-ps__desc">{provider.description}</p>
            ) : null
          ) : editingDesc ? (
            <input
              className="lr-ps__desc-input"
              value={descDraft}
              autoFocus
              onFocus={(e) => e.currentTarget.select()}
              onChange={(e) => setDescDraft(e.target.value)}
              onBlur={commitDescEdit}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  e.currentTarget.blur()
                } else if (e.key === 'Escape') {
                  e.preventDefault()
                  setDescDraft(provider.description || '')
                  setEditingDesc(false)
                }
              }}
              maxLength={120}
              spellCheck={false}
              placeholder="一句话备注，如 中转代理 / 国内镜像"
              aria-label="服务商描述"
            />
          ) : (
            <p
              className={`settings-detail-sub lr-ps__desc lr-ps__desc--editable${provider.description ? '' : ' lr-ps__desc--empty'}`}
              title="点击编辑描述（Enter 保存 / Esc 取消）"
              onClick={() => { setDescDraft(provider.description || ''); setEditingDesc(true) }}
            >
              <span className="lr-ps__desc-text">
                {provider.description || '添加描述…'}
              </span>
              <Pencil size={11} className="lr-ps__desc-pen" />
            </p>
          )}
          {(provider.homepage || provider.api_key_url) && (
            <span className="lr-ps__desc-links">
              {provider.homepage && (
                <a href={provider.homepage} target="_blank" rel="noreferrer" className="lr-ps__link">
                  官网 <ExternalLink size={11} />
                </a>
              )}
              {provider.api_key_url && (
                <a href={provider.api_key_url} target="_blank" rel="noreferrer" className="lr-ps__link">
                  获取 Key <ExternalLink size={11} />
                </a>
              )}
            </span>
          )}
        </div>
      </header>

      {/* 连接配置 */}
      <section className="settings-detail-conn">
        <div className="settings-conn-row lr-ps__conn-row">
          <label className="settings-conn-label" htmlFor={`provider-${provider.id}-base-url`}>接口地址</label>
          {/* 接口地址输入框跨到 row 最后一列：与下方 API Key 行的检测按钮右边缘对齐。
              API Key 行 input 只占列 2，检测按钮占列 3——接口地址行没检测按钮，
              input 默认只到列 2 右端，视觉上比 API Key 行的 input 短。
              加 .lr-ps__host-input-wrap 让它 grid-column: 2 / -1 填满剩余列。 */}
          <div className={`settings-conn-input lr-ps__host-input-wrap ${baseUrlLocked ? 'locked' : ''}`}>
            <input
              id={`provider-${provider.id}-base-url`}
              name="base-url"
              type="url"
              value={hostDraft}
              autoComplete="off"
              spellCheck={false}
              placeholder="https://api.example.com…"
              disabled={baseUrlLocked}
              onChange={(e) => { isDirtyRef.current = true; setHostDraft(e.target.value) }}
            />
            <button
              type="button"
              className={`settings-conn-copy${copiedId === 'baseUrl' ? ' is-copied' : ''}`}
              onClick={() => { void copyContent('baseUrl', hostDraft) }}
              disabled={!hostDraft.trim()}
              title={copiedId === 'baseUrl' ? '接口地址已复制' : '复制接口地址'}
              aria-label={copiedId === 'baseUrl' ? '接口地址已复制' : '复制接口地址'}
            >
              {copiedId === 'baseUrl' ? <Check size={14} /> : <Copy size={14} />}
            </button>
            {provider.freeze_url && provider.is_builtin && (
              <button
                type="button"
                className="settings-conn-lock"
                onClick={() => setHostUnlocked((v) => !v)}
                title={hostUnlocked ? '恢复默认锁定' : '解锁后可改为代理地址'}
              >
                {hostUnlocked ? <Unlock size={14} /> : <Lock size={14} />}
              </button>
            )}
            {/* 自定义请求头入口：嵌入 input 右内侧，跟 lock/eye 同样不显眼。
                项目默认已注入 Lry/Liuruyan 品牌头，只有极少数用户需要 override——
                不该占独立一行制造负担。已配置时右上角加微小色点提示。 */}
            {(() => {
              const n = provider.extra_headers ? Object.keys(provider.extra_headers).length : 0
              return (
                <button
                  type="button"
                  className={`settings-conn-lock lr-ps__headers-btn${n > 0 ? ' has-config' : ''}`}
                  onClick={() => setShowHeadersEditor(true)}
                  title={n > 0 ? `自定义请求头（已配置 ${n} 项）` : '自定义请求头（高级：默认值被拒时才需要）'}
                  aria-label="编辑自定义请求头"
                >
                  <Settings2 size={14} />
                </button>
              )
            })()}
          </div>
        </div>

        <div className="settings-conn-row lr-ps__conn-row lr-ps__conn-row--key">
          <label className="settings-conn-label" htmlFor={`provider-${provider.id}-api-key`}>API Key</label>
          <div className="lr-ps__key-field">
            <div className="lr-ps__key-line">
              <div className="settings-conn-input lr-ps__keyinput">
                <input
                  id={`provider-${provider.id}-api-key`}
                  name="api-key"
                  type={keyVisible ? 'text' : 'password'}
                  value={localKey}
                  autoComplete="off"
                  spellCheck={false}
                  aria-describedby={conn.kind === 'failed' ? `provider-${provider.id}-connection-error` : undefined}
                  placeholder={
                    provider.api_key_prefix
                      ? `以 ${provider.api_key_prefix} 开头…`
                      : '输入或粘贴 API Key…'
                  }
                  onChange={(e) => {
                    isDirtyRef.current = true
                    setLocalKey(e.target.value)
                    if (conn.kind !== 'idle') setConn({ kind: 'idle' })
                  }}
                />
                <button
                  type="button"
                  className="settings-conn-eye"
                  onClick={() => setKeyVisible((v) => !v)}
                  title={keyVisible ? '隐藏' : '显示'}
                  aria-label={keyVisible ? '隐藏 API Key' : '显示 API Key'}
                >
                  {keyVisible ? <Eye size={14} /> : <EyeOff size={14} />}
                </button>
                <button
                  type="button"
                  className={`settings-conn-copy${copiedId === 'apiKey' ? ' is-copied' : ''}`}
                  onClick={() => { void copyContent('apiKey', localKey) }}
                  disabled={!localKey.trim()}
                  title={copiedId === 'apiKey' ? 'API Key 已复制' : '复制 API Key'}
                  aria-label={copiedId === 'apiKey' ? 'API Key 已复制' : '复制 API Key'}
                >
                  {copiedId === 'apiKey' ? <Check size={14} /> : <Copy size={14} />}
                </button>
              </div>
              <button
                type="button"
                className={'lr-ps__check-btn' + (conn.kind === 'success' ? ' is-success' : '')}
                onClick={onCheckApi}
                disabled={conn.kind === 'checking' || provider.models.length === 0}
                aria-describedby={conn.kind === 'failed' ? `provider-${provider.id}-connection-error` : undefined}
                title={
                  provider.models.length === 0
                    ? '请先点「获取模型」或「+ 添加」加一个模型后再检测'
                    : conn.kind === 'failed'
                      ? '重新检测连接'
                      : '检测连通性（无需 Key 的服务也能直接点）'
                }
              >
                {conn.kind === 'checking' && <><Loader2 size={13} className="spin" aria-hidden="true" /> 检测…</>}
                {conn.kind === 'success' && <><Check size={13} aria-hidden="true" /> 通过</>}
                {conn.kind === 'failed' && <><RefreshCw size={13} aria-hidden="true" /> 重试</>}
                {conn.kind === 'idle' && '检测'}
              </button>
            </div>
            {conn.kind === 'failed' && (
              <div
                id={`provider-${provider.id}-connection-error`}
                className="lr-ps__conn-feedback is-error"
                role="status"
                aria-live="polite"
              >
                <AlertCircle size={14} aria-hidden="true" />
                <span className="lr-ps__conn-feedback-title">连接失败</span>
                <span className="lr-ps__conn-feedback-message">{getConnectionFailureMessage(conn.message)}</span>
              </div>
            )}
          </div>
        </div>

        {detectedModel && (
          <div className="lr-ps__detected-model">
            实际模型: {detectedModel}
          </div>
        )}
        {/* ── 中继转接探测结果（A+B 轻检测警告框） ──
            仅 verdict≠native 时浮框列出 signals，避免给原生 provider 加视觉噪音。 */}
        {diagnostics && diagnostics.verdict !== 'native' && diagnostics.signals.length > 0 && (
          <div className={`lr-ps__relay-warn lr-ps__relay-warn--${diagnostics.verdict}`}>
            <div className="lr-ps__relay-warn-head">
              <AlertTriangle size={13} />
              <span>
                {diagnostics.verdict === 'likely_relay'
                  ? '疑似中继/反代'
                  : '请求路径存在异常'}
                <span className="lr-ps__relay-conf">（置信度: {diagnostics.confidence}）</span>
              </span>
            </div>
            <ul className="lr-ps__relay-signals">
              {diagnostics.signals.map((s, i) => (
                <li key={i} className={`lr-ps__relay-signal lr-ps__relay-signal--${s.level}`}>
                  <span className="lr-ps__relay-signal-name">{s.name}</span>
                  <span className="lr-ps__relay-signal-detail">{s.detail}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* ── 深度检测入口 ──
            只要 transport=anthropic_messages 且检测通过，**始终**显示。
            原因：A+B 轻探测在 max_tokens=1 小请求下灵敏度有限——反代可能"诚实翻译"
            导致 verdict=native 漏报。深度检测（prompt cache 探针）才是决定性证据，
            必须独立暴露入口让用户能主动触发，不依赖 A+B 是否提示可疑。 */}
        {diagnostics && provider.transport === 'anthropic_messages' && (
          <div className="lr-ps__relay-deep-section">
            {deepProbe.kind === 'idle' && (
              <button
                type="button"
                className="lr-ps__relay-deep-btn"
                onClick={onDeepCheck}
                title="发两次相同长 prompt + cache_control 测 prompt cache 命中，决定性识别 Anthropic 反代（~5 秒，消耗少量 token）"
              >
                深度检测（prompt cache 探针）
                {diagnostics.verdict === 'native' && (
                  <span className="lr-ps__relay-deep-hint">— 轻检测未发现可疑，可深度验证</span>
                )}
              </button>
            )}
            {deepProbe.kind === 'running' && (
              <div className="lr-ps__relay-deep-status">
                <Loader2 size={12} className="spin" /> 正在发两次探针请求...
              </div>
            )}
            {deepProbe.kind === 'error' && (
              <div className="lr-ps__relay-deep-status is-error">
                <AlertCircle size={12} /> {deepProbe.message}
              </div>
            )}
            {deepProbe.kind === 'done' && (
              <div className={`lr-ps__relay-deep-result lr-ps__relay-deep-result--${deepProbe.verdict}`}>
                <div className="lr-ps__relay-deep-head">
                  {deepProbe.verdict === 'native' ? (
                    <><Check size={12} /> 深度检测：原生 Anthropic（置信度 {deepProbe.confidence}）</>
                  ) : deepProbe.verdict === 'likely_relay' ? (
                    <><AlertTriangle size={12} /> 深度检测：确认中继/反代（置信度 {deepProbe.confidence}）</>
                  ) : (
                    <><AlertCircle size={12} /> 深度检测：结果不确定</>
                  )}
                </div>
                <div className="lr-ps__relay-deep-cache">
                  cache_creation: {deepProbe.cacheCreate ?? '—'} / cache_read: {deepProbe.cacheRead ?? '—'}
                </div>
                {/* signals 列表：把后端的判定依据展开给用户看，
                    比 message 那句"详见 signals"实用得多。 */}
                {deepProbe.signals.length > 0 && (
                  <ul className="lr-ps__relay-signals">
                    {deepProbe.signals.map((s, i) => (
                      <li key={i} className={`lr-ps__relay-signal lr-ps__relay-signal--${s.level}`}>
                        <span className="lr-ps__relay-signal-name">{s.name}</span>
                        <span className="lr-ps__relay-signal-detail">{s.detail}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {deepProbe.message && (
                  <div className="lr-ps__relay-deep-msg">{deepProbe.message}</div>
                )}
              </div>
            )}
          </div>
        )}
        {!provider.has_api_key && provider.id !== 'ollama' && provider.id !== 'lmstudio' && (
          <div className="settings-conn-warn">
            <AlertCircle size={13} /> 未配置 API Key
          </div>
        )}
      </section>

      {/* 模型列表 + 一键发现 */}
      <section className="settings-models-table">
        <div className="settings-table-header">
          <span className="settings-table-title">已启用模型 ({(provider.models || []).length})</span>
          {/* "+" 和 "获取模型" 用 compact 分组，视觉上贴在一起（中间无间距，中间边界共享）。 */}
          <div className="settings-table-actions lr-ps__actions-group">
            <button
              type="button"
              className="settings-table-action lr-ps__add-model-icon"
              onClick={() => { setEditingModelId(null); setShowAddModel(true) }}
              title="手动添加单个模型（/v1/models 不返回的小众模型、ollama 本地型号等）"
              aria-label="手动添加模型"
            >
              <Plus size={13} />
            </button>
            <button
              type="button"
              className="settings-table-action lr-ps__discover"
              onClick={() => setShowManage(true)}
              title="从 API 拉取远端模型列表，按免费/推理/视觉等标签筛选后批量导入"
            >
              <RefreshCw size={13} /> 获取模型
            </button>
          </div>
        </div>

        {(provider.models || []).length === 0 ? (
          <div className="lr-ps__empty">
            <Plus size={14} />
            <span>尚未启用任何模型，点右上「发现模型」一键拉取，或点「手动添加」自己填</span>
          </div>
        ) : (
          <ModelListGrouped
            provider={provider}
            currentModel={currentModel}
            currentProvider={currentProvider}
            onEdit={(id) => { setEditingModelId(id); setShowAddModel(true) }}
            onDelete={onDeleteModel}
            onSetDefault={async (id) => {
              // PUT /providers/active scope=global：同时切换 current_provider + current_model
              // 失败静默（例如后端未运行时）；成功后 onChanged() 重新 listProviders 刷 badge
              try {
                await setActiveModel({
                  scope: 'global',
                  provider_id: provider.id,
                  model: id,
                })
                onChanged()
              } catch { /* 失败保持原状，让用户自行重试 */ }
            }}
          />
        )}
      </section>


      {showManage && (
        <ManageModelsPopup
          provider={provider}
          existingIds={existingIds}
          onClose={() => setShowManage(false)}
          onImported={async () => { await onChanged() }}
        />
      )}
      {showAddModel && (
        <AddModelDialog
          provider={provider}
          editingModelId={editingModelId}
          onClose={() => { setShowAddModel(false); setEditingModelId(null) }}
          onSaved={async () => { await onChanged() }}
        />
      )}
      {showLogoEditor && (
        <ProviderLogoEditor
          provider={provider}
          onClose={() => setShowLogoEditor(false)}
          onChanged={async () => { await onChanged() }}
        />
      )}
      {showHeadersEditor && (
        <ExtraHeadersDialog
          provider={provider}
          onClose={() => setShowHeadersEditor(false)}
          onSaved={async () => { await onChanged() }}
        />
      )}
      {confirmDialog}
      {alertDialog}
    </>
  )
}
