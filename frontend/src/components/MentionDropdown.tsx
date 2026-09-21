import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { File, Folder, ScrollText, Map as MapIcon, Code2, ChevronRight, ArrowLeft } from 'lucide-react'
import { matchScopeItems, type ScopeItem, type ScopeItemType } from '../api/chat'

const TYPE_ICONS: Record<ScopeItemType, JSX.Element> = {
  file: <File size={14} />,
  directory: <Folder size={14} />,
  rule: <ScrollText size={14} />,
  code_map: <MapIcon size={14} />,
  code_context: <Code2 size={14} />,
}

const TYPE_LABEL_SHORT: Record<ScopeItemType, string> = {
  file: 'File',
  directory: 'Dir',
  rule: 'Rule',
  code_map: 'Map',
  code_context: 'Sym',
}

interface Category {
  type: ScopeItemType
  label: string
  hint: string
}

/** 分类抽屉入口（空 query 下显示）。顺序参考 Windsurf @ 菜单，但只列柳如烟已支持的 5 种。 */
const CATEGORIES: Category[] = [
  { type: 'file', label: 'Files', hint: '项目文件' },
  { type: 'directory', label: 'Directories', hint: '目录' },
  { type: 'code_context', label: 'Code Context Items', hint: '符号（class / function）' },
  { type: 'rule', label: 'Rules', hint: '.windsurf/rules/' },
  { type: 'code_map', label: 'Code Map', hint: '项目地图（_docs/CODE_MAP.md）' },
]

interface MentionAnchor {
  left: number
  top: number
}

interface MentionDropdownProps {
  query: string
  anchor: MentionAnchor | null
  onSelect: (item: ScopeItem) => void
  onClose: () => void
}

type Page =
  | { kind: 'categories' }
  | { kind: 'category'; type: ScopeItemType; label: string }

/** 统一的行类型：categories 页显示 'category' 行，其它时候显示 'item' 行。 */
type Row =
  | { kind: 'category'; category: Category }
  | { kind: 'item'; item: ScopeItem }

/**
 * @ mention 下拉。两层结构（参考 Windsurf @ 菜单）：
 * - 空 query + categories 页：显示类型分类入口（→ 右箭头进入子类）
 * - 空 query + category 页：该类型下的空 query 推荐（如 top-level dirs / rules）
 * - 非空 query：受当前页类型约束的搜索结果（categories 页 = 全类型混合）
 *
 * 键盘：
 * - ArrowUp/Down 移动；Enter/Tab/ArrowRight 选中（category 行则进入子类）
 * - ArrowLeft / Backspace(空 query 时) 从子类退回 categories
 * - Esc 关闭
 */
