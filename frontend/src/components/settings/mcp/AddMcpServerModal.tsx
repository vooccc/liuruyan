/**
 * 添加/编辑 MCP server 的配置表单。
 *
 * 字段覆盖 M1 必要项：server_id、transport、command/args/url/headers、env、enabled、timeout、agents。
 * 高级字段（sampling / elicitation / oauth）留 M2+ 展开。
 */

import { useEffect, useMemo, useState } from 'react'
import { X } from 'lucide-react'
import { getAgents } from '../../../api/chat'
import type { MCPServerConfig } from '../../../api/mcp'

interface Props {
  title: string
  initialServerId: string
  initialConfig: MCPServerConfig
  requiresToken: boolean
  onCancel: () => void
  onConfirm: (serverId: string, config: MCPServerConfig) => Promise<void>
}

type TransportKind = 'stdio' | 'streamable_http'

export default function AddMcpServerModal({
  title,
  initialServerId,
  initialConfig,
  requiresToken,
  onCancel,
  onConfirm,
}: Props) {
  const [serverId, setServerId] = useState(initialServerId)
  const [transport, setTransport] = useState<TransportKind>(
    initialConfig.url ? 'streamable_http' : 'stdio',
  )
  const [command, setCommand] = useState(initialConfig.command ?? '')
  const [argsText, setArgsText] = useState((initialConfig.args ?? []).join('\n'))
  const [url, setUrl] = useState(initialConfig.url ?? '')
  const [envRows, setEnvRows] = useState<Array<[string, string]>>(
    Object.entries(initialConfig.env ?? {}),
  )
  const [headersRows, setHeadersRows] = useState<Array<[string, string]>>(
    Object.entries(initialConfig.headers ?? {}),
  )
  const [description, setDescription] = useState(initialConfig.description ?? '')
  const [timeout, setTimeout] = useState(initialConfig.timeout)
  const [connectTimeout, setConnectTimeout] = useState(initialConfig.connect_timeout)
  // M3: 角色级权限——'*' 表示所有角色，否则按 agent_id 白名单
  const [agents, setAgents] = useState<string[]>(
    initialConfig.agents && initialConfig.agents.length > 0 ? initialConfig.agents : ['*'],
  )
  const allAgents = useMemo(() => getAgents(), [])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const isWildcard = agents.includes('*')
  const toggleAgent = (id: string) => {
    setAgents((prev) => {
      if (id === '*') return ['*']
      const filtered = prev.filter((x) => x !== '*')
      if (filtered.includes(id)) {
        const next = filtered.filter((x) => x !== id)
        // 全部取消勾选 → 回退 wildcard 防止误锁
        return next.length === 0 ? ['*'] : next
      }
      return [...filtered, id]
    })
  }

  useEffect(() => {
    setServerId(initialServerId)
  }, [initialServerId])

  const addEnvRow = () => setEnvRows((rows) => [...rows, ['', '']])
  const delEnvRow = (idx: number) => setEnvRows((rows) => rows.filter((_, i) => i !== idx))
  const updateEnvRow = (idx: number, k: string, v: string) =>
    setEnvRows((rows) => rows.map((row, i) => (i === idx ? [k, v] : row)))

  const addHeaderRow = () => setHeadersRows((rows) => [...rows, ['', '']])
  const delHeaderRow = (idx: number) =>
    setHeadersRows((rows) => rows.filter((_, i) => i !== idx))
  const updateHeaderRow = (idx: number, k: string, v: string) =>
    setHeadersRows((rows) => rows.map((row, i) => (i === idx ? [k, v] : row)))

  const handleSubmit = async () => {
    setError(null)
    const sid = serverId.trim()
    if (!sid) {
      setError('server_id 不能为空')
      return
    }
    if (!/^[a-z0-9_-]+$/i.test(sid)) {
      setError('server_id 仅支持字母/数字/下划线/短横（工具前缀规范）')
      return
    }

    const env: Record<string, string> = {}
    for (const [k, v] of envRows) {
      if (k) env[k] = v
    }
    const headers: Record<string, string> = {}
    for (const [k, v] of headersRows) {
      if (k) headers[k] = v
    }

    const cfg: MCPServerConfig = {
      ...initialConfig,
      command: transport === 'stdio' ? command.trim() || null : null,
      args:
        transport === 'stdio'
          ? argsText
              .split('\n')
              .map((s) => s.trim())
              .filter(Boolean)
          : [],
      url: transport === 'streamable_http' ? url.trim() || null : null,
      headers: transport === 'streamable_http' ? headers : {},
      env,
      transport: null, // 让后端按 url/command 推断
      enabled: true,
      timeout,
      connect_timeout: connectTimeout,
      description,
      agents,
    }

    setSubmitting(true)
    try {
      await onConfirm(sid, cfg)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="mcp-modal-backdrop" onClick={onCancel}>
      <div className="mcp-modal" onClick={(e) => e.stopPropagation()}>
        <header className="mcp-modal-head">
          <h3>{title}</h3>
          <button type="button" className="mcp-icon-btn" onClick={onCancel}>
            <X size={16} />
          </button>
        </header>

        <div className="mcp-modal-body">
          {requiresToken ? (
            <p className="mcp-modal-hint">
              该 server 需要 API token —— 在下方 env 里填入具体值，或用
              <code>${`{env:VAR}`}</code> 占位符引用系统环境变量（推荐）。
            </p>
          ) : null}

          <label className="mcp-form-row">
            <span>server_id（也是工具前缀 mcp__&lt;id&gt;__…）</span>
            <input
              type="text"
              value={serverId}
              onChange={(e) => setServerId(e.target.value)}
              placeholder="github"
              autoFocus
            />
          </label>

          <div className="mcp-form-row-group">
            <span className="mcp-form-row-label">传输方式</span>
            <div className="mcp-transport-switch">
              <label>
                <input
                  type="radio"
                  name="mcp-transport"
                  checked={transport === 'stdio'}
                  onChange={() => setTransport('stdio')}
                />
                stdio（本地命令，如 npx / uvx）
              </label>
              <label>
                <input
                  type="radio"
                  name="mcp-transport"
                  checked={transport === 'streamable_http'}
                  onChange={() => setTransport('streamable_http')}
                />
                HTTP（远程 server）
              </label>
            </div>
          </div>

          {transport === 'stdio' ? (
            <>
              <label className="mcp-form-row">
                <span>命令</span>
                <input
                  type="text"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  placeholder="npx"
                />
              </label>
              <label className="mcp-form-row">
                <span>参数（每行一个）</span>
                <textarea
                  rows={3}
                  value={argsText}
                  onChange={(e) => setArgsText(e.target.value)}
                  placeholder="-y&#10;@modelcontextprotocol/server-github"
                />
              </label>
            </>
          ) : (
            <>
              <label className="mcp-form-row">
                <span>URL</span>
                <input
                  type="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://mcp.example.com/sse"
                />
              </label>
              <KeyValueEditor
                title="HTTP Headers"
                rows={headersRows}
                onAdd={addHeaderRow}
                onDelete={delHeaderRow}
                onUpdate={updateHeaderRow}
              />
            </>
          )}

          <KeyValueEditor
            title="环境变量（支持 ${env:VAR} 占位符）"
            rows={envRows}
            onAdd={addEnvRow}
            onDelete={delEnvRow}
            onUpdate={updateEnvRow}
          />

          <div className="mcp-form-row-inline">
            <label>
              <span>连接超时（秒）</span>
              <input
                type="number"
                min={1}
                value={connectTimeout}
                onChange={(e) => setConnectTimeout(Number(e.target.value) || 60)}
              />
            </label>
            <label>
              <span>调用超时（秒）</span>
              <input
                type="number"
                min={1}
                value={timeout}
                onChange={(e) => setTimeout(Number(e.target.value) || 120)}
              />
            </label>
          </div>

          <div className="mcp-form-row-group">
            <span className="mcp-form-row-label">可见角色</span>
            <p className="mcp-form-hint">
              控制哪些角色可以调用本 server 的工具。默认全部角色可见（推荐）。
            </p>
            <div className="mcp-agents-picker">
              <button
                type="button"
                className={`mcp-tag-chip ${isWildcard ? 'active' : ''}`}
                onClick={() => toggleAgent('*')}
                title="所有角色（含未来新建的）"
              >
                * 全部
              </button>
              {allAgents.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className={`mcp-tag-chip ${!isWildcard && agents.includes(a.id) ? 'active' : ''}`}
                  onClick={() => toggleAgent(a.id)}
                  disabled={isWildcard}
                  title={a.id}
                >
                  {a.name || a.id}
                </button>
              ))}
              {allAgents.length === 0 ? (
                <span className="mcp-empty-inline">（暂无可选角色）</span>
              ) : null}
            </div>
          </div>

          <label className="mcp-form-row">
            <span>备注</span>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="可选"
            />
          </label>

          {error ? <div className="mcp-error-banner">{error}</div> : null}
        </div>

        <footer className="mcp-modal-foot">
          <button type="button" className="mcp-btn-secondary" onClick={onCancel} disabled={submitting}>
            取消
          </button>
          <button
            type="button"
            className="mcp-btn-primary"
            onClick={() => void handleSubmit()}
            disabled={submitting}
          >
            {submitting ? '添加中…' : '保存并启动'}
          </button>
        </footer>
      </div>
    </div>
  )
}

interface KVProps {
  title: string
  rows: Array<[string, string]>
  onAdd: () => void
  onDelete: (idx: number) => void
  onUpdate: (idx: number, k: string, v: string) => void
}

function KeyValueEditor({ title, rows, onAdd, onDelete, onUpdate }: KVProps) {
  return (
    <div className="mcp-kv-editor">
      <div className="mcp-kv-editor-head">
        <span>{title}</span>
        <button type="button" className="mcp-link-btn" onClick={onAdd}>
          + 添加
        </button>
      </div>
      {rows.length === 0 ? (
        <p className="mcp-empty-inline">（空）</p>
      ) : (
        <ul className="mcp-kv-editor-list">
          {rows.map(([k, v], idx) => (
            <li key={idx}>
              <input
                type="text"
                placeholder="KEY"
                value={k}
                onChange={(e) => onUpdate(idx, e.target.value, v)}
              />
              <input
                type="text"
                placeholder="VALUE"
                value={v}
                onChange={(e) => onUpdate(idx, k, e.target.value)}
              />
              <button type="button" className="mcp-link-btn-danger" onClick={() => onDelete(idx)}>
                删除
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
