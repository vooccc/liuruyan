/**
 * 设置 → Gateway 面板。
 *
 * Gateway 把柳如烟变本地 LLM 网关：外部 OpenAI/Anthropic 兼容客户端配 base URL + 令牌
 * 就能调用柳如烟里所有 provider。
 *
 * 信息架构：
 *   Hero（总开关 + 健康胶囊，始终可见）
 *   ├─ 令牌管理 · 接入地址 + 令牌列表 + 折叠模型查询
 *   ├─ 模型别名 · alias + fallback 链
 *   ├─ 客户端接入 · 一键配置 CLI 与启动（launcher 已合并进 InstallerPanel 卡片）
 *   └─ 用量统计 · KPI / 柱状图 / fallback jumps
 *
 * 设计纪律：
 *   - 文案精炼，靠视觉自解释——别堆中长说明
 *   - 进页面不预拉任何"消费数据"（模型清单、令牌明文等），按需折叠展开再加载
 *   - 整个面板用 width:100% + minWidth:0 + box-sizing:border-box 让宽度跟随父级
 *
 * 设计文档：docs/GATEWAY.md · 隔离纪律：网关端口隔离与安全沙箱规范
 */
import { useCallback, useEffect, useState } from 'react'
import {
  AlertCircle,
  BarChart2,
  Key,
  Loader2,
  Monitor,
  SquareStack,
  Waypoints,
} from 'lucide-react'
import CloudKey from '../icons/CloudKeyIcon'
import {
  GATEWAY_HOST_PORT,
  type GatewayAliasMap,
  type GatewayConfig,
  type GatewayHealth,
  type GatewayLauncherSnippet,
  type GatewayModelInfo,
  getGatewayConfig,
  getGatewayHealth,
  getGatewayLaunchers,
  listAdminModels,
  listGatewayAliases,
  updateGatewayConfig,
} from '../../api/gateway'
import GatewayAliasManager from './GatewayAliasManager'
import GatewayInstallerPanel from './GatewayInstallerPanel'
import GatewayTokensPanel from './GatewayTokensPanel'
import GatewayUsagePanel from './GatewayUsagePanel'

type TabId = 'tokens' | 'aliases' | 'installers' | 'usage'

interface TabDef {
  id: TabId
  label: string
  icon: typeof Waypoints
}

const TABS: readonly TabDef[] = [
  { id: 'tokens', label: '令牌管理', icon: Key },
  { id: 'aliases', label: '模型映射', icon: SquareStack },
  { id: 'installers', label: '客户端接入', icon: Monitor },
  { id: 'usage', label: '用量统计', icon: BarChart2 },
] as const