export default function MentionDropdown({ query, anchor, onSelect, onClose }: MentionDropdownProps) {
  const [items, setItems] = useState<ScopeItem[]>([])
  const [loading, setLoading] = useState(false)
  const [activeIdx, setActiveIdx] = useState(0)
  const [page, setPage] = useState<Page>({ kind: 'categories' })
  const debounceTimerRef = useRef<number | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)

  // 是否要显示 categories（空 query + categories 页）——这时不需要后端检索
  const showCategories = page.kind === 'categories' && query.length === 0

  // 后端检索：仅在需要 items 时才拉
  useEffect(() => {
    if (showCategories) {
      setItems([])
      setLoading(false)
      return
    }
    if (debounceTimerRef.current !== null) {
      window.clearTimeout(debounceTimerRef.current)
    }
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }
    setLoading(true)
    const types = page.kind === 'category' ? [page.type] : undefined
    debounceTimerRef.current = window.setTimeout(() => {
      const ctrl = new AbortController()
      abortRef.current = ctrl
      void (async () => {
        try {
          const result = await matchScopeItems(query, { limit: 20, types, signal: ctrl.signal })
          if (!ctrl.signal.aborted) {
            setItems(result)
            setActiveIdx(0)
          }
        } catch {
          if (!ctrl.signal.aborted) {
            setItems([])
            setActiveIdx(0)
          }
        } finally {
          if (!ctrl.signal.aborted) setLoading(false)
        }
      })()
    }, query.length === 0 ? 0 : 220)
    return () => {
      if (debounceTimerRef.current !== null) {
        window.clearTimeout(debounceTimerRef.current)
        debounceTimerRef.current = null
      }
    }
  }, [query, page, showCategories])

  // 页面切换时把高亮归零（useEffect 深 compare 比较复杂；直接 page 变就重置）
  useEffect(() => {
    setActiveIdx(0)
  }, [page])

  // 构造当前要显示的行（union 类型，键盘统一处理）
  const rows = useMemo<Row[]>(() => {
    if (showCategories) {
      return CATEGORIES.map((c) => ({ kind: 'category' as const, category: c }))
    }
    return items.map((it) => ({ kind: 'item' as const, item: it }))
  }, [showCategories, items])

  const selectRow = useCallback((row: Row) => {
    if (row.kind === 'category') {
      setPage({ kind: 'category', type: row.category.type, label: row.category.label })
    } else {
      onSelect(row.item)
    }
  }, [onSelect])

  const goBackToCategories = useCallback(() => {
    setPage({ kind: 'categories' })
  }, [])

  // 鼠标点击外面关闭。**必须用 click，不是 mousedown**——
  // CategoryRow 的 onMouseDown 触发 setPage 后 React 同步 commit 把整个 rows 重建，
  // 旧 row 节点 detach；如果用 mousedown 监听，document 这层后跑时 e.target 已不在 DOM 树里，
  // containerRef.current.contains(target) 返回 false，会误判"点在外面"导致 onClose unmount，
  // 子页 page 状态丢失，鼠标点完看着就像"没反应"。click 在 element detach 后压根不 fire，规避此坑。
  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (!containerRef.current) return
      if (!containerRef.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('click', onDocClick)
    return () => document.removeEventListener('click', onDocClick)
  }, [onClose])

  // host textarea 通过 window 事件转发键盘
  useEffect(() => {
    function onKey(e: Event) {
      const detail = (e as CustomEvent<{ key: string }>).detail
      if (!detail) return
      if (detail.key === 'ArrowDown') {
        setActiveIdx((idx) => (rows.length === 0 ? 0 : (idx + 1) % rows.length))
      } else if (detail.key === 'ArrowUp') {
        setActiveIdx((idx) => (rows.length === 0 ? 0 : (idx - 1 + rows.length) % rows.length))
      } else if (detail.key === 'Enter' || detail.key === 'Tab' || detail.key === 'ArrowRight') {
        const sel = rows[activeIdx]
        if (sel) selectRow(sel)
      } else if (detail.key === 'Escape') {
        onClose()
      }
    }
    window.addEventListener('liuruyan:mention-key', onKey as EventListener)
    return () => window.removeEventListener('liuruyan:mention-key', onKey as EventListener)
  }, [rows, activeIdx, selectRow, onClose])

  const style = useMemo<React.CSSProperties | undefined>(() => {
    if (!anchor) return undefined
    return {
      position: 'fixed',
      left: anchor.left,
      bottom: window.innerHeight - anchor.top + 6,
      maxHeight: 400,
      width: 380,
      zIndex: 1100,
    }
  }, [anchor])

  if (!anchor || !style) return null

  const headerText = showCategories
    ? 'Pin context'
    : page.kind === 'category'
      ? (query ? `${page.label} · "${query}"` : page.label)
      : (query ? `匹配 "${query}"` : '推荐')

  return (
    <div ref={containerRef} className="mention-dropdown" style={style}>
      <div className="mention-dropdown-header">
        <div className="mention-dropdown-header-left">
          {page.kind === 'category' && (
            <button
              type="button"
              className="mention-back-btn"
              onMouseDown={(e) => { e.preventDefault(); goBackToCategories() }}
              aria-label="返回"
            >
              <ArrowLeft size={12} />
            </button>
          )}
          <span>{headerText}</span>
        </div>
        {loading && <span className="mention-dropdown-loading">…</span>}
      </div>
      <div className="mention-dropdown-list">
        {loading && rows.length === 0 && (
          <div className="mention-dropdown-empty">加载中…</div>
        )}
        {!loading && rows.length === 0 && (
          <div className="mention-dropdown-empty">
            {query
              ? '未匹配到候选'
              : page.kind === 'category' && page.type === 'code_context'
                ? '键入要查找的符号名（类 / 函数）…'
                : '开始打字搜索…'}
          </div>
        )}
        {rows.map((row, idx) => {
          if (row.kind === 'category') {
            return (
              <CategoryRow
                key={`cat:${row.category.type}`}
                category={row.category}
                active={idx === activeIdx}
                onMouseEnter={() => setActiveIdx(idx)}
                onSelect={() => selectRow(row)}
              />
            )
          }
          return (
            <MentionRow
              key={`item:${row.item.type}:${row.item.path}:${idx}`}
              item={row.item}
              active={idx === activeIdx}
              onMouseEnter={() => setActiveIdx(idx)}
              onSelect={() => selectRow(row)}
            />
          )
        })}
      </div>
      <MentionStyle />
    </div>
  )
}

