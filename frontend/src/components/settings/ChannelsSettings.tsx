import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, CheckCircle2, Eye, EyeOff, Loader2, Plus, Trash2, X } from 'lucide-react'
import { API_BASE } from '../../api/base'
import { DingTalkIcon, FeishuIcon, MatrixIcon, TelegramIcon, QQIcon, WeChatIcon } from './channelIcons'
import { useConfirmDialog } from '../ConfirmDialog'

// 跟后端 _v2_channels_routes.MASK_VALUE 一致；漏一边即泄密 / 凭据丢失
const MASK_VALUE = '••••••••'

interface ChannelDef {
  id: string
  name: string
  logo: React.ReactNode
  accent: string
  description: string
  /** 后端实现状态：ready=已就绪可启用，planned=规划中 */
  status: 'ready' | 'planned'
}

const CHANNELS: ChannelDef[] = [
  {
    id: 'feishu',
    name: '飞书',
    logo: <FeishuIcon />,
    accent: '#3370ff',
    description: '通过 WebSocket 使用飞书 / Lark 机器人，接入企业群与私聊',
    status: 'ready',
  },
  {
    id: 'dingtalk',
    name: '钉钉',
    logo: <DingTalkIcon />,
    accent: '#007fff',
    description: '通过钉钉机器人 WebSocket 接入，支持企业群机器人与单聊',
    status: 'ready',
  },
  {
    id: 'matrix',
    name: 'Matrix',
    logo: <MatrixIcon />,
    accent: '#0DBD8B',
    description: '通过自建 Synapse 接入，跨平台 + 端到端加密，移动远程的私有方案',
    status: 'ready',
  },
  {
    id: 'telegram',
    name: 'Telegram',
    logo: <TelegramIcon />,
    accent: '#229ed9',
    description: '通过 Telegram 机器人使用长轮询，支持私聊与群聊',
    status: 'planned',
  },
  {
    id: 'qq',
    name: 'QQ',
    logo: <QQIcon />,
    accent: '#12b7f5',
    description: '通过 QQ 机器人官方 API 接收和回复消息',
    status: 'planned',
  },
  {
    id: 'wechat',
    name: '微信',
    logo: <WeChatIcon />,
    accent: '#07c160',
    description: '通过微信 iLink Bot API 接收和回复消息',
    status: 'planned',
  },
]

interface HealthzResponse {
  v2_channels?: string
  v2_channels_web?: boolean
  v2_channels_count?: number
}

// 后端 ChannelConfigResponse pydantic schema 镜像
interface ChannelConfigDTO {
  id: string
  channel: string
  config: Record<string, any>
  enabled: boolean
  created_at: number
  status: string
  last_error: string
}

interface AgentItem {
  id: string
  name: string
}

export default function ChannelsSettings() {
  const [health, setHealth] = useState<HealthzResponse | null>(null)
  const [healthErr, setHealthErr] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string>('dingtalk')

  useEffect(() => {
    fetch(`${API_BASE}/healthz`)
      .then((res) => res.ok ? res.json() : Promise.reject(`${res.status}`))
      .then((data) => setHealth(data))
      .catch((e) => setHealthErr(String(e)))
  }, [])

  const selected = CHANNELS.find((c) => c.id === selectedId) || CHANNELS[0]

  return (
    <div className="settings-models">
      <aside className="settings-models-providers" aria-label="频道列表">
        <div className="settings-providers-header">
          <span className="settings-channels-header-title">通讯频道</span>
        </div>
        <div className="settings-providers-list settings-channels-cards">
          {CHANNELS.map((ch) => (
            <button
              key={ch.id}
              type="button"
              className={`settings-channel-card ${selectedId === ch.id ? 'active' : ''}`}
              onClick={() => setSelectedId(ch.id)}
              style={{ ['--provider-accent' as any]: ch.accent }}
            >
              <span className="settings-channel-logo">{ch.logo}</span>
              <span className="settings-channel-info">
                <span className="settings-channel-name">{ch.name}</span>
                <span className="settings-channel-desc">{ch.description}</span>
              </span>
              <span className={`settings-channel-status ${ch.status}`}>
                {ch.status === 'ready' ? '后端就绪' : '规划中'}
              </span>
            </button>
          ))}
        </div>
      </aside>

      <main className="settings-models-detail">
        <header className="settings-detail-header">
          <div className="settings-detail-title">
            <span className="settings-detail-logo">{selected.logo}</span>
            <h2>{selected.name}</h2>
            <span className={`settings-channel-status ${selected.status}`}>
              {selected.status === 'ready' ? '后端就绪' : '规划中'}
            </span>
          </div>
          <p className="settings-detail-sub">{selected.description}</p>
        </header>

        <div className="settings-detail-body">
          {selected.id === 'dingtalk' ? (
            <DingTalkConfigPanel health={health} healthErr={healthErr} />
          ) : selected.id === 'matrix' ? (
            <MatrixConfigPanel health={health} healthErr={healthErr} />
          ) : selected.id === 'feishu' ? (
            <FeishuConfigPanel health={health} healthErr={healthErr} />
          ) : (
            <PlannedChannelDetail channel={selected} />
          )}
        </div>
      </main>
    </div>
  )
}

/* ========================================================================
   钉钉配置面板（GET/POST/PUT/DELETE /v2/channels/configs）
   ======================================================================== */

