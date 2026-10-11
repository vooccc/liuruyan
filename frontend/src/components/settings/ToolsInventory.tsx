import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  Check,
  Edit3,
  Loader2,
  Lock,
  Plus,
  RotateCcw,
  Save,
  Search,
  ShieldAlert,
  Users,
  Wrench,
  X,
} from 'lucide-react'
import ToolInspectIcon from '../icons/ToolInspectIcon'
import {
  getToolsInventory,
  setAgentAllowedTools,
  setAgentToolPreset,
  setToolDescription,
  toggleDevMode,
  type AgentInventoryItem,
  type ToolInventoryItem,
  type ToolPreset,
  type ToolSource,
  type ToolsInventoryResponse,
} from '../../api/toolsInventory'
import { getAvatarSrc, getAvatarFallback } from '../avatarUtils'
import { useConfirmDialog } from '../ConfirmDialog'

/** 工具来源的中文显示名（分组标题 / 过滤 pill / 详情页共用） */
const SOURCE_LABEL: Record<ToolSource, string> = { builtin: '内置', ide: 'IDE', mcp: 'MCP' }

/** Token 预算警戒线（粗估）。超过 → hero 显示警告 */
const TOKEN_WARN = 8000
const TOKEN_DANGER = 12000
/** 进入开发者模式的隐藏手势：连续点击 「内置」 stat N 次 */
const DEV_GESTURE_CLICKS = 5
/** 两次点击间最大间隔（ms），超过则计数清零 */
const DEV_GESTURE_TIMEOUT_MS = 1500
/** 容器宽度阈值：小于此值时左右两栏改竖排堆叠 */
const NARROW_THRESHOLD_PX = 720

/**
 * 观察元素宽度是否窄于阈值（用于响应式左右两栏 <=> 上下堆叠切换）。
 *
 * 为什么不用 CSS media query：组件嵌在 settings-tab-content 里，
 * 父容器宽度跟 viewport 宽度不是线性关系（viewport 1200 但 tabs 栏占 220 → 容器 980）。
 * 用 ResizeObserver 观察容器本身宽度才精准。
 */
function useNarrow(
  ref: React.RefObject<HTMLElement | null>,
  threshold = NARROW_THRESHOLD_PX,
): boolean {
  const [narrow, setNarrow] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0
      setNarrow(w < threshold)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref, threshold])
  return narrow
}

/**
 * 设置 → 工具。
 *
 * 视图：
 * - 按角色（默认）：左角色列表 / 右角色工具配置面板——档位预设（极简/全开）一键切换，
 *   或自定义白名单逐个勾选（一次性圈一批工具给该角色）
 * - 按工具：左工具列表（按名称/描述搜索） / 右工具详情（description、schema、哪些角色启用）
 *
 * 档位预设定义在后端 backend.tool_presets（单一事实源），前端只渲染不重复实现；
 * 自定义白名单才是逐工具勾选。两种配置都直接影响该角色 prompt 的 token 占用。
 *
 * Dev mode（env LIURUYAN_DEV_MODE=1 启用）：
 * - 工具详情面板出现「编辑 description」按钮
 * - 写入 data/tool_overrides.json，立即生效到后续 LLM 调用
 * - 普通模式下后端 PUT /tools/inventory/{name}/description 返 403
 */