function CategoryRow({
  category,
  active,
  onMouseEnter,
  onSelect,
}: {
  category: Category
  active: boolean
  onMouseEnter: () => void
  onSelect: () => void
}) {
  return (
    <div
      className={`mention-row mention-row--category ${active ? 'mention-row--active' : ''}`}
      onMouseEnter={onMouseEnter}
      onMouseDown={(e) => {
        e.preventDefault()
        onSelect()
      }}
    >
      <span className="mention-row-icon">{TYPE_ICONS[category.type]}</span>
      <span className="mention-category-label">{category.label}</span>
      <span className="mention-category-chevron" aria-hidden="true">
        <ChevronRight size={14} />
      </span>
    </div>
  )
}

// 大目录阈值：跟后端 DIRECTORY_PREVIEW_MAX_ENTRIES 对齐——超过这个数 pin 后会被一级清单截断
const LARGE_DIR_THRESHOLD = 200
// 后端 _count_dir_entries 的上限（DIR_ENTRIES_COUNT_CAP）；命中即显示 "≥N"
const DIR_ENTRIES_COUNT_CAP = 2000

function formatDirEntriesHint(count: number): string {
  if (count >= DIR_ENTRIES_COUNT_CAP) return `≥${count} 项`
  return `${count} 项`
}

function MentionRow({
  item,
  active,
  onMouseEnter,
  onSelect,
}: {
  item: ScopeItem
  active: boolean
  onMouseEnter: () => void
  onSelect: () => void
}) {
  const label = item.label || (item.type === 'code_context' ? item.path : item.path.split(/[\\/]/).pop() || item.path)
  const baseSubtitle = item.type === 'code_context' ? item.label || '' : item.path
  const isDirWithCount = item.type === 'directory' && typeof item.entries_count === 'number'
  const isLargeDir = isDirWithCount && (item.entries_count as number) >= LARGE_DIR_THRESHOLD
  const dirHint = isDirWithCount ? formatDirEntriesHint(item.entries_count as number) : null
  const subtitle = dirHint ? (baseSubtitle ? `${baseSubtitle} · ${dirHint}` : dirHint) : baseSubtitle
  const subtitleTitle = isLargeDir
    ? `较大目录，pin 后只列前 ${LARGE_DIR_THRESHOLD} 个一级条目`
    : undefined
  return (
    <div
      className={`mention-row ${active ? 'mention-row--active' : ''}`}
      onMouseEnter={onMouseEnter}
      onMouseDown={(e) => {
        e.preventDefault()
        onSelect()
      }}
    >
      <span className="mention-row-icon">{TYPE_ICONS[item.type]}</span>
      <span className="mention-row-name">{label}</span>
      <span className="mention-row-type">{TYPE_LABEL_SHORT[item.type]}</span>
      {subtitle && (
        <span
          className={`mention-row-subtitle ${isLargeDir ? 'mention-row-subtitle--warning' : ''}`}
          title={subtitleTitle}
        >
          {subtitle}
        </span>
      )}
    </div>
  )
}

