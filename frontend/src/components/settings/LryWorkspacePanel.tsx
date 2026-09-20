import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  BookOpen,
  Eye,
  EyeOff,
  FileText,
  FolderOpen,
  Loader2,
  Lock,
  Map as MapIcon,
  RefreshCw,
  ScrollText,
  Workflow,
} from 'lucide-react'
import {
  formatBytes,
  formatRelativeTime,
  getLryCodemap,
  getLryMemoryFile,
  getLryNotes,
  getLryRule,
  getLryStatus,
  getLryWorkflow,
  listLryMemory,
  listLryRules,
  listLryWorkflows,
  type LryCodemapResponse,
  type LryMemoryListItem,
  type LryNotesResponse,
  type LryRuleListItem,
  type LryStatus,
  type LryWorkflowListItem,
} from '../../api/lry'

/* ============================================================================
   类型 / 常量
   ============================================================================ */

type LryTabKey = 'rules' | 'workflows' | 'notes' | 'memory' | 'codemap'

interface LryTabDef {
  key: LryTabKey
  label: string
  icon: React.ReactNode
}

const LRY_TABS: LryTabDef[] = [
  { key: 'rules', label: '规则', icon: <ScrollText size={14} /> },
  { key: 'workflows', label: '工作流', icon: <Workflow size={14} /> },
  { key: 'notes', label: '当下进度', icon: <FileText size={14} /> },
  { key: 'memory', label: '记忆', icon: <BookOpen size={14} /> },
  { key: 'codemap', label: '代码地图', icon: <MapIcon size={14} /> },
]

const SEVERITY_COLOR: Record<string, string> = {
  critical: 'var(--accent-red)',
  warning: 'var(--accent-amber, #d97706)',
  info: 'var(--text-tertiary)',
}

/* ============================================================================
   主组件
   ============================================================================ */

/**
 * 设置 → `.lry/` 工作区面板。
 *
 * 一次性加载 status，按需各 tab 加载列表。点条目展开右侧详情。
 *
 * 数据流：
 * - 顶部 status 一次性 fetch（counts / has_notes / has_codemap）
 * - 各 tab 进入时按需 fetch list
 * - 点条目按需 fetch 单文件 detail
 *
 * 不做：
 * - 写操作 / 编辑（设计文档 §10 P4 是 readonly view，写靠 LLM 工具）
 * - 实时刷新（rules 改后 prompt cache 都要重置，前端不该频繁触发）
 */
export default function LryWorkspacePanel() {
  const [status, setStatus] = useState<LryStatus | null>(null)
  const [statusLoading, setStatusLoading] = useState(true)
  const [statusErr, setStatusErr] = useState<string | null>(null)
  const [tab, setTab] = useState<LryTabKey>('rules')

  const refreshStatus = useCallback(async () => {
    setStatusLoading(true)
    setStatusErr(null)
    try {
      const result = await getLryStatus()
      setStatus(result)
    } catch (e) {
      setStatusErr(e instanceof Error ? e.message : String(e))
    } finally {
      setStatusLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshStatus()
  }, [refreshStatus])

  // 当前 tab 在 status 里的 count
  const currentCount = useMemo(() => {
    if (!status?.exists) return null
    if (tab === 'rules') return status.counts?.rules ?? 0
    if (tab === 'workflows') return status.counts?.workflows ?? 0
    if (tab === 'memory') return status.counts?.memory ?? 0
    if (tab === 'notes') return status.has_notes ? 1 : 0
    if (tab === 'codemap') return status.has_codemap ? 1 : 0
    return null
  }, [status, tab])

  if (statusLoading) {
    return (
      <div style={{ padding: 24 }}>
        <Loader2 size={16} className="spin" /> 加载 .lry/ 状态…
      </div>
    )
  }

  if (statusErr) {
    return (
      <div style={{ padding: 24, color: 'var(--accent-red)' }}>
        <AlertCircle size={16} /> 加载失败：{statusErr}
        <button
          type="button"
          className="toolbar-action-btn"
          onClick={refreshStatus}
          style={{ marginLeft: 12 }}
        >
          <RefreshCw size={13} /> 重试
        </button>
      </div>
    )
  }

  return (
    <div style={{ padding: 24, overflow: 'auto', flex: 1 }}>
      <Hero status={status} onRefresh={refreshStatus} />

      <div
        style={{
          display: 'flex',
          gap: 4,
          marginTop: 16,
          marginBottom: 12,
          borderBottom: '1px solid var(--border-color, #e5e5e5)',
        }}
      >
        {LRY_TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setTab(item.key)}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
              padding: '8px 14px',
              border: 'none',
              borderBottom:
                tab === item.key
                  ? '2px solid var(--accent-blue, #2563eb)'
                  : '2px solid transparent',
              background: 'transparent',
              cursor: 'pointer',
              color: tab === item.key ? 'var(--text-primary)' : 'var(--text-secondary)',
              fontSize: 13,
              fontWeight: tab === item.key ? 600 : 400,
              marginBottom: -1,
            }}
          >
            {item.icon}
            <span>{item.label}</span>
            {currentCount !== null &&
              tab === item.key &&
              (item.key === 'rules' ||
                item.key === 'workflows' ||
                item.key === 'memory') && (
                <span
                  style={{
                    background: 'var(--bg-secondary, #f1f5f9)',
                    color: 'var(--text-tertiary)',
                    padding: '1px 7px',
                    borderRadius: 10,
                    fontSize: 11,
                    fontWeight: 500,
                  }}
                >
                  {currentCount}
                </span>
              )}
          </button>
        ))}
      </div>

      <div style={{ marginTop: 16 }}>
        {!status?.exists ? (
          <EmptyState message=".lry/ 目录还不存在。柳如烟在干活时会按需创建。" />
        ) : (
          <>
            {tab === 'rules' && <RulesTab />}
            {tab === 'workflows' && <WorkflowsTab />}
            {tab === 'notes' && <NotesTab />}
            {tab === 'memory' && <MemoryTab />}
            {tab === 'codemap' && <CodemapTab />}
          </>
        )}
      </div>
    </div>
  )
}

