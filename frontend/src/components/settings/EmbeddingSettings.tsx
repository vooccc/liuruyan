import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  Brain,
  CheckCircle2,
  ChevronDown,
  Database,
  Eye,
  EyeOff,
  Loader2,
  RefreshCw,
  Sparkles,
  X,
  Zap,
} from 'lucide-react'
import {
  getEmbeddingConfig,
  getEmbeddingStats,
  reindexEmbeddingBucketStream,
  testEmbedding,
  updateEmbeddingConfig,
  type EmbeddingBucketStats,
  type EmbeddingConfig,
  type EmbeddingConfigResponse,
  type EmbeddingEligibleProvider,
  type EmbeddingStatsResponse,
  type EmbeddingTestResult,
} from '../../api/embedding'

/**
 * 设置 → Embedding 长期记忆 tab。
 *
 * 设计语言对齐 VoiceSettings：hero + section + footer 三段式。
 * 但比语音多了"向量库可视化"——记忆是看不见的，UI 必须把它变可见，
 * 让用户实感"我有 N 条向量"，否则配置完没反馈用户会怀疑没生效。
 *
 * 关键交互：
 * - 启用开关：禁用时上层 chat 回退 BM25，行为跟之前一样（明确告知）
 * - 推荐 provider：硅基流动 BAAI/bge-m3 永久免费 → 金色高亮卡片
 * - 测试连通性：维度不匹配时一键自动修正 dim 字段（用户最容易踩的坑）
 * - 全量重算：扫每个 bucket 顺序跑（V1 同步，N>1000 会卡——后端 V2 改 SSE 进度）
 */