// ====== 钩子：在 textarea 上检测 @ token、转发键盘事件 ======

export interface UseMentionControllerOptions {
  textareaRef: React.RefObject<HTMLTextAreaElement>
  input: string
  onInputChange: (next: string) => void
}

export interface MentionState {
  open: boolean
  query: string
  anchor: MentionAnchor | null
  atIndex: number
}

const INITIAL_STATE: MentionState = { open: false, query: '', anchor: null, atIndex: -1 }

function detectMention(input: string, cursor: number): { atIndex: number; query: string } | null {
  if (cursor <= 0) return null
  let i = cursor - 1
  while (i >= 0) {
    const ch = input[i]
    if (ch === '@') break
    if (/\s/.test(ch)) return null
    i--
  }
  if (i < 0) return null
  if (i > 0 && !/\s/.test(input[i - 1])) return null
  return { atIndex: i, query: input.slice(i + 1, cursor) }
}

export function useMentionController({ textareaRef, input, onInputChange }: UseMentionControllerOptions) {
  const [state, setState] = useState<MentionState>(INITIAL_STATE)

  const updateFromTextarea = useCallback(() => {
    const ta = textareaRef.current
    if (!ta) {
      setState(INITIAL_STATE)
      return
    }
    const cursor = ta.selectionStart ?? input.length
    const detected = detectMention(input, cursor)
    if (!detected) {
      setState((prev) => (prev.open ? INITIAL_STATE : prev))
      return
    }
    const rect = ta.getBoundingClientRect()
    setState({
      open: true,
      query: detected.query,
      atIndex: detected.atIndex,
      anchor: { left: rect.left, top: rect.top },
    })
  }, [input, textareaRef])

  useEffect(() => {
    updateFromTextarea()
  }, [input, updateFromTextarea])

  const handleSelect = useCallback((item: ScopeItem) => {
    const ta = textareaRef.current
    if (!ta) {
      setState(INITIAL_STATE)
      return
    }
    if (state.atIndex < 0) {
      setState(INITIAL_STATE)
      return
    }
    const cursor = ta.selectionStart ?? input.length
    const before = input.slice(0, state.atIndex)
    const after = input.slice(cursor)
    // 插入文本：目前各类型都用 path 作可视化 token；真正"钉住"由 Chat.handleMentionSelect
    // 调 updateSessionPinnedScope 落到 PinnedBar，不靠 textarea 里这段字符串。
    const insertText = item.path
    const nextInput = `${before}${insertText}${after}`.replace(/\s+$/, '') + ' '
    onInputChange(nextInput)
    setState(INITIAL_STATE)
    requestAnimationFrame(() => {
      ta.focus()
      const pos = before.length + insertText.length + 1
      ta.setSelectionRange(pos, pos)
    })
  }, [input, onInputChange, state.atIndex, textareaRef])

  const handleClose = useCallback(() => {
    setState(INITIAL_STATE)
  }, [])

  const handleKeyDownIntercept = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!state.open) return false
    // 拦截集合：上下导航 / 选中 / 关闭。ArrowLeft / Backspace 不拦截，由 textarea 自然处理
    // —— 光标移出 @ 范围后 detectMention 会自动关闭 dropdown，体验更直观。
    // ArrowRight 等同 Enter（进入子类或选中），是参考 Windsurf @ 菜单的常用操作。
    if (['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape', 'ArrowRight'].includes(e.key)) {
      e.preventDefault()
      window.dispatchEvent(new CustomEvent('liuruyan:mention-key', {
        detail: { key: e.key },
      }))
      return true
    }
    return false
  }, [state.open])

  return {
    state,
    handleSelect,
    handleClose,
    handleKeyDownIntercept,
  }
}

