/**
 * 已安装 MCP server 列表：每个 server 一个卡片，含状态、工具数、操作菜单。
 */

import { useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  KeyRound,
  Loader2,
  Power,
  RotateCw,
  Store,
  Trash2,
} from 'lucide-react'
import type { MCPServerListItem, MCPServerState } from '../../../api/mcp'
import { useConfirmDialog } from '../../ConfirmDialog'

interface Props {
  servers: MCPServerListItem[]
  loading: boolean
  onToggle: (serverId: string, enabled: boolean) => Promise<void>
  onRestart: (serverId: string) => Promise<void>
  onDelete: (serverId: string) => Promise<void>
  onReauth: (serverId: string) => Promise<void>
  onPickFromCatalog: () => void
}

export default function McpServersList({
  servers,
  loading,
  onToggle,
  onRestart,
  onDelete,
  onReauth,
  onPickFromCatalog,
}: Props) {
  if (loading && servers.length === 0) {
    return (
      <div className="mcp-empty">
        <Loader2 size={24} className="lr-spin" />
        <p>加载中…</p>
      </div>
    )
  }
  if (servers.length === 0) {
    return (
      <div className="mcp-empty">
        <p>还没有添加任何 MCP server。</p>
        <button type="button" className="mcp-cta-btn" onClick={onPickFromCatalog}>
          <Store size={16} /> 浏览目录
        </button>
      </div>
    )
  }
  return (
    <ul className="mcp-server-card-list">
      {servers.map((item) => (
        <ServerCard
          key={item.server_id}
          item={item}
          onToggle={onToggle}
          onRestart={onRestart}
          onDelete={onDelete}
          onReauth={onReauth}
        />
      ))}
    </ul>
  )
}

interface CardProps {
  item: MCPServerListItem
  onToggle: (serverId: string, enabled: boolean) => Promise<void>
  onRestart: (serverId: string) => Promise<void>
  onDelete: (serverId: string) => Promise<void>
  onReauth: (serverId: string) => Promise<void>
}

function ServerCard({ item, onToggle, onRestart, onDelete, onReauth }: CardProps) {
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy] = useState(false)
  const { config, status, tools } = item
  // Tauri webview 不支持 window.confirm
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog()

  const runBusy = async (fn: () => Promise<void>) => {
    setBusy(true)
    try {
      await fn()
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className={`mcp-server-card mcp-state-${status.state}`}>
      <header className="mcp-server-card-head">
        <button
          type="button"
          className="mcp-server-card-expand"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </button>
        <div className="mcp-server-card-title-wrap">
          <h3 className="mcp-server-card-title">{item.server_id}</h3>
          <div className="mcp-server-card-meta">
            <StateBadge state={status.state} />
            <span className="mcp-server-card-transport">
              {config.url ? 'HTTP' : config.command ?? '—'}
            </span>
            <span className="mcp-server-card-tool-count">{tools.length} 工具</span>
          </div>
          {config.description ? (
            <p className="mcp-server-card-desc">{config.description}</p>
          ) : null}
          {status.error ? (
            <p className="mcp-server-card-error">
              <AlertTriangle size={13} /> {status.error}
            </p>
          ) : null}
        </div>
        <div className="mcp-server-card-actions">
          {(status.state === 'needs_oauth' || status.state === 'needs_reauth') ? (
            <button
              type="button"
              className="mcp-icon-btn mcp-icon-btn-warn"
              disabled={busy}
              onClick={() => void runBusy(() => onReauth(item.server_id))}
              title="重新授权"
            >
              <KeyRound size={15} />
            </button>
          ) : null}
          <button
            type="button"
            className="mcp-icon-btn"
            disabled={busy}
            onClick={() => void runBusy(() => onToggle(item.server_id, !config.enabled))}
            title={config.enabled ? '禁用' : '启用'}
          >
            <Power size={15} />
          </button>
          <button
            type="button"
            className="mcp-icon-btn"
            disabled={busy || !config.enabled}
            onClick={() => void runBusy(() => onRestart(item.server_id))}
            title="重启"
          >
            {busy ? <Loader2 size={15} className="lr-spin" /> : <RotateCw size={15} />}
          </button>
          <button
            type="button"
            className="mcp-icon-btn mcp-icon-btn-danger"
            disabled={busy}
            onClick={async () => {
              const ok = await askConfirm('删除 MCP server', `删除 MCP server 「${item.server_id}」？`, 'danger', '删除')
              if (ok) {
                void runBusy(() => onDelete(item.server_id))
              }
            }}
            title="删除"
          >
            <Trash2 size={15} />
          </button>
        </div>
      </header>
      {expanded ? (
        <div className="mcp-server-card-body">
          {tools.length === 0 ? (
            <p className="mcp-empty-inline">此 server 目前未暴露任何工具。</p>
          ) : (
            <ul className="mcp-tool-list">
              {tools.map((t) => (
                <li key={t.qualified_name} className="mcp-tool-row">
                  <span className="mcp-tool-name">{t.name}</span>
                  <span className="mcp-tool-qname" title={t.qualified_name}>
                    {t.qualified_name}
                  </span>
                  {t.description ? (
                    <span className="mcp-tool-desc">{t.description}</span>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
      {confirmDialog}
    </li>
  )
}

function StateBadge({ state }: { state: MCPServerState }) {
  const map: Record<MCPServerState, { label: string; icon: React.ReactNode }> = {
    idle: { label: '未启动', icon: <span className="mcp-state-dot" /> },
    starting: { label: '启动中', icon: <Loader2 size={12} className="lr-spin" /> },
    running: { label: '运行中', icon: <CheckCircle2 size={12} /> },
    degraded: { label: '失败', icon: <AlertTriangle size={12} /> },
    stopping: { label: '停止中', icon: <Loader2 size={12} className="lr-spin" /> },
    needs_oauth: { label: '待授权', icon: <AlertTriangle size={12} /> },
    needs_reauth: { label: '需重新授权', icon: <AlertTriangle size={12} /> },
  }
  const entry = map[state]
  return (
    <span className={`mcp-state-badge mcp-state-badge-${state}`}>
      {entry.icon}
      {entry.label}
    </span>
  )
}