/* ============================================================================
   Hero 状态卡
   ============================================================================ */

function Hero({
  status,
  onRefresh,
}: {
  status: LryStatus | null
  onRefresh: () => void
}) {
  return (
    <header
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 16,
        padding: 20,
        background: 'var(--bg-secondary, #f8fafc)',
        borderRadius: 8,
        border: '1px solid var(--border-color, #e5e5e5)',
      }}
    >
      <div
        style={{
          width: 44,
          height: 44,
          borderRadius: 10,
          background: 'var(--bg-primary, #fff)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--accent-blue, #2563eb)',
        }}
      >
        <FolderOpen size={22} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <h2 style={{ margin: '0 0 4px 0', fontSize: 16, fontWeight: 600 }}>
          .lry/ 工作区
        </h2>
        <p
          style={{
            margin: 0,
            fontSize: 12,
            color: 'var(--text-secondary)',
            wordBreak: 'break-all',
          }}
        >
          {status?.workspace_root ?? '未识别工作区根'}
        </p>
        {status?.exists ? (
          <div
            style={{
              display: 'flex',
              gap: 14,
              marginTop: 8,
              fontSize: 12,
              color: 'var(--text-tertiary)',
              flexWrap: 'wrap',
            }}
          >
            <span>规则 {status.counts?.rules ?? 0}</span>
            <span>·</span>
            <span>工作流 {status.counts?.workflows ?? 0}</span>
            <span>·</span>
            <span>记忆 {status.counts?.memory ?? 0}</span>
            {status.counts?.private_memory ? (
              <>
                <span>·</span>
                <span
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 3,
                    color: 'var(--accent-amber, #d97706)',
                  }}
                  title="私密记忆（.lry/private/memory/，不入 git）"
                >
                  <Lock size={11} />
                  私密 {status.counts.private_memory}
                </span>
              </>
            ) : null}
            <span>·</span>
            <span>{status.has_notes ? '✓ NOTES' : '— NOTES'}</span>
            <span>·</span>
            <span>{status.has_codemap ? '✓ CODE_MAP' : '— CODE_MAP'}</span>
            {status.has_gitignore ? (
              <>
                <span>·</span>
                <span title=".lry/.gitignore 已存在，private/ 已被忽略">✓ .gitignore</span>
              </>
            ) : null}
          </div>
        ) : (
          <p
            style={{
              margin: '8px 0 0 0',
              fontSize: 12,
              color: 'var(--text-tertiary)',
            }}
          >
            柳如烟在干活时会按需创建 .lry/ 目录。
          </p>
        )}
      </div>
      <button
        type="button"
        onClick={onRefresh}
        title="刷新"
        style={{
          background: 'transparent',
          border: '1px solid var(--border-color, #e5e5e5)',
          padding: '6px 10px',
          borderRadius: 6,
          cursor: 'pointer',
          color: 'var(--text-secondary)',
          display: 'inline-flex',
          alignItems: 'center',
          gap: 4,
          fontSize: 12,
        }}
      >
        <RefreshCw size={13} /> 刷新
      </button>
    </header>
  )
}