export default function ToolsInventory() {
  const [data, setData] = useState<ToolsInventoryResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  // Tauri webview 不支持 window.confirm，统一走自定义对话框
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog()
  // 视图模式持久化：切到别的设置 tab 再回来不重置
  const [viewMode, setViewMode] = useState<'tools' | 'agents'>(() => {
    try {
      return localStorage.getItem('toolsInventory.viewMode') === 'tools' ? 'tools' : 'agents'
    } catch {
      return 'agents'
    }
  })
  useEffect(() => {
    try { localStorage.setItem('toolsInventory.viewMode', viewMode) } catch { /* ignore */ }
  }, [viewMode])
  // 按角色列表的预算筛选：全部 / 超标 / 偏高（hero 的紧凑提示一键切入）
  const [agentFilter, setAgentFilter] = useState<'all' | 'danger' | 'warn'>('all')
  // 点击 5 次进 dev 手势状态
  const [clickCount, setClickCount] = useState(0)
  const [devToggleErr, setDevToggleErr] = useState<string | null>(null)
  const [devFlash, setDevFlash] = useState<string | null>(null)
  // 用 ref 持久化 timer id，避免 re-render 把 let 变量重置丢失旧 timer 引用
  const gestureTimerRef = useRef<number | null>(null)
  useEffect(() => {
    return () => {
      if (gestureTimerRef.current !== null) window.clearTimeout(gestureTimerRef.current)
    }
  }, [])

  const refresh = async () => {
    setLoading(true)
    setErr(null)
    try {
      const payload = await getToolsInventory()
      setData(payload)
    } catch (exc) {
      setErr(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (loading && !data) {
    return (
      <div className="settings-permissions">
        <div className="settings-permissions-loading">
          <Loader2 size={14} className="spin" /> 加载工具清单中…
        </div>
      </div>
    )
  }

  if (err && !data) {
    return (
      <div className="settings-permissions">
        <div className="settings-permissions-load-err">
          <AlertCircle size={14} /> {err}
          <button type="button" className="guardian-bar-btn" onClick={refresh}>重试</button>
        </div>
      </div>
    )
  }

  if (!data) return null

  // 全局预算警告：找出超过阈值的 agent
  const overBudgetAgents = data.agents.filter((a) => a.approx_tokens > TOKEN_WARN)
  const dangerAgents = data.agents.filter((a) => a.approx_tokens > TOKEN_DANGER)

  const devState = data.dev_mode_state

  // 点击“内置” stat 进 dev。env 已开 不需手势。
  const handleSecretClick = async () => {
    if (devState.via_env) return // env 开后手势无义
    if (devState.enabled) return // 已开不重复触发
    setDevToggleErr(null)
    if (gestureTimerRef.current !== null) {
      window.clearTimeout(gestureTimerRef.current)
      gestureTimerRef.current = null
    }
    const next = clickCount + 1
    setClickCount(next)
    if (next >= DEV_GESTURE_CLICKS) {
      setClickCount(0)
      try {
        await toggleDevMode(true)
        await refresh()
        // 不弹任何成功提示——dev banner 自然出现就是确认
      } catch (exc) {
        setDevToggleErr(exc instanceof Error ? exc.message : String(exc))
      }
      return
    }
    gestureTimerRef.current = window.setTimeout(() => {
      setClickCount(0)
      gestureTimerRef.current = null
    }, DEV_GESTURE_TIMEOUT_MS)
  }

  const handleExitDev = async () => {
    if (!devState.can_toggle_off) return
    const ok = await askConfirm(
      '退出开发者模式',
      '退出开发者模式？已存在的 description 覆盖仍然生效，只是不能再编辑。',
      'warning',
      '退出',
    )
    if (!ok) return
    setDevToggleErr(null)
    try {
      await toggleDevMode(false)
      await refresh()
      setDevFlash('已退出开发者模式')
      window.setTimeout(() => setDevFlash(null), 2500)
    } catch (exc) {
      setDevToggleErr(exc instanceof Error ? exc.message : String(exc))
    }
  }

  return (
    <div className="settings-permissions" style={{ display: 'flex', flexDirection: 'column' }}>
      {/* ======= Hero ======= */}
      <header
        className="settings-permissions-hero"
        style={{ flexWrap: 'wrap', alignItems: 'flex-start' }}
      >
        <div className="settings-permissions-hero-icon">
          <ToolInspectIcon size={20} />
        </div>
        {/* flex-basis 360 让窗口较窄时 stats wrap 到下一行（默认 min-width:0 会让 text 一直挤压 stats） */}
        <div className="settings-permissions-hero-text" style={{ flex: '1 1 360px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 4 }}>
            <h2 style={{ margin: 0 }}>工具</h2>
            {(overBudgetAgents.length > 0 || dangerAgents.length > 0) && (
              <BudgetNotice
                dangerCount={dangerAgents.length}
                warnCount={overBudgetAgents.length - dangerAgents.length}
                onFocusDanger={() => {
                  setViewMode('agents')
                  setAgentFilter('danger')
                }}
              />
            )}
          </div>
          <p>
            列出系统当前可用的所有工具（内置 / MCP / IDE 桥接），可查看每个工具注入给模型的 description 与参数 schema。
          </p>
          {devToggleErr && (
            <div style={{ ...errorBannerStyle, marginTop: 8, marginBottom: 0 }}>
              <AlertCircle size={13} style={{ verticalAlign: '-2px', marginRight: 4 }} />
              {devToggleErr}
            </div>
          )}
          {devFlash && (
            <div
              style={{
                marginTop: 8,
                padding: '6px 10px',
                fontSize: 12.5,
                border: '1px solid #2e9e5f',
                borderRadius: 6,
                background: 'rgba(46,158,95,0.10)',
                color: '#2e9e5f',
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
              }}
            >
              <Check size={13} /> {devFlash}
            </div>
          )}
          {devState.enabled && (
            <div
              style={{
                marginTop: 8,
                padding: '6px 10px',
                fontSize: 12.5,
                border: '1px solid #b58900',
                borderRadius: 6,
                background: 'rgba(181,137,0,0.10)',
                color: '#b58900',
                display: 'inline-flex',
                alignItems: 'center',
                gap: 8,
                flexWrap: 'wrap',
              }}
            >
              <ShieldAlert size={13} />
              <span>
                <strong>开发者模式已开启</strong>
                {devState.via_env ? '（env LIURUYAN_DEV_MODE）' : '（手势激活）'}
                ：可编辑内置工具 description。
              </span>
              {devState.can_toggle_off && (
                <button
                  type="button"
                  onClick={handleExitDev}
                  style={{
                    padding: '2px 8px',
                    fontSize: 11.5,
                    border: '1px solid #b58900',
                    borderRadius: 6,
                    background: 'transparent',
                    color: '#b58900',
                    cursor: 'pointer',
                  }}
                >
                  退出开发者模式
                </button>
              )}
            </div>
          )}
        </div>
        <div
          className="settings-permissions-hero-stats"
          style={{
            // flex: '0 0 auto' = 不 shrink，保持自然宽度；
            // 宽度不够时与 text 同在一行 → wrap 到下一行。
            // 不要用 '0 1 auto'，那会只挤压不换行。
            flex: '0 0 auto',
            flexWrap: 'wrap',
            justifyContent: 'flex-end',
          }}
        >
          <Stat label="总工具" value={data.totals.total} />
          <Stat
            label="内置"
            value={data.totals.builtin}
            onClick={devState.enabled || devState.via_env ? undefined : handleSecretClick}
          />
          <Stat label="IDE" value={data.totals.ide} />
          <Stat label="MCP" value={data.totals.mcp} />
        </div>
      </header>

      {/* ======= 视图切换 ======= */}
      <div
        style={{
          display: 'flex',
          gap: 4,
          padding: '8px 16px 0',
          borderBottom: '1px solid var(--border-color)',
        }}
      >
        <ViewModeTab
          active={viewMode === 'agents'}
          onClick={() => setViewMode('agents')}
          icon={<Users size={13} />}
          label="按角色"
        />
        <ViewModeTab
          active={viewMode === 'tools'}
          onClick={() => setViewMode('tools')}
          icon={<Wrench size={13} />}
          label="按工具"
        />
      </div>

      {/* ======= 主体 ======= */}
      {viewMode === 'tools' ? (
        <ToolsView data={data} onMutated={refresh} />
      ) : (
        <AgentsView
          data={data}
          onMutated={refresh}
          filter={agentFilter}
          onFilterChange={setAgentFilter}
        />
      )}
      {confirmDialog}
    </div>
  )
}

// ============================================================
// Hero 组件
// ============================================================

function Stat({
  label,
  value,
  onClick,
}: {
  label: string
  value: number | string
  onClick?: () => void
}) {
  // 即使有 onClick 也不给 cursor:pointer / hover / outline〔进入 dev 手势需要隐蔽，
  // 与普通 stat 视觉上不可区别〕
  return (
    <div
      className="settings-permissions-stat"
      onClick={onClick}
      style={onClick ? { userSelect: 'none' } : undefined}
    >
      <span className="settings-permissions-stat-value">{value}</span>
      <span className="settings-permissions-stat-label">{label}</span>
    </div>
  )
}

/** 标题行右侧的紧凑预算提示：只报数量，不占版面；点「只看超标」去按角色列表筛 */
function BudgetNotice({
  dangerCount,
  warnCount,
  onFocusDanger,
}: {
  dangerCount: number
  warnCount: number
  onFocusDanger: () => void
}) {
  const danger = dangerCount > 0
  return (
    <div
      style={{
        padding: '2px 10px',
        fontSize: 11.5,
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        border: `1px solid ${danger ? '#e5484d' : '#d99e2b'}`,
        borderRadius: 6,
        background: danger ? 'rgba(229,72,77,0.10)' : 'rgba(217,158,43,0.10)',
        color: danger ? '#e5484d' : '#9c6b00',
      }}
    >
      <AlertTriangle size={12} style={{ flexShrink: 0 }} />
      <span>
        工具预算：{danger && <strong>{dangerCount} 个角色超标</strong>}
        {danger && warnCount > 0 && '、'}
        {warnCount > 0 && <span>{warnCount} 个偏高</span>}
      </span>
      <button
        type="button"
        onClick={onFocusDanger}
        style={{
          padding: '1px 8px',
          fontSize: 11,
          border: '1px solid currentColor',
          borderRadius: 10,
          background: 'transparent',
          color: 'inherit',
          cursor: 'pointer',
        }}
      >
        只看超标
      </button>
    </div>
  )
}

function ViewModeTab({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean
  onClick: () => void
  icon: React.ReactNode
  label: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '8px 14px',
        fontSize: 13.5,
        border: 'none',
        borderBottom: active ? '2px solid var(--primary-color)' : '2px solid transparent',
        background: 'transparent',
        color: active ? 'var(--primary-color)' : 'var(--text-secondary)',
        cursor: 'pointer',
        fontWeight: active ? 600 : 400,
      }}
    >
      {icon}
      {label}
    </button>
  )
}

// ============================================================
// 按工具看视图
// ============================================================

function ToolsView({
  data,
  onMutated,
}: {
  data: ToolsInventoryResponse
  onMutated: () => Promise<void> | void
}) {
  const sectionRef = useRef<HTMLElement>(null)
  const narrow = useNarrow(sectionRef)
  const [query, setQuery] = useState('')
  const [sourceFilter, setSourceFilter] = useState<'all' | ToolSource>('all')
  const [selectedName, setSelectedName] = useState<string | null>(
    data.tools.length > 0 ? data.tools[0].name : null,
  )

  // 当前选中工具消失（被 MCP 注销等）→ 重新选第一个
  useEffect(() => {
    if (selectedName && !data.tools.some((t) => t.name === selectedName)) {
      setSelectedName(data.tools[0]?.name ?? null)
    }
  }, [data.tools, selectedName])

  const filteredTools = useMemo(() => {
    const q = query.trim().toLowerCase()
    return data.tools.filter((t) => {
      if (sourceFilter !== 'all' && t.source !== sourceFilter) return false
      if (!q) return true
      return t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)
    })
  }, [data.tools, query, sourceFilter])

  const grouped = useMemo(() => {
    const g: Record<ToolSource, ToolInventoryItem[]> = { builtin: [], ide: [], mcp: [] }
    for (const t of filteredTools) g[t.source].push(t)
    return g
  }, [filteredTools])

  const selectedTool = useMemo(
    () => data.tools.find((t) => t.name === selectedName) ?? null,
    [data.tools, selectedName],
  )

  return (
    <section
      ref={sectionRef}
      style={{
        display: 'flex',
        flex: 1,
        minHeight: 0,
        minWidth: 0,
        gap: 12,
        padding: '12px 16px',
        // narrow: 竖排堆叠 + 整体滚动；wide: 横排 + 两栏各自内滚
        flexDirection: narrow ? 'column' : 'row',
        overflow: narrow ? 'auto' : 'hidden',
      }}
    >
      <aside
        style={{
          // 宽屏：左侧固定 320 可压到 200；窄屏：100% 宽 + 高度上限
          flex: narrow ? '0 0 auto' : '0 1 320px',
          width: narrow ? '100%' : undefined,
          minWidth: narrow ? 0 : 200,
          maxHeight: narrow ? '42vh' : undefined,
          display: 'flex',
          flexDirection: 'column',
          border: '1px solid var(--border-color)',
          borderRadius: 8,
          background: 'var(--bg-secondary, #fafafa)',
          overflow: 'hidden',
        }}
      >
        <div style={{ padding: '10px 10px 6px', borderBottom: '1px solid var(--border-color)' }}>
          <div style={{ position: 'relative' }}>
            <Search
              size={13}
              style={{
                position: 'absolute',
                left: 8,
                top: '50%',
                transform: 'translateY(-50%)',
                color: 'var(--text-secondary)',
              }}
            />
            <input
              type="text"
              placeholder="按名称 / 描述搜索…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              style={searchInputStyle}
            />
          </div>
          <div style={{ display: 'flex', gap: 4, marginTop: 6 }}>
            {(['all', 'builtin', 'ide', 'mcp'] as const).map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setSourceFilter(s)}
                style={pillStyle(sourceFilter === s)}
              >
                {s === 'all' ? '全部' : SOURCE_LABEL[s]}
              </button>
            ))}
          </div>
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: '4px 0' }}>
          {(['builtin', 'ide', 'mcp'] as ToolSource[]).map((src) => {
            const items = grouped[src]
            if (items.length === 0) return null
            return (
              <div key={src}>
                <div style={listGroupHeaderStyle}>{SOURCE_LABEL[src]} · {items.length}</div>
                {items.map((t) => (
                  <button
                    key={t.name}
                    type="button"
                    onClick={() => setSelectedName(t.name)}
                    style={listItemStyle(selectedName === t.name)}
                  >
                    <span style={{ fontFamily: 'var(--font-mono, monospace)', fontSize: 13, display: 'inline-flex', alignItems: 'center', gap: 4, minWidth: 0, wordBreak: 'break-all' }}>
                      {t.is_overridden && (
                        <Edit3 size={11} style={{ color: '#b58900', flexShrink: 0 }} aria-label="已被 dev 覆盖" />
                      )}
                      {t.name}
                    </span>
                    <span style={{ fontSize: 11, color: 'var(--text-tertiary, #888)', flexShrink: 0, marginLeft: 4 }}>~{t.approx_tokens}t</span>
                  </button>
                ))}
              </div>
            )
          })}
          {filteredTools.length === 0 && (
            <div style={{ padding: 12, fontSize: 12, color: 'var(--text-secondary)' }}>无匹配工具</div>
          )}
        </div>
      </aside>

      <div
        style={{
          ...detailPanelStyle,
          // 窄屏时 detail 占满宽度 + 保证最小高度能看到内容
          width: narrow ? '100%' : undefined,
          minHeight: narrow ? '60vh' : undefined,
        }}
      >
        {selectedTool ? (
          <ToolDetail
            tool={selectedTool}
            agents={data.agents}
            devMode={data.dev_mode}
            onMutated={onMutated}
          />
        ) : (
          <div style={{ padding: 24, fontSize: 13, color: 'var(--text-secondary)' }}>
            从左侧选择一个工具查看详情
          </div>
        )}
      </div>
    </section>
  )
}

