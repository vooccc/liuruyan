/**
 * MCP 子系统设置主页。
 *
 * 设计文档：_docs/MCP_DESIGN.md
 * 组件分工：
 *   - McpServersList: 已添加 server 列表（状态 + 启停 + 重启 + 删除）
 *   - ServerRegistrySpotlight: 从内置目录挑 server 一键添加
 *   - AddMcpServerModal: 自定义 server 配置表单（当 registry 里没有时用）
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Download, Loader2, Plus, Puzzle, RefreshCw, Store } from 'lucide-react'
import {
  addServer,
  deleteServer,
  getRegistry,
  installMcpb,
  listServers,
  reauthServer,
  restartServer,
  toggleServer,
  type MCPCatalogEntry,
  type MCPServerConfig,
  type MCPServerListItem,
} from '../../../api/mcp'
import ServerRegistrySpotlight from './ServerRegistrySpotlight'
import AddMcpServerModal from './AddMcpServerModal'
import McpServersList from './McpServersList'
import { useMcpEvents } from './useMcpEvents'
import ElicitationDialog, { type ElicitationRequest } from './ElicitationDialog'
import MarketplaceSettings from './MarketplaceSettings'

type Tab = 'installed' | 'catalog'

export default function MCPSettings() {
  const [tab, setTab] = useState<Tab>('installed')
  const [servers, setServers] = useState<MCPServerListItem[]>([])
  const [catalog, setCatalog] = useState<MCPCatalogEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [spotlightEntry, setSpotlightEntry] = useState<MCPCatalogEntry | null>(null)
  const [pendingElicit, setPendingElicit] = useState<ElicitationRequest | null>(null)
  const [customOpen, setCustomOpen] = useState(false)
  const [installing, setInstalling] = useState(false)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [srv, reg] = await Promise.all([listServers(), getRegistry()])
      setServers(srv)
      setCatalog(reg)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // M2: SSE 状态广播替代轮询。事件触发时刷一次完整 list（事件携带 state，但 tools/config
  // 仍要从 list 拿；做一次 fetch 比维护增量补丁简单且不易撕裂）。
  // 浏览器自动重连 + ring buffer 续传，丢事件时下次 mount 也能复原。
  useMcpEvents({
    onEvent: (evt) => {
      if (evt.type === 'mcp.server.status' || evt.type === 'mcp.tools.changed') {
        void listServers()
          .then(setServers)
          .catch(() => undefined)
      } else if (evt.type === 'mcp.oauth.needed' && typeof evt.authorize_url === 'string') {
        // 后端已经 webbrowser.open 了；这里只是给用户一个回灌提示
        console.info('[MCP] oauth needed for', evt.server_id, '→', evt.authorize_url)
      } else if (
        evt.type === 'mcp.elicitation.request' &&
        typeof evt.request_id === 'string' &&
        typeof evt.server_id === 'string'
      ) {
        setPendingElicit({
          server_id: evt.server_id,
          request_id: evt.request_id,
          params: (evt.params as ElicitationRequest['params']) ?? {},
        })
      }
    },
  })

  const installedNames = useMemo(
    () => new Set(servers.map((s) => s.server_id)),
    [servers],
  )

  const handleCatalogPick = (entry: MCPCatalogEntry) => {
    setSpotlightEntry(entry)
  }

  const handleConfirmAdd = async (serverId: string, config: MCPServerConfig) => {
    await addServer(serverId, config)
    setSpotlightEntry(null)
    setCustomOpen(false)
    setTab('installed')
    await refresh()
  }

  const handleToggle = async (serverId: string, enabled: boolean) => {
    await toggleServer(serverId, enabled)
    await refresh()
  }

  const handleRestart = async (serverId: string) => {
    await restartServer(serverId)
    await refresh()
  }

  const handleDelete = async (serverId: string) => {
    await deleteServer(serverId)
    await refresh()
  }

  const handleReauth = async (serverId: string) => {
    await reauthServer(serverId)
    await refresh()
  }

  const handleInstallMcpb = async (file: File) => {
    setInstalling(true)
    setError(null)
    try {
      await installMcpb(file)
      setTab('installed')
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setInstalling(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  return (
    <div className="mcp-settings">
      <header className="mcp-settings-header">
        <div className="mcp-settings-title-row">
          <Puzzle size={22} />
          <h2>MCP</h2>
          <span className="mcp-badge-beta">beta</span>
        </div>
        <p className="mcp-settings-subtitle">
          通过 Model Context Protocol 接入第三方工具（GitHub / Notion / 浏览器自动化…），
          柳如烟里的角色可以直接调用。配置兼容 Claude Desktop / Cursor 同名文件。
        </p>
        <nav className="mcp-tab-bar">
          <button
            type="button"
            className={`mcp-tab-btn ${tab === 'installed' ? 'active' : ''}`}
            onClick={() => setTab('installed')}
          >
            已安装
            <span className="mcp-tab-count">{servers.length}</span>
          </button>
          <button
            type="button"
            className={`mcp-tab-btn ${tab === 'catalog' ? 'active' : ''}`}
            onClick={() => setTab('catalog')}
          >
            <Store size={15} />
            发现
          </button>
          <div className="mcp-tab-spacer" />
          <input
            ref={fileInputRef}
            type="file"
            accept=".mcpb,.dxt,application/zip"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) void handleInstallMcpb(f)
            }}
          />
          <button
            type="button"
            className="mcp-tab-action"
            onClick={() => fileInputRef.current?.click()}
            title="安装 .mcpb / .dxt 包"
            disabled={installing}
          >
            {installing ? <Loader2 size={15} className="lr-spin" /> : <Download size={15} />}
            {installing ? '安装中…' : '安装包'}
          </button>
          <button
            type="button"
            className="mcp-tab-action"
            onClick={() => setCustomOpen(true)}
            title="手动添加 MCP server"
          >
            <Plus size={15} /> 自定义
          </button>
          <button
            type="button"
            className="mcp-tab-action"
            onClick={() => void refresh()}
            title="刷新"
          >
            {loading ? <Loader2 size={15} className="lr-spin" /> : <RefreshCw size={15} />}
          </button>
        </nav>
      </header>

      {error ? <div className="mcp-error-banner">{error}</div> : null}

      <div className="mcp-settings-body">
        {tab === 'installed' ? (
          <McpServersList
            servers={servers}
            loading={loading}
            onToggle={handleToggle}
            onRestart={handleRestart}
            onDelete={handleDelete}
            onReauth={handleReauth}
            onPickFromCatalog={() => setTab('catalog')}
          />
        ) : (
          <>
            <ServerRegistrySpotlight
              entries={catalog}
              installed={installedNames}
              onPick={handleCatalogPick}
            />
            <MarketplaceSettings onCatalogChanged={() => void refresh()} />
          </>
        )}
      </div>

      {spotlightEntry ? (
        <AddMcpServerModal
          title={`添加 ${spotlightEntry.title}`}
          initialServerId={spotlightEntry.name}
          initialConfig={buildConfigFromEntry(spotlightEntry)}
          requiresToken={spotlightEntry.requires_token}
          onCancel={() => setSpotlightEntry(null)}
          onConfirm={handleConfirmAdd}
        />
      ) : null}

      {customOpen ? (
        <AddMcpServerModal
          title="自定义 MCP Server"
          initialServerId=""
          initialConfig={buildConfigFromEntry(null)}
          requiresToken={false}
          onCancel={() => setCustomOpen(false)}
          onConfirm={handleConfirmAdd}
        />
      ) : null}

      {pendingElicit ? (
        <ElicitationDialog
          request={pendingElicit}
          onClose={() => setPendingElicit(null)}
        />
      ) : null}
    </div>
  )
}

// ── helpers ──────────────────────────────────────────────────────────────
function buildConfigFromEntry(entry: MCPCatalogEntry | null): MCPServerConfig {
  const cfg = (entry?.configuration ?? {}) as Record<string, unknown>
  return {
    command: (cfg.command as string) ?? null,
    args: Array.isArray(cfg.args) ? ((cfg.args as string[]) ?? []) : [],
    env: (cfg.env as Record<string, string>) ?? {},
    url: (cfg.url as string) ?? null,
    headers: (cfg.headers as Record<string, string>) ?? {},
    transport: null,
    enabled: true,
    timeout: 120,
    connect_timeout: 60,
    description: entry?.description ?? '',
    cwd: null,
    agents: ['*'],
    concurrency_class: 'read',
    sampling: { enabled: false, allowed_models: [], max_tokens_cap: 4096, max_rpm: 10 },
    elicitation: { enabled: true, timeout_seconds: 60 },
  }
}