function EmptyState({ message }: { message: string }) {
  return (
    <div
      style={{
        padding: 32,
        textAlign: 'center',
        color: 'var(--text-tertiary)',
        fontSize: 13,
        background: 'var(--bg-secondary, #f8fafc)',
        borderRadius: 8,
        border: '1px dashed var(--border-color, #e5e5e5)',
      }}
    >
      {message}
    </div>
  )
}

function ContentPane({
  title,
  meta,
  content,
}: {
  title: string
  meta?: string
  content: string
}) {
  return (
    <div
      style={{
        marginTop: 12,
        background: 'var(--bg-primary, #fff)',
        border: '1px solid var(--border-color, #e5e5e5)',
        borderRadius: 8,
        padding: 16,
        maxHeight: 480,
        overflow: 'auto',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          marginBottom: 8,
        }}
      >
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600 }}>{title}</h3>
        {meta ? (
          <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>{meta}</span>
        ) : null}
      </div>
      <pre
        style={{
          margin: 0,
          fontFamily:
            'ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
          fontSize: 12,
          lineHeight: 1.6,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          color: 'var(--text-primary)',
        }}
      >
        {content}
      </pre>
    </div>
  )
}

/* ============================================================================
   Rules tab
   ============================================================================ */

function RulesTab() {
  const [items, setItems] = useState<LryRuleListItem[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [content, setContent] = useState<string>('')
  const [contentLoading, setContentLoading] = useState(false)

  useEffect(() => {
    listLryRules()
      .then((data) => setItems(data.items))
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [])

  useEffect(() => {
    if (!selected) {
      setContent('')
      return
    }
    setContentLoading(true)
    getLryRule(selected)
      .then((d) => setContent(d.content))
      .catch((e) => setContent(`# 加载失败\n${e}`))
      .finally(() => setContentLoading(false))
  }, [selected])

  if (err) return <div style={{ color: 'var(--accent-red)' }}>{err}</div>
  if (!items) return <Loader2 size={16} className="spin" />
  if (items.length === 0)
    return <EmptyState message="还没有规则。柳如烟干活时会用 add_project_rule 添加。" />

  return (
    <div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {items.map((item) => (
          <button
            key={item.slug}
            type="button"
            onClick={() => setSelected(item.slug === selected ? null : item.slug)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              padding: '10px 14px',
              border: `1px solid ${
                selected === item.slug
                  ? 'var(--accent-blue, #2563eb)'
                  : 'var(--border-color, #e5e5e5)'
              }`,
              borderRadius: 6,
              cursor: 'pointer',
              background:
                selected === item.slug ? 'var(--bg-secondary, #f8fafc)' : 'transparent',
              textAlign: 'left',
            }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: SEVERITY_COLOR[item.severity] ?? SEVERITY_COLOR.info,
                flexShrink: 0,
              }}
              title={item.severity}
            />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div
                style={{
                  fontSize: 13,
                  fontWeight: 500,
                  color: 'var(--text-primary)',
                }}
              >
                {item.slug}
              </div>
              <div
                style={{
                  fontSize: 11,
                  color: 'var(--text-tertiary)',
                  marginTop: 2,
                }}
              >
                {item.severity}
                {item.added ? ` · ${item.added}` : ''}
                {item.by ? ` · by ${item.by}` : ''}
                {' · '}
                {formatBytes(item.size)}
              </div>
            </div>
            <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
              {formatRelativeTime(item.mtime)}
            </span>
          </button>
        ))}
      </div>
      {selected && (
        <ContentPane
          title={selected}
          meta={contentLoading ? '加载中…' : undefined}
          content={content}
        />
      )}
    </div>
  )
}

/* ============================================================================
   Workflows tab
   ============================================================================ */