export default function GatewaySettings() {
  const [config, setConfig] = useState<GatewayConfig | null>(null)
  const [health, setHealth] = useState<GatewayHealth | null>(null)
  const [launchers, setLaunchers] = useState<GatewayLauncherSnippet[]>([])
  const [aliases, setAliases] = useState<GatewayAliasMap>({})
  const [aliasTabModels, setAliasTabModels] = useState<GatewayModelInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<TabId>('tokens')

  const refreshAliases = useCallback(async () => {
    try {
      setAliases(await listGatewayAliases())
    } catch (e) {
      console.warn('[Gateway] listAliases 失败', e)
      setAliases({})
    }
  }, [])

  const refresh = useCallback(async () => {
    setError(null)
    try {
      const [hp, cfg] = await Promise.all([getGatewayHealth(), getGatewayConfig()])
      setHealth(hp)
      setConfig(cfg)
      if (cfg.enabled) {
        await refreshAliases()
        try {
          const lr = await getGatewayLaunchers()
          setLaunchers(lr.launchers)
        } catch (e) {
          console.warn('[Gateway] launchers 失败', e)
          setLaunchers([])
        }
      } else {
        setLaunchers([])
        setAliases({})
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [refreshAliases])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // 模型别名 tab 按需拉一次模型清单（候选池用）；不进该 tab 不动用
  useEffect(() => {
    if (activeTab !== 'aliases' || !config?.enabled || aliasTabModels.length > 0) return
    void (async () => {
      try {
        const r = await listAdminModels()
        setAliasTabModels(r.data)
      } catch (e) {
        console.warn('[Gateway] listAdminModels for aliases tab 失败', e)
      }
    })()
  }, [activeTab, config?.enabled, aliasTabModels.length])

  const handleToggleEnabled = async () => {
    if (!config) return
    setBusy(true)
    try {
      const next = await updateGatewayConfig({ enabled: !config.enabled })
      setConfig(next)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  // 只保留可执行的 runnable launcher（claude_code/codex）。外部兼容客户端通过令牌与端点连接。
  const runnableLaunchers = launchers.filter((l) => l.runnable)

  if (loading && !config) {
    return (
      <div style={loadingStyle}>
        <Loader2 size={16} className="spin" />
        <span>加载中…</span>
      </div>
    )
  }

  if (error && !config) {
    return (
      <div style={errBlockStyle}>
        <AlertCircle size={16} />
        <span>{error}</span>
        <button type="button" style={btnGhostStyle} onClick={() => void refresh()}>重试</button>
      </div>
    )
  }

  const enabled = !!config?.enabled

  return (
    <div style={pageStyle}>
      <style>{GW_DOT_PULSE_KF}</style>
      {/* 页面头 */}
      <header style={pageHeadStyle}>
        <div style={pageTitleRowStyle}>
          <CloudKey size={20} style={{ color: 'var(--primary-color)', flex: '0 0 auto' }} />
          <span style={pageTitleStyle}>模型共享</span>
          {health?.version && <span style={versionTagStyle}>{health.version}</span>}
        </div>
        <div style={pageSubStyle}>将本地模型服务以标准接口共享给其他客户端使用</div>
      </header>

      {/* 状态总览 */}
      <section style={statusBarStyle}>
        <div style={statusCellStyle}>
          <span style={statusKeyStyle}>服务状态</span>
          <span style={statusValStyle}>
            <span style={enabled ? dotOnStyle : dotOffStyle} />
            {enabled ? '运行中' : '未启用'}
          </span>
        </div>
        <div style={statusDividerStyle} />
        <div style={statusCellStyle}>
          <span style={statusKeyStyle}>访问范围</span>
          <span style={statusValStyle}>仅本机 · {config?.host || '127.0.0.1'}</span>
        </div>
        <div style={statusDividerStyle} />
        <div style={statusCellStyle}>
          <span style={statusKeyStyle}>监听端口</span>
          <span style={{ ...statusValStyle, fontFamily: 'var(--font-mono, ui-monospace, Consolas, monospace)' }}>
            {GATEWAY_HOST_PORT.split(':').pop() || '—'}
          </span>
        </div>
        <div style={{ flex: 1 }} />
        <span
          style={{
            fontSize: 12,
            fontWeight: 500,
            color: enabled ? 'var(--accent-green, #3fb950)' : 'var(--text-tertiary, #888)',
          }}
        >
          {enabled ? '已启用' : '已停用'}
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label="启用或停用模型共享"
          title={enabled ? '点击停用' : '点击启用'}
          style={switchTrackStyle(enabled)}
          onClick={() => void handleToggleEnabled()}
          disabled={busy}
        >
          <span style={switchKnobStyle(enabled)}>
            {busy && <Loader2 size={11} className="spin" style={{ color: '#888', display: 'block', margin: 3.5 }} />}
          </span>
        </button>
      </section>

      {error && (
        <div style={errStyle}>
          <AlertCircle size={14} />
          <span style={{ flex: 1, minWidth: 0, overflowWrap: 'anywhere' }}>{error}</span>
          <button type="button" style={btnGhostStyle} onClick={() => setError(null)}>×</button>
        </div>
      )}

      {/* Tab nav */}
      <nav style={tabBarStyle} role="tablist" aria-label="Gateway 子页面">
        {TABS.map((t) => {
          const Icon = t.icon
          const active = t.id === activeTab
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={active}
              style={active ? tabActiveStyle : tabStyle}
              onClick={() => setActiveTab(t.id)}
            >
              <Icon size={15} strokeWidth={1.6} />
              <span style={{ marginLeft: 6 }}>{t.label}</span>
            </button>
          )
        })}
      </nav>

      {/* Tab content */}
      <div role="tabpanel" style={tabPanelStyle}>
        {activeTab === 'tokens' && <GatewayTokensPanel enabled={enabled} aliases={aliases} />}
        {activeTab === 'aliases' && <GatewayAliasManager enabled={enabled} models={aliasTabModels} />}
        {activeTab === 'installers' && <InstallersTab enabled={enabled} launchers={runnableLaunchers} />}
        {activeTab === 'usage' && <GatewayUsagePanel enabled={enabled} />}
      </div>
    </div>
  )
}

/* ─────────── 客户端接入 Tab：薄壳——把 launchers 透传给 InstallerPanel ───────────
 *
 * 旧版（M5 早期）这里维护 cwd / shellMode / launchToast / cwdMap / pickCwdFor 等大量
 * 状态，并独立渲染一个"启动命令"section。M5 合并后这些状态全部下放到 GatewayInstallerPanel
 * 内部，每个 installer 卡片自带启动按钮 + cwd + shell + 命令片段，UX 更连贯。
 *
 * 外部通用客户端可直接在令牌管理 tab 查看 OpenAI/Anthropic base URL 与连接令牌。
 */
function InstallersTab({ enabled, launchers }: { enabled: boolean; launchers: GatewayLauncherSnippet[] }) {
  return <GatewayInstallerPanel enabled={enabled} launchers={launchers} />
}

/* ─────────── styles ─────────── */

const pageStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 14,
  padding: '20px 24px 32px',
  // 关键：让面板宽度跟随父级 settings-tab-content（避免内部固定/min-content 把外层撑爆）
  width: '100%',
  minWidth: 0,
  boxSizing: 'border-box',
  overflow: 'auto',
  // 宽屏下内容不拉满整行：限宽 + 居中，阅读密度更接近成熟产品
  marginLeft: 'auto',
  marginRight: 'auto',
  maxWidth: 1080,
}

/* ── 页面头 ── */

const pageHeadStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 3,
  minWidth: 0,
}

const pageTitleRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  minWidth: 0,
}

const pageTitleStyle: React.CSSProperties = {
  fontFamily: '"Noto Serif SC", ' + '"Source Han Serif SC", ' + '"Songti SC", SimSun, serif',
  fontSize: 20,
  fontWeight: 600,
  letterSpacing: 0.2,
  color: 'var(--text-color)',
}

const versionTagStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono, ui-monospace, Consolas, monospace)',
  fontSize: 10.5,
  color: 'var(--text-tertiary, #888)',
  border: '1px solid var(--border-color, rgba(0,0,0,0.1))',
  borderRadius: 999,
  padding: '1.5px 9px',
  background: 'var(--card-bg, rgba(0,0,0,0.02))',
}