function DingTalkConfigPanel({ health, healthErr }: {
  health: HealthzResponse | null
  healthErr: string | null
}) {
  const [configs, setConfigs] = useState<ChannelConfigDTO[]>([])
  const [agents, setAgents] = useState<AgentItem[]>([])
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  // 'create' / config_id（编辑） / null（关闭）
  const [editing, setEditing] = useState<string | null>(null)

  const v2On = health?.v2_channels === 'running'

  const refresh = useCallback(async () => {
    if (!v2On) {
      setConfigs([])
      setLoading(false)
      return
    }
    try {
      const res = await fetch(`${API_BASE}/v2/channels/configs?channel=dingtalk`)
      if (!res.ok) throw new Error(`${res.status}`)
      setConfigs(await res.json())
      setLoadErr(null)
    } catch (e: any) {
      setLoadErr(e.message || '读取失败')
    } finally {
      setLoading(false)
    }
  }, [v2On])

  // 拉 agents 列表（默认角色 select 用）
  useEffect(() => {
    fetch(`${API_BASE}/agents`)
      .then((res) => res.ok ? res.json() : Promise.reject(`${res.status}`))
      .then((data) => {
        const list: AgentItem[] = (data.agents || []).map((a: any) => ({
          id: a.id,
          name: a.name || a.id,
        }))
        setAgents(list)
      })
      .catch(() => setAgents([]))
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  if (!v2On) {
    return (
      <div className="settings-coming-soon">
        <AlertCircle size={20} style={{ flexShrink: 0 }} />
        <div className="settings-coming-soon-text">
          <strong>v2 channels 未启用</strong>
          <p>
            {healthErr
              ? `读取后端状态失败：${healthErr}`
              : <>当前 <code>v2_channels = {health?.v2_channels || 'unknown'}</code>。设置环境变量 <code>LIURUYAN_V2_CHANNELS=1</code> 后重启即可启用。</>
            }
          </p>
          <p className="settings-coming-soon-current">
            启用后这里会列出钉钉账号配置（AppKey / AppSecret / 默认角色），可在线增删改启停。
          </p>
        </div>
      </div>
    )
  }

  return (
    <>
      <div className="settings-channels-status">
        <div className="settings-channels-status-row">
          <span className="settings-channels-status-label">后端 v2 channels</span>
          <span className="settings-channels-status-value ok">running</span>
        </div>
        <div className="settings-channels-status-row">
          <span className="settings-channels-status-label">已启动频道总数</span>
          <span className="settings-channels-status-value">{health?.v2_channels_count ?? 0}</span>
        </div>
      </div>

      <section className="settings-dingtalk-list">
        <div className="settings-table-header">
          <span className="settings-table-title">钉钉账号 ({configs.length})</span>
          <div className="settings-table-actions">
            <button
              type="button"
              className="settings-table-action"
              onClick={() => setEditing('create')}
              disabled={editing === 'create'}
            >
              <Plus size={13} /> 新增账号
            </button>
          </div>
        </div>

        {loading && (
          <div className="settings-fetcher-loading">
            <Loader2 size={14} className="spin" /> 加载中…
          </div>
        )}
        {loadErr && (
          <div className="settings-fetcher-error">
            <AlertCircle size={13} /> {loadErr}
          </div>
        )}

        {!loading && configs.length === 0 && editing !== 'create' && (
          <div className="settings-coming-soon" style={{ marginTop: 8 }}>
            <AlertCircle size={18} style={{ flexShrink: 0 }} />
            <div className="settings-coming-soon-text">
              <strong>还没有钉钉账号</strong>
              <p>点击右上角"新增账号"配置一个钉钉应用的 AppKey / AppSecret，机器人即可上线。</p>
            </div>
          </div>
        )}

        {editing === 'create' && (
          <DingTalkConfigForm
            mode="create"
            agents={agents}
            existingIds={new Set(configs.map((c) => c.id))}
            onCancel={() => setEditing(null)}
            onSaved={async () => {
              setEditing(null)
              await refresh()
            }}
          />
        )}

        {configs.map((cfg) => (
          editing === cfg.id ? (
            <DingTalkConfigForm
              key={cfg.id}
              mode="edit"
              initial={cfg}
              agents={agents}
              existingIds={new Set(configs.map((c) => c.id))}
              onCancel={() => setEditing(null)}
              onSaved={async () => {
                setEditing(null)
                await refresh()
              }}
            />
          ) : (
            <DingTalkConfigCard
              key={cfg.id}
              config={cfg}
              agents={agents}
              onEdit={() => setEditing(cfg.id)}
              onChanged={refresh}
            />
          )
        ))}
      </section>
    </>
  )
}

/* ========================================================================
   单条钉钉配置卡片：状态 / 切启停 / 编辑 / 删除
   ======================================================================== */

function DingTalkConfigCard({ config, agents, onEdit, onChanged }: {
  config: ChannelConfigDTO
  agents: AgentItem[]
  onEdit: () => void
  onChanged: () => Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Tauri webview 不支持 window.confirm
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog()

  const agentName = agents.find((a) => a.id === config.config.default_agent)?.name
    || config.config.default_agent
    || '未设置'
  const clientId = String(config.config.client_id || '')
  const maskedClientId = clientId.length > 6
    ? `${clientId.slice(0, 4)}***${clientId.slice(-4)}`
    : clientId

  const callAndRefresh = useCallback(async (path: string, method = 'POST') => {
    setBusy(true)
    setErr(null)
    try {
      const res = await fetch(`${API_BASE}${path}`, { method })
      if (!res.ok && res.status !== 204) {
        const body = await res.text()
        throw new Error(`${res.status}: ${body}`)
      }
      await onChanged()
    } catch (e: any) {
      setErr(e.message || '操作失败')
    } finally {
      setBusy(false)
    }
  }, [onChanged])

  const toggleEnabled = async () => {
    if (busy) return
    if (config.enabled) {
      await callAndRefresh(`/v2/channels/configs/${encodeURIComponent(config.id)}/disable`)
    } else {
      await callAndRefresh(`/v2/channels/configs/${encodeURIComponent(config.id)}/enable`)
    }
  }

  const handleDelete = async () => {
    if (busy) return
    const ok = await askConfirm('删除钉钉账号', `确定删除钉钉账号 「${config.id}」 吗？`, 'danger', '删除')
    if (!ok) return
    await callAndRefresh(`/v2/channels/configs/${encodeURIComponent(config.id)}`, 'DELETE')
  }

  const statusClass = config.status === 'running'
    ? 'ok'
    : config.status === 'error' ? 'fail' : 'idle'

  return (
    <div className="settings-dingtalk-card">
      <div className="settings-dingtalk-card-head">
        <span className={`settings-dingtalk-status-dot ${statusClass}`} />
        <span className="settings-dingtalk-card-id">{config.id}</span>
        <span className="settings-dingtalk-status-text">{config.status}</span>
        <div className="settings-dingtalk-card-actions">
          <label className="settings-dingtalk-toggle">
            <input
              type="checkbox"
              checked={config.enabled}
              onChange={toggleEnabled}
              disabled={busy}
            />
            <span>{config.enabled ? '已启用' : '已停用'}</span>
          </label>
          <button
            type="button"
            className="settings-dingtalk-btn"
            onClick={onEdit}
            disabled={busy}
          >
            编辑
          </button>
          <button
            type="button"
            className="settings-dingtalk-btn settings-dingtalk-btn-danger"
            onClick={handleDelete}
            disabled={busy}
            title="删除"
          >
            <Trash2 size={12} />
          </button>
        </div>
      </div>
      <div className="settings-dingtalk-card-body">
        <div className="settings-dingtalk-row">
          <span className="settings-dingtalk-label">AppKey</span>
          <span className="settings-dingtalk-value"><code>{maskedClientId || '—'}</code></span>
        </div>
        <div className="settings-dingtalk-row">
          <span className="settings-dingtalk-label">默认角色</span>
          <span className="settings-dingtalk-value">{agentName}</span>
        </div>
        {config.config.robot_code && (
          <div className="settings-dingtalk-row">
            <span className="settings-dingtalk-label">RobotCode</span>
            <span className="settings-dingtalk-value"><code>{String(config.config.robot_code)}</code></span>
          </div>
        )}
        {config.last_error && (
          <div className="settings-dingtalk-row settings-dingtalk-error">
            <AlertCircle size={12} />
            <span>{config.last_error}</span>
          </div>
        )}
        {err && (
          <div className="settings-dingtalk-row settings-dingtalk-error">
            <AlertCircle size={12} />
            <span>{err}</span>
          </div>
        )}
      </div>
      {confirmDialog}
    </div>
  )
}

/* ========================================================================
   钉钉配置内联表单（创建 + 编辑共用）
   ======================================================================== */

interface DingTalkFormState {
  id: string
  client_id: string
  client_secret: string  // "" 表示"未改"（编辑模式）；提交时若为空 → 送 MASK_VALUE
  default_agent: string
  robot_code: string
  enabled: boolean
}

function DingTalkConfigForm({
  mode, initial, agents, existingIds, onCancel, onSaved,
}: {
  mode: 'create' | 'edit'
  initial?: ChannelConfigDTO
  agents: AgentItem[]
  existingIds: Set<string>
  onCancel: () => void
  onSaved: () => Promise<void>
}) {
  const isCreate = mode === 'create'
  const hasExistingSecret = !isCreate && Boolean(initial?.config.client_secret)

  const [form, setForm] = useState<DingTalkFormState>(() => ({
    id: initial?.id || '',
    client_id: String(initial?.config.client_id || ''),
    client_secret: '',  // 编辑模式留空——后端会保留原值
    // default_agent 初始空字符串：agents 异步加载完才能拿到合法 id。下方 effect
    // 兜底——别在这里硬编码 "liuruyan"，那是 template_id 不是 agent.id（agents
    // 列表里没人匹配 → select 显示首项但 state 保留无效值 → onChange 不触发）。
    default_agent: String(initial?.config.default_agent || ''),
    robot_code: String(initial?.config.robot_code || ''),
    enabled: initial?.enabled ?? true,
  }))
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [showSecret, setShowSecret] = useState(false)

  const update = <K extends keyof DingTalkFormState>(key: K, v: DingTalkFormState[K]) => {
    setForm((s) => ({ ...s, [key]: v }))
  }

  // agents 异步加载完后：如 form.default_agent 是空（create 模式 + 还没加载完
  // 时设置的初始空串），自动选第一个 agent。不动"非空但不在列表"的值——那是
  // 历史配置存了已删除的 agent_id 场景，select 会把它显示成"(已删除？)"，submit
  // 守卫强制用户重选，避免静默改写旧数据。
  useEffect(() => {
    if (agents.length === 0) return
    if (!form.default_agent) {
      update('default_agent', agents[0].id)
    }
  }, [agents, form.default_agent])

  const handleSubmit = async () => {
    setErr(null)
    if (isCreate) {
      const id = form.id.trim()
      if (!id) return setErr('账号 ID 不能为空（用于本地唯一标识，建议 dingtalk-公司名）')
      if (existingIds.has(id)) return setErr(`账号 ID "${id}" 已存在`)
    }
    if (!form.client_id.trim()) return setErr('AppKey 不能为空')
    if (isCreate && !form.client_secret.trim()) return setErr('AppSecret 不能为空')
    if (!form.default_agent) return setErr('请选择默认角色（角色列表加载中？）')
    if (agents.length > 0 && !agents.some((a) => a.id === form.default_agent)) {
      return setErr(`默认角色 "${form.default_agent}" 不在当前 agents 列表（已删除？请重选）`)
    }

    const configPayload: Record<string, any> = {
      client_id: form.client_id.trim(),
      // 编辑模式 secret 留空 → 送 MASK_VALUE 让后端保留；填了就送新值
      client_secret: form.client_secret || (hasExistingSecret ? MASK_VALUE : ''),
      default_agent: form.default_agent,
    }
    if (form.robot_code.trim()) {
      configPayload.robot_code = form.robot_code.trim()
    }

    setSaving(true)
    try {
      let res: Response
      if (isCreate) {
        res = await fetch(`${API_BASE}/v2/channels/configs`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            id: form.id.trim(),
            channel: 'dingtalk',
            config: configPayload,
            enabled: form.enabled,
          }),
        })
      } else {
        res = await fetch(`${API_BASE}/v2/channels/configs/${encodeURIComponent(initial!.id)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            config: configPayload,
            enabled: form.enabled,
          }),
        })
      }
      if (!res.ok) {
        const body = await res.text()
        throw new Error(`${res.status}: ${body}`)
      }
      await onSaved()
    } catch (e: any) {
      setErr(e.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="settings-dingtalk-form">
      <div className="settings-dingtalk-form-head">
        <span>{isCreate ? '新增钉钉账号' : `编辑：${initial?.id}`}</span>
        <button type="button" className="settings-dingtalk-btn" onClick={onCancel} disabled={saving}>
          <X size={12} />
        </button>
      </div>

      <div className="settings-dingtalk-form-body">
        {isCreate && (
          <div className="settings-conn-row">
            <label className="settings-conn-label">账号 ID</label>
            <div className="settings-conn-input">
              <input
                type="text"
                value={form.id}
                placeholder="dingtalk-corp-a"
                onChange={(e) => update('id', e.target.value)}
              />
            </div>
          </div>
        )}

        <div className="settings-conn-row">
          <label className="settings-conn-label">AppKey</label>
          <div className="settings-conn-input">
            <input
              type="text"
              value={form.client_id}
              placeholder="钉钉应用 AppKey（client_id）"
              onChange={(e) => update('client_id', e.target.value)}
            />
          </div>
        </div>

        <div className="settings-conn-row">
          <label className="settings-conn-label">AppSecret</label>
          <div className="settings-conn-input">
            <input
              type={showSecret ? 'text' : 'password'}
              value={form.client_secret}
              placeholder={hasExistingSecret ? '已设置 · 留空保留原值' : '钉钉应用 AppSecret'}
              onChange={(e) => update('client_secret', e.target.value)}
              autoComplete="new-password"
            />
            <button
              type="button"
              className="settings-conn-eye"
              onClick={() => setShowSecret((v) => !v)}
              title={showSecret ? '隐藏' : '显示'}
            >
              {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
        </div>

        <div className="settings-conn-row">
          <label className="settings-conn-label">默认角色</label>
          <select
            className="settings-dingtalk-select"
            value={form.default_agent}
            onChange={(e) => update('default_agent', e.target.value)}
            disabled={agents.length === 0}
          >
            {agents.length === 0 && <option value="">加载角色列表中…</option>}
            {/* 历史配置的 default_agent 已被删除时，仍把它当一个特殊选项展示，
                避免 select.value 与 option.value 脱节（不然浏览器会把 state
                和"视觉首项"错位） */}
            {form.default_agent && !agents.some((a) => a.id === form.default_agent) && (
              <option value={form.default_agent}>
                {form.default_agent}（已删除？）
              </option>
            )}
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.name}（{a.id}）</option>
            ))}
          </select>
        </div>

        <div className="settings-conn-row">
          <label className="settings-conn-label">RobotCode <span className="settings-dingtalk-optional">（可选）</span></label>
          <div className="settings-conn-input">
            <input
              type="text"
              value={form.robot_code}
              placeholder="留空使用 AppKey 作为 robotCode"
              onChange={(e) => update('robot_code', e.target.value)}
            />
          </div>
        </div>

        <div className="settings-conn-row">
          <label className="settings-conn-label">启用状态</label>
          <div className="settings-conn-input">
            <label className="settings-dingtalk-toggle">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => update('enabled', e.target.checked)}
              />
              <span>{form.enabled ? '保存后立即启动' : '保存后停止'}</span>
            </label>
          </div>
        </div>
      </div>

      {err && (
        <div className="settings-dingtalk-error" style={{ padding: '4px 12px 0' }}>
          <AlertCircle size={12} /> <span>{err}</span>
        </div>
      )}

      <div className="settings-dingtalk-form-foot">
        <button type="button" className="settings-dingtalk-btn" onClick={onCancel} disabled={saving}>
          取消
        </button>
        <button
          type="button"
          className="settings-dingtalk-btn settings-dingtalk-btn-primary"
          onClick={handleSubmit}
          disabled={saving}
        >
          {saving ? <Loader2 size={12} className="spin" /> : <CheckCircle2 size={12} />}
          {isCreate ? '创建并启动' : '保存修改'}
        </button>
      </div>
    </div>
  )
}

/* ========================================================================
   Matrix 配置面板（GET/POST/PUT/DELETE /v2/channels/configs?channel=matrix）
   ======================================================================== */

function MatrixConfigPanel({ health, healthErr }: {
  health: HealthzResponse | null
  healthErr: string | null
}) {
  const [configs, setConfigs] = useState<ChannelConfigDTO[]>([])
  const [agents, setAgents] = useState<AgentItem[]>([])
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)

  const v2On = health?.v2_channels === 'running'

  const refresh = useCallback(async () => {
    if (!v2On) {
      setConfigs([])
      setLoading(false)
      return
    }
    try {
      const res = await fetch(`${API_BASE}/v2/channels/configs?channel=matrix`)
      if (!res.ok) throw new Error(`${res.status}`)
      setConfigs(await res.json())
      setLoadErr(null)
    } catch (e: any) {
      setLoadErr(e.message || '读取失败')
    } finally {
      setLoading(false)
    }
  }, [v2On])

  useEffect(() => {
    fetch(`${API_BASE}/agents`)
      .then((res) => res.ok ? res.json() : Promise.reject(`${res.status}`))
      .then((data) => {
        const list: AgentItem[] = (data.agents || []).map((a: any) => ({
          id: a.id,
          name: a.name || a.id,
        }))
        setAgents(list)
      })
      .catch(() => setAgents([]))
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  if (!v2On) {
    return (
      <div className="settings-coming-soon">
        <AlertCircle size={20} style={{ flexShrink: 0 }} />
        <div className="settings-coming-soon-text">
          <strong>v2 channels 未启用</strong>
          <p>
            {healthErr
              ? `读取后端状态失败：${healthErr}`
              : <>当前 <code>v2_channels = {health?.v2_channels || 'unknown'}</code>。设置环境变量 <code>LIURUYAN_V2_CHANNELS=1</code> 后重启即可启用。</>
            }
          </p>
          <p className="settings-coming-soon-current">
            启用后这里会列出 Matrix 账号配置（Homeserver / Bot User ID / Access Token / 默认角色），可在线增删改启停。
          </p>
        </div>
      </div>
    )
  }

  return (
    <>
      <div className="settings-channels-status">
        <div className="settings-channels-status-row">
          <span className="settings-channels-status-label">后端 v2 channels</span>
          <span className="settings-channels-status-value ok">running</span>
        </div>
        <div className="settings-channels-status-row">
          <span className="settings-channels-status-label">已启动频道总数</span>
          <span className="settings-channels-status-value">{health?.v2_channels_count ?? 0}</span>
        </div>
      </div>

      <section className="settings-dingtalk-list">
        <div className="settings-table-header">
          <span className="settings-table-title">Matrix 账号 ({configs.length})</span>
          <div className="settings-table-actions">
            <button
              type="button"
              className="settings-table-action"
              onClick={() => setEditing('create')}
              disabled={editing === 'create'}
            >
              <Plus size={13} /> 新增账号
            </button>
          </div>
        </div>

        {loading && (
          <div className="settings-fetcher-loading">
            <Loader2 size={14} className="spin" /> 加载中…
          </div>
        )}
        {loadErr && (
          <div className="settings-fetcher-error">
            <AlertCircle size={13} /> {loadErr}
          </div>
        )}

        {!loading && configs.length === 0 && editing !== 'create' && (
          <div className="settings-coming-soon" style={{ marginTop: 8 }}>
            <AlertCircle size={18} style={{ flexShrink: 0 }} />
            <div className="settings-coming-soon-text">
              <strong>还没有 Matrix 账号</strong>
              <p>点击右上角"新增账号"配置 Synapse Homeserver 和 bot 凭据，机器人即可上线。</p>
            </div>
          </div>
        )}

        {editing === 'create' && (
          <MatrixConfigForm
            mode="create"
            agents={agents}
            existingIds={new Set(configs.map((c) => c.id))}
            onCancel={() => setEditing(null)}
            onSaved={async () => {
              setEditing(null)
              await refresh()
            }}
          />
        )}

        {configs.map((cfg) => (
          editing === cfg.id ? (
            <MatrixConfigForm
              key={cfg.id}
              mode="edit"
              initial={cfg}
              agents={agents}
              existingIds={new Set(configs.map((c) => c.id))}
              onCancel={() => setEditing(null)}
              onSaved={async () => {
                setEditing(null)
                await refresh()
              }}
            />
          ) : (
            <MatrixConfigCard
              key={cfg.id}
              config={cfg}
              agents={agents}
              onEdit={() => setEditing(cfg.id)}
              onChanged={refresh}
            />
          )
        ))}
      </section>
    </>
  )
}

/* ========================================================================
   单条 Matrix 配置卡片
   ======================================================================== */

function MatrixConfigCard({ config, agents, onEdit, onChanged }: {
  config: ChannelConfigDTO
  agents: AgentItem[]
  onEdit: () => void
  onChanged: () => Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Tauri webview 不支持 window.confirm
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog()

  const agentName = agents.find((a) => a.id === config.config.default_agent)?.name
    || config.config.default_agent
    || '未设置'
  const homeserver = String(config.config.homeserver_url || '')
  const userId = String(config.config.user_id || '')

  const callAndRefresh = useCallback(async (path: string, method = 'POST') => {
    setBusy(true)
    setErr(null)
    try {
      const res = await fetch(`${API_BASE}${path}`, { method })
      if (!res.ok && res.status !== 204) {
        const body = await res.text()
        throw new Error(`${res.status}: ${body}`)
      }
      await onChanged()
    } catch (e: any) {
      setErr(e.message || '操作失败')
    } finally {
      setBusy(false)
    }
  }, [onChanged])

  const toggleEnabled = async () => {
    if (busy) return
    if (config.enabled) {
      await callAndRefresh(`/v2/channels/configs/${encodeURIComponent(config.id)}/disable`)
    } else {
      await callAndRefresh(`/v2/channels/configs/${encodeURIComponent(config.id)}/enable`)
    }
  }

  const handleDelete = async () => {
    if (busy) return
    const ok = await askConfirm('删除 Matrix 账号', `确定删除 Matrix 账号 「${config.id}」 吗？`, 'danger', '删除')
    if (!ok) return
    await callAndRefresh(`/v2/channels/configs/${encodeURIComponent(config.id)}`, 'DELETE')
  }

  const statusClass = config.status === 'running'
    ? 'ok'
    : config.status === 'error' ? 'fail' : 'idle'

  return (
    <div className="settings-dingtalk-card">
      <div className="settings-dingtalk-card-head">
        <span className={`settings-dingtalk-status-dot ${statusClass}`} />
        <span className="settings-dingtalk-card-id">{config.id}</span>
        <span className="settings-dingtalk-status-text">{config.status}</span>
        <div className="settings-dingtalk-card-actions">
          <label className="settings-dingtalk-toggle">
            <input
              type="checkbox"
              checked={config.enabled}
              onChange={toggleEnabled}
              disabled={busy}
            />
            <span>{config.enabled ? '已启用' : '已停用'}</span>
          </label>
          <button
            type="button"
            className="settings-dingtalk-btn"
            onClick={onEdit}
            disabled={busy}
          >
            编辑
          </button>
          <button
            type="button"
            className="settings-dingtalk-btn settings-dingtalk-btn-danger"
            onClick={handleDelete}
            disabled={busy}
            title="删除"
          >
            <Trash2 size={12} />
          </button>
        </div>
      </div>
      <div className="settings-dingtalk-card-body">
        <div className="settings-dingtalk-row">
          <span className="settings-dingtalk-label">Homeserver</span>
          <span className="settings-dingtalk-value"><code>{homeserver || '—'}</code></span>
        </div>
        <div className="settings-dingtalk-row">
          <span className="settings-dingtalk-label">Bot User ID</span>
          <span className="settings-dingtalk-value"><code>{userId || '—'}</code></span>
        </div>
        <div className="settings-dingtalk-row">
          <span className="settings-dingtalk-label">默认角色</span>
          <span className="settings-dingtalk-value">{agentName}</span>
        </div>
        {config.last_error && (
          <div className="settings-dingtalk-row settings-dingtalk-error">
            <AlertCircle size={12} />
            <span>{config.last_error}</span>
          </div>
        )}
        {err && (
          <div className="settings-dingtalk-row settings-dingtalk-error">
            <AlertCircle size={12} />
            <span>{err}</span>
          </div>
        )}
      </div>
      {confirmDialog}
    </div>
  )
}

/* ========================================================================
   Matrix 配置内联表单（创建 + 编辑共用）
   ======================================================================== */

interface MatrixFormState {
  id: string
  homeserver_url: string
  user_id: string
  access_token: string  // "" 表示"未改"（编辑模式）；提交时若为空 → 送 MASK_VALUE
  default_agent: string
  enabled: boolean
}

function MatrixConfigForm({
  mode, initial, agents, existingIds, onCancel, onSaved,
}: {
  mode: 'create' | 'edit'
  initial?: ChannelConfigDTO
  agents: AgentItem[]
  existingIds: Set<string>
  onCancel: () => void
  onSaved: () => Promise<void>
}) {
  const isCreate = mode === 'create'
  const hasExistingToken = !isCreate && Boolean(initial?.config.access_token)

  const [form, setForm] = useState<MatrixFormState>(() => ({
    id: initial?.id || '',
    homeserver_url: String(initial?.config.homeserver_url || ''),
    user_id: String(initial?.config.user_id || ''),
    access_token: '',  // 编辑模式留空——后端会保留原值
    default_agent: String(initial?.config.default_agent || ''),
    enabled: initial?.enabled ?? true,
  }))
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [showToken, setShowToken] = useState(false)

  const update = <K extends keyof MatrixFormState>(key: K, v: MatrixFormState[K]) => {
    setForm((s) => ({ ...s, [key]: v }))
  }

  // agents 异步加载完后自动选第一个（参照 DingTalk 表单同款守卫）
  useEffect(() => {
    if (agents.length === 0) return
    if (!form.default_agent) {
      update('default_agent', agents[0].id)
    }
  }, [agents, form.default_agent])

  const handleSubmit = async () => {
    setErr(null)
    if (isCreate) {
      const id = form.id.trim()
      if (!id) return setErr('账号 ID 不能为空（用于本地唯一标识，建议 matrix-个人 / matrix-工作）')
      if (existingIds.has(id)) return setErr(`账号 ID "${id}" 已存在`)
    }
    if (!form.homeserver_url.trim()) return setErr('Homeserver URL 不能为空')
    // 简单 URL 格式校验：必须 http:// 或 https://
    if (!/^https?:\/\//.test(form.homeserver_url.trim())) {
      return setErr('Homeserver URL 必须以 http:// 或 https:// 开头')
    }
    if (!form.user_id.trim()) return setErr('Bot User ID 不能为空')
    if (!form.user_id.startsWith('@') || !form.user_id.includes(':')) {
      return setErr('Bot User ID 格式应为 @username:server.com')
    }
    if (isCreate && !form.access_token.trim()) return setErr('Access Token 不能为空')
    if (!form.default_agent) return setErr('请选择默认角色（角色列表加载中？）')
    if (agents.length > 0 && !agents.some((a) => a.id === form.default_agent)) {
      return setErr(`默认角色 "${form.default_agent}" 不在当前 agents 列表（已删除？请重选）`)
    }

    const configPayload: Record<string, any> = {
      homeserver_url: form.homeserver_url.trim(),
      user_id: form.user_id.trim(),
      // 编辑模式 token 留空 → 送 MASK_VALUE 让后端保留；填了就送新值
      access_token: form.access_token || (hasExistingToken ? MASK_VALUE : ''),
      default_agent: form.default_agent,
    }

    setSaving(true)
    try {
      let res: Response
      if (isCreate) {
        res = await fetch(`${API_BASE}/v2/channels/configs`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            id: form.id.trim(),
            channel: 'matrix',
            config: configPayload,
            enabled: form.enabled,
          }),
        })
      } else {
        res = await fetch(`${API_BASE}/v2/channels/configs/${encodeURIComponent(initial!.id)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            config: configPayload,
            enabled: form.enabled,
          }),
        })
      }
      if (!res.ok) {
        const body = await res.text()
        throw new Error(`${res.status}: ${body}`)
      }
      await onSaved()
    } catch (e: any) {
      setErr(e.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="settings-dingtalk-form">
      <div className="settings-dingtalk-form-head">
        <span>{isCreate ? '新增 Matrix 账号' : `编辑：${initial?.id}`}</span>
        <button type="button" className="settings-dingtalk-btn" onClick={onCancel} disabled={saving}>
          <X size={12} />
        </button>
      </div>

      <div className="settings-dingtalk-form-body">
        {isCreate && (
          <div className="settings-conn-row">
            <label className="settings-conn-label">账号 ID</label>
            <div className="settings-conn-input">
              <input
                type="text"
                value={form.id}
                placeholder="matrix-personal"
                onChange={(e) => update('id', e.target.value)}
              />
            </div>
          </div>
        )}

        <div className="settings-conn-row">
          <label className="settings-conn-label">Homeserver URL</label>
          <div className="settings-conn-input">
            <input
              type="text"
              value={form.homeserver_url}
              placeholder="https://matrix.yourdomain.cn"
              onChange={(e) => update('homeserver_url', e.target.value)}
            />
          </div>
        </div>

        <div className="settings-conn-row">
          <label className="settings-conn-label">Bot User ID</label>
          <div className="settings-conn-input">
            <input
              type="text"
              value={form.user_id}
              placeholder="@liuruyan-bot:yourdomain.cn"
              onChange={(e) => update('user_id', e.target.value)}
            />
          </div>
        </div>

        <div className="settings-conn-row">
          <label className="settings-conn-label">Access Token</label>
          <div className="settings-conn-input">
            <input
              type={showToken ? 'text' : 'password'}
              value={form.access_token}
              placeholder={hasExistingToken ? '已设置 · 留空保留原值' : 'bot 账号的 Matrix access_token'}
              onChange={(e) => update('access_token', e.target.value)}
              autoComplete="new-password"
            />
            <button
              type="button"
              className="settings-conn-eye"
              onClick={() => setShowToken((v) => !v)}
              title={showToken ? '隐藏' : '显示'}
            >
              {showToken ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
        </div>

        <div className="settings-conn-row">
          <label className="settings-conn-label">默认角色</label>
          <select
            className="settings-dingtalk-select"
            value={form.default_agent}
            onChange={(e) => update('default_agent', e.target.value)}
            disabled={agents.length === 0}
          >
            {agents.length === 0 && <option value="">加载角色列表中…</option>}
            {form.default_agent && !agents.some((a) => a.id === form.default_agent) && (
              <option value={form.default_agent}>
                {form.default_agent}（已删除？）
              </option>
            )}
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.name}（{a.id}）</option>
            ))}
          </select>
        </div>

        <div className="settings-conn-row">
          <label className="settings-conn-label">启用状态</label>
          <div className="settings-conn-input">
            <label className="settings-dingtalk-toggle">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => update('enabled', e.target.checked)}
              />
              <span>{form.enabled ? '保存后立即启动' : '保存后停止'}</span>
            </label>
          </div>
        </div>
      </div>

      {err && (
        <div className="settings-dingtalk-error" style={{ padding: '4px 12px 0' }}>
          <AlertCircle size={12} /> <span>{err}</span>
        </div>
      )}

      <div className="settings-dingtalk-form-foot">
        <button type="button" className="settings-dingtalk-btn" onClick={onCancel} disabled={saving}>
          取消
        </button>
        <button
          type="button"
          className="settings-dingtalk-btn settings-dingtalk-btn-primary"
          onClick={handleSubmit}
          disabled={saving}
        >
          {saving ? <Loader2 size={12} className="spin" /> : <CheckCircle2 size={12} />}
          {isCreate ? '创建并启动' : '保存修改'}
        </button>
      </div>
    </div>
  )
}

