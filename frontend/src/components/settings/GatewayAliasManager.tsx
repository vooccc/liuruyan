/**
 * Gateway 别名映射 + Fallback 链管理（M3 子组件）。
 *
 * 设计文档：docs/GATEWAY.md
 *
 * 用户故事：
 *   - 在外部 CLI 或客户端里填 alias 名字（如 `claude-opus`），
 *     就能让请求自动按 candidates 顺序 fallback
 *   - 主中转挂了，自动切官方/备用，客户端无感
 *   - candidates 用上下移调整优先级
 *
 * 独立成子组件以隔离主面板加载失败风险（M3 与基础 M1 解耦）。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  Plus,
  Trash2,
  X,
  Activity,
} from 'lucide-react'
import { useConfirmDialog } from '../ConfirmDialog'
import {
  type GatewayAliasMap,
  type GatewayHealthState,
  type GatewayModelInfo,
  clearGatewayHealthBlacklist,
  createGatewayAlias,
  deleteGatewayAlias,
  getGatewayUnhealthyProviders,
  listGatewayAliases,
  updateGatewayAlias,
} from '../../api/gateway'

export interface GatewayAliasManagerProps {
  enabled: boolean
  models: GatewayModelInfo[]
}

export default function GatewayAliasManager({ enabled, models }: GatewayAliasManagerProps) {
  // 默认展开——用户就是为了看 / 改别名才点进这个 tab 的，再让他点一下展开
  // 是白白多一步无意义交互。
  const [open, setOpen] = useState(true)
  // Tauri webview 不支持 window.confirm，走自定义对话框
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog()
  const [aliases, setAliases] = useState<GatewayAliasMap>({})
  const [unhealthy, setUnhealthy] = useState<Record<string, GatewayHealthState>>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)  // alias name 或 '__new__' 或 null
  const [editName, setEditName] = useState('')
  const [editCandidates, setEditCandidates] = useState<string[]>([])
  const [editDesc, setEditDesc] = useState('')

  // 真实模型候选池（不含 alias 自身，防嵌套）。下拉 label 走友好名，
  // value 仍是后端认的 `provider_id:model_id`——这样用户看到的是「硅基流动·deepseek-chat」
  // 这种人话，存进去还是「custom-wsqoho4fru:deepseek-chat」。
  const candidatePool = useMemo(
    () => models.filter((m) => m.owned_by !== 'alias'),
    [models],
  )

  const refresh = useCallback(async () => {
    if (!enabled) return
    setLoading(true)
    setError(null)
    try {
      const [list, snap] = await Promise.all([
        listGatewayAliases(),
        getGatewayUnhealthyProviders(),
      ])
      setAliases(list)
      setUnhealthy(snap)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [enabled])

  useEffect(() => {
    if (open) void refresh()
  }, [open, refresh])

  const startCreate = () => {
    setEditing('__new__')
    setEditName('')
    setEditCandidates([])
    setEditDesc('')
  }

  const startEdit = (name: string) => {
    const a = aliases[name]
    if (!a) return
    setEditing(name)
    setEditName(name)
    setEditCandidates([...a.candidates])
    setEditDesc(a.description || '')
  }

  const cancelEdit = () => {
    setEditing(null)
    setEditName('')
    setEditCandidates([])
    setEditDesc('')
  }

  const saveEdit = async () => {
    setError(null)
    try {
      if (editing === '__new__') {
        await createGatewayAlias(editName, editCandidates, editDesc)
      } else if (editing) {
        await updateGatewayAlias(editing, editCandidates, editDesc)
      }
      cancelEdit()
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const onDelete = async (name: string) => {
    const ok = await askConfirm(
      '删除别名',
      `删除别名 ${name}？\n\n所有用此名字的外部客户端会立即报 404。`,
      'danger',
      '删除',
    )
    if (!ok) return
    setError(null)
    try {
      await deleteGatewayAlias(name)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const onClearHealth = async () => {
    setError(null)
    try {
      await clearGatewayHealthBlacklist()
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const addCandidate = (id: string) => {
    if (!id || editCandidates.includes(id)) return
    setEditCandidates((cs) => [...cs, id])
  }

  const removeCandidate = (idx: number) => {
    setEditCandidates((cs) => cs.filter((_, i) => i !== idx))
  }

  const moveCandidate = (idx: number, delta: -1 | 1) => {
    setEditCandidates((cs) => {
      const ni = idx + delta
      if (ni < 0 || ni >= cs.length) return cs
      const out = [...cs]
      ;[out[idx], out[ni]] = [out[ni], out[idx]]
      return out
    })
  }

  if (!enabled) return null

  const aliasNames = Object.keys(aliases).sort()
  const unhealthyCount = Object.keys(unhealthy).length

  return (
    <section style={cardStyle}>
      <div style={sectionHeadStyle}>
        <button type="button" style={collapseHeadStyle} onClick={() => setOpen((v) => !v)}>
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <span style={{ marginLeft: 4 }}>
            模型别名
            <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--text-tertiary, #888)', marginLeft: 6 }}>
              {aliasNames.length} 个别名
              {unhealthyCount > 0 && (
                <span style={{ color: '#e5484d', marginLeft: 6 }}>
                  · {unhealthyCount} provider 当前不健康
                </span>
              )}
            </span>
          </span>
        </button>
        {open && (
          <div style={{ display: 'flex', gap: 6 }}>
            <button type="button" style={btnGhostStyle} onClick={() => void refresh()} disabled={loading}>
              {loading ? <Loader2 size={13} className="spin" /> : <Activity size={13} />}
              <span style={{ marginLeft: 4 }}>刷新</span>
            </button>
            <button type="button" style={btnPrimaryStyle} onClick={startCreate}>
              <Plus size={13} />
              <span style={{ marginLeft: 4 }}>新建别名</span>
            </button>
          </div>
        )}
      </div>

      {open && (
        <>
          {error && (
            <div style={errBoxStyle}>
              <AlertCircle size={13} />
              <span style={{ flex: 1 }}>{error}</span>
              <button type="button" style={btnGhostStyle} onClick={() => setError(null)}>×</button>
            </div>
          )}

          <p style={hintStyle}>
            外部客户端的 <code style={codeStyle}>model</code> 字段填别名（如 <code style={codeStyle}>claude-opus</code>），
            柳如烟按候选顺序自动尝试；遇 401/429/5xx/超时/网络错自动切下一个，全程客户端无感。
            单候选 = 固定走那个 provider，无 fallback。
          </p>

          {/* alias 列表 */}
          {aliasNames.length === 0 && editing !== '__new__' && (
            <div style={emptyStyle}>
              还没有别名。点上方"新建别名"开始；或把 <code style={codeStyle}>claude-opus</code> 这种逻辑名字配上多个候选，让中转挂了也无感切换。
            </div>
          )}

          {aliasNames.map((name) => {
            if (editing === name) {
              return (
                <AliasEditor
                  key={name}
                  isNew={false}
                  name={editName}
                  setName={setEditName}
                  candidates={editCandidates}
                  description={editDesc}
                  setDescription={setEditDesc}
                  candidatePool={candidatePool}
                  unhealthy={unhealthy}
                  onAddCandidate={addCandidate}
                  onRemoveCandidate={removeCandidate}
                  onMoveCandidate={moveCandidate}
                  onCancel={cancelEdit}
                  onSave={() => void saveEdit()}
                />
              )
            }
            const entry = aliases[name]
            return (
              <div key={name} style={aliasRowStyle}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                    <code style={aliasNameStyle}>{name}</code>
                    {entry.candidates.length === 1 ? (
                      <span style={pillSmStyle}>单候选 · 固定路由</span>
                    ) : (
                      <span style={pillSmStyle}>{entry.candidates.length} 候选 · fallback 链</span>
                    )}
                  </div>
                  {entry.description && (
                    <div style={{ fontSize: 11.5, color: 'var(--text-secondary)', marginBottom: 4 }}>
                      {entry.description}
                    </div>
                  )}
                  <ol style={{ margin: 0, paddingLeft: 18, fontSize: 11.5, color: 'var(--text-secondary)' }}>
                    {entry.candidates.map((c) => {
                      const pid = c.split(':')[0]
                      const isUnhealthy = unhealthy[pid]
                      return (
                        <li key={c} style={{ lineHeight: 1.8 }}>
                          <code style={candCodeStyle}>{c}</code>
                          {isUnhealthy && (
                            <span style={{ color: '#e5484d', marginLeft: 6, fontSize: 10.5 }}>
                              · 黑屋中（{isUnhealthy.failure_kind}，剩 {isUnhealthy.remaining_secs}s）
                            </span>
                          )}
                        </li>
                      )
                    })}
                  </ol>
                </div>
                <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
                  <button type="button" style={btnGhostStyle} onClick={() => startEdit(name)}>编辑</button>
                  <button type="button" style={btnDangerStyle} onClick={() => void onDelete(name)}>
                    <Trash2 size={12} />
                  </button>
                </div>
              </div>
            )
          })}

          {editing === '__new__' && (
            <AliasEditor
              isNew
              name={editName}
              setName={setEditName}
              candidates={editCandidates}
              description={editDesc}
              setDescription={setEditDesc}
              candidatePool={candidatePool}
              unhealthy={unhealthy}
              onAddCandidate={addCandidate}
              onRemoveCandidate={removeCandidate}
              onMoveCandidate={moveCandidate}
              onCancel={cancelEdit}
              onSave={() => void saveEdit()}
            />
          )}

          {/* 健康状态调试 */}
          {unhealthyCount > 0 && (
            <div style={{ ...cardStyle, marginTop: 12, background: 'rgba(229,72,77,0.06)' }}>
              <div style={sectionHeadStyle}>
                <span>
                  <AlertCircle size={12} style={{ verticalAlign: -2, marginRight: 4, color: '#e5484d' }} />
                  当前不健康的 provider（30~300s 黑屋内）
                </span>
                <button type="button" style={btnGhostStyle} onClick={() => void onClearHealth()}>
                  强制重置
                </button>
              </div>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 11.5, color: 'var(--text-secondary)', lineHeight: 1.8 }}>
                {Object.entries(unhealthy).map(([pid, st]) => (
                  <li key={pid}>
                    <code style={candCodeStyle}>{pid}</code> · {st.failure_kind} · 剩 {st.remaining_secs}s
                    {st.error_message && <span style={{ marginLeft: 6, opacity: 0.7 }}>({st.error_message})</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
      {confirmDialog}
    </section>
  )
}

/* ─────────── alias 编辑 inline form ─────────── */

interface EditorProps {
  isNew: boolean
  name: string
  setName: (v: string) => void
  candidates: string[]
  description: string
  setDescription: (v: string) => void
  candidatePool: GatewayModelInfo[]
  unhealthy: Record<string, GatewayHealthState>
  onAddCandidate: (id: string) => void
  onRemoveCandidate: (idx: number) => void
  onMoveCandidate: (idx: number, delta: -1 | 1) => void
  onCancel: () => void
  onSave: () => void
}

/** "provider_id:model_id" → "model_id"。UI 显示用，provider 在另一列走「友好名」 */
function stripProvider(id: string): string {
  const idx = id.indexOf(':')
  return idx > 0 ? id.slice(idx + 1) : id
}

function AliasEditor(props: EditorProps) {
  const {
    isNew, name, setName, candidates, description, setDescription,
    candidatePool, unhealthy,
    onAddCandidate, onRemoveCandidate, onMoveCandidate, onCancel, onSave,
  } = props

  const [pickValue, setPickValue] = useState('')
  const canSave = name.trim().length > 0 && candidates.length > 0
  const available = candidatePool.filter((m) => !candidates.includes(m.id))

  // 已选候选 id → 友好名查找表，渲染列表时用
  const idToFriendly = useMemo(() => {
    const m = new Map<string, { provider_name: string; model_name: string }>()
    for (const e of candidatePool) {
      m.set(e.id, { provider_name: e.provider_name || e.provider || '', model_name: stripProvider(e.id) })
    }
    return m
  }, [candidatePool])

  return (
    <div style={{ ...cardStyle, background: 'rgba(0,0,0,0.04)', marginBottom: 8 }}>
      <div style={sectionHeadStyle}>
        <span>{isNew ? '新建别名' : `编辑：${name}`}</span>
        <div style={{ display: 'flex', gap: 6 }}>
          <button type="button" style={btnGhostStyle} onClick={onCancel}>取消</button>
          <button type="button" style={btnPrimaryStyle} onClick={onSave} disabled={!canSave}>
            <CheckCircle2 size={13} />
            <span style={{ marginLeft: 4 }}>保存</span>
          </button>
        </div>
      </div>

      {/* 名字 */}
      <div style={{ marginBottom: 8 }}>
        <label style={labelStyle}>名字（外部客户端 model 字段就填这个）</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          disabled={!isNew}
          placeholder="claude-opus / fast / cheap..."
          style={inputStyle}
        />
        <div style={subHintStyle}>
          只能用 字母/数字/<code>.</code> <code>_</code> <code>-</code>，1~64 字符，不能含 <code>:</code> <code>/</code>
        </div>
      </div>

      {/* 候选顺序 */}
      <div style={{ marginBottom: 8 }}>
        <label style={labelStyle}>候选列表（按优先级，从上到下）</label>
        {candidates.length === 0 && (
          <div style={subHintStyle}>还没有候选。从下面下拉选一个加进来。</div>
        )}
        {candidates.map((c, idx) => {
          const pid = c.split(':')[0]
          const bad = unhealthy[pid]
          const friendly = idToFriendly.get(c)
          return (
            <div key={c} style={candidateRowStyle}>
              <span style={{ fontSize: 11, color: 'var(--text-tertiary, #888)', width: 18 }}>{idx + 1}.</span>
              <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
                {friendly ? (
                  <span style={{ fontSize: 11.5, color: 'var(--text-color)' }}>
                    <span style={{ color: 'var(--text-tertiary, #888)' }}>{friendly.provider_name}</span>
                    <span style={{ margin: '0 4px', color: 'var(--text-tertiary, #aaa)' }}>·</span>
                    {friendly.model_name}
                  </span>
                ) : (
                  <code style={candCodeStyle}>{c}</code>
                )}
                <code style={{ ...candCodeStyle, fontSize: 10, opacity: 0.7 }} title={c}>{c}</code>
              </div>
              {bad && (
                <span style={{ fontSize: 10, color: '#e5484d' }}>
                  黑屋({bad.failure_kind})
                </span>
              )}
              <button type="button" style={btnIconStyle} onClick={() => onMoveCandidate(idx, -1)} disabled={idx === 0} title="上移">
                <ArrowUp size={12} />
              </button>
              <button type="button" style={btnIconStyle} onClick={() => onMoveCandidate(idx, 1)} disabled={idx === candidates.length - 1} title="下移">
                <ArrowDown size={12} />
              </button>
              <button type="button" style={btnIconStyle} onClick={() => onRemoveCandidate(idx)} title="移除">
                <X size={12} />
              </button>
            </div>
          )
        })}
        <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
          <select
            value={pickValue}
            onChange={(e) => setPickValue(e.target.value)}
            style={{ ...inputStyle, flex: 1 }}
          >
            <option value="">— 选一个模型加进候选 —</option>
            {available.map((m) => {
              const provider = m.provider_name || m.provider || ''
              const model = stripProvider(m.id)
              return (
                <option key={m.id} value={m.id} title={m.id}>
                  {provider ? `${provider} · ${model}` : model}
                </option>
              )
            })}
          </select>
          <button
            type="button"
            style={btnGhostStyle}
            onClick={() => {
              if (pickValue) {
                onAddCandidate(pickValue)
                setPickValue('')
              }
            }}
            disabled={!pickValue}
          >
            <Plus size={13} /> 加入
          </button>
        </div>
      </div>

      {/* 描述 */}
      <div>
        <label style={labelStyle}>描述（可选）</label>
        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="例如：主 8yes.cc 备 anthropic-direct"
          style={inputStyle}
        />
      </div>
    </div>
  )
}

/* ─────────── inline styles ─────────── */

const cardStyle: React.CSSProperties = {
  background: 'var(--card-bg, rgba(0,0,0,0.02))',
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  borderRadius: 12,
  padding: '14px 18px',
  boxShadow: '0 1px 2px rgba(0,0,0,0.04), 0 8px 24px -12px rgba(0,0,0,0.10)',
}

const sectionHeadStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  fontSize: 12.5,
  fontWeight: 600,
  color: 'var(--text-color)',
  marginBottom: 8,
  gap: 8,
  flexWrap: 'wrap',
  // 锁行高，避免按钮显隐时标题上下跳
  minHeight: 28,
}

const collapseHeadStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  background: 'transparent',
  border: 'none',
  cursor: 'pointer',
  padding: 0,
  font: 'inherit',
  color: 'inherit',
}

const btnBaseStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  padding: '4px 10px',
  border: '1px solid var(--border-color, rgba(0,0,0,0.12))',
  borderRadius: 8,
  background: 'var(--card-bg, transparent)',
  fontSize: 11.5,
  cursor: 'pointer',
  color: 'var(--text-color)',
}

const btnGhostStyle: React.CSSProperties = { ...btnBaseStyle }
const btnPrimaryStyle: React.CSSProperties = {
  ...btnBaseStyle,
  background: 'var(--primary-color, #5e6ad2)',
  color: '#fff',
  borderColor: 'transparent',
}
const btnDangerStyle: React.CSSProperties = {
  ...btnBaseStyle,
  borderColor: '#e5484d',
  color: '#e5484d',
}
const btnIconStyle: React.CSSProperties = {
  ...btnBaseStyle,
  padding: '2px 6px',
}

const aliasRowStyle: React.CSSProperties = {
  display: 'flex',
  gap: 8,
  padding: '12px 14px',
  border: '1px solid var(--border-color, rgba(0,0,0,0.06))',
  borderRadius: 9,
  marginBottom: 8,
  background: 'var(--bg-secondary, rgba(0,0,0,0.015))',
  alignItems: 'flex-start',
}

const aliasNameStyle: React.CSSProperties = {
  fontFamily: 'var(--font-mono, ui-monospace, SF Mono, Menlo, Consolas, monospace)',
  fontSize: 13,
  fontWeight: 600,
  background: 'var(--accent-bg, rgba(196,167,125,0.12))',
  color: 'var(--primary-color)',
  padding: '2px 7px',
  borderRadius: 5,
}