const pageSubStyle: React.CSSProperties = {
  fontSize: 12.5,
  color: 'var(--text-tertiary, #888)',
  paddingLeft: 30, // 与标题文字左对齐（icon 20 + gap 10）
}

/* ── 状态总览条 ── */

const statusBarStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 24,
  flexWrap: 'wrap',
  background: 'var(--card-bg, rgba(0,0,0,0.02))',
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  borderRadius: 12,
  padding: '13px 20px',
  boxShadow: '0 1px 2px rgba(0,0,0,0.04), 0 8px 24px -12px rgba(0,0,0,0.12)',
  minWidth: 0,
}

const statusCellStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 1,
}

const statusKeyStyle: React.CSSProperties = {
  fontSize: 10.5,
  letterSpacing: 0.6,
  color: 'var(--text-tertiary, #888)',
}

const statusValStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 7,
  fontSize: 13.5,
  fontWeight: 500,
  color: 'var(--text-color)',
}

const statusDividerStyle: React.CSSProperties = {
  width: 1,
  alignSelf: 'stretch',
  background: 'var(--border-color, rgba(0,0,0,0.08))',
  opacity: 0.6,
}

/* 状态呼吸点：keyframes 见 GW_DOT_PULSE_KF，用 <style> 注入一次 */
const GW_DOT_PULSE_KF = `@keyframes gwDotPulse {
  0%, 100% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-green, #3fb950) 14%, transparent); }
  50% { box-shadow: 0 0 0 5.5px color-mix(in srgb, var(--accent-green, #3fb950) 7%, transparent); }
}`