/* ========================================================================
   飞书配置面板（GET/POST/PUT/DELETE /v2/channels/configs?channel=feishu）
   ======================================================================== */

function FeishuConfigPanel({ health, healthErr }: {
  health: HealthzResponse | null
  healthErr: string | null
}) {
  const [configs, setConfigs] = useState<ChannelConfigDTO[]>([])
  const [agents, setAgents] = useState<AgentItem[]>([])
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)

  const v2On = health?.v2_channels === 'running'

  const refresh = useCallback(async () => {
    if (!v2On) {
      setConfigs([])
      setLoading(false)
      return
    }
    try {
      const res = await fetch(`${API_BASE}/v2/channels/configs?channel=feishu`)
      if (!res.ok) throw new Error(`${res.status}`)
      setConfigs(await res.json())
      setLoadErr(null)
    } catch (e: any) {
      setLoadErr(e.message || '读取失败')
    } finally {
      setLoading(false)
    }
  }, [v2On])

  useEffect(() => {
    fetch(`${API_BASE}/agents`)
      .then((res) => res.ok ? res.json() : Promise.reject(`${res.status}`))
      .then((data) => {
        const list: AgentItem[] = (data.agents || []).map((a: any) => ({
          id: a.id,
          name: a.name || a.id,
        }))
        setAgents(list)
      })
      .catch(() => setAgents([]))
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  if (!v2On) {
    return (
      <div className="settings-coming-soon">
        <AlertCircle size={20} style={{ flexShrink: 0 }} />
        <div className="settings-coming-soon-text">
          <strong>v2 channels 未启用</strong>
          <p>
            {healthErr
              ? `读取后端状态失败：${healthErr}`
              : <>当前 <code>v2_channels = {health?.v2_channels || 'unknown'}</code>。设置环境变量 <code>LIURUYAN_V2_CHANNELS=1</code> 后重启即可启用。</>
            }
          </p>
          <p className="settings-coming-soon-current">
            启用后这里会列出飞书账号配置（AppID / AppSecret / 默认角色 / 国际版 toggle），可在线增删改启停。
          </p>
        </div>
      </div>
    )
  }

  return (
    <>
      <div className="settings-channels-status">
        <div className="settings-channels-status-row">
          <span className="settings-channels-status-label">后端 v2 channels</span>
          <span className="settings-channels-status-value ok">running</span>
        </div>
        <div className="settings-channels-status-row">
          <span className="settings-channels-status-label">已启动频道总数</span>
          <span className="settings-channels-status-value">{health?.v2_channels_count ?? 0}</span>
        </div>
      </div>

      <section className="settings-dingtalk-list">
        <div className="settings-table-header">
          <span className="settings-table-title">飞书账号 ({configs.length})</span>
          <div className="settings-table-actions">
            <button
              type="button"
              className="settings-table-action"
              onClick={() => setEditing('create')}
              disabled={editing === 'create'}
            >
              <Plus size={13} /> 新增账号
            </button>
          </div>
        </div>

        {loading && (
          <div className="settings-fetcher-loading">
            <Loader2 size={14} className="spin" /> 加载中…
          </div>
        )}
        {loadErr && (
          <div className="settings-fetcher-error">
            <AlertCircle size={13} /> {loadErr}
          </div>
        )}

        {!loading && configs.length === 0 && editing !== 'create' && (
          <div className="settings-coming-soon" style={{ marginTop: 8 }}>
            <AlertCircle size={18} style={{ flexShrink: 0 }} />
            <div className="settings-coming-soon-text">
              <strong>还没有飞书账号</strong>
              <p>点击右上角"新增账号"配置飞书企业自建应用的 AppID / AppSecret，机器人即可上线。</p>
            </div>
          </div>
        )}

        {editing === 'create' && (
          <FeishuConfigForm
            mode="create"
            agents={agents}
            existingIds={new Set(configs.map((c) => c.id))}
            onCancel={() => setEditing(null)}
            onSaved={async () => {
              setEditing(null)
              await refresh()
            }}
          />
        )}

        {configs.map((cfg) => (
          editing === cfg.id ? (
            <FeishuConfigForm
              key={cfg.id}
              mode="edit"
              initial={cfg}
              agents={agents}
              existingIds={new Set(configs.map((c) => c.id))}
              onCancel={() => setEditing(null)}
              onSaved={async () => {
                setEditing(null)
                await refresh()
              }}
            />
          ) : (
            <FeishuConfigCard
              key={cfg.id}
              config={cfg}
              agents={agents}
              onEdit={() => setEditing(cfg.id)}
              onChanged={refresh}
            />
          )
        ))}
      </section>
    </>
  )
}

/* ========================================================================
   单条飞书配置卡片
   ======================================================================== */

function FeishuConfigCard({ config, agents, onEdit, onChanged }: {
  config: ChannelConfigDTO
  agents: AgentItem[]
  onEdit: () => void
  onChanged: () => Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Tauri webview 不支持 window.confirm
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog()

  const agentName = agents.find((a) => a.id === config.config.default_agent)?.name
    || config.config.default_agent
    || '未设置'
  const appId = String(config.config.app_id || '')
  const maskedAppId = appId.length > 6
    ? `${appId.slice(0, 4)}***${appId.slice(-4)}`
    : appId
  const isIntl = String(config.config.domain || 'feishu') === 'lark'

  const callAndRefresh = useCallback(async (path: string, method = 'POST') => {
    setBusy(true)
    setErr(null)
    try {
      const res = await fetch(`${API_BASE}${path}`, { method })
      if (!res.ok && res.status !== 204) {
        const body = await res.text()
        throw new Error(`${res.status}: ${body}`)
      }
      await onChanged()
    } catch (e: any) {
      setErr(e.message || '操作失败')
    } finally {
      setBusy(false)
    }
  }, [onChanged])

  const toggleEnabled = async () => {
    if (busy) return
    if (config.enabled) {
      await callAndRefresh(`/v2/channels/configs/${encodeURIComponent(config.id)}/disable`)
    } else {
      await callAndRefresh(`/v2/channels/configs/${encodeURIComponent(config.id)}/enable`)
    }
  }

  const handleDelete = async () => {
    if (busy) return
    const ok = await askConfirm('删除飞书账号', `确定删除飞书账号 「${config.id}」 吗？`, 'danger', '删除')
    if (!ok) return
    await callAndRefresh(`/v2/channels/configs/${encodeURIComponent(config.id)}`, 'DELETE')
  }

  const statusClass = config.status === 'running'
    ? 'ok'
    : config.status === 'error' ? 'fail' : 'idle'

  return (
    <div className="settings-dingtalk-card">
      <div className="settings-dingtalk-card-head">
        <span className={`settings-dingtalk-status-dot ${statusClass}`} />
        <span className="settings-dingtalk-card-id">{config.id}</span>
        <span className="settings-dingtalk-status-text">{config.status}</span>
        <div className="settings-dingtalk-card-actions">
          <label className="settings-dingtalk-toggle">
            <input
              type="checkbox"
              checked={config.enabled}
              onChange={toggleEnabled}
              disabled={busy}
            />
            <span>{config.enabled ? '已启用' : '已停用'}</span>
          </label>
          <button
            type="button"
            className="settings-dingtalk-btn"
            onClick={onEdit}
            disabled={busy}
          >
            编辑
          </button>
          <button
            type="button"
            className="settings-dingtalk-btn settings-dingtalk-btn-danger"
            onClick={handleDelete}
            disabled={busy}
            title="删除"
          >
            <Trash2 size={12} />
          </button>
        </div>
      </div>
      <div className="settings-dingtalk-card-body">
        <div className="settings-dingtalk-row">
          <span className="settings-dingtalk-label">AppID</span>
          <span className="settings-dingtalk-value"><code>{maskedAppId || '—'}</code></span>
        </div>
        <div className="settings-dingtalk-row">
          <span className="settings-dingtalk-label">默认角色</span>
          <span className="settings-dingtalk-value">{agentName}</span>
        </div>
        <div className="settings-dingtalk-row">
          <span className="settings-dingtalk-label">版本</span>
          <span className="settings-dingtalk-value">{isIntl ? '国际版 Lark' : '中国版飞书'}</span>
        </div>
        {config.last_error && (
          <div className="settings-dingtalk-row settings-dingtalk-error">
            <AlertCircle size={12} />
            <span>{config.last_error}</span>
          </div>
        )}
        {err && (
          <div className="settings-dingtalk-row settings-dingtalk-error">
            <AlertCircle size={12} />
            <span>{err}</span>
          </div>
        )}
      </div>
      {confirmDialog}
    </div>
  )
}

/* ========================================================================
   飞书配置内联表单（创建 + 编辑共用）
   ======================================================================== */

interface FeishuFormState {
  id: string
  app_id: string
  app_secret: string  // "" 表示未改（编辑模式）；提交时若为空 → 送 MASK_VALUE
  default_agent: string
  domain: 'feishu' | 'lark'
  enabled: boolean
}

function FeishuConfigForm({
  mode, initial, agents, existingIds, onCancel, onSaved,
}: {
  mode: 'create' | 'edit'
  initial?: ChannelConfigDTO
  agents: AgentItem[]
  existingIds: Set<string>
  onCancel: () => void
  onSaved: () => Promise<void>
}) {
  const isCreate = mode === 'create'
  const hasExistingSecret = !isCreate && Boolean(initial?.config.app_secret)

  const [form, setForm] = useState<FeishuFormState>(() => ({
    id: initial?.id || '',
    app_id: String(initial?.config.app_id || ''),
    app_secret: '',  // 编辑模式留空——后端会保留原值
    default_agent: String(initial?.config.default_agent || ''),
    domain: (String(initial?.config.domain || 'feishu') === 'lark' ? 'lark' : 'feishu') as 'feishu' | 'lark',
    enabled: initial?.enabled ?? true,
  }))
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [showSecret, setShowSecret] = useState(false)

  const update = <K extends keyof FeishuFormState>(key: K, v: FeishuFormState[K]) => {
    setForm((s) => ({ ...s, [key]: v }))
  }

  // agents 异步加载完后自动选第一个（钉钉/Matrix 同款守卫）
  useEffect(() => {
    if (agents.length === 0) return
    if (!form.default_agent) {
      update('default_agent', agents[0].id)
    }
  }, [agents, form.default_agent])

  const handleSubmit = async () => {
    setErr(null)
    if (isCreate) {
      const id = form.id.trim()
      if (!id) return setErr('账号 ID 不能为空（用于本地唯一标识，建议 feishu-公司名）')
      if (existingIds.has(id)) return setErr(`账号 ID "${id}" 已存在`)
    }
    if (!form.app_id.trim()) return setErr('AppID 不能为空')
    if (isCreate && !form.app_secret.trim()) return setErr('AppSecret 不能为空')
    if (!form.default_agent) return setErr('请选择默认角色（角色列表加载中？）')
    if (agents.length > 0 && !agents.some((a) => a.id === form.default_agent)) {
      return setErr(`默认角色 "${form.default_agent}" 不在当前 agents 列表（已删除？请重选）`)
    }

    const configPayload: Record<string, any> = {
      app_id: form.app_id.trim(),
      app_secret: form.app_secret || (hasExistingSecret ? MASK_VALUE : ''),
      default_agent: form.default_agent,
      domain: form.domain,
    }

    setSaving(true)
    try {
      let res: Response
      if (isCreate) {
        res = await fetch(`${API_BASE}/v2/channels/configs`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            id: form.id.trim(),
            channel: 'feishu',
            config: configPayload,
            enabled: form.enabled,
          }),
        })
      } else {
        res = await fetch(`${API_BASE}/v2/channels/configs/${encodeURIComponent(initial!.id)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            config: configPayload,
            enabled: form.enabled,
          }),
        })
      }
      if (!res.ok) {
        const body = await res.text()
        throw new Error(`${res.status}: ${body}`)
      }
      await onSaved()
    } catch (e: any) {
      setErr(e.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="settings-dingtalk-form">
      <div className="settings-dingtalk-form-head">
        <span>{isCreate ? '新增飞书账号' : `编辑：${initial?.id}`}</span>
        <button type="button" className="settings-dingtalk-btn" onClick={onCancel} disabled={saving}>
          <X size={12} />
        </button>
      </div>

      <div className="settings-dingtalk-form-body">
        {isCreate && (
          <div className="settings-conn-row">
            <label className="settings-conn-label">账号 ID</label>
            <div className="settings-conn-input">
              <input
                type="text"
                value={form.id}
                placeholder="feishu-corp-a"
                onChange={(e) => update('id', e.target.value)}
              />
            </div>
          </div>
        )}

        <div className="settings-conn-row">
          <label className="settings-conn-label">AppID</label>
          <div className="settings-conn-input">
            <input
              type="text"
              value={form.app_id}
              placeholder="飞书应用 AppID（cli_xxx）"
              onChange={(e) => update('app_id', e.target.value)}
            />
          </div>
        </div>

        <div className="settings-conn-row">
          <label className="settings-conn-label">AppSecret</label>
          <div className="settings-conn-input">
            <input
              type={showSecret ? 'text' : 'password'}
              value={form.app_secret}
              placeholder={hasExistingSecret ? '已设置 · 留空保留原值' : '飞书应用 AppSecret'}
              onChange={(e) => update('app_secret', e.target.value)}
              autoComplete="new-password"
            />
            <button
              type="button"
              className="settings-conn-eye"
              onClick={() => setShowSecret((v) => !v)}
              title={showSecret ? '隐藏' : '显示'}
            >
              {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
        </div>

        <div className="settings-conn-row">
          <label className="settings-conn-label">默认角色</label>
          <select
            className="settings-dingtalk-select"
            value={form.default_agent}
            onChange={(e) => update('default_agent', e.target.value)}
            disabled={agents.length === 0}
          >
            {agents.length === 0 && <option value="">加载角色列表中…</option>}
            {form.default_agent && !agents.some((a) => a.id === form.default_agent) && (
              <option value={form.default_agent}>
                {form.default_agent}（已删除？）
              </option>
            )}
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.name}（{a.id}）</option>
            ))}
          </select>
        </div>

        <div className="settings-conn-row">
          <label className="settings-conn-label">版本</label>
          <div className="settings-conn-input">
            <label className="settings-dingtalk-toggle">
              <input
                type="checkbox"
                checked={form.domain === 'lark'}
                onChange={(e) => update('domain', e.target.checked ? 'lark' : 'feishu')}
              />
              <span>{form.domain === 'lark' ? '国际版 Lark' : '中国版飞书'}</span>
            </label>
          </div>
        </div>

        <div className="settings-conn-row">
          <label className="settings-conn-label">启用状态</label>
          <div className="settings-conn-input">
            <label className="settings-dingtalk-toggle">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => update('enabled', e.target.checked)}
              />
              <span>{form.enabled ? '保存后立即启动' : '保存后停止'}</span>
            </label>
          </div>
        </div>
      </div>

      {err && (
        <div className="settings-dingtalk-error" style={{ padding: '4px 12px 0' }}>
          <AlertCircle size={12} /> <span>{err}</span>
        </div>
      )}

      <div className="settings-dingtalk-form-foot">
        <button type="button" className="settings-dingtalk-btn" onClick={onCancel} disabled={saving}>
          取消
        </button>
        <button
          type="button"
          className="settings-dingtalk-btn settings-dingtalk-btn-primary"
          onClick={handleSubmit}
          disabled={saving}
        >
          {saving ? <Loader2 size={12} className="spin" /> : <CheckCircle2 size={12} />}
          {isCreate ? '创建并启动' : '保存修改'}
        </button>
      </div>
    </div>
  )
}

/* ========================================================================
   规划中频道（qq / wechat / telegram）
   ======================================================================== */

function PlannedChannelDetail({ channel }: { channel: ChannelDef }) {
  return (
    <div className="settings-coming-soon">
      <AlertCircle size={20} style={{ flexShrink: 0 }} />
      <div className="settings-coming-soon-text">
        <strong>{channel.name}：规划中</strong>
        <p>
          这个频道还没有后端实现。等 v2 channels 系统接入 {channel.name} 适配器后，会出现在这里供你配置。
        </p>
        <p className="settings-coming-soon-current">
          关注后续版本，或在 <code>backend/channels/</code> 下贡献一个 adapter——参考现有的
          <code> dingtalk.py</code> / <code>web.py</code> 实现即可。
        </p>
      </div>
    </div>
  )
}