const candCodeStyle: React.CSSProperties = {
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 11.5,
  background: 'rgba(0,0,0,0.05)',
  padding: '1px 5px',
  borderRadius: 3,
}

const codeStyle: React.CSSProperties = {
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 11,
  background: 'rgba(0,0,0,0.05)',
  padding: '1px 5px',
  borderRadius: 3,
}

const pillSmStyle: React.CSSProperties = {
  fontSize: 10.5,
  padding: '1px 6px',
  borderRadius: 10,
  background: 'rgba(0,0,0,0.04)',
  color: 'var(--text-secondary)',
}

const candidateRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 4,
  padding: '4px 0',
}

const inputStyle: React.CSSProperties = {
  display: 'block',
  width: '100%',
  padding: '5px 8px',
  borderRadius: 5,
  border: '1px solid var(--border-color, rgba(0,0,0,0.12))',
  background: 'var(--card-bg, transparent)',
  color: 'var(--text-color)',
  fontSize: 12,
  boxSizing: 'border-box',
}

const labelStyle: React.CSSProperties = {
  display: 'block',
  fontSize: 11.5,
  color: 'var(--text-secondary)',
  marginBottom: 4,
}

const subHintStyle: React.CSSProperties = {
  fontSize: 11,
  color: 'var(--text-tertiary, #888)',
  marginTop: 4,
}

const hintStyle: React.CSSProperties = {
  margin: '0 0 8px',
  fontSize: 11.5,
  color: 'var(--text-secondary)',
  lineHeight: 1.6,
}

const emptyStyle: React.CSSProperties = {
  padding: '12px',
  fontSize: 12,
  color: 'var(--text-secondary)',
  background: 'rgba(0,0,0,0.02)',
  borderRadius: 6,
  lineHeight: 1.6,
}

const errBoxStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  padding: 8,
  fontSize: 11.5,
  color: '#e5484d',
  background: 'rgba(229,72,77,0.08)',
  borderRadius: 6,
  marginBottom: 8,
}
