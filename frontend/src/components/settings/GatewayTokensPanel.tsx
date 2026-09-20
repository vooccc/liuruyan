/**
 * GatewayTokensPanel —— 令牌管理（合并自原"概览 + Tokens"）。
 *
 * 信息架构（自上而下）：
 *   1. 接入地址（OpenAI / Anthropic base URL × 一键复制）
 *   2. 令牌列表 + 新建按钮
 *      - 行内：名字 / 状态 · 打码密钥 [👁 显示] [📋 复制] · 配额进度 · 限定模型 · 操作
 *      - 编辑行展开：限额三档 + allow/deny
 *   3. 折叠：「外部 model 字段怎么写」—— 按需拉 /admin/models + 已建别名，双区块展示
 *
 * 设计纪律（依用户反馈定稿）：
 *   - 文案精炼、靠视觉自解释，不写中长说明
 *   - 明文随时显示/复制，不搞"一次性 modal"——本地软件场景，明文反复用才合理
 *   - 「默认令牌」首次启动自动 bootstrap，用户进来就有，0 步骤上手
 *   - 模型清单不预拉，展开时按需 fetch，不缓存（provider 改了之后看到的就是最新）
 *   - 「重置密钥」语义清晰，不再用「旋转」这种黑话
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Copy,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react'
import { useConfirmDialog } from '../ConfirmDialog'
import {
  createGatewayToken,
  deleteGatewayTokenEntry,
  gatewayAnthropicBaseUrl,
  gatewayOpenAIBaseUrl,
  listAdminModels,
  listGatewayTokens,
  rotateGatewayTokenEntry,
  updateGatewayTokenEntry,
  type GatewayAliasMap,
  type GatewayModelInfo,
  type GatewayTokenEntry,
  type GatewayTokenQuota,
} from '../../api/gateway'

interface Props {
  enabled: boolean
  /** 父级（GatewaySettings）已经拉的别名表；面板里的"模型查询"折叠区会用。 */
  aliases: GatewayAliasMap
}

type EditableQuota = {
  calls_per_minute: string
  calls_per_day: string
  total_tokens_per_day: string
}

const EMPTY_QUOTA: EditableQuota = {
  calls_per_minute: '',
  calls_per_day: '',
  total_tokens_per_day: '',
}

function quotaToInput(q: GatewayTokenQuota): EditableQuota {
  return {
    calls_per_minute: q.calls_per_minute == null ? '' : String(q.calls_per_minute),
    calls_per_day: q.calls_per_day == null ? '' : String(q.calls_per_day),
    total_tokens_per_day: q.total_tokens_per_day == null ? '' : String(q.total_tokens_per_day),
  }
}

function inputToQuota(q: EditableQuota): Partial<GatewayTokenQuota> {
  const norm = (s: string): number | null => {
    const t = s.trim()
    if (!t) return null
    const n = parseInt(t, 10)
    return Number.isFinite(n) && n > 0 ? n : null
  }
  return {
    calls_per_minute: norm(q.calls_per_minute),
    calls_per_day: norm(q.calls_per_day),
    total_tokens_per_day: norm(q.total_tokens_per_day),
  }
}