export default function EmbeddingSettings() {
  const [data, setData] = useState<EmbeddingConfigResponse | null>(null)
  const [stats, setStats] = useState<EmbeddingStatsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)

  // 表单本地状态：用户编辑过程中先在前端缓冲，"保存"按钮才落盘
  // 跟 VoiceSettings 的 partial-PUT 实时保存不一样——embedding 字段多 + dim 改动有破坏性，
  // 用户改一半被实时保存反而困惑。enabled 开关另说（单开关用实时切）。
  const [draft, setDraft] = useState<Partial<EmbeddingConfig>>({})
  const [showApiKey, setShowApiKey] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<EmbeddingTestResult | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setLoadErr(null)
    try {
      const [cfg, st] = await Promise.all([getEmbeddingConfig(), getEmbeddingStats()])
      setData(cfg)
      setStats(st)
      // 初始化 draft 为当前配置（有则填，无则留空）
      setDraft({
        enabled: cfg.config.enabled,
        provider: cfg.config.provider,
        model: cfg.config.model,
        base_url: cfg.config.base_url,
        api_key: cfg.config.api_key,
        dim: cfg.config.dim,
        timeout: cfg.config.timeout,
        cache_size: cfg.config.cache_size,
      })
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  /** 实时切换 enabled（单开关，不需要走"保存"按钮） */
  const toggleEnabled = useCallback(
    async (next: boolean) => {
      if (!data) return
      setSaving(true)
      setSaveErr(null)
      try {
        const result = await updateEmbeddingConfig({ enabled: next })
        setData((prev) => (prev ? { ...prev, config: result.config } : prev))
        setDraft((prev) => ({ ...prev, enabled: next }))
      } catch (e) {
        setSaveErr(e instanceof Error ? e.message : String(e))
      } finally {
        setSaving(false)
      }
    },
    [data],
  )

  /** 保存整个 draft（其他字段都用这个，避免改一半被中途读到） */
  const saveDraft = useCallback(async () => {
    if (!data) return
    setSaving(true)
    setSaveErr(null)
    try {
      const result = await updateEmbeddingConfig(draft)
      setData((prev) => (prev ? { ...prev, config: result.config } : prev))
      // stats 可能因为 dim 变化失效，重拉
      const newStats = await getEmbeddingStats()
      setStats(newStats)
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }, [data, draft])

  /** 测试连通性：用 draft 字段发临时请求，不落盘 */
  const runTest = useCallback(async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const result = await testEmbedding({
        model: draft.model,
        base_url: draft.base_url,
        api_key: draft.api_key,
        dim: draft.dim,
        timeout: draft.timeout,
      })
      setTestResult(result)
    } catch (e) {
      setTestResult({
        ok: false,
        dim: 0,
        expected_dim: draft.dim ?? 0,
        elapsed_ms: 0,
        error: e instanceof Error ? e.message : String(e),
      })
    } finally {
      setTesting(false)
    }
  }, [draft])

  /** 应用 provider 推荐配置（一键填好 base_url + model + dim） */
  const applyProvider = useCallback(
    (p: EmbeddingEligibleProvider) => {
      const next: Partial<EmbeddingConfig> = {
        provider: p.id,
        base_url: p.base_url,
      }
      if (p.recommended) {
        next.model = p.recommended.model
        next.dim = p.recommended.dim
      }
      setDraft((prev) => ({ ...prev, ...next }))
      setTestResult(null)
    },
    [],
  )

  // draft 跟 data.config 是否有未保存的差异
  const hasUnsaved = useMemo(() => {
    if (!data) return false
    const keys: Array<keyof EmbeddingConfig> = [
      'provider', 'model', 'base_url', 'api_key', 'dim', 'timeout', 'cache_size',
    ]
    return keys.some((k) => (draft[k] ?? '') !== ((data.config[k] as unknown as string | number) ?? ''))
  }, [draft, data])

  if (loading) {
    return (
      <div className="embedding-settings-loading">
        <Loader2 size={16} className="spin" />
        <span>加载 embedding 配置…</span>
      </div>
    )
  }

  if (loadErr || !data) {
    return (
      <div className="embedding-settings-error">
        <AlertCircle size={16} />
        <span>加载失败：{loadErr ?? '未知错误'}</span>
        <button type="button" className="embedding-btn-ghost" onClick={refresh}>
          重试
        </button>
      </div>
    )
  }

  const cfg = data.config
  const isReady = cfg.enabled && cfg.has_api_key && !!cfg.model && cfg.dim > 0
  const totalVectors = stats?.total_vectors ?? 0

  return (
    <div className="embedding-settings">
      {/* ========== Hero（精简：标题/开关一行 + 副标题 + 状态徽章） ========== */}
      <header className={`embedding-hero ${isReady ? 'is-ready' : ''}`}>
        <div className="embedding-hero-row">
          <div className="embedding-hero-title">
            <Brain size={16} className="embedding-hero-brain" />
            <h2>长期记忆</h2>
            <span className="embedding-hero-sub">向量语义检索</span>
          </div>
          <ToggleSwitch
            checked={cfg.enabled}
            onChange={toggleEnabled}
            disabled={saving}
            label={cfg.enabled ? '已启用' : '未启用'}
          />
        </div>
        <p className="embedding-hero-desc">
          让 <code>recall_memory</code> 从 BM25 字面匹配升级到向量近邻 —— 关闭时自动回落，跟以前一样。
        </p>
        <div className="embedding-hero-stats">
          <span className="embedding-stat-pill">
            <Database size={12} /> {totalVectors.toLocaleString()} 条向量
          </span>
          {isReady && (
            <span className="embedding-stat-pill ok">
              <Sparkles size={12} /> {cfg.model} · {cfg.dim} 维
            </span>
          )}
        </div>
      </header>

      {/* ========== Provider 推荐卡片 ========== */}
      <section className="embedding-section">
        <div className="embedding-section-header">
          <h3>选择 Provider</h3>
          {data.eligible_providers.length === 0 && (
            <span className="embedding-section-hint">
              没有可用的 OpenAI 兼容 provider — 请先到 <strong>模型服务</strong> 配置一个
            </span>
          )}
        </div>
        {data.eligible_providers.length > 0 && (
          <ProviderSelect
            providers={data.eligible_providers}
            selectedId={draft.provider ?? null}
            onSelect={applyProvider}
          />
        )}
      </section>

      {/* ========== 详细配置字段 ========== */}
      <section className="embedding-section">
        <h3>配置详情</h3>
        <div className="embedding-form">
          <FormField label="Base URL" hint="OpenAI 兼容 /embeddings 端点（不含 /embeddings 后缀）">
            <input
              className="embedding-input"
              type="text"
              placeholder="https://api.siliconflow.cn/v1"
              value={draft.base_url ?? ''}
              onChange={(e) => setDraft((p) => ({ ...p, base_url: e.target.value }))}
            />
          </FormField>

          <FormField label="API Key" hint="保存到 ~/.liuruyan/embedding_config.json（明文，跟 models.json 同口径）">
            <div className="embedding-input-with-action">
              <input
                className="embedding-input"
                type={showApiKey ? 'text' : 'password'}
                placeholder="sk-..."
                value={draft.api_key ?? ''}
                onChange={(e) => setDraft((p) => ({ ...p, api_key: e.target.value }))}
              />
              <button
                type="button"
                className="embedding-input-eye"
                onClick={() => setShowApiKey((v) => !v)}
                title={showApiKey ? '隐藏' : '显示'}
              >
                {showApiKey ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </div>
          </FormField>

          <div className="embedding-form-row">
            <FormField label="模型" hint="如 BAAI/bge-m3 / text-embedding-3-small">
              <input
                className="embedding-input"
                type="text"
                placeholder="BAAI/bge-m3"
                value={draft.model ?? ''}
                onChange={(e) => setDraft((p) => ({ ...p, model: e.target.value }))}
              />
            </FormField>

            <FormField label="维度" hint="跟模型实际输出一致">
              <input
                className="embedding-input embedding-input-narrow"
                type="number"
                min={1}
                placeholder="1024"
                value={draft.dim || ''}
                onChange={(e) =>
                  setDraft((p) => ({ ...p, dim: Number(e.target.value) || 0 }))
                }
              />
            </FormField>
          </div>

          {/* 测试连通性 */}
          <div className="embedding-form-actions">
            <button
              type="button"
              className="embedding-btn-ghost"
              onClick={runTest}
              disabled={testing || !draft.base_url || !draft.api_key || !draft.model || !draft.dim}
            >
              {testing ? <Loader2 size={14} className="spin" /> : <Zap size={14} />}
              测试连通性
            </button>
            {testResult && <TestResultPill result={testResult} onAutoFixDim={(d) => setDraft((p) => ({ ...p, dim: d }))} />}
          </div>

          {/* 保存按钮（draft 有变化时高亮） */}
          <div className="embedding-form-save">
            <button
              type="button"
              className={`embedding-btn-primary ${hasUnsaved ? 'has-unsaved' : ''}`}
              disabled={saving || !hasUnsaved}
              onClick={saveDraft}
            >
              {saving ? <Loader2 size={14} className="spin" /> : null}
              {hasUnsaved ? '保存配置' : '已同步'}
            </button>
            {saveErr && (
              <span className="embedding-form-err">
                <AlertCircle size={13} /> {saveErr}
              </span>
            )}
          </div>
        </div>
      </section>

      {/* ========== Bucket Stats 仪表盘 ========== */}
      {stats && stats.buckets.length > 0 && (
        <section className="embedding-section">
          <div className="embedding-section-header">
            <h3>向量库总览</h3>
            <button
              type="button"
              className="embedding-btn-ghost embedding-btn-mini"
              onClick={() => void refresh()}
              title="刷新统计"
            >
              <RefreshCw size={12} />
            </button>
          </div>
          <BucketDashboard
            buckets={stats.buckets}
            totalVectors={stats.total_vectors}
            isReady={isReady}
            onAfterReindex={refresh}
          />
        </section>
      )}

      {stats && stats.buckets.length === 0 && cfg.enabled && (
        <div className="embedding-empty-hint">
          <Database size={14} />
          <span>还没有任何向量索引。让角色保存几条记忆，或对已有记忆点 <strong>全量重算</strong>。</span>
        </div>
      )}

      {/* footer 已合并到 Hero 状态徽章 + 表单内 saveErr 提示，避免页面拖长 */}
    </div>
  )
}