function WorkflowsTab() {
  const [items, setItems] = useState<LryWorkflowListItem[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [content, setContent] = useState<string>('')

  useEffect(() => {
    listLryWorkflows()
      .then((data) => setItems(data.items))
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [])

  useEffect(() => {
    if (!selected) {
      setContent('')
      return
    }
    getLryWorkflow(selected)
      .then((d) => setContent(d.content))
      .catch((e) => setContent(`# 加载失败\n${e}`))
  }, [selected])

  if (err) return <div style={{ color: 'var(--accent-red)' }}>{err}</div>
  if (!items) return <Loader2 size={16} className="spin" />
  if (items.length === 0)
    return <EmptyState message="还没有工作流。柳如烟干活时会用 add_project_workflow 添加。" />

  return (
    <div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {items.map((item) => (
          <button
            key={item.name}
            type="button"
            onClick={() => setSelected(item.name === selected ? null : item.name)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              padding: '10px 14px',
              border: `1px solid ${
                selected === item.name
                  ? 'var(--accent-blue, #2563eb)'
                  : 'var(--border-color, #e5e5e5)'
              }`,
              borderRadius: 6,
              cursor: 'pointer',
              background:
                selected === item.name ? 'var(--bg-secondary, #f8fafc)' : 'transparent',
              textAlign: 'left',
            }}
          >
            <Workflow size={14} style={{ color: 'var(--text-tertiary)', flexShrink: 0 }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, fontWeight: 500 }}>{item.name}</div>
              {item.description ? (
                <div
                  style={{
                    fontSize: 11,
                    color: 'var(--text-secondary)',
                    marginTop: 2,
                  }}
                >
                  {item.description}
                </div>
              ) : null}
              <div
                style={{
                  fontSize: 11,
                  color: 'var(--text-tertiary)',
                  marginTop: 2,
                }}
              >
                {item.added ? `${item.added} · ` : ''}
                {item.by ? `by ${item.by} · ` : ''}
                {formatBytes(item.size)}
              </div>
            </div>
            <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
              {formatRelativeTime(item.mtime)}
            </span>
          </button>
        ))}
      </div>
      {selected && <ContentPane title={selected} content={content} />}
    </div>
  )
}

/* ============================================================================
   Notes tab
   ============================================================================ */

function NotesTab() {
  const [data, setData] = useState<LryNotesResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    getLryNotes()
      .then(setData)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [])

  if (err) return <div style={{ color: 'var(--accent-red)' }}>{err}</div>
  if (!data) return <Loader2 size={16} className="spin" />
  if (!data.exists)
    return (
      <EmptyState message="还没有 PROJECT_NOTES.md。柳如烟会用 update_project_notes 自动维护当下进度。" />
    )

  const meta = `${formatBytes(data.size ?? 0)} · 修改于 ${formatRelativeTime(
    data.mtime ?? Date.now() / 1000,
  )}`

  return <ContentPane title="PROJECT_NOTES.md" meta={meta} content={data.content} />
}

/* ============================================================================
   Memory tab
   ============================================================================ */

/**
 * Memory tab 内部 key：{filename, private}。单纯 filename 不够——
 * private 与 public 目录理论上可以同名（不同目录下），所以 selected 需带来源。
 */
interface MemorySelection {
  filename: string
  isPrivate: boolean
}