export default function GatewayTokensPanel({ enabled, aliases }: Props) {
  const [tokens, setTokens] = useState<GatewayTokenEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Tauri webview 不支持 window.confirm，走自定义对话框
  const { confirm: askConfirm, dialog: confirmDialog } = useConfirmDialog()
  const [busy, setBusy] = useState<string | null>(null)

  const [creating, setCreating] = useState(false)
  const [draftName, setDraftName] = useState('')
  const [draftQuota, setDraftQuota] = useState<EditableQuota>(EMPTY_QUOTA)
  const [draftAllow, setDraftAllow] = useState('')
  const [draftDeny, setDraftDeny] = useState('')
  const [draftAllowIps, setDraftAllowIps] = useState('')

  const [revealed, setRevealed] = useState<Record<string, boolean>>({})
  const [copiedKey, setCopiedKey] = useState<string | null>(null)

  const [editingId, setEditingId] = useState<string | null>(null)
  const [editQuota, setEditQuota] = useState<EditableQuota>(EMPTY_QUOTA)
  const [editAllow, setEditAllow] = useState('')
  const [editDeny, setEditDeny] = useState('')
  const [editAllowIps, setEditAllowIps] = useState('')

  const openaiBase = gatewayOpenAIBaseUrl()
  const anthropicBase = gatewayAnthropicBaseUrl()

  const refresh = useCallback(async () => {
    setError(null)
    try {
      setTokens(await listGatewayTokens())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return
    }
    void refresh()
  }, [enabled, refresh])

  const copy = (key: string, txt: string) => {
    void navigator.clipboard.writeText(txt)
    setCopiedKey(key)
    setTimeout(() => setCopiedKey((c) => (c === key ? null : c)), 1500)
  }

  const toggleReveal = (id: string) => {
    setRevealed((m) => ({ ...m, [id]: !m[id] }))
  }

  const handleCreate = async () => {
    const name = draftName.trim()
    if (!name) {
      setError('请填写令牌名字')
      return
    }
    setBusy('create')
    try {
      const created = await createGatewayToken({
        name,
        quota: inputToQuota(draftQuota),
        allow_models: parseList(draftAllow),
        deny_models: parseList(draftDeny),
        allow_ips: parseList(draftAllowIps),
      })
      // 新建后默认显示明文，方便立即复制
      setRevealed((m) => ({ ...m, [created.id]: true }))
      setCreating(false)
      setDraftName('')
      setDraftQuota(EMPTY_QUOTA)
      setDraftAllow('')
      setDraftDeny('')
      setDraftAllowIps('')
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const handleToggleDisabled = async (t: GatewayTokenEntry) => {
    setBusy(t.id)
    try {
      await updateGatewayTokenEntry(t.id, { disabled: !t.disabled })
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const handleResetKey = async (t: GatewayTokenEntry) => {
    const ok = await askConfirm(
      '重置令牌密钥',
      `重置「${t.name}」的密钥？\n\n旧密钥立即失效，外部客户端需要换上新的。`,
      'warning',
      '重置',
    )
    if (!ok) return
    setBusy(t.id)
    try {
      await rotateGatewayTokenEntry(t.id)
      // 重置后默认显示新明文方便复制
      setRevealed((m) => ({ ...m, [t.id]: true }))
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const handleDelete = async (t: GatewayTokenEntry) => {
    const ok = await askConfirm(
      '删除令牌',
      `删除「${t.name}」？\n\n不可撤销，该令牌立即失效。`,
      'danger',
      '删除',
    )
    if (!ok) return
    setBusy(t.id)
    try {
      await deleteGatewayTokenEntry(t.id)
      if (editingId === t.id) setEditingId(null)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const beginEdit = (t: GatewayTokenEntry) => {
    setEditingId(t.id)
    setEditQuota(quotaToInput(t.quota))
    setEditAllow(t.allow_models.join('\n'))
    setEditDeny(t.deny_models.join('\n'))
    setEditAllowIps(t.allow_ips.join('\n'))
  }

  const handleSaveEdit = async (t: GatewayTokenEntry) => {
    setBusy(t.id)
    try {
      await updateGatewayTokenEntry(t.id, {
        quota: inputToQuota(editQuota),
        allow_models: parseList(editAllow),
        deny_models: parseList(editDeny),
        allow_ips: parseList(editAllowIps),
      })
      setEditingId(null)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  if (!enabled) {
    return (
      <div style={cardStyle}>
        <div style={emptyStyle}>Gateway 未启用，请在上方打开开关。</div>
      </div>
    )
  }

  return (
    <div style={containerStyle}>
      {/* 接入地址 */}
      <section style={{ ...cardStyle, padding: '10px 14px' }}>
        <div style={endpointGridStyle}>
          <EndpointRow
            label="OpenAI 客户端"
            url={openaiBase}
            copied={copiedKey === 'oa'}
            onCopy={() => copy('oa', openaiBase)}
          />
          <EndpointRow
            label="Anthropic 客户端"
            url={anthropicBase}
            copied={copiedKey === 'ap'}
            onCopy={() => copy('ap', anthropicBase)}
          />
        </div>
      </section>

      {error && (
        <div style={errStyle}>
          <AlertCircle size={13} />
          <span style={{ flex: 1, minWidth: 0, overflowWrap: 'anywhere' }}>{error}</span>
          <button type="button" style={btnGhostMicroStyle} onClick={() => setError(null)}>×</button>
        </div>
      )}

      {/* 令牌列表 */}
      <section style={cardStyle}>
        <div style={sectionHeadStyle}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
            <KeyRound size={14} style={{ color: 'var(--primary-color)' }} />
            <span style={{ fontSize: 13.5, fontWeight: 600 }}>令牌</span>
            {!loading && (
              <span style={{ fontSize: 11.5, color: 'var(--text-tertiary, #888)' }}>
                {tokens.length} 个
              </span>
            )}
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <button
              type="button"
              style={btnGhostStyle}
              onClick={() => void refresh()}
              disabled={loading}
              title="刷新"
            >
              <RefreshCw size={13} />
            </button>
            <button
              type="button"
              style={btnPrimaryStyle}
              onClick={() => setCreating(true)}
              disabled={creating}
            >
              <Plus size={13} />
              <span style={{ marginLeft: 4 }}>新建</span>
            </button>
          </div>
        </div>

        {creating && (
          <div style={createCardStyle}>
            <input
              type="text"
              autoFocus
              value={draftName}
              onChange={(e) => setDraftName(e.target.value)}
              placeholder="名字（如 Cline、手机 Claude）"
              style={inputStyle}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && draftName.trim()) void handleCreate()
                if (e.key === 'Escape') setCreating(false)
              }}
            />
            <div style={subLabelStyle}>限额（留空 = 无限）</div>
            <QuotaInputs value={draftQuota} onChange={setDraftQuota} />
            <div style={subLabelStyle}>限定模型（可选 · 每行一条；写别名或 provider:model）</div>
            <AllowDenyInputs
              allow={draftAllow}
              deny={draftDeny}
              onAllow={setDraftAllow}
              onDeny={setDraftDeny}
            />
            <div style={subLabelStyle}>IP 白名单（可选 · 留空 = 不限制）</div>
            <AllowIpsInput value={draftAllowIps} onChange={setDraftAllowIps} />
            <div style={createActionsStyle}>
              <button
                type="button"
                style={btnGhostStyle}
                onClick={() => {
                  setCreating(false)
                  setDraftName('')
                  setDraftQuota(EMPTY_QUOTA)
                  setDraftAllow('')
                  setDraftDeny('')
                  setDraftAllowIps('')
                }}
                disabled={busy === 'create'}
              >
                取消
              </button>
              <button
                type="button"
                style={btnPrimaryStyle}
                onClick={() => void handleCreate()}
                disabled={busy === 'create' || !draftName.trim()}
              >
                {busy === 'create' ? <Loader2 size={13} className="spin" /> : <CheckCircle2 size={13} />}
                <span style={{ marginLeft: 4 }}>创建</span>
              </button>
            </div>
          </div>
        )}

        {loading ? (
          <div style={{ ...hintStyle, display: 'flex', gap: 6, alignItems: 'center' }}>
            <Loader2 size={13} className="spin" /> 加载中…
          </div>
        ) : tokens.length === 0 && !creating ? (
          <ZeroState onCreate={() => setCreating(true)} />
        ) : (
          <div style={tokenListStyle}>
            {tokens.map((t) => (
              <TokenRow
                key={t.id}
                entry={t}
                revealed={!!revealed[t.id]}
                onToggleReveal={() => toggleReveal(t.id)}
                busy={busy === t.id}
                editing={editingId === t.id}
                editQuota={editQuota}
                editAllow={editAllow}
                editDeny={editDeny}
                editAllowIps={editAllowIps}
                copiedKey={copiedKey}
                onCopy={copy}
                onToggleDisabled={() => void handleToggleDisabled(t)}
                onResetKey={() => void handleResetKey(t)}
                onDelete={() => void handleDelete(t)}
                onBeginEdit={() => beginEdit(t)}
                onCancelEdit={() => setEditingId(null)}
                onChangeQuota={setEditQuota}
                onChangeAllow={setEditAllow}
                onChangeDeny={setEditDeny}
                onChangeAllowIps={setEditAllowIps}
                onSaveEdit={() => void handleSaveEdit(t)}
              />
            ))}
          </div>
        )}
      </section>

      {/* 折叠：外部 model 字段怎么写 */}
      <ModelHelper aliases={aliases} />
      {confirmDialog}
    </div>
  )
}

/* ─────────── 子组件 ─────────── */

function ZeroState({ onCreate }: { onCreate: () => void }) {
  return (
    <div style={zeroStateStyle}>
      <KeyRound size={22} style={{ color: 'var(--text-tertiary, #aaa)', marginBottom: 6 }} />
      <div style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginBottom: 10 }}>
        还没有令牌
      </div>
      <button type="button" style={btnPrimaryStyle} onClick={onCreate}>
        <Plus size={13} />
        <span style={{ marginLeft: 4 }}>新建令牌</span>
      </button>
    </div>
  )
}

function EndpointRow({
  label,
  url,
  copied,
  onCopy,
}: {
  label: string
  url: string
  copied: boolean
  onCopy: () => void
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
      <span style={{ fontSize: 11, color: 'var(--text-tertiary, #888)' }}>{label}</span>
      <div style={endpointBoxStyle}>
        <input
          type="text"
          readOnly
          value={url}
          title={url}
          style={endpointCodeStyle}
          onFocus={(e) => e.currentTarget.select()}
          spellCheck={false}
          aria-label={label}
        />
        <button type="button" style={btnGhostMicroStyle} onClick={onCopy}>
          <Copy size={11} />
          <span style={{ marginLeft: 3 }}>{copied ? '已复制' : '复制'}</span>
        </button>
      </div>
    </div>
  )
}

interface RowProps {
  entry: GatewayTokenEntry
  revealed: boolean
  onToggleReveal: () => void
  busy: boolean
  editing: boolean
  editQuota: EditableQuota
  editAllow: string
  editDeny: string
  editAllowIps: string
  copiedKey: string | null
  onCopy: (key: string, text: string) => void
  onToggleDisabled: () => void
  onResetKey: () => void
  onDelete: () => void
  onBeginEdit: () => void
  onCancelEdit: () => void
  onChangeQuota: (q: EditableQuota) => void
  onChangeAllow: (s: string) => void
  onChangeDeny: (s: string) => void
  onChangeAllowIps: (s: string) => void
  onSaveEdit: () => void
}

function TokenRow({
  entry: t,
  revealed,
  onToggleReveal,
  busy,
  editing,
  editQuota,
  editAllow,
  editDeny,
  editAllowIps,
  copiedKey,
  onCopy,
  onToggleDisabled,
  onResetKey,
  onDelete,
  onBeginEdit,
  onCancelEdit,
  onChangeQuota,
  onChangeAllow,
  onChangeDeny,
  onChangeAllowIps,
  onSaveEdit,
}: RowProps) {
  const disabled = t.disabled
  const copyKey = `t:${t.id}`
  const copied = copiedKey === copyKey
  return (
    <div style={{ ...rowCardStyle, opacity: disabled ? 0.62 : 1 }}>
      <div style={rowHeadStyle}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 13.5, fontWeight: 600 }}>{t.name}</span>
            {disabled && <span style={dangerPillStyle}>已禁用</span>}
          </div>
          <div style={tokenLineStyle}>
            {/*
              仿 NewAPI 风格：边框包裹 chip + 眼睛 + 复制三者，三个都是容器内部
              元素，边框不动。text 部分定宽 18ch + ellipsis，两个按钮贴右。
              不管怎么切换隐藏/显示，整个容器位置零位移。
            */}
            <div style={tokenChipBoxStyle}>
              {/*
                用 <input readOnly> 而不是 <code>：input 在内容超出宽度时能
                原生水平滚动，鼠标拖选时光标会跟随内容可见区域跳转，
                能拖到完整明文。CSS 的 text-overflow:ellipsis 反而让被截
                断部分隐形不可选。仿 NewAPI 走 input 实现。
              */}
              <input
                type="text"
                readOnly
                value={revealed ? t.token : t.preview}
                title={revealed ? t.token : t.preview}
                style={tokenChipTextStyle}
                onFocus={(e) => e.currentTarget.select()}
                spellCheck={false}
                aria-label={revealed ? '明文密钥' : '隐藏后的密钥'}
              />
              <button
                type="button"
                style={btnInChipStyle}
                onClick={onToggleReveal}
                disabled={busy}
                title={revealed ? '隐藏' : '显示完整密钥'}
                aria-label={revealed ? '隐藏密钥' : '显示完整密钥'}
              >
                {revealed ? <EyeOff size={12} /> : <Eye size={12} />}
              </button>
              <button
                type="button"
                style={btnInChipStyle}
                onClick={() => onCopy(copyKey, t.token)}
                disabled={busy}
                title="复制完整密钥"
                aria-label={copied ? '已复制' : '复制完整密钥'}
              >
                <Copy size={12} style={{ color: copied ? '#16a34a' : undefined }} />
              </button>
            </div>
            <span
              style={{
                fontSize: 10.5,
                color: 'var(--text-tertiary, #999)',
                flex: '0 0 auto',
                whiteSpace: 'nowrap',  // 「最近使用 X 分钟前」作为整体不可被捣烂
              }}
            >
              · 最近使用 {t.last_used_at ? formatTs(t.last_used_at) : '从未'}
            </span>
          </div>
        </div>
        <div style={rowActionsStyle}>
          <button type="button" style={btnGhostStyle} onClick={onToggleDisabled} disabled={busy}>
            {disabled ? <Play size={13} /> : <Pause size={13} />}
            <span style={{ marginLeft: 4 }}>{disabled ? '启用' : '禁用'}</span>
          </button>
          <button type="button" style={btnGhostStyle} onClick={onResetKey} disabled={busy}>
            <RefreshCw size={13} />
            <span style={{ marginLeft: 4 }}>重置密钥</span>
          </button>
          <button type="button" style={btnDangerStyle} onClick={onDelete} disabled={busy}>
            <Trash2 size={13} />
            <span style={{ marginLeft: 4 }}>删除</span>
          </button>
        </div>
      </div>

      {editing ? (
        <div style={editAreaStyle}>
          <div style={subLabelStyle}>限额（留空 = 无限）</div>
          <QuotaInputs value={editQuota} onChange={onChangeQuota} />
          <div style={subLabelStyle}>限定模型（每行一条；写别名或 provider:model）</div>
          <AllowDenyInputs allow={editAllow} deny={editDeny} onAllow={onChangeAllow} onDeny={onChangeDeny} />
          <div style={subLabelStyle}>IP 白名单（留空 = 不限制）</div>
          <AllowIpsInput value={editAllowIps} onChange={onChangeAllowIps} />
          <div style={editActionsStyle}>
            <button type="button" style={btnGhostStyle} onClick={onCancelEdit} disabled={busy}>取消</button>
            <button type="button" style={btnPrimaryStyle} onClick={onSaveEdit} disabled={busy}>
              {busy ? <Loader2 size={13} className="spin" /> : <CheckCircle2 size={13} />}
              <span style={{ marginLeft: 4 }}>保存</span>
            </button>
          </div>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 6 }}>
          <QuotaProgress quota={t.quota} runtime={t.runtime} />
          <AllowDenySummary allow={t.allow_models} deny={t.deny_models} />
          <AllowIpsSummary ips={t.allow_ips} />
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <button type="button" style={btnGhostMicroStyle} onClick={onBeginEdit} disabled={busy}>
              修改限额 / 限定模型 / IP
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function QuotaInputs({
  value,
  onChange,
}: {
  value: EditableQuota
  onChange: (q: EditableQuota) => void
}) {
  const upd = (k: keyof EditableQuota) => (e: React.ChangeEvent<HTMLInputElement>) =>
    onChange({ ...value, [k]: e.target.value })
  return (
    <div style={quotaGridStyle}>
      <QuotaField label="次/分钟" value={value.calls_per_minute} onChange={upd('calls_per_minute')} />
      <QuotaField label="次/日" value={value.calls_per_day} onChange={upd('calls_per_day')} />
      <QuotaField label="token/日" value={value.total_tokens_per_day} onChange={upd('total_tokens_per_day')} />
    </div>
  )
}

function QuotaField({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void
}) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 11, minWidth: 0 }}>
      <span style={{ color: 'var(--text-tertiary, #888)' }}>{label}</span>
      <input
        type="text"
        inputMode="numeric"
        value={value}
        onChange={onChange}
        placeholder="无限"
        style={smallInputStyle}
      />
    </label>
  )
}

function QuotaProgress({ quota, runtime }: { quota: GatewayTokenQuota; runtime: GatewayTokenEntry['runtime'] }) {
  const items = [
    { label: '次/分钟', cur: runtime.calls_last_minute, lim: quota.calls_per_minute },
    { label: '次/日', cur: runtime.calls_last_day, lim: quota.calls_per_day },
    { label: 'token/日', cur: runtime.tokens_last_day, lim: quota.total_tokens_per_day },
  ]
  return (
    <div style={quotaGridStyle}>
      {items.map((it) => (
        <div key={it.label} style={quotaCellStyle}>
          <div style={quotaCellHeadStyle}>
            <span>{it.label}</span>
            <span>{it.cur}{it.lim != null ? ` / ${it.lim}` : ' · 无限'}</span>
          </div>
          {it.lim != null ? (
            <div style={barOuterStyle}>
              <div
                style={{
                  ...barInnerStyle,
                  width: `${Math.min(100, (it.cur / Math.max(1, it.lim)) * 100)}%`,
                  background: it.cur >= it.lim ? '#e5484d' : 'var(--primary-color, #4f7cff)',
                }}
              />
            </div>
          ) : (
            <div style={{ ...barOuterStyle, opacity: 0.4 }}>
              <div style={{ ...barInnerStyle, width: '100%', background: 'rgba(0,0,0,0.08)' }} />
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function AllowDenyInputs({
  allow,
  deny,
  onAllow,
  onDeny,
}: {
  allow: string
  deny: string
  onAllow: (s: string) => void
  onDeny: (s: string) => void
}) {
  return (
    <div style={allowDenyGridStyle}>
      <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 11, minWidth: 0 }}>
        <span style={{ color: 'var(--text-tertiary, #888)' }}>仅允许（留空 = 全部）</span>
        <textarea
          value={allow}
          onChange={(e) => onAllow(e.target.value)}
          placeholder={'chat\nsmart'}
          rows={3}
          style={textareaStyle}
        />
      </label>
      <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 11, minWidth: 0 }}>
        <span style={{ color: 'var(--text-tertiary, #888)' }}>禁止（优先级更高）</span>
        <textarea
          value={deny}
          onChange={(e) => onDeny(e.target.value)}
          placeholder="expensive"
          rows={3}
          style={textareaStyle}
        />
      </label>
    </div>
  )
}

function AllowDenySummary({ allow, deny }: { allow: string[]; deny: string[] }) {
  if (allow.length === 0 && deny.length === 0) {
    return <div style={{ fontSize: 11, color: 'var(--text-tertiary, #999)' }}>所有模型都能用</div>
  }
  return (
    <div style={summaryStyle}>
      {allow.map((p) => (
        <code key={`a:${p}`} style={{ ...inlineCodeStyle, color: '#16a34a', borderColor: 'rgba(22,163,74,0.3)' }}>
          ✓ {p}
        </code>
      ))}
      {deny.map((p) => (
        <code key={`d:${p}`} style={{ ...inlineCodeStyle, color: '#e5484d', borderColor: 'rgba(229,72,77,0.3)' }}>
          ✗ {p}
        </code>
      ))}
    </div>
  )
}

function AllowIpsInput({ value, onChange }: { value: string; onChange: (s: string) => void }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 11, minWidth: 0 }}>
      <span style={{ color: 'var(--text-tertiary, #888)' }}>
        每行一条；支持单 IP（如 <code style={inlineHintCodeStyle}>10.0.0.5</code>）或 CIDR（如 <code style={inlineHintCodeStyle}>192.168.1.0/24</code>）
      </span>
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={'192.168.1.0/24\n10.0.0.5'}
        rows={2}
        style={textareaStyle}
      />
    </label>
  )
}

function AllowIpsSummary({ ips }: { ips: string[] }) {
  if (ips.length === 0) {
    return (
      <div style={{ fontSize: 11, color: 'var(--text-tertiary, #999)' }}>任意 IP 可调用（未设白名单）</div>
    )
  }
  return (
    <div style={summaryStyle}>
      <span style={{ fontSize: 11, color: 'var(--text-tertiary, #888)', marginRight: 4 }}>仅 IP：</span>
      {ips.map((ip) => (
        <code
          key={`ip:${ip}`}
          style={{ ...inlineCodeStyle, color: '#2563eb', borderColor: 'rgba(37,99,235,0.3)' }}
        >
          {ip}
        </code>
      ))}
    </div>
  )
}

const inlineHintCodeStyle: React.CSSProperties = {
  fontFamily:
    'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
  fontSize: 10.5,
  padding: '0 3px',
  borderRadius: 3,
  background: 'var(--bg-secondary, rgba(127,127,127,0.08))',
}

/* ─────────── 模型查询折叠区 ─────────── */

function ModelHelper({ aliases }: { aliases: GatewayAliasMap }) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [models, setModels] = useState<GatewayModelInfo[]>([])
  const [copiedKey, setCopiedKey] = useState<string | null>(null)

  const fetchModels = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await listAdminModels()
      setModels(r.data)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open && models.length === 0 && !loading) {
      void fetchModels()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const copy = (key: string, txt: string) => {
    void navigator.clipboard.writeText(txt)
    setCopiedKey(key)
    setTimeout(() => setCopiedKey((c) => (c === key ? null : c)), 1500)
  }

  // 过滤掉 alias 本身（admin/models 也会把 alias 当成虚拟 model 返回，避免重复）
  const realModels = useMemo(
    () => models.filter((m) => m.transport !== 'alias' && m.owned_by !== 'alias'),
    [models],
  )

  const groupedByProvider = useMemo(() => {
    const m = new Map<string, GatewayModelInfo[]>()
    for (const x of realModels) {
      const k = x.provider_name || x.provider || 'unknown'
      const arr = m.get(k) || []
      arr.push(x)
      m.set(k, arr)
    }
    return Array.from(m.entries())
  }, [realModels])

  const aliasEntries = useMemo(
    () => Object.entries(aliases).sort((a, b) => a[0].localeCompare(b[0])),
    [aliases],
  )

  const findProviderNameFor = useCallback((modelId: string): string => {
    const hit = realModels.find((m) => m.id === modelId)
    return hit?.provider_name || hit?.provider || ''
  }, [realModels])

  return (
    <section style={cardStyle}>
      <div style={modelHelperHeadStyle}>
        <button type="button" style={modelHelperTitleBtnStyle} onClick={() => setOpen((v) => !v)}>
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          <span style={{ fontSize: 13.5, fontWeight: 600 }}>模型 ID</span>
        </button>
        {open && (
          <button type="button" style={btnGhostStyle} onClick={() => void fetchModels()} disabled={loading} title="刷新">
            {loading ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />}
            <span style={{ marginLeft: 4 }}>刷新</span>
          </button>
        )}
      </div>

      {open && (
        <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 12 }}>
          {error && (
            <div style={errStyle}>
              <AlertCircle size={12} />
              <span style={{ flex: 1, minWidth: 0, overflowWrap: 'anywhere' }}>{error}</span>
            </div>
          )}

          {/* 我的别名 */}
          {aliasEntries.length > 0 && (
            <div>
              <div style={modelGroupTitleStyle}>我的别名 · {aliasEntries.length} 个</div>
              <div style={modelChipBoxStyle}>
                {aliasEntries.map(([name, info]) => {
                  const first = info.candidates[0] || ''
                  const providerName = findProviderNameFor(first)
                  return (
                    <button
                      key={`alias:${name}`}
                      type="button"
                      style={modelLineStyle}
                      onClick={() => copy(`alias:${name}`, name)}
                      title={`复制 ${name}（外部 model 字段直接填）`}
                    >
                      <code style={aliasNameStyle}>{name}</code>
                      <span style={modelArrowStyle}>→</span>
                      <span style={modelDestStyle}>
                        {providerName && <span style={providerHintStyle}>{providerName} · </span>}
                        <code style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{first}</code>
                        {info.candidates.length > 1 && (
                          <span style={{ marginLeft: 4, fontSize: 10, color: 'var(--text-tertiary, #999)' }}>
                            +{info.candidates.length - 1}
                          </span>
                        )}
                      </span>
                      <Copy size={11} style={{ marginLeft: 'auto', color: copiedKey === `alias:${name}` ? '#16a34a' : 'var(--text-tertiary, #aaa)' }} />
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          {/* 直接调用：按 provider 分组 */}
          {loading && models.length === 0 ? (
            <div style={{ ...hintStyle, display: 'flex', alignItems: 'center', gap: 6 }}>
              <Loader2 size={12} className="spin" /> 加载模型…
            </div>
          ) : groupedByProvider.length === 0 && !error ? (
            <div style={hintStyle}>暂无可用模型——到「模型服务」标签新增 provider 后再回来。</div>
          ) : (
            <div>
              <div style={modelGroupTitleStyle}>直接调用 · {realModels.length} 个</div>
              {groupedByProvider.map(([providerName, list]) => (
                <div key={providerName} style={{ marginBottom: 6 }}>
                  <div style={providerSectionTitleStyle}>{providerName}</div>
                  <div style={modelChipBoxStyle}>
                    {list.map((m) => {
                      const ck = `m:${m.id}`
                      return (
                        <button
                          key={m.id}
                          type="button"
                          style={modelLineStyle}
                          onClick={() => copy(ck, m.id)}
                          title={`复制完整 model id：${m.id}`}
                        >
                          <code style={{ fontSize: 11, color: 'var(--text-color)' }}>
                            {extractModelName(m.id)}
                          </code>
                          <Copy size={11} style={{ marginLeft: 'auto', color: copiedKey === ck ? '#16a34a' : 'var(--text-tertiary, #aaa)' }} />
                        </button>
                      )
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

/* ─────────── helpers ─────────── */

function parseList(raw: string): string[] {
  return raw
    .split(/[\n,]+/g)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
}

function formatTs(iso: string): string {
  try {
    const d = new Date(iso)
    const diff = Date.now() - d.getTime()
    if (diff < 60_000) return '刚刚'
    if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
    if (diff < 86_400_000) return `${Math.floor(diff / 3600_000)} 小时前`
    return d.toLocaleDateString()
  } catch {
    return iso
  }
}

/** "provider:model_id" → "model_id"。UI 显示用——provider 已经在 group 标题里。 */
function extractModelName(id: string): string {
  const idx = id.indexOf(':')
  return idx > 0 ? id.slice(idx + 1) : id
}

/* ─────────── styles ─────────── */

const containerStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 12,
  width: '100%',
  minWidth: 0,
}

const cardStyle: React.CSSProperties = {
  background: 'var(--card-bg, rgba(0,0,0,0.02))',
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  borderRadius: 8,
  padding: '12px 14px',
  minWidth: 0,
}

const sectionHeadStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  marginBottom: 8,
  gap: 8,
  flexWrap: 'wrap',
}

const hintStyle: React.CSSProperties = {
  fontSize: 11.5,
  color: 'var(--text-secondary)',
  lineHeight: 1.6,
}

const subLabelStyle: React.CSSProperties = {
  fontSize: 11,
  color: 'var(--text-tertiary, #888)',
  marginTop: 8,
  marginBottom: 4,
}

const emptyStyle: React.CSSProperties = {
  ...hintStyle,
  textAlign: 'center',
  padding: '18px 0',
}

const zeroStateStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  padding: '22px 12px',
  border: '1px dashed var(--border-color, rgba(0,0,0,0.12))',
  borderRadius: 8,
}

const errStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  padding: '6px 10px',
  fontSize: 12,
  color: '#e5484d',
  background: 'rgba(229,72,77,0.08)',
  borderRadius: 6,
  minWidth: 0,
}

const endpointGridStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
  gap: 10,
}

const endpointBoxStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  padding: '5px 8px',
  background: 'rgba(0,0,0,0.04)',
  borderRadius: 5,
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  minWidth: 0,
}

// baseUrl 显示框：同样用 input readOnly。超出宽度时原生水平滚动，鼠标
// 拖选能拖到完整 URL；focus 时自动全选便于 Ctrl+C。去掉 input 默认装饰。
const endpointCodeStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 12,
  color: 'var(--text-color)',
  background: 'transparent',
  border: 'none',
  outline: 'none',
  padding: 0,
  margin: 0,
}

const tokenListStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
}

const rowCardStyle: React.CSSProperties = {
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  borderRadius: 6,
  padding: '10px 12px',
  background: 'rgba(0,0,0,0.015)',
  display: 'flex',
  flexDirection: 'column',
  minWidth: 0,
}

const rowHeadStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 8,
  flexWrap: 'wrap',
  minWidth: 0,
}

const rowActionsStyle: React.CSSProperties = {
  display: 'flex',
  gap: 4,
  flexWrap: 'wrap',
  justifyContent: 'flex-end',
}

// 令牌行：chip + 「· 最近使用」。`flexWrap: 'nowrap'` 确保 span 永远跟随 chip 同行
// 不被换到下一行；chip 本身 `flex: 0 1 25ch` 能被压缩，窗口缩小时会优先
// 让 chip 变短（反正 input 能水平滚动），而不会让 span 被挤换行。
const tokenLineStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  marginTop: 4,
  flexWrap: 'nowrap',
  minWidth: 0,
}

// chip 边框容器：默认宽 25ch（~25 个字符），窗口缩小会被压缩到下限 0，
// 窗口放大不会再变宽。flex 表达式翻译：`grow=0` 不拉伸 / `shrink=1` 可
// 被压 / `basis=25ch` 默认宽度。两态等宽仍然成立（后端保长 mask）。
const tokenChipBoxStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 2,
  padding: '1px 2px 1px 8px',
  borderRadius: 4,
  background: 'rgba(0,0,0,0.05)',
  border: '1px dashed var(--border-color, rgba(0,0,0,0.15))',
  flex: '0 1 25ch',
  minWidth: 0,
}

// chip 内部 text（input readOnly）：仿 NewAPI 。input 超出宽度时原生可水
// 平滚动，鼠标拖选能打到完整明文（不会被 CSS ellipsis 截住）。去掉
const tokenChipTextStyle: React.CSSProperties = {
  fontSize: 11.5,
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  color: 'var(--text-color)',
  flex: '1 1 auto',
  minWidth: 0,
  width: '100%',
  // 去掉 input 默认装饰
  background: 'transparent',
  border: 'none',
  outline: 'none',
  padding: 0,
  margin: 0,
  // input 默认 cursor:text，奇他不动
}

// 内嵌进 chip 边框里的 icon 按钮：无边框、hover 高亮，尺寸跟 text 高度对齐。
const btnInChipStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: 20,
  height: 18,
  padding: 0,
  borderRadius: 3,
  background: 'transparent',
  border: 'none',
  color: 'var(--text-secondary)',
  cursor: 'pointer',
  flex: '0 0 auto',
}

const dangerPillStyle: React.CSSProperties = {
  fontSize: 10,
  padding: '1px 6px',
  borderRadius: 999,
  color: '#e5484d',
  background: 'rgba(229,72,77,0.08)',
  border: '1px solid rgba(229,72,77,0.25)',
}

const inlineCodeStyle: React.CSSProperties = {
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 11,
  padding: '1px 5px',
  background: 'rgba(0,0,0,0.05)',
  borderRadius: 3,
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
}

const summaryStyle: React.CSSProperties = {
  display: 'flex',
  gap: 4,
  flexWrap: 'wrap',
  fontSize: 11,
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
}

const btnGhostMicroStyle: React.CSSProperties = {
  ...btnGhostStyle,
  fontSize: 10.5,
  padding: '2px 6px',
}

const btnPrimaryStyle: React.CSSProperties = {
  ...btnGhostStyle,
  background: 'var(--primary-color, #4f7cff)',
  color: '#fff',
  borderColor: 'transparent',
}

const btnDangerStyle: React.CSSProperties = {
  ...btnGhostStyle,
  color: '#e5484d',
  borderColor: 'rgba(229,72,77,0.4)',
}

const createCardStyle: React.CSSProperties = {
  border: '1px solid var(--primary-color, #4f7cff)',
  borderRadius: 6,
  padding: '10px 12px',
  marginBottom: 10,
  background: 'rgba(79,124,255,0.04)',
  display: 'flex',
  flexDirection: 'column',
}

const createActionsStyle: React.CSSProperties = {
  display: 'flex',
  gap: 6,
  marginTop: 10,
  justifyContent: 'flex-end',
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  fontSize: 12.5,
  padding: '6px 10px',
  borderRadius: 4,
  border: '1px solid var(--border-color, rgba(0,0,0,0.15))',
  background: 'var(--input-bg, #fff)',
  color: 'var(--text-color)',
  boxSizing: 'border-box',
}

const smallInputStyle: React.CSSProperties = {
  ...inputStyle,
  fontSize: 11.5,
  padding: '4px 7px',
}

const textareaStyle: React.CSSProperties = {
  ...inputStyle,
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
  fontSize: 11,
  resize: 'vertical',
}

const editAreaStyle: React.CSSProperties = {
  borderTop: '1px dashed var(--border-color, rgba(0,0,0,0.12))',
  marginTop: 8,
  paddingTop: 8,
}

const editActionsStyle: React.CSSProperties = {
  display: 'flex',
  gap: 6,
  justifyContent: 'flex-end',
  marginTop: 8,
}

const quotaGridStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))',
  gap: 6,
}

const allowDenyGridStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
  gap: 6,
}