// ============================================================
// 工具详情面板（含 dev edit）
// ============================================================

interface ToolDetailProps {
  tool: ToolInventoryItem
  agents: AgentInventoryItem[]
  devMode: boolean
  onMutated: () => Promise<void> | void
}

function ToolDetail({ tool, agents, devMode, onMutated }: ToolDetailProps) {
  const [busyAgentId, setBusyAgentId] = useState<string | null>(null)
  const [opErr, setOpErr] = useState<string | null>(null)
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog()

  // dev edit 状态
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [savingDesc, setSavingDesc] = useState(false)

  const canEdit = devMode && tool.can_edit_description

  // 切换工具时重置编辑态
  useEffect(() => {
    setEditing(false)
    setDraft('')
    setOpErr(null)
  }, [tool.name])

  const buckets = useMemo(() => {
    const allowedExplicit: AgentInventoryItem[] = []
    const allOpen: AgentInventoryItem[] = []
    const presetIncluded: AgentInventoryItem[] = []
    const presetExcluded: AgentInventoryItem[] = []
    const notAllowed: AgentInventoryItem[] = []
    for (const a of agents) {
      if (a.scope === 'preset') {
        // 档位角色：allowed_tools 是后端展开的档位列表，是否含此工具由档位定义决定，
        // 不能在此单独增删——去「按角色」调档位
        ;(a.allowed_tools?.includes(tool.name) ? presetIncluded : presetExcluded).push(a)
      } else if (a.allowed_tools === null) allOpen.push(a)
      else if (a.allowed_tools.includes(tool.name)) allowedExplicit.push(a)
      else notAllowed.push(a)
    }
    return { allowedExplicit, allOpen, presetIncluded, presetExcluded, notAllowed }
  }, [agents, tool.name])

  const handleAddAgent = async (agent: AgentInventoryItem) => {
    if (agent.allowed_tools === null) return
    setBusyAgentId(agent.id)
    setOpErr(null)
    try {
      await setAgentAllowedTools(agent.id, [...agent.allowed_tools, tool.name])
      await onMutated()
    } catch (exc) {
      setOpErr(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusyAgentId(null)
    }
  }

  const handleRemoveAgent = async (agent: AgentInventoryItem) => {
    if (agent.allowed_tools === null) return
    setBusyAgentId(agent.id)
    setOpErr(null)
    try {
      await setAgentAllowedTools(
        agent.id,
        agent.allowed_tools.filter((n) => n !== tool.name),
      )
      await onMutated()
    } catch (exc) {
      setOpErr(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusyAgentId(null)
    }
  }

  const startEditing = () => {
    setDraft(tool.description)
    setEditing(true)
    setOpErr(null)
  }

  const saveEdit = async () => {
    setSavingDesc(true)
    setOpErr(null)
    try {
      await setToolDescription(tool.name, draft)
      await onMutated()
      setEditing(false)
    } catch (exc) {
      setOpErr(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setSavingDesc(false)
    }
  }

  const resetToBaseline = async () => {
    const ok = await askConfirm(
      '恢复默认 description',
      `确认把 ${tool.name} 的 description 恢复到源码原版？`,
      'warning',
      '恢复',
    )
    if (!ok) return
    setSavingDesc(true)
    setOpErr(null)
    try {
      await setToolDescription(tool.name, null)
      await onMutated()
      setEditing(false)
    } catch (exc) {
      setOpErr(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setSavingDesc(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div style={detailHeaderStyle}>
        <Wrench size={18} style={{ color: 'var(--primary-color)' }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontFamily: 'var(--font-mono, monospace)', fontSize: 14, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', wordBreak: 'break-all' }}>
            <span style={{ wordBreak: 'break-all' }}>{tool.name}</span>
            {tool.is_overridden && (
              <span
                title="此工具的 description 已被 dev override 覆盖"
                style={{ fontSize: 10, padding: '1px 6px', borderRadius: 8, background: 'rgba(181,137,0,0.18)', color: '#b58900' }}
              >
                overridden
              </span>
            )}
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 2 }}>
            {SOURCE_LABEL[tool.source]} · 并发类 {tool.concurrency_class} · description {tool.description_chars} 字 · 约 {tool.approx_tokens} token（含 schema）
          </div>
        </div>
        {!canEdit && (
          <>
            <Lock size={12} style={{ color: 'var(--text-tertiary, #888)' }} />
            <span style={{ fontSize: 10.5, color: 'var(--text-tertiary, #888)' }}>只读</span>
          </>
        )}
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '12px 16px' }}>
        {/* Description（编辑/只读） */}
        <Section
          title="Description（注入给模型的提示语）"
          right={
            canEdit && !editing ? (
              <div style={{ display: 'inline-flex', gap: 6 }}>
                <button type="button" style={chipBtnStyle()} onClick={startEditing}>
                  <Edit3 size={11} /> 编辑
                </button>
                {tool.is_overridden && (
                  <button type="button" style={chipBtnStyle()} onClick={resetToBaseline} disabled={savingDesc}>
                    <RotateCcw size={11} /> 重置为源码版
                  </button>
                )}
              </div>
            ) : null
          }
        >
          {devMode && !tool.can_edit_description && (
            <div
              style={{
                padding: '6px 10px',
                marginBottom: 8,
                fontSize: 12,
                color: 'var(--text-tertiary, #888)',
                border: '1px dashed var(--border-color)',
                borderRadius: 6,
              }}
            >
              <Lock size={11} style={{ verticalAlign: '-2px', marginRight: 4 }} />
              本工具为{SOURCE_LABEL[tool.source]}来源，description 来自外部源不能在此编辑。
            </div>
          )}
          {opErr && editing && (
            <div style={errorBannerStyle}>
              <AlertCircle size={12} style={{ marginRight: 4, verticalAlign: '-2px' }} />
              {opErr}
            </div>
          )}
          {editing ? (
            <>
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                disabled={savingDesc}
                style={{
                  width: '100%',
                  minHeight: 280,
                  padding: 10,
                  fontSize: 12,
                  lineHeight: 1.55,
                  fontFamily: 'var(--font-mono, monospace)',
                  border: '1px solid var(--primary-color)',
                  borderRadius: 6,
                  background: 'var(--bg-color)',
                  color: 'var(--text-color)',
                  boxSizing: 'border-box',
                  resize: 'vertical',
                }}
              />
              <div style={{ display: 'flex', gap: 6, marginTop: 6, alignItems: 'center' }}>
                <button
                  type="button"
                  style={{ ...chipBtnStyle(), background: 'var(--primary-color)', color: '#fff', borderColor: 'var(--primary-color)' }}
                  onClick={saveEdit}
                  disabled={savingDesc}
                >
                  {savingDesc ? <Loader2 size={11} className="spin" /> : <Save size={11} />} 保存覆盖
                </button>
                <button
                  type="button"
                  style={chipBtnStyle()}
                  onClick={() => {
                    setEditing(false)
                    setDraft('')
                    setOpErr(null)
                  }}
                  disabled={savingDesc}
                >
                  取消
                </button>
                <span style={{ fontSize: 10.5, color: 'var(--text-tertiary, #888)' }}>
                  保存后立即生效，写入 data/tool_overrides.json
                </span>
              </div>
            </>
          ) : (
            <pre style={preBlockStyle(360)}>{tool.description}</pre>
          )}
          {tool.is_overridden && tool.baseline_description && !editing && (
            <details style={{ marginTop: 6 }}>
              <summary style={{ fontSize: 11, color: 'var(--text-tertiary, #888)', cursor: 'pointer' }}>
                查看源码原版 description（已被覆盖）
              </summary>
              <pre style={{ ...preBlockStyle(220), marginTop: 4, opacity: 0.75 }}>{tool.baseline_description}</pre>
            </details>
          )}
        </Section>

        {/* Schema 永远只读 */}
        <Section title="Input Schema (JSON Schema)">
          <pre style={preBlockStyle(320, 'pre-wrap')}>{JSON.stringify(tool.input_schema, null, 2)}</pre>
        </Section>

        {/* 白名单 */}
        <Section title="按角色启用此工具">
          {opErr && !editing && (
            <div style={errorBannerStyle}>
              <AlertCircle size={12} style={{ marginRight: 4, verticalAlign: '-2px' }} />
              {opErr}
            </div>
          )}

          <SubGroup
            title="已显式启用"
            empty="暂无角色显式启用此工具"
            items={buckets.allowedExplicit.map((a) => (
              <AgentChip
                key={a.id}
                agent={a}
                busy={busyAgentId === a.id}
                action="remove"
                onClick={() => handleRemoveAgent(a)}
              />
            ))}
          />
          <SubGroup
            title="未启用（点击加入白名单）"
            empty="所有有白名单的角色都已启用"
            items={buckets.notAllowed.map((a) => (
              <AgentChip
                key={a.id}
                agent={a}
                busy={busyAgentId === a.id}
                action="add"
                onClick={() => handleAddAgent(a)}
              />
            ))}
          />
          {buckets.presetIncluded.length > 0 && (
            <SubGroup
              title="随档位生效"
              empty=""
              items={buckets.presetIncluded.map((a) => (
                <AgentChip
                  key={a.id}
                  agent={a}
                  busy={false}
                  action="locked"
                  titleOverride={`${a.name} 通过档位预设启用此工具；请在「按角色」调整档位`}
                  onClick={() => {}}
                />
              ))}
            />
          )}
          {buckets.presetExcluded.length > 0 && (
            <SubGroup
              title="档位未包含"
              empty=""
              items={buckets.presetExcluded.map((a) => (
                <AgentChip
                  key={a.id}
                  agent={a}
                  busy={false}
                  action="locked"
                  titleOverride={`${a.name} 的档位未包含此工具；请在「按角色」调档位或转自定义白名单`}
                  onClick={() => {}}
                />
              ))}
            />
          )}
          {buckets.allOpen.length > 0 && (
            <SubGroup
              title="全开模式（默认包含此工具，不在此处单独切换）"
              empty=""
              items={buckets.allOpen.map((a) => (
                <AgentChip key={a.id} agent={a} busy={false} action="locked" onClick={() => {}} />
              ))}
            />
          )}
        </Section>
      </div>
      {confirmDialog}
    </div>
  )
}

// ============================================================
// 按角色视图
// ============================================================

function AgentsView({
  data,
  onMutated,
  filter,
  onFilterChange,
}: {
  data: ToolsInventoryResponse
  onMutated: () => Promise<void> | void
  /** 预算筛选：全部 / 超标 / 偏高（状态在父组件，hero 提示可一键切入） */
  filter: 'all' | 'danger' | 'warn'
  onFilterChange: (f: 'all' | 'danger' | 'warn') => void
}) {
  const sectionRef = useRef<HTMLElement>(null)
  const narrow = useNarrow(sectionRef)
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(() => {
    try {
      const saved = localStorage.getItem('toolsInventory.selectedAgentId')
      if (saved && data.agents.some((a) => a.id === saved)) return saved
    } catch { /* ignore */ }
    return data.agents[0]?.id ?? null
  })

  const dangerCount = data.agents.filter((a) => a.approx_tokens > TOKEN_DANGER).length
  const warnCount = data.agents.filter(
    (a) => a.approx_tokens > TOKEN_WARN && a.approx_tokens <= TOKEN_DANGER,
  ).length
  const visibleAgents = data.agents.filter(
    (a) =>
      filter === 'all' ||
      (filter === 'danger' && a.approx_tokens > TOKEN_DANGER) ||
      (filter === 'warn' && a.approx_tokens > TOKEN_WARN && a.approx_tokens <= TOKEN_DANGER),
  )

  // 选中项持久化；选中角色消失（被删除）→ 回退第一个
  useEffect(() => {
    try { localStorage.setItem('toolsInventory.selectedAgentId', selectedAgentId ?? '') } catch { /* ignore */ }
    if (selectedAgentId && !data.agents.some((a) => a.id === selectedAgentId)) {
      setSelectedAgentId(data.agents[0]?.id ?? null)
    }
  }, [selectedAgentId, data.agents])

  const selectedAgent = useMemo(
    () => data.agents.find((a) => a.id === selectedAgentId) ?? null,
    [data.agents, selectedAgentId],
  )

  return (
    <section
      ref={sectionRef}
      style={{
        display: 'flex',
        flex: 1,
        minHeight: 0,
        minWidth: 0,
        gap: 12,
        padding: '12px 16px',
        flexDirection: narrow ? 'column' : 'row',
        overflow: narrow ? 'auto' : 'hidden',
      }}
    >
      <aside
        style={{
          flex: narrow ? '0 0 auto' : '0 1 280px',
          width: narrow ? '100%' : undefined,
          minWidth: narrow ? 0 : 200,
          maxHeight: narrow ? '35vh' : undefined,
          display: 'flex',
          flexDirection: 'column',
          border: '1px solid var(--border-color)',
          borderRadius: 8,
          background: 'var(--bg-secondary, #fafafa)',
          overflow: 'hidden',
        }}
      >
        <div style={{ padding: '8px 10px 8px', borderBottom: '1px solid var(--border-color)' }}>
          <div style={{ fontSize: 11, color: 'var(--text-tertiary, #888)', textTransform: 'uppercase', letterSpacing: 0.4 }}>
            角色 · {data.agents.length}
          </div>
          <div style={{ display: 'flex', gap: 4, marginTop: 6 }}>
            {([
              ['all', `全部 ${data.agents.length}`],
              ['danger', `超标 ${dangerCount}`],
              ['warn', `偏高 ${warnCount}`],
            ] as const).map(([f, label]) => (
              <button key={f} type="button" onClick={() => onFilterChange(f)} style={pillStyle(filter === f)}>
                {label}
              </button>
            ))}
          </div>
        </div>
        <div style={{ flex: 1, overflowY: 'auto' }}>
          {visibleAgents.map((a) => {
            const danger = a.approx_tokens > TOKEN_DANGER
            const warn = !danger && a.approx_tokens > TOKEN_WARN
            const presetName = a.tool_preset
              ? data.presets.find((p) => p.id === a.tool_preset)?.name ?? a.tool_preset
              : null
            const modeLabel = a.scope === 'all'
              ? '全开'
              : presetName
                ? `${presetName} · ${a.tool_count}`
                : `白名单 · ${a.tool_count}`
            return (
              <button
                key={a.id}
                type="button"
                onClick={() => setSelectedAgentId(a.id)}
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 2,
                  width: '100%',
                  padding: '8px 12px',
                  border: 'none',
                  background: selectedAgentId === a.id ? 'var(--primary-color-soft, rgba(0,120,212,0.12))' : 'transparent',
                  color: 'var(--text-color)',
                  cursor: 'pointer',
                  textAlign: 'left',
                  borderLeft: selectedAgentId === a.id ? '2px solid var(--primary-color)' : '2px solid transparent',
                }}
              >
                <span style={{ fontSize: 13.5, fontWeight: 500, display: 'flex', alignItems: 'center', gap: 5 }}>
                  {(() => {
                    const src = getAvatarSrc(a)
                    if (src) return <img src={src} alt="" style={{ width: 20, height: 20, borderRadius: '50%', objectFit: 'cover', flexShrink: 0 }} />
                    const fb = getAvatarFallback(a, '')
                    return fb ? <span style={{ flexShrink: 0, lineHeight: 1 }}>{fb}</span> : null
                  })()}
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{a.name}</span>
                </span>
                <span style={{ fontSize: 11.5, color: 'var(--text-tertiary, #888)' }}>
                  {modeLabel} ·{' '}
                  <span style={{ color: danger ? '#e5484d' : warn ? '#d99e2b' : 'inherit' }}>
                    ~{a.approx_tokens}t
                  </span>
                </span>
              </button>
            )
          })}
          {visibleAgents.length === 0 && (
            <div style={{ padding: 12, fontSize: 12, color: 'var(--text-secondary)' }}>
              该筛选下没有角色
            </div>
          )}
        </div>
      </aside>

      <div
        style={{
          ...detailPanelStyle,
          width: narrow ? '100%' : undefined,
          minHeight: narrow ? '60vh' : undefined,
        }}
      >
        {selectedAgent ? (
          <AgentToolsPanel
            agent={selectedAgent}
            tools={data.tools}
            presets={data.presets}
            onMutated={onMutated}
          />
        ) : (
          <div style={{ padding: 24, fontSize: 13, color: 'var(--text-secondary)' }}>
            从左侧选择一个角色
          </div>
        )}
      </div>
    </section>
  )
}

/** 面板草稿三态：全开 / 档位预设 / 自定义白名单（与后端 meta.json 配置一一对应） */
type ToolDraft =
  | { mode: 'all' }
  | { mode: 'preset'; id: string }
  | { mode: 'custom'; tools: string[] }

function AgentToolsPanel({
  agent,
  tools,
  presets,
  onMutated,
}: {
  agent: AgentInventoryItem
  tools: ToolInventoryItem[]
  presets: ToolPreset[]
  onMutated: () => Promise<void> | void
}) {
  const [saving, setSaving] = useState(false)
  const [opErr, setOpErr] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog()

  // 展示级档位映射：显式 tool_preset 优先；旧数据（无 preset 字段）白名单
  // 恰好等于某预设展开时也按该预设展示，保存时自然迁移成 preset 字段
  const effectivePresetId = useMemo(() => {
    if (agent.tool_preset) return agent.tool_preset
    if (agent.allowed_tools) {
      const key = (l: string[]) => [...l].sort().join(',')
      const agentKey = key(agent.allowed_tools)
      return presets.find((p) => key(p.tools) === agentKey)?.id ?? null
    }
    return null
  }, [agent.tool_preset, agent.allowed_tools, presets])

  const initialDraft = useMemo((): ToolDraft => {
    if (effectivePresetId) return { mode: 'preset', id: effectivePresetId }
    if (agent.allowed_tools === null) return { mode: 'all' }
    return { mode: 'custom', tools: [...agent.allowed_tools] }
  }, [effectivePresetId, agent.allowed_tools])

  const [draft, setDraft] = useState<ToolDraft>(initialDraft)

  // 服务端配置真实变化（保存成功 / 别处改动）或切换 agent 时重置 draft。
  // 用序列化 key 比较而不是引用相等：refresh 每次产出新数组，
  // 按 [agent.id, agent.allowed_tools] 判断会把未保存的编辑静默冲掉。
  const serverKey = JSON.stringify([agent.id, agent.tool_preset, agent.allowed_tools])
  const lastServerKeyRef = useRef<string | null>(null)
  const lastAgentIdRef = useRef<string | null>(null)
  useEffect(() => {
    if (lastServerKeyRef.current === serverKey) return
    const agentChanged = lastAgentIdRef.current !== agent.id
    lastServerKeyRef.current = serverKey
    lastAgentIdRef.current = agent.id
    setDraft(initialDraft)
    setOpErr(null)
    if (agentChanged) setQuery('')
  }, [serverKey, initialDraft])

  const isWhitelistMode = draft.mode !== 'all'
  const draftSet = useMemo(() => {
    if (draft.mode === 'all') return null
    if (draft.mode === 'preset') {
      return new Set(presets.find((p) => p.id === draft.id)?.tools ?? [])
    }
    return new Set(draft.tools)
  }, [draft, presets])

  const filteredTools = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return tools
    return tools.filter(
      (t) => t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q),
    )
  }, [tools, query])

  const grouped = useMemo(() => {
    const g: Record<ToolSource, ToolInventoryItem[]> = { builtin: [], ide: [], mcp: [] }
    for (const t of filteredTools) g[t.source].push(t)
    return g
  }, [filteredTools])

  // token 估算直接累加后端按"描述+schema"算好的单工具值，前端不再自算口径
  const draftTokenEstimate = useMemo(() => {
    if (draft.mode === 'all') return tools.reduce((s, t) => s + t.approx_tokens, 0)
    const names = draftSet ?? new Set<string>()
    return tools.reduce((s, t) => s + (names.has(t.name) ? t.approx_tokens : 0), 0)
  }, [draft, draftSet, tools])

  const dirty = useMemo(() => {
    if (draft.mode === 'all') return agent.allowed_tools !== null || agent.tool_preset !== null
    if (draft.mode === 'preset') return draft.id !== effectivePresetId
    if (agent.allowed_tools === null) return true
    if (agent.allowed_tools.length !== draft.tools.length) return true
    const sa = new Set(agent.allowed_tools)
    return draft.tools.some((n) => !sa.has(n))
  }, [draft, agent.allowed_tools, agent.tool_preset, effectivePresetId])

  const applyPreset = (id: string) => setDraft({ mode: 'preset', id })

  const switchToAll = async () => {
    if (draft.mode === 'all') return
    const ok = await askConfirm(
      '切换全开模式',
      '切换到全开模式后，该角色 prompt 会包含全部工具描述（token 占用大）。继续？',
      'warning',
      '继续全开',
    )
    if (!ok) return
    setDraft({ mode: 'all' })
  }

  const switchToCustom = () => {
    // 等价切换：从当前生效集合起步（档位 → 展开列表；全开 → 全部工具），
    // 用户只做减法/微调，不会因为切个模式就静默丢工具
    if (draft.mode === 'custom') return
    if (draft.mode === 'preset') {
      const base = presets.find((p) => p.id === draft.id)?.tools ?? []
      setDraft({ mode: 'custom', tools: [...base] })
      return
    }
    setDraft({ mode: 'custom', tools: tools.map((t) => t.name) })
  }

  // 一批工具并入/移出 draft；档位 draft 先降级成自定义再改（改了就不再是纯档位）
  const mutateTools = (names: string[], add: boolean) => {
    setDraft((d) => {
      if (d.mode === 'all') return d
      const base = d.mode === 'preset'
        ? [...(presets.find((p) => p.id === d.id)?.tools ?? [])]
        : [...d.tools]
      const next = add
        ? Array.from(new Set([...base, ...names]))
        : base.filter((n) => !names.includes(n))
      return { mode: 'custom', tools: next }
    })
  }

  const toggleTool = (name: string) => {
    if (!isWhitelistMode) return
    mutateTools([name], !(draftSet?.has(name) ?? false))
  }

  const handleSave = async () => {
    // 空白名单 = 角色零工具，容易误触且破坏性大，保存前确认一次
    if (draft.mode === 'custom' && draft.tools.length === 0) {
      const ok = await askConfirm(
        '保存空白名单？',
        '该角色将没有任何工具可用（连读文件都不行）。确定继续？',
        'warning',
        '仍要保存',
      )
      if (!ok) return
    }
    setSaving(true)
    setOpErr(null)
    try {
      if (draft.mode === 'preset') {
        await setAgentToolPreset(agent.id, draft.id)
      } else {
        await setAgentAllowedTools(agent.id, draft.mode === 'all' ? null : draft.tools)
      }
      await onMutated()
    } catch (exc) {
      setOpErr(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setSaving(false)
    }
  }

  const handleReset = () => {
    setDraft(initialDraft)
    setOpErr(null)
  }

  const danger = draftTokenEstimate > TOKEN_DANGER
  const warn = !danger && draftTokenEstimate > TOKEN_WARN

  const activePreset = draft.mode === 'preset' ? presets.find((p) => p.id === draft.id) : null
  const modeLabel = draft.mode === 'all'
    ? '全开（所有工具）'
    : draft.mode === 'preset'
      ? `${activePreset?.name ?? draft.id}（${activePreset?.tool_count ?? draftSet?.size ?? 0} 个工具）`
      : `自定义白名单（${draft.tools.length} 个工具）`

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
      <div style={detailHeaderStyle}>
        <Users size={18} style={{ color: 'var(--primary-color)' }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6 }}>
            {(() => {
              const src = getAvatarSrc(agent)
              if (src) return <img src={src} alt="" style={{ width: 22, height: 22, borderRadius: '50%', objectFit: 'cover', flexShrink: 0 }} />
              const fb = getAvatarFallback(agent, '')
              return fb ? <span style={{ flexShrink: 0, lineHeight: 1 }}>{fb}</span> : null
            })()}
            <span>{agent.name}</span>
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 2 }}>
            模式：{modeLabel} ·
            预估 prompt（含 schema）：<span style={{ color: danger ? '#e5484d' : warn ? '#d99e2b' : 'inherit', fontWeight: 600 }}>~{draftTokenEstimate} token</span>
          </div>
        </div>
        <div style={{ display: 'inline-flex', gap: 6, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          {presets.map((p) => (
            <button
              key={p.id}
              type="button"
              title={p.description}
              onClick={() => applyPreset(p.id)}
              style={modeChipStyle(draft.mode === 'preset' && draft.id === p.id)}
            >
              {p.name} · ~{p.approx_tokens}t
            </button>
          ))}
          <button type="button" onClick={switchToCustom} style={modeChipStyle(draft.mode === 'custom')}>
            自定义
          </button>
          <button type="button" onClick={switchToAll} style={modeChipStyle(draft.mode === 'all')}>
            全开
          </button>
        </div>
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '12px 16px' }}>
        {opErr && (
          <div style={errorBannerStyle}>
            <AlertCircle size={12} style={{ marginRight: 4, verticalAlign: '-2px' }} />
            {opErr}
          </div>
        )}

        {draft.mode === 'all' && (
          <div style={hintBoxStyle}>
            <Lock size={12} style={{ verticalAlign: '-2px', marginRight: 6 }} />
            此角色当前为<strong>全开模式</strong>，没有白名单，所有已注册的工具都进 prompt。
            点上方「极简」一键收紧，或「自定义」从当前全集做减法，可立即看到 token 节省。
          </div>
        )}
        {draft.mode === 'preset' && activePreset && (
          <div style={hintBoxStyle}>
            <Check size={12} style={{ verticalAlign: '-2px', marginRight: 6 }} />
            <strong>{activePreset.name}档</strong>：{activePreset.description}
            点「自定义」可在此基础上微调（会转为普通白名单）。
          </div>
        )}

        <div style={{ position: 'relative', marginBottom: 10 }}>
          <Search
            size={13}
            style={{
              position: 'absolute',
              left: 8,
              top: '50%',
              transform: 'translateY(-50%)',
              color: 'var(--text-secondary)',
            }}
          />
          <input
            type="text"
            placeholder="按名称 / 描述过滤工具…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={searchInputStyle}
          />
        </div>

        {(['builtin', 'ide', 'mcp'] as ToolSource[]).map((src) => {
          const items = grouped[src]
          if (items.length === 0) return null
          const itemNames = items.map((t) => t.name)
          return (
            <Section
              key={src}
              title={`${SOURCE_LABEL[src]} · ${items.length}`}
              right={
                isWhitelistMode ? (
                  <span style={{ display: 'inline-flex', gap: 4 }}>
                    <button type="button" style={chipBtnStyle()} onClick={() => mutateTools(itemNames, true)}>
                      全选
                    </button>
                    <button type="button" style={chipBtnStyle()} onClick={() => mutateTools(itemNames, false)}>
                      清空
                    </button>
                  </span>
                ) : null
              }
            >
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 6 }}>
                {items.map((t) => {
                  const checked = !isWhitelistMode || draftSet?.has(t.name) === true
                  return (
                    <label
                      key={t.name}
                      title={t.description.slice(0, 200)}
                      style={{
                        display: 'flex',
                        alignItems: 'flex-start',
                        gap: 6,
                        padding: '6px 8px',
                        fontSize: 12,
                        border: '1px solid var(--border-color)',
                        borderRadius: 6,
                        background: checked ? 'var(--primary-color-soft, rgba(0,120,212,0.10))' : 'transparent',
                        cursor: isWhitelistMode ? 'pointer' : 'not-allowed',
                        opacity: isWhitelistMode ? 1 : 0.6,
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={!isWhitelistMode}
                        onChange={() => toggleTool(t.name)}
                        style={{ marginTop: 2 }}
                      />
                      <span style={{ flex: 1, minWidth: 0 }}>
                        <span style={{ fontFamily: 'var(--font-mono, monospace)', fontSize: 11.5, display: 'block', wordBreak: 'break-all' }}>
                          {t.name}
                        </span>
                        <span style={{ fontSize: 10, color: 'var(--text-tertiary, #888)' }}>
                          ~{t.approx_tokens}t · {t.concurrency_class}
                        </span>
                      </span>
                    </label>
                  )
                })}
              </div>
            </Section>
          )
        })}
      </div>

      {/* 底部 sticky action bar */}
      <footer
        style={{
          padding: '10px 16px',
          borderTop: '1px solid var(--border-color)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          background: 'var(--bg-color)',
        }}
      >
        <span style={{ fontSize: 11.5, color: dirty ? '#d99e2b' : 'var(--text-tertiary, #888)' }}>
          {dirty ? '有未保存修改' : '与服务端同步'}
        </span>
        <div style={{ display: 'flex', gap: 6 }}>
          <button type="button" style={chipBtnStyle()} onClick={handleReset} disabled={!dirty || saving}>
            <RotateCcw size={11} /> 撤销
          </button>
          <button
            type="button"
            style={{ ...chipBtnStyle(), background: 'var(--primary-color)', color: '#fff', borderColor: 'var(--primary-color)' }}
            onClick={handleSave}
            disabled={!dirty || saving}
          >
            {saving ? <Loader2 size={11} className="spin" /> : <Save size={11} />} 保存
          </button>
        </div>
      </footer>
      {confirmDialog}
    </div>
  )
}