function MemoryTab() {
  const [items, setItems] = useState<LryMemoryListItem[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [selected, setSelected] = useState<MemorySelection | null>(null)
  const [content, setContent] = useState<string>('')
  // 显示 private：默认关；打开会重新 fetch 合并列表。
  // 用 sessionStorage-less 本地 state —— 不跨面板保留，避免用户离开后忘了自己开过。
  const [showPrivate, setShowPrivate] = useState(false)

  const refreshList = useCallback((includePrivate: boolean) => {
    setItems(null)
    setErr(null)
    listLryMemory({ includePrivate })
      .then((d) => setItems(d.items))
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [])

  useEffect(() => {
    refreshList(showPrivate)
  }, [showPrivate, refreshList])

  useEffect(() => {
    if (!selected) {
      setContent('')
      return
    }
    getLryMemoryFile(selected.filename, { isPrivate: selected.isPrivate })
      .then((d) => setContent(d.content))
      .catch((e) => setContent(`# 加载失败\n${e}`))
  }, [selected])

  const toggleLabel = showPrivate ? '隐藏私密' : '显示私密'
  const ToggleIcon = showPrivate ? EyeOff : Eye

  if (err) return <div style={{ color: 'var(--accent-red)' }}>{err}</div>

  return (
    <div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'flex-end',
          marginBottom: 8,
        }}
      >
        <button
          type="button"
          onClick={() => {
            setShowPrivate((v) => !v)
            setSelected(null)
          }}
          title="切换是否合并 .lry/private/memory/ 的条目"
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 5,
            fontSize: 12,
            padding: '4px 10px',
            border: '1px solid var(--border-color, #e5e5e5)',
            borderRadius: 6,
            background: showPrivate
              ? 'var(--bg-secondary, #f8fafc)'
              : 'transparent',
            color: 'var(--text-secondary)',
            cursor: 'pointer',
          }}
        >
          <ToggleIcon size={13} />
          {toggleLabel}
        </button>
      </div>

      {!items ? (
        <Loader2 size={16} className="spin" />
      ) : items.length === 0 ? (
        <EmptyState
          message={
            showPrivate
              ? '还没有任何记忆（公开或私密）。柳如烟会用 save_project_memory 沉淀。'
              : '还没有记忆。柳如烟会用 save_project_memory 沉淀历史 / 教训 / 决策。'
          }
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {items.map((item) => {
            const isSelected =
              selected?.filename === item.filename &&
              selected?.isPrivate === Boolean(item.private)
            const isPrivate = Boolean(item.private)
            return (
              <button
                key={`${isPrivate ? 'priv' : 'pub'}:${item.filename}`}
                type="button"
                onClick={() =>
                  setSelected(
                    isSelected
                      ? null
                      : { filename: item.filename, isPrivate },
                  )
                }
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                  padding: '10px 14px',
                  border: `1px solid ${
                    isSelected
                      ? 'var(--accent-blue, #2563eb)'
                      : isPrivate
                        ? 'var(--accent-amber, #d97706)'
                        : 'var(--border-color, #e5e5e5)'
                  }`,
                  borderRadius: 6,
                  cursor: 'pointer',
                  background: isSelected
                    ? 'var(--bg-secondary, #f8fafc)'
                    : 'transparent',
                  textAlign: 'left',
                }}
              >
                {item.kind ? (
                  <span
                    style={{
                      padding: '1px 6px',
                      fontSize: 10,
                      borderRadius: 4,
                      background: 'var(--bg-secondary, #f1f5f9)',
                      color: 'var(--text-secondary)',
                      fontWeight: 500,
                      flexShrink: 0,
                    }}
                  >
                    {item.kind}
                  </span>
                ) : null}
                {isPrivate ? (
                  <span
                    title="私密记忆 · .lry/private/memory/（不入 git）"
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: 3,
                      padding: '1px 5px',
                      fontSize: 10,
                      borderRadius: 4,
                      background: 'rgba(217, 119, 6, 0.12)',
                      color: 'var(--accent-amber, #d97706)',
                      fontWeight: 600,
                      flexShrink: 0,
                    }}
                  >
                    <Lock size={10} />
                    私密
                  </span>
                ) : null}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 13,
                      fontWeight: 500,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {item.topic ?? item.filename}
                  </div>
                  <div
                    style={{
                      fontSize: 11,
                      color: 'var(--text-tertiary)',
                      marginTop: 2,
                    }}
                  >
                    {item.filename}
                    {item.by ? ` · by ${item.by}` : ''}
                    {' · '}
                    {formatBytes(item.size)}
                  </div>
                </div>
                <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                  {formatRelativeTime(item.mtime)}
                </span>
              </button>
            )
          })}
        </div>
      )}
      {selected && (
        <ContentPane
          title={`${selected.filename}${selected.isPrivate ? '  ·  🔒 private' : ''}`}
          content={content}
        />
      )}
    </div>
  )
}

/* ============================================================================
   Codemap tab
   ============================================================================ */

function CodemapTab() {
  const [data, setData] = useState<LryCodemapResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    getLryCodemap()
      .then(setData)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
  }, [])

  if (err) return <div style={{ color: 'var(--accent-red)' }}>{err}</div>
  if (!data) return <Loader2 size={16} className="spin" />
  if (!data.exists)
    return (
      <EmptyState message="还没有 CODE_MAP.md。可以让柳如烟整理一份并用 write_file 写到 .lry/CODE_MAP.md。" />
    )

  const meta = `${formatBytes(data.size ?? 0)} · 修改于 ${formatRelativeTime(
    data.mtime ?? Date.now() / 1000,
  )}`
  return <ContentPane title="CODE_MAP.md" meta={meta} content={data.content} />
}