function MentionStyle() {
  return (
    <style>{`
      .mention-dropdown {
        background: var(--bg-card, #fff);
        color: var(--text-primary, #222);
        border: 1px solid var(--border-light, rgba(0,0,0,0.14));
        border-radius: 10px;
        box-shadow: 0 12px 36px rgba(0,0,0,0.18);
        display: flex;
        flex-direction: column;
        overflow: hidden;
        font-size: 13px;
      }
      .mention-dropdown-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 7px 10px;
        background: var(--bg-elevated, rgba(0,0,0,0.03));
        border-bottom: 1px solid var(--border-light, rgba(0,0,0,0.06));
        color: var(--text-secondary, #666);
        font-size: 11.5px;
        font-weight: 500;
      }
      .mention-dropdown-header-left {
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }
      .mention-back-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 2px 4px;
        border: none;
        background: transparent;
        border-radius: 4px;
        cursor: pointer;
        color: var(--text-secondary, #777);
      }
      .mention-back-btn:hover {
        background: var(--bg-card, rgba(0,0,0,0.06));
        color: var(--text-primary, #222);
      }
      .mention-dropdown-loading {
        opacity: 0.55;
      }
      .mention-dropdown-list {
        overflow-y: auto;
        max-height: 360px;
        padding: 4px;
      }
      .mention-dropdown-empty {
        padding: 14px;
        text-align: center;
        color: var(--text-secondary, #888);
        font-style: italic;
      }
      /* ——— 子类 item 行（含 icon + 名字 + 类型 chip + 路径副标题）——— */
      .mention-row {
        display: grid;
        grid-template-columns: 22px 1fr auto;
        grid-template-areas:
          "icon name type"
          "icon subtitle subtitle";
        column-gap: 10px;
        align-items: center;
        padding: 6px 10px;
        border-radius: 6px;
        cursor: pointer;
      }
      .mention-row--active {
        background: var(--bg-elevated, rgba(80, 100, 220, 0.10));
      }
      .mention-row-icon {
        grid-area: icon;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        color: var(--text-secondary, #777);
      }
      .mention-row--active .mention-row-icon {
        color: var(--text-primary, #333);
      }
      .mention-row-name {
        grid-area: name;
        font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
        font-size: 12.5px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        font-weight: 500;
      }
      .mention-row-type {
        grid-area: type;
        font-size: 10.5px;
        color: var(--text-secondary, #888);
        background: var(--bg-elevated, rgba(0,0,0,0.04));
        border-radius: 4px;
        padding: 1px 5px;
      }
      .mention-row-subtitle {
        grid-area: subtitle;
        font-size: 11px;
        color: var(--text-secondary, #999);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .mention-row-subtitle--warning {
        color: var(--warning-color, #c47b3a);
      }
      .mention-row-subtitle--warning::after {
        content: " ⚠";
      }
      /* ——— categories 页的行（扁平单行，右侧 chevron 提示可进入）——— */
      .mention-row--category {
        grid-template-columns: 22px 1fr 16px;
        grid-template-areas: "icon label chev";
        padding: 9px 12px;
      }
      .mention-category-label {
        grid-area: label;
        font-size: 13.5px;
        font-weight: 400;
        color: var(--text-primary, #222);
      }
      .mention-category-chevron {
        grid-area: chev;
        color: var(--text-secondary, #aaa);
        display: inline-flex;
        align-items: center;
        justify-content: center;
      }
      .mention-row--category.mention-row--active .mention-category-chevron {
        color: var(--text-primary, #333);
      }
    `}</style>
  )
}