/* ========================================================================
   子组件
   ======================================================================== */

function ToggleSwitch({
  checked,
  onChange,
  disabled,
  label,
}: {
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  label?: string
}) {
  return (
    <label className={`embedding-toggle ${checked ? 'on' : ''} ${disabled ? 'disabled' : ''}`}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="embedding-toggle-track">
        <span className="embedding-toggle-thumb" />
      </span>
      {label && <span className="embedding-toggle-label">{label}</span>}
    </label>
  )
}

/**
 * Provider 下拉选择器：默认仅占一行，点击展开浮层列表。
 * 替代之前的多排卡片网格，省空间且更专业（Cursor / Linear 风格）。
 */
function ProviderSelect({
  providers,
  selectedId,
  onSelect,
}: {
  providers: EmbeddingEligibleProvider[]
  selectedId: string | null
  onSelect: (p: EmbeddingEligibleProvider) => void
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const selected = providers.find((p) => p.id === selectedId) ?? null

  // 点击外部关闭
  useEffect(() => {
    if (!open) return
    function onDown(e: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  return (
    <div className="embedding-provider-select" ref={wrapRef}>
      <button
        type="button"
        className={`embedding-provider-trigger ${open ? 'open' : ''}`}
        onClick={() => setOpen((v) => !v)}
      >
        {selected ? (
          <ProviderRowInline provider={selected} />
        ) : (
          <span className="embedding-provider-placeholder">点击选择 Provider…</span>
        )}
        <ChevronDown size={14} className="embedding-provider-chevron" />
      </button>
      {open && (
        <div className="embedding-provider-menu" role="listbox">
          {providers.map((p) => {
            const isSel = p.id === selectedId
            const disabled = !p.has_api_key
            return (
              <button
                key={p.id}
                type="button"
                role="option"
                aria-selected={isSel}
                className={[
                  'embedding-provider-option',
                  isSel ? 'selected' : '',
                  disabled ? 'disabled' : '',
                ].filter(Boolean).join(' ')}
                disabled={disabled}
                onClick={() => {
                  onSelect(p)
                  setOpen(false)
                }}
                title={disabled ? '该 provider 还没配 API Key — 请到「模型服务」填' : undefined}
              >
                <ProviderRowInline provider={p} showStatus />
                {isSel && <CheckCircle2 size={14} className="embedding-provider-check" />}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

/** Provider 单行展示：名称 + 模型 + 维度 + （可选）状态徽章 */
function ProviderRowInline({
  provider,
  showStatus = false,
}: {
  provider: EmbeddingEligibleProvider
  showStatus?: boolean
}) {
  const isFree = provider.id === 'siliconflow'
  return (
    <div className="embedding-provider-inline">
      <span className="embedding-provider-inline-name">{provider.name}</span>
      {isFree && <span className="embedding-provider-free">免费</span>}
      {provider.recommended ? (
        <>
          <span className="embedding-provider-inline-sep">·</span>
          <code className="embedding-provider-inline-model">
            {shortModel(provider.recommended.model)}
          </code>
          <span className="embedding-provider-inline-dim">{provider.recommended.dim}维</span>
        </>
      ) : (
        <>
          <span className="embedding-provider-inline-sep">·</span>
          <span className="embedding-provider-inline-no-rec">需手填模型</span>
        </>
      )}
      {showStatus && !provider.has_api_key && (
        <span className="embedding-provider-inline-warn">未配 Key</span>
      )}
    </div>
  )
}

function shortModel(name: string): string {
  // BAAI/bge-m3 → bge-m3 / text-embedding-3-small → text-embedding-3-small
  const slash = name.lastIndexOf('/')
  return slash >= 0 ? name.slice(slash + 1) : name
}

function FormField({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="embedding-field">
      <div className="embedding-field-label">{label}</div>
      {children}
      {hint && <div className="embedding-field-hint">{hint}</div>}
    </div>
  )
}

function TestResultPill({
  result,
  onAutoFixDim,
}: {
  result: EmbeddingTestResult
  onAutoFixDim: (d: number) => void
}) {
  if (result.ok) {
    return (
      <span className="embedding-test-pill ok">
        <CheckCircle2 size={13} /> 连通成功 · {result.dim} 维 · {result.elapsed_ms}ms
      </span>
    )
  }
  // 维度不一致 → 一键自动填正确的 dim
  const dimMismatch =
    result.dim > 0 && result.expected_dim > 0 && result.dim !== result.expected_dim
  return (
    <span className="embedding-test-pill err">
      <AlertCircle size={13} />
      <span>{result.error || '失败'}</span>
      {dimMismatch && (
        <button
          type="button"
          className="embedding-test-fix"
          onClick={() => onAutoFixDim(result.dim)}
        >
          自动改为 {result.dim} 维
        </button>
      )}
    </span>
  )
}

/** 单个 bucket 的实时重算进度（streaming 期间逐条更新） */
interface BucketProgress {
  total: number
  processed: number
  succeeded: number
  failed: number
  currentTitle: string
  abort: AbortController
}

function BucketDashboard({
  buckets,
  totalVectors,
  isReady,
  onAfterReindex,
}: {
  buckets: EmbeddingBucketStats[]
  totalVectors: number
  isReady: boolean
  /** 重算完成后的回调，让父组件刷 stats */
  onAfterReindex?: () => void
}) {
  // 按 count 倒序，让大库排前面
  const sorted = [...buckets].sort((a, b) => b.count - a.count)
  const max = Math.max(1, ...sorted.map((b) => b.count))
  /** 全局只允许一个 reindex 同时跑（embedding API 通常 RPM 受限，并发反而更慢） */
  const [activeKey, setActiveKey] = useState<string | null>(null)
  const [progressMap, setProgressMap] = useState<Record<string, BucketProgress>>({})
  const [results, setResults] = useState<Record<string, { ok: boolean; text: string }>>({})

  /** 取消正在跑的某个 bucket */
  const onCancel = useCallback((key: string) => {
    setProgressMap((prev) => {
      const cur = prev[key]
      if (cur) {
        try { cur.abort.abort() } catch { /* ignore */ }
      }
      return prev
    })
  }, [])

  const onReindex = useCallback(
    async (bucket: EmbeddingBucketStats) => {
      const key = bucket.scope
      const scope: 'private' | 'shared' = bucket.scope === 'shared' ? 'shared' : 'private'
      const abort = new AbortController()

      // 初始化进度状态：total 暂用 bucket.count（后端 start 事件会回填精确值）
      setActiveKey(key)
      setProgressMap((p) => ({
        ...p,
        [key]: {
          total: bucket.count,
          processed: 0,
          succeeded: 0,
          failed: 0,
          currentTitle: '',
          abort,
        },
      }))
      setResults((p) => {
        const next = { ...p }
        delete next[key]
        return next
      })

      let aborted = false
      try {
        for await (const ev of reindexEmbeddingBucketStream(
          { scope, agent_id: bucket.agent_id },
          abort.signal,
        )) {
          if (ev.event === 'start') {
            setProgressMap((p) => ({
              ...p,
              [key]: { ...p[key], total: ev.total },
            }))
          } else if (ev.event === 'progress') {
            setProgressMap((p) => ({
              ...p,
              [key]: {
                ...p[key],
                processed: ev.processed,
                succeeded: ev.succeeded,
                failed: ev.failed,
                currentTitle: ev.current_title,
              },
            }))
          } else if (ev.event === 'done') {
            setResults((p) => ({
              ...p,
              [key]: {
                ok: true,
                text: `✓ ${ev.succeeded}/${ev.processed} 条 · ${formatElapsed(ev.elapsed_ms)}${ev.failed > 0 ? ` · ${ev.failed} 条失败` : ''}`,
              },
            }))
          } else if (ev.event === 'error') {
            setResults((p) => ({
              ...p,
              [key]: { ok: false, text: `✗ [${ev.stage}] ${ev.error}` },
            }))
          }
        }
      } catch (e) {
        // AbortError 是预期内的（用户点取消）；其它才显错
        if (abort.signal.aborted) {
          aborted = true
        } else {
          setResults((p) => ({
            ...p,
            [key]: { ok: false, text: `✗ ${e instanceof Error ? e.message : String(e)}` },
          }))
        }
      } finally {
        if (aborted) {
          // 用最新进度状态写入 result
          setProgressMap((p) => {
            const cur = p[key]
            if (cur) {
              setResults((r) => ({
                ...r,
                [key]: {
                  ok: false,
                  text: `已取消 · 已处理 ${cur.processed}/${cur.total}（成功 ${cur.succeeded} · 失败 ${cur.failed}）`,
                },
              }))
            }
            return p
          })
        }
        setProgressMap((p) => {
          const next = { ...p }
          delete next[key]
          return next
        })
        setActiveKey(null)
        onAfterReindex?.()
      }
    },
    [onAfterReindex],
  )

  return (
    <div className="embedding-buckets">
      {sorted.map((b) => {
        const pct = max > 0 ? Math.round((b.count / max) * 100) : 0
        const sharePct = totalVectors > 0 ? Math.round((b.count / totalVectors) * 100) : 0
        const key = b.scope
        const progress = progressMap[key]
        const isReindexingThis = !!progress
        const result = results[key]
        const reindexPct =
          progress && progress.total > 0
            ? Math.min(100, Math.round((progress.processed / progress.total) * 100))
            : 0

        return (
          <div
            key={key}
            className={`embedding-bucket-card ${isReindexingThis ? 'reindexing' : ''}`}
          >
            <div className="embedding-bucket-row">
              <div className="embedding-bucket-name">
                <span className={`embedding-bucket-tag ${b.scope === 'shared' ? 'shared' : 'private'}`}>
                  {b.scope === 'shared' ? '🌐 共享' : '🔒 私有'}
                </span>
                <span className="embedding-bucket-id">
                  {b.scope === 'shared' ? '所有角色共用' : b.agent_id}
                </span>
              </div>
              <div className="embedding-bucket-count">
                <strong>{b.count.toLocaleString()}</strong>
                <span>条 · {sharePct}%</span>
              </div>
            </div>

            {/* 进度条：未重算时显示 size 占比；重算时切换为实时进度 */}
            {isReindexingThis ? (
              <div className="embedding-bucket-bar embedding-bucket-bar-running">
                <div
                  className="embedding-bucket-bar-fill embedding-bucket-bar-fill-running"
                  style={{ width: `${reindexPct}%` }}
                />
              </div>
            ) : (
              <div className="embedding-bucket-bar">
                <div className="embedding-bucket-bar-fill" style={{ width: `${pct}%` }} />
              </div>
            )}

            {/* 实时计数 + 当前条目（仅重算中显示） */}
            {progress && (
              <div className="embedding-bucket-progress">
                <div className="embedding-bucket-progress-stats">
                  <span className="embedding-bucket-progress-pct">
                    {reindexPct}%
                  </span>
                  <span className="embedding-bucket-progress-count">
                    {progress.processed}<span className="embedding-bucket-progress-sep">/</span>{progress.total}
                  </span>
                  <span className="embedding-bucket-progress-ok">✓ {progress.succeeded}</span>
                  {progress.failed > 0 && (
                    <span className="embedding-bucket-progress-err">✗ {progress.failed}</span>
                  )}
                </div>
                {progress.currentTitle && (
                  <div className="embedding-bucket-progress-current" title={progress.currentTitle}>
                    {progress.currentTitle}
                  </div>
                )}
              </div>
            )}

            <div className="embedding-bucket-meta">
              <span>
                <code>{shortModel(b.model)}</code> · {b.dim}维
              </span>
              {isReindexingThis ? (
                <button
                  type="button"
                  className="embedding-btn-ghost embedding-btn-mini embedding-btn-danger"
                  onClick={() => onCancel(key)}
                  title="取消重算（已处理的部分仍有效）"
                >
                  <X size={11} />
                  取消
                </button>
              ) : (
                <button
                  type="button"
                  className="embedding-btn-ghost embedding-btn-mini"
                  disabled={!isReady || (!!activeKey && activeKey !== key)}
                  onClick={() => void onReindex(b)}
                  title={isReady
                    ? (activeKey ? '另一个 bucket 正在重算' : '用当前配置重算所有向量')
                    : '先启用并完整配置 embedding'}
                >
                  <RefreshCw size={11} />
                  重算
                </button>
              )}
            </div>

            {result && (
              <div className={`embedding-bucket-result ${result.ok ? 'ok' : 'err'}`}>
                {result.text}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

/** 把毫秒数格式化成易读：< 1s 显示 ms；>= 1s 显示 s；>= 60s 显示 mm:ss */
function formatElapsed(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  const mm = Math.floor(s / 60)
  const ss = Math.round(s % 60)
  return `${mm}m${ss}s`
}