const dotBaseStyle: React.CSSProperties = {
  width: 7,
  height: 7,
  borderRadius: '50%',
  flexShrink: 0,
}

const dotOnStyle: React.CSSProperties = {
  ...dotBaseStyle,
  background: 'var(--accent-green, #3fb950)',
  animation: 'gwDotPulse 2.4s infinite',
}

const dotOffStyle: React.CSSProperties = {
  ...dotBaseStyle,
  background: 'var(--text-muted, #999)',
}

/* ── iOS 风开关 ── */

const switchTrackStyle = (on: boolean): React.CSSProperties => ({
  position: 'relative',
  width: 40,
  height: 23,
  borderRadius: 999,
  background: on ? 'var(--primary-color, #c4a77d)' : 'var(--border-color, #ccc)',
  border: 'none',
  cursor: 'pointer',
  padding: 0,
  flexShrink: 0,
  transition: 'background .2s',
})

const switchKnobStyle = (on: boolean): React.CSSProperties => ({
  position: 'absolute',
  top: 2.5,
  left: on ? 19.5 : 2.5,
  width: 18,
  height: 18,
  borderRadius: '50%',
  background: '#fff',
  boxShadow: '0 1px 3px rgba(0,0,0,0.28)',
  transition: 'left .2s cubic-bezier(.2,.7,.2,1)',
})

const btnGhostStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  fontSize: 11.5,
  padding: '4px 8px',
  borderRadius: 6,
  background: 'transparent',
  border: '1px solid var(--border-color, rgba(0,0,0,0.12))',
  color: 'var(--text-color)',
  cursor: 'pointer',
  flex: '0 0 auto',
}

const loadingStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: 24,
  color: 'var(--text-secondary)',
}

const errBlockStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: 24,
  color: '#e5484d',
}

const errStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: 8,
  fontSize: 12,
  color: '#e5484d',
  background: 'rgba(229,72,77,0.08)',
  borderRadius: 6,
  minWidth: 0,
}

const tabBarStyle: React.CSSProperties = {
  display: 'flex',
  gap: 6,
  flexWrap: 'wrap',
  borderBottom: '1px solid var(--border-color, rgba(0,0,0,0.1))',
}

const tabStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  fontSize: 13,
  fontWeight: 500,
  padding: '9px 14px 11px',
  background: 'transparent',
  border: 'none',
  // 指示线走 inset box-shadow 而不是 borderBottom——这个 WebView 会把
  // borderBottom:'2px solid transparent' 渲染成纯黑条（实测像素 (0,0,0)），
  // box-shadow 用 transparent 色则不绘制，无此问题。
  boxShadow: 'inset 0 -2px 0 0 transparent',
  color: 'var(--text-secondary)',
  cursor: 'pointer',
  marginBottom: -1,
  transition: 'color .18s, box-shadow .18s',
  // 去掉 button 默认 focus 黑色 outline 和移动端点击高亮
  // 视觉反馈走 box-shadow 指示线（active 时金色），不需要额外 focus ring
  outline: 'none',
  WebkitTapHighlightColor: 'transparent',
}

const tabActiveStyle: React.CSSProperties = {
  ...tabStyle,
  color: 'var(--text-color)',
  boxShadow: 'inset 0 -2px 0 0 var(--primary-color, #c4a77d)',
  fontWeight: 600,
}

const tabPanelStyle: React.CSSProperties = {
  minWidth: 0,
  width: '100%',
}

// launcher 相关样式已全部迁移到 GatewayInstallerPanel.tsx。如需调整启动/复制/工作区行等
// 视觉，请去那里。本文件只保留 hero / tab nav / pill / loading 全局样式。