// ============================================================
// 共享小组件 / 样式
// ============================================================

function Section({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section style={{ marginBottom: 18 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
        <h3
          style={{
            margin: 0,
            fontSize: 11,
            fontWeight: 600,
            color: 'var(--text-tertiary, #888)',
            textTransform: 'uppercase',
            letterSpacing: 0.5,
          }}
        >
          {title}
        </h3>
        {right}
      </div>
      {children}
    </section>
  )
}

function SubGroup({ title, empty, items }: { title: string; empty: string; items: React.ReactNode[] }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>{title}</div>
      {items.length > 0 ? (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>{items}</div>
      ) : empty ? (
        <div style={{ fontSize: 11, color: 'var(--text-tertiary, #888)', fontStyle: 'italic' }}>{empty}</div>
      ) : null}
    </div>
  )
}

function AgentChip({
  agent,
  busy,
  action,
  titleOverride,
  onClick,
}: {
  agent: AgentInventoryItem
  busy: boolean
  action: 'add' | 'remove' | 'locked'
  /** 覆盖默认 hover 提示（档位角色等特殊态用） */
  titleOverride?: string
  onClick: () => void
}) {
  const isLocked = action === 'locked'
  const isRemove = action === 'remove'
  const title = titleOverride
    ?? (isLocked
      ? '该角色当前为全开模式（无白名单）。要单独管理工具，请在「按角色」里改为自定义白名单。'
      : isRemove
        ? '从该角色白名单移除'
        : '加入该角色白名单')
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={isLocked || busy}
      title={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        padding: '3px 8px',
        fontSize: 11.5,
        border: '1px solid var(--border-color)',
        borderRadius: 12,
        background: isRemove
          ? 'var(--primary-color-soft, rgba(0,120,212,0.12))'
          : isLocked
            ? 'transparent'
            : 'var(--bg-secondary, #fafafa)',
        color: isLocked ? 'var(--text-tertiary, #888)' : 'var(--text-color)',
        cursor: isLocked ? 'not-allowed' : busy ? 'wait' : 'pointer',
        opacity: isLocked ? 0.65 : 1,
      }}
    >
      <span>{agent.name}</span>
      {busy ? (
        <Loader2 size={10} className="spin" />
      ) : isRemove ? (
        <X size={10} />
      ) : isLocked ? (
        <Check size={10} />
      ) : (
        <Plus size={10} />
      )}
    </button>
  )
}

