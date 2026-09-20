/**
 * 设置 → 外部 Agent (ACP) 面板。
 *
 * 设计：纯状态展示。
 * - 不在这里收 API key 表单（避免跟「模型服务」面板两套 UI 冲突）
 * - 每个 agent 卡显示：是否可用 / 从哪借的 key / 缺什么 + 一键跳转「模型服务」
 *
 * 用户路径：
 * 1. 看到 claude-code "未配置" → 点"去配 Anthropic provider" 按钮
 * 2. 跳到模型服务页配 Anthropic 的 API key
 * 3. 回这边刷新看到 "✓ 已就绪（来自 anthropic provider）"
 * 4. 在对话里说"用 claude-code"就能跑了
 */
import { useEffect, useState } from 'react'
import {
  AlertCircle,
  Bot,
  CheckCircle2,
  ExternalLink,
  Loader2,
  RefreshCw,
  Settings,
} from 'lucide-react'
import {
  AcpAgentStatus,
  AcpAgentsListResponse,
  ResolvedVia,
  getAcpAgentsStatus,
  setAcpAgentProvider,
} from '../../api/acpAgents'

interface AcpSettingsProps {
  onJumpToProviders?: () => void
}

export default function AcpSettings({ onJumpToProviders }: AcpSettingsProps) {
  const [data, setData] = useState<AcpAgentsListResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  const refresh = async () => {
    setLoading(true)
    setErr(null)
    try {
      const payload = await getAcpAgentsStatus()
      setData(payload)
    } catch (exc) {
      setErr(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  if (loading && !data) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: 24, color: 'var(--text-secondary)' }}>
        <Loader2 size={16} className="spin" />
        <span>加载外部 agent 列表…</span>
      </div>
    )
  }

  if (err && !data) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: 24, color: '#e5484d' }}>
        <AlertCircle size={16} />
        <span>{err}</span>
        <button type="button" style={btnGhostStyle} onClick={() => void refresh()}>重试</button>
      </div>
    )
  }

  const agents = data?.agents ?? []
  const availableCount = agents.filter((a) => a.available).length

  return (
    <div style={{ padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* Hero */}
      <header style={heroStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Bot size={20} style={{ color: 'var(--primary-color)' }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>ACP <span style={{ fontSize: 11, color: 'var(--text-tertiary, #888)', fontWeight: 400, marginLeft: 6 }}>Agent Client Protocol</span></h2>
            <p style={{ margin: '4px 0 0', fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.5 }}>
              通过 Agent Client Protocol 召唤外部 agent（如 Claude Code、Gemini CLI）作为协作角色。
              在「模型服务」配过对应 provider 的 API key 后自动可用。
            </p>
          </div>
          <button type="button" style={btnGhostStyle} onClick={() => void refresh()} title="刷新状态">
            <RefreshCw size={13} />
          </button>
        </div>
        <div style={{ display: 'flex', gap: 6, marginTop: 10, fontSize: 11.5, color: 'var(--text-secondary)' }}>
          <span style={pillStyle}>
            <CheckCircle2 size={12} /> {availableCount} / {agents.length} 可用
          </span>
        </div>
      </header>

      {/* Agent 卡片列表 */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {agents.map((a) => (
          <AgentCard
            key={a.agent}
            agent={a}
            onJumpToProviders={onJumpToProviders}
            onUpdated={(next) => {
              // 单条 agent 状态更新（用户改 provider 下拉），就地替换避免整页刷
              setData((prev) => prev ? {
                ...prev,
                agents: prev.agents.map(x => x.agent === next.agent ? next : x),
              } : prev)
            }}
          />
        ))}
      </div>

      {/* 使用说明 */}
      <section style={tipStyle}>
        <strong style={{ fontSize: 12, color: 'var(--text-color)' }}>怎么用</strong>
        <ul style={{ margin: '6px 0 0', paddingLeft: 18, fontSize: 11.5, lineHeight: 1.7, color: 'var(--text-secondary)' }}>
          <li>角色工具白名单要勾选 <code style={codeStyle}>delegate_acp_agent</code>（在「工具」面板）</li>
          <li>对话里说"用 claude-code 帮我看 xxx"，柳如烟会自动调用</li>
          <li>外部 agent 会用<strong>柳如烟当前工作目录</strong>跑（除非显式指定 cwd 参数）</li>
          <li>每次调用 spawn 一个子进程，<strong>典型耗时 30-120 秒</strong></li>
        </ul>
      </section>
    </div>
  )
}

function AgentCard({
  agent,
  onJumpToProviders,
  onUpdated,
}: {
  agent: AcpAgentStatus
  onJumpToProviders?: () => void
  onUpdated: (next: AcpAgentStatus) => void
}) {
  const { available, resolved_via, missing_env, provider_id, effective_provider_id, compatible_providers } = agent
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)

  // 下拉的当前值：用户没覆盖时显示默认 provider_id（让用户看清楚现在用的是谁）
  const selectValue = effective_provider_id ?? provider_id ?? ''
  // 是否处于"用户已手动选过 provider"状态——非默认值即视为覆盖
  const isOverridden = !!effective_provider_id && effective_provider_id !== provider_id

  async function handleChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const next = e.target.value || null
    setSaving(true)
    setSaveErr(null)
    try {
      // null = 清除覆盖回到默认；非空 = 设为该 provider
      const result = await setAcpAgentProvider(agent.agent, next === provider_id ? null : next)
      onUpdated(result)
    } catch (exc) {
      setSaveErr(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div style={available ? cardOkStyle : cardWarnStyle}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
        <div
          style={{
            width: 32,
            height: 32,
            borderRadius: 8,
            background: available ? 'rgba(46, 160, 67, 0.12)' : 'rgba(229, 72, 77, 0.10)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          {available ? <CheckCircle2 size={16} style={{ color: '#2ea043' }} /> : <AlertCircle size={16} style={{ color: '#e5484d' }} />}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 2 }}>
            <code style={agentNameStyle}>{agent.agent}</code>
            <StatusBadge available={available} via={resolved_via} />
            {isOverridden && (
              <span style={{ ...badgeStyle, background: 'rgba(110, 86, 207, 0.14)', color: '#a48dff' }}>用户自定义</span>
            )}
          </div>
          <p style={{ margin: '2px 0 6px', fontSize: 11.5, color: 'var(--text-secondary)', lineHeight: 1.5 }}>
            {agent.description || '(无描述)'}
          </p>
          <div style={{ fontSize: 11, color: 'var(--text-tertiary, #888)', display: 'flex', flexWrap: 'wrap', gap: 10 }}>
            <span>命令：<code style={codeStyle}>{agent.command}</code></span>
            <span>需要 env：<code style={codeStyle}>{agent.env_required.join(', ') || '(无)'}</code></span>
          </div>

          {/* Provider 选择下拉：哪怕 agent 已可用也允许切换 */}
          {compatible_providers.length > 0 && (
            <div style={providerPickerStyle}>
              <label style={{ fontSize: 11.5, color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: 6 }}>
                <span>借 key 自：</span>
                <select
                  value={selectValue}
                  onChange={handleChange}
                  disabled={saving}
                  style={selectStyle}
                  title={isOverridden ? `用户选了 ${effective_provider_id}；选回 ${provider_id} 即清除覆盖` : `默认用 ${provider_id}`}
                >
                  {compatible_providers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name || p.id}
                      {p.is_default ? '（默认）' : ''}
                      {p.has_key ? '' : '（未配 key）'}
                    </option>
                  ))}
                </select>
                {saving && <Loader2 size={11} className="spin" />}
              </label>
              {saveErr && (
                <div style={{ fontSize: 11, color: '#e5484d', marginTop: 4 }}>{saveErr}</div>
              )}
            </div>
          )}

          {/* 不可用时给引导 */}
          {!available && (
            <div style={warnBoxStyle}>
              <div style={{ fontSize: 11.5, marginBottom: 6 }}>
                <strong>缺：</strong>{missing_env.join(', ')}
              </div>
              <div style={{ fontSize: 11.5, lineHeight: 1.6, color: 'var(--text-secondary)', marginBottom: 8 }}>
                {provider_id ? (
                  <>
                    去「<strong>模型服务</strong>」配置：
                    <ul style={{ margin: '4px 0 4px 16px', paddingLeft: 0 }}>
                      <li>
                        <strong>官方 key</strong>：填进内置 <code style={codeStyle}>{provider_id}</code> provider 的 API Key 即可
                      </li>
                      <li>
                        <strong>中转 key</strong>：内置 provider 的 base_url 锁死了改不了 →
                        新建一个<strong>自定义 provider</strong>（协议选 <code style={codeStyle}>Anthropic Messages</code>），
                        填中转地址和中转 key，柳如烟会自动识别
                      </li>
                    </ul>
                  </>
                ) : (
                  <>
                    请在系统环境变量里设置 <code style={codeStyle}>{missing_env[0]}</code>，
                    或在启动柳如烟前 <code style={codeStyle}>set {missing_env[0]}=...</code>
                  </>
                )}
              </div>
              {provider_id && onJumpToProviders && (
                <button type="button" style={btnPrimaryStyle} onClick={onJumpToProviders}>
                  <Settings size={12} /> 去配置 {provider_id} provider <ExternalLink size={11} />
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function StatusBadge({ available, via }: { available: boolean; via: ResolvedVia }) {
  if (!available) {
    return <span style={{ ...badgeStyle, background: 'rgba(229, 72, 77, 0.12)', color: '#e5484d' }}>未就绪</span>
  }
  const label = via === 'env' ? '环境变量' : via === 'provider' ? 'provider 借 key' : '已就绪'
  return <span style={{ ...badgeStyle, background: 'rgba(46, 160, 67, 0.12)', color: '#2ea043' }}>✓ {label}</span>
}

// ========== 样式 ==========
const heroStyle: React.CSSProperties = {
  padding: 16,
  borderRadius: 8,
  background: 'var(--bg-secondary, rgba(0,0,0,0.02))',
  border: '1px solid var(--border-color)',
}

const cardOkStyle: React.CSSProperties = {
  padding: 14,
  borderRadius: 8,
  border: '1px solid var(--border-color)',
  background: 'var(--bg-color)',
}

const cardWarnStyle: React.CSSProperties = {
  padding: 14,
  borderRadius: 8,
  border: '1px solid rgba(229, 72, 77, 0.30)',
  background: 'rgba(229, 72, 77, 0.04)',
}

const tipStyle: React.CSSProperties = {
  padding: 12,
  borderRadius: 6,
  border: '1px dashed var(--border-color)',
  background: 'transparent',
}

const codeStyle: React.CSSProperties = {
  fontFamily: 'var(--mono-font, ui-monospace, monospace)',
  fontSize: 11,
  padding: '1px 5px',
  borderRadius: 3,
  background: 'var(--bg-secondary, rgba(0,0,0,0.04))',
}

const agentNameStyle: React.CSSProperties = {
  fontFamily: 'var(--mono-font, ui-monospace, monospace)',
  fontSize: 13,
  fontWeight: 600,
  color: 'var(--text-color)',
}

const badgeStyle: React.CSSProperties = {
  fontSize: 10.5,
  padding: '2px 6px',
  borderRadius: 4,
  fontWeight: 500,
  whiteSpace: 'nowrap',
}

const pillStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
  padding: '3px 8px',
  borderRadius: 12,
  background: 'var(--bg-secondary, rgba(0,0,0,0.04))',
  border: '1px solid var(--border-color)',
}

const warnBoxStyle: React.CSSProperties = {
  marginTop: 10,
  padding: 10,
  borderRadius: 6,
  border: '1px solid rgba(229, 72, 77, 0.20)',
  background: 'rgba(229, 72, 77, 0.04)',
}

const btnGhostStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
  padding: '4px 10px',
  fontSize: 11.5,
  border: '1px solid var(--border-color)',
  background: 'var(--bg-color)',
  color: 'var(--text-color)',
  borderRadius: 4,
  cursor: 'pointer',
}

const providerPickerStyle: React.CSSProperties = {
  marginTop: 8,
  padding: '6px 8px',
  borderRadius: 6,
  background: 'var(--bg-secondary, rgba(0,0,0,0.03))',
  border: '1px solid var(--border-color)',
}

const selectStyle: React.CSSProperties = {
  fontSize: 11.5,
  padding: '3px 6px',
  borderRadius: 4,
  border: '1px solid var(--border-color)',
  background: 'var(--bg-color)',
  color: 'var(--text-color)',
  fontFamily: 'var(--mono-font, ui-monospace, monospace)',
  minWidth: 160,
}

const btnPrimaryStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 5,
  padding: '5px 12px',
  fontSize: 11.5,
  border: '1px solid var(--primary-color)',
  background: 'var(--primary-color)',
  color: '#fff',
  borderRadius: 4,
  cursor: 'pointer',
  fontWeight: 500,
}