const quotaCellStyle: React.CSSProperties = {
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  borderRadius: 4,
  padding: '4px 6px',
  background: 'rgba(0,0,0,0.015)',
  minWidth: 0,
}

const quotaCellHeadStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  fontSize: 10.5,
  color: 'var(--text-secondary)',
  gap: 4,
  flexWrap: 'wrap',
}

const barOuterStyle: React.CSSProperties = {
  height: 3,
  borderRadius: 2,
  background: 'rgba(0,0,0,0.06)',
  overflow: 'hidden',
  marginTop: 3,
}

const barInnerStyle: React.CSSProperties = {
  height: '100%',
  transition: 'width .2s',
}

const modelHelperHeadStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  gap: 8,
  marginBottom: 0,
  // 锁行高，避免「刷新」按钮显隐时标题上下跳
  minHeight: 28,
}

const modelHelperTitleBtnStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  background: 'transparent',
  border: 'none',
  cursor: 'pointer',
  padding: 0,
  font: 'inherit',
  color: 'var(--text-color)',
}

const modelGroupTitleStyle: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: 'var(--text-secondary)',
  marginBottom: 6,
}

const providerSectionTitleStyle: React.CSSProperties = {
  fontSize: 10.5,
  color: 'var(--text-tertiary, #888)',
  letterSpacing: 0.3,
  margin: '8px 0 3px',
}

const modelChipBoxStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
  gap: 4,
}

const modelLineStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  padding: '4px 8px',
  borderRadius: 4,
  border: '1px solid var(--border-color, rgba(0,0,0,0.08))',
  background: 'rgba(0,0,0,0.02)',
  cursor: 'pointer',
  textAlign: 'left',
  font: 'inherit',
  color: 'inherit',
  minWidth: 0,
}

const aliasNameStyle: React.CSSProperties = {
  fontSize: 11.5,
  fontWeight: 600,
  color: 'var(--primary-color, #4f7cff)',
  padding: '1px 5px',
  borderRadius: 3,
  background: 'rgba(79,124,255,0.08)',
  fontFamily: 'ui-monospace, SF Mono, Menlo, Consolas, monospace',
}

const modelArrowStyle: React.CSSProperties = {
  fontSize: 10,
  color: 'var(--text-tertiary, #999)',
  flex: '0 0 auto',
}

const modelDestStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  fontSize: 11,
  display: 'inline-flex',
  alignItems: 'center',
  gap: 2,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
}

const providerHintStyle: React.CSSProperties = {
  fontSize: 10.5,
  color: 'var(--text-tertiary, #999)',
}