// ----- 共享 inline style -----

const searchInputStyle: React.CSSProperties = {
  width: '100%',
  padding: '7px 8px 7px 28px',
  fontSize: 13,
  border: '1px solid var(--border-color)',
  borderRadius: 6,
  background: 'var(--bg-color)',
  color: 'var(--text-color)',
  boxSizing: 'border-box',
}

const detailPanelStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  border: '1px solid var(--border-color)',
  borderRadius: 8,
  background: 'var(--bg-color)',
  overflow: 'hidden',
  display: 'flex',
  flexDirection: 'column',
}

const detailHeaderStyle: React.CSSProperties = {
  padding: '12px 16px',
  borderBottom: '1px solid var(--border-color)',
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  flexShrink: 0,
}

const errorBannerStyle: React.CSSProperties = {
  padding: '6px 10px',
  marginBottom: 8,
  fontSize: 12,
  color: 'var(--danger-color, #e5484d)',
  border: '1px solid var(--danger-color, #e5484d)',
  borderRadius: 6,
  background: 'rgba(229,72,77,0.08)',
}

const listGroupHeaderStyle: React.CSSProperties = {
  padding: '8px 12px 3px',
  fontSize: 11,
  fontWeight: 600,
  color: 'var(--text-tertiary, #888)',
  textTransform: 'uppercase',
  letterSpacing: 0.4,
}

