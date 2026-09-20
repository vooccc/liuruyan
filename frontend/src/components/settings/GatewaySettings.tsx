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
 * 设计文档：_docs/GATEWAY.md · 隔离纪律：.windsurf/rules/gateway-isolation.md
 */
import { useCallback, useEffect, useState } from 'react'
import {
  AlertCircle,
  BarChart3,
  CheckCircle2,
  KeyRound,
  Loader2,
  Plug,
  Power,
  SquareStack,
  Waypoints,
} from 'lucide-react'
import CloudKey from '../icons/CloudKeyIcon'
import {
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
  { id: 'tokens', label: '令牌管理', icon: KeyRound },
  { id: 'aliases', label: '模型映射', icon: SquareStack },
  { id: 'installers', label: '客户端接入', icon: Plug },
  { id: 'usage', label: '用量统计', icon: BarChart3 },
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

  // 只保留 runnable launcher（claude_code/codex）。GUI 类客户端（cline/cursor/cherry studio 等）
  // 不再独立列出——令牌管理 tab 已露出 OpenAI/Anthropic base URL + 令牌，足以让用户手填 GUI 设置。
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
      {/* Hero */}
      <header style={heroStyle}>
        <div style={heroRowStyle}>
          <CloudKey size={20} style={{ color: 'var(--primary-color)', flex: '0 0 auto' }} />
          <div style={heroTitleStyle}>
            <span style={{ fontSize: 14.5, fontWeight: 600 }}>模型共享</span>
            {health?.version && (
              <span style={{ fontSize: 10.5, color: 'var(--text-tertiary, #888)', marginLeft: 6 }}>
                {health.version}
              </span>
            )}
          </div>
          <button
            type="button"
            style={enabled ? btnPrimaryStyle : btnGhostStyle}
            onClick={() => void handleToggleEnabled()}
            disabled={busy}
          >
            {busy ? <Loader2 size={13} className="spin" /> : <Power size={13} />}
            <span style={{ marginLeft: 4 }}>{enabled ? '已启用' : '已关闭'}</span>
          </button>
        </div>
        <div style={pillRowStyle}>
          <span style={pillStyle}>
            {enabled ? <CheckCircle2 size={11} /> : <AlertCircle size={11} />}
            {enabled ? '运行中' : '未启用'}
          </span>
          <span style={pillStyle}>仅本机访问（127.0.0.1）</span>
        </div>
      </header>

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
              <Icon size={13} />
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
 * GUI 类客户端（cline/cursor/cherry studio 等）不再单独列出——令牌管理 tab 已露出
 * OpenAI/Anthropic base URL + 令牌，足以让用户手填 GUI 设置。
 */
function InstallersTab({ enabled, launchers }: { enabled: boolean; launchers: GatewayLauncherSnippet[] }) {
  return <GatewayInstallerPanel enabled={enabled} launchers={launchers} />
}

/* ─────────── styles ─────────── */

const pageStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 12,
  padding: '16px 20px',
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

const heroStyle: React.CSSProperties = {
  background: 'var(--card-bg, rgba(0,0,0,0.02))',
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  borderRadius: 8,
  padding: '10px 14px',
  minWidth: 0,
}

const heroRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  minWidth: 0,
}

const heroTitleStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  display: 'flex',
  alignItems: 'baseline',
  overflow: 'hidden',
}

const pillRowStyle: React.CSSProperties = {
  display: 'flex',
  gap: 6,
  marginTop: 8,
  fontSize: 11,
  color: 'var(--text-secondary)',
  flexWrap: 'wrap',
}

const pillStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
  padding: '2px 8px',
  borderRadius: 999,
  background: 'rgba(0,0,0,0.04)',
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
}

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

const btnPrimaryStyle: React.CSSProperties = {
  ...btnGhostStyle,
  background: 'var(--primary-color, #4f7cff)',
  color: '#fff',
  borderColor: 'transparent',
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
  gap: 4,
  flexWrap: 'wrap',
  borderBottom: '1px solid var(--border-color, rgba(0,0,0,0.1))',
}

const tabStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  fontSize: 12.5,
  fontWeight: 500,
  padding: '8px 14px',
  background: 'transparent',
  border: 'none',
  borderBottom: '2px solid transparent',
  color: 'var(--text-secondary)',
  cursor: 'pointer',
  marginBottom: -1,
  // 去掉 button 默认 focus 黑色 outline 和移动端点击高亮
  // 视觉反馈走 borderBottom 颜色（active 时金色），不需要额外 focus ring
  outline: 'none',
  WebkitTapHighlightColor: 'transparent',
}

const tabActiveStyle: React.CSSProperties = {
  ...tabStyle,
  color: 'var(--primary-color, #4f7cff)',
  borderBottomColor: 'var(--primary-color, #4f7cff)',
  fontWeight: 600,
}

const tabPanelStyle: React.CSSProperties = {
  minWidth: 0,
  width: '100%',
}

// launcher 相关样式已全部迁移到 GatewayInstallerPanel.tsx。如需调整启动/复制/工作区行等
// 视觉，请去那里。本文件只保留 hero / tab nav / pill / loading 全局样式。