const listItemStyle = (active: boolean): React.CSSProperties => ({
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  width: '100%',
  padding: '7px 12px',
  fontSize: 13,
  border: 'none',
  background: active ? 'var(--primary-color-soft, rgba(0,120,212,0.12))' : 'transparent',
  color: 'var(--text-color)',
  cursor: 'pointer',
  textAlign: 'left',
  borderLeft: active ? '2px solid var(--primary-color)' : '2px solid transparent',
})

const pillStyle = (active: boolean): React.CSSProperties => ({
  padding: '4px 10px',
  fontSize: 12,
  border: '1px solid var(--border-color)',
  borderRadius: 12,
  background: active ? 'var(--primary-color)' : 'transparent',
  color: active ? '#fff' : 'var(--text-secondary)',
  cursor: 'pointer',
})

const chipBtnStyle = (): React.CSSProperties => ({
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
  padding: '5px 10px',
  fontSize: 12.5,
  border: '1px solid var(--border-color)',
  borderRadius: 6,
  background: 'var(--bg-secondary, #fafafa)',
  color: 'var(--text-color)',
  cursor: 'pointer',
})

/** 档位切换 chip（极简 / 自定义 / 全开） */
const modeChipStyle = (active: boolean): React.CSSProperties => ({
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
  padding: '4px 10px',
  fontSize: 12,
  border: `1px solid ${active ? 'var(--primary-color)' : 'var(--border-color)'}`,
  borderRadius: 12,
  background: active ? 'var(--primary-color-soft, rgba(0,120,212,0.12))' : 'transparent',
  color: active ? 'var(--primary-color)' : 'var(--text-secondary)',
  cursor: 'pointer',
  fontWeight: active ? 600 : 400,
  whiteSpace: 'nowrap',
})

const hintBoxStyle: React.CSSProperties = {
  padding: 10,
  fontSize: 11.5,
  lineHeight: 1.55,
  border: '1px dashed var(--border-color)',
  borderRadius: 6,
  color: 'var(--text-secondary)',
  marginBottom: 10,
}

const preBlockStyle = (maxHeight: number, whiteSpace: 'pre' | 'pre-wrap' = 'pre-wrap'): React.CSSProperties => ({
  whiteSpace,
  wordBreak: whiteSpace === 'pre-wrap' ? 'break-word' : 'normal',
  fontSize: 13.5,
  lineHeight: 1.6,
  padding: 12,
  background: 'var(--bg-secondary, #fafafa)',
  border: '1px solid var(--border-color)',
  borderRadius: 6,
  margin: 0,
  fontFamily: 'var(--font-mono, monospace)',
  maxHeight,
  overflow: 'auto',
})
