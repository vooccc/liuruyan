import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  CheckCircle2,
  Code2,
  Cpu,
  Eye,
  EyeOff,
  Layers,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Sliders,
  Sparkles,
} from 'lucide-react'
import {
  getFastContextSettings,
  updateFastContextSettings,
  fetchLocalDevinTokenForFastContext,
  type FastContextSettingsResponse,
} from '../../api/fast_context'
import { listProviders, type ProviderView } from '../../api/providers'
import { MODELS_CATALOG_CHANGED_EVENT } from '../../api/chat'

export default function FastContextPane() {
  const [data, setData] = useState<FastContextSettingsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)
  const [saveSuccess, setSaveSuccess] = useState(false)

  // 模型服务商列表
  const [providers, setProviders] = useState<ProviderView[]>([])

  // 表单状态：'session' | 'custom' | 'devin'（默认跟随当前会话模型）
  const [mode, setMode] = useState<'session' | 'custom' | 'devin'>('session')
  const [customProvider, setCustomProvider] = useState<string>('')
  const [customModel, setCustomModel] = useState<string>('')
  const [customModelManual, setCustomModelManual] = useState<boolean>(false)
  const [devinToken, setDevinToken] = useState<string>('')
  const [showToken, setShowToken] = useState<boolean>(false)
  const [maxTurns, setMaxTurns] = useState<number>(3)

  // 手动获取本地 Devin 凭据状态
  const [syncingToken, setSyncingToken] = useState(false)
  const [syncMsg, setSyncMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null)

  // 刷新 Providers
  const refreshProviders = useCallback(async () => {
    try {
      const resp = await listProviders()
      setProviders(resp.providers ?? [])
    } catch {
      /* 静默处理 */
    }
  }, [])

  // 刷新当前配置
  const refreshConfig = useCallback(async () => {
    setLoading(true)
    setLoadErr(null)
    try {
      const res = await getFastContextSettings()
      setData(res)
      setMode(res.mode || 'session')
      setCustomProvider(res.custom_provider || '')
      setCustomModel(res.custom_model || '')
      setDevinToken(res.devin_token || '')
      setMaxTurns(res.max_turns || 3)
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshConfig()
    void refreshProviders()

    const onChanged = () => {
      void refreshProviders()
    }
    window.addEventListener(MODELS_CATALOG_CHANGED_EVENT, onChanged)
    return () => window.removeEventListener(MODELS_CATALOG_CHANGED_EVENT, onChanged)
  }, [refreshConfig, refreshProviders])

  // 可用的有效 Provider（已启用）
  const enabledProviders = useMemo(
    () => providers.filter((p) => p.enabled !== false),
    [providers],
  )

  // 当前选中 Provider 下的模型列表
  const currentProviderModels = useMemo(() => {
    if (!customProvider) return []
    const target = providers.find((p) => p.id === customProvider)
    return target?.models || []
  }, [providers, customProvider])

  // 自动修正初始选中 provider 与 model
  useEffect(() => {
    if (mode === 'custom') {
      if (!customProvider && enabledProviders.length > 0) {
        const first = enabledProviders[0]
        setCustomProvider(first.id)
        if (first.models.length > 0 && !customModel) {
          setCustomModel(first.models[0].id)
        }
      }
    }
  }, [mode, enabledProviders, customProvider, customModel])

  // 手动按需提取本地 Devin Token
  const handleFetchDevinToken = async () => {
    setSyncingToken(true)
    setSyncMsg(null)
    try {
      const res = await fetchLocalDevinTokenForFastContext()
      if (res.success && res.token) {
        setDevinToken(res.token)
        setSyncMsg({
          type: 'ok',
          text: `已成功读取本机 Devin 登录态 (${res.masked})，点击下方保存即可生效`,
        })
      } else {
        setSyncMsg({
          type: 'err',
          text: res.error || '未在本地检测到有效 Token',
        })
      }
    } catch (e) {
      setSyncMsg({
        type: 'err',
        text: e instanceof Error ? e.message : String(e),
      })
    } finally {
      setSyncingToken(false)
      setTimeout(() => setSyncMsg(null), 5000)
    }
  }

  // 检查脏数据
  const isDirty = useMemo(() => {
    if (!data) return false
    return (
      mode !== data.mode ||
      customProvider !== (data.custom_provider || '') ||
      customModel !== (data.custom_model || '') ||
      devinToken.trim() !== (data.devin_token || '') ||
      maxTurns !== (data.max_turns || 3)
    )
  }, [data, mode, customProvider, customModel, devinToken, maxTurns])

  // 保存配置
  const handleSave = async () => {
    setSaving(true)
    setSaveErr(null)
    setSaveSuccess(false)
    try {
      const res = await updateFastContextSettings({
        mode,
        custom_provider: customProvider.trim(),
        custom_model: customModel.trim(),
        devin_token: devinToken.trim(),
        max_turns: maxTurns,
      })
      setData(res)
      setSaveSuccess(true)
      setTimeout(() => setSaveSuccess(false), 3500)
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <div className="voice-settings-loading">
        <Loader2 className="spinning" size={24} />
        <span>正在加载 Fast Context 配置…</span>
      </div>
    )
  }

  if (loadErr) {
    return (
      <div className="voice-settings-error">
        <AlertCircle size={20} />
        <div className="voice-settings-error-text">
          <strong>加载配置失败</strong>
          <p>{loadErr}</p>
        </div>
        <button type="button" className="btn-secondary btn-sm" onClick={refreshConfig}>
          重试
        </button>
      </div>
    )
  }

  return (
    <div className="voice-settings">
      {/* 极简 Hero 横幅 */}
      <header className="voice-settings-hero" style={{ padding: '16px 20px', marginBottom: 20 }}>
        <div
          className="voice-settings-hero-icon"
          style={{
            background: 'linear-gradient(135deg, rgba(59,130,246,0.15), rgba(99,102,241,0.18))',
            width: 42,
            height: 42,
            borderRadius: 10,
          }}
        >
          <Code2 size={20} color="#3b82f6" />
        </div>
        <div className="voice-settings-hero-text">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h2 style={{ fontSize: 17, fontWeight: 600 }}>Fast Context 代码探索</h2>
            <span className="fc-badge-recommended" style={{ fontSize: 11 }}>
              子代理
            </span>
          </div>
          <p style={{ margin: '4px 0 0', fontSize: 12.5, color: 'var(--text-secondary)' }}>
            多轮语义代码探索引擎，可在对话中自动检索代码库并精准定位实现与定义。
          </p>
        </div>
      </header>

      {/* ── 模块 1：检索推演引擎选择（垂直卡片组，视觉更工整） ── */}
      <section className="voice-settings-section">
        <div className="voice-settings-section-title">
          <Layers size={16} />
          <h3>检索推演引擎</h3>
        </div>

        <div className="fc-modes-stack">
          {/* 选项 1：跟随当前会话模型（首位 · 默认） */}
          <div
            className={`fc-mode-item ${mode === 'session' ? 'active' : ''}`}
            onClick={() => setMode('session')}
          >
            <div className="fc-mode-header">
              <span className={`fc-custom-radio ${mode === 'session' ? 'checked' : ''}`}>
                <span className="fc-custom-radio-dot" />
              </span>
              <div className="fc-mode-info">
                <div className="fc-mode-title-row">
                  <span className="fc-mode-title">跟随当前会话模型</span>
                  <span className="fc-badge-recommended">推荐 · 默认</span>
                </div>
                <p className="fc-mode-desc">
                  直接继承主聊天窗口正在使用的模型（如 Claude Fable 5.1、GPT‑6 Astra、GLM 5.3 等），随对话切换无缝生效，开箱即用。
                </p>
              </div>
            </div>
          </div>

          {/* 选项 2：指定独立模型服务商（次位 · 进阶自选） */}
          <div
            className={`fc-mode-item ${mode === 'custom' ? 'active' : ''}`}
            onClick={() => setMode('custom')}
          >
            <div className="fc-mode-header">
              <span className={`fc-custom-radio ${mode === 'custom' ? 'checked' : ''}`}>
                <span className="fc-custom-radio-dot" />
              </span>
              <div className="fc-mode-info">
                <div className="fc-mode-title-row">
                  <span className="fc-mode-title">指定独立模型服务商</span>
                  <span className="fc-badge-custom">自选模型</span>
                </div>
                <p className="fc-mode-desc">
                  为代码探索挑选专精代码推演的模型（如 DeepSeek-V4.1-Flash、GLM 5.3 Flash 等），独立于聊天会话。
                </p>
              </div>
            </div>

            {/* 自选模型展开配置 */}
            {mode === 'custom' && (
              <div className="fc-mode-subpanel" onClick={(e) => e.stopPropagation()}>
                <div
                  style={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
                    gap: 14,
                  }}
                >
                  {/* 服务商选择 */}
                  <div>
                    <label
                      style={{
                        display: 'block',
                        fontSize: 12.5,
                        fontWeight: 500,
                        marginBottom: 6,
                        color: 'var(--text-color)',
                      }}
                    >
                      模型服务商 (Provider)
                    </label>
                    <select
                      value={customProvider}
                      onChange={(e) => {
                        const nextP = e.target.value
                        setCustomProvider(nextP)
                        const pObj = providers.find((p) => p.id === nextP)
                        if (pObj && pObj.models.length > 0) {
                          setCustomModel(pObj.models[0].id)
                          setCustomModelManual(false)
                        }
                      }}
                      className="voice-input"
                      style={{ width: '100%', height: 36 }}
                    >
                      <option value="">请选择模型服务商…</option>
                      {enabledProviders.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name || p.id} ({p.models.length} 个模型)
                        </option>
                      ))}
                    </select>
                  </div>

                  {/* 模型选择 */}
                  <div>
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        marginBottom: 6,
                      }}
                    >
                      <label style={{ fontSize: 12.5, fontWeight: 500, color: 'var(--text-color)' }}>
                        模型名称 (Model)
                      </label>
                      <button
                        type="button"
                        onClick={() => setCustomModelManual((prev) => !prev)}
                        style={{
                          background: 'none',
                          border: 'none',
                          color: 'var(--accent-color, #3b82f6)',
                          fontSize: 11.5,
                          cursor: 'pointer',
                          padding: 0,
                        }}
                      >
                        {customModelManual ? '选择列表模型' : '手动输入模型 ID'}
                      </button>
                    </div>

                    {customModelManual || currentProviderModels.length === 0 ? (
                      <input
                        type="text"
                        value={customModel}
                        onChange={(e) => setCustomModel(e.target.value)}
                        placeholder="例如 deepseek-chat, qwen2.5-coder-32b-instruct"
                        className="voice-input"
                        style={{ width: '100%', height: 36 }}
                      />
                    ) : (
                      <select
                        value={customModel}
                        onChange={(e) => setCustomModel(e.target.value)}
                        className="voice-input"
                        style={{ width: '100%', height: 36 }}
                      >
                        <option value="">请选择具体模型…</option>
                        {currentProviderModels.map((m) => (
                          <option key={m.id} value={m.id}>
                            {m.name || m.id}
                          </option>
                        ))}
                      </select>
                    )}
                  </div>
                </div>

                {/* 选定状态指示 */}
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    fontSize: 12,
                    color: 'var(--text-secondary)',
                  }}
                >
                  <Cpu size={13} color="#818cf8" />
                  <span>当前生效：</span>
                  <code
                    style={{
                      background: 'var(--bg-card, rgba(255,255,255,0.06))',
                      padding: '2px 8px',
                      borderRadius: 4,
                      border: '1px solid var(--border-color)',
                      fontSize: 11.5,
                    }}
                  >
                    {customProvider ? `[${customProvider}] ` : ''}
                    {customModel || '未选择具体模型'}
                  </code>
                </div>
              </div>
            )}
          </div>

          {/* 选项 3：Devin 官方子代理（末位 · 外部备选） */}
          <div
            className={`fc-mode-item ${mode === 'devin' ? 'active' : ''}`}
            onClick={() => setMode('devin')}
          >
            <div className="fc-mode-header">
              <span className={`fc-custom-radio ${mode === 'devin' ? 'checked' : ''}`}>
                <span className="fc-custom-radio-dot" />
              </span>
              <div className="fc-mode-info">
                <div className="fc-mode-title-row">
                  <span className="fc-mode-title">
                    <Sparkles size={14} color="#94a3b8" />
                    <span>Devin 官方子代理 (Devstral)</span>
                  </span>
                  <span className="fc-badge-backup">实验 · 外部备选</span>
                </div>
                <p className="fc-mode-desc">
                  借用 Devin 官方云端 SWE-grep 深度代码检索子代理（需本机已安装并登录 Devin IDE），仅作为备用方案。
                </p>
              </div>
            </div>

            {/* Devin 凭据展开配置 */}
            {mode === 'devin' && (
              <div className="fc-mode-subpanel" onClick={(e) => e.stopPropagation()}>
                {/* 状态与获取按钮 */}
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    flexWrap: 'wrap',
                    gap: 10,
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    {data?.detected_devin?.installed ? (
                      <span className="search-card-detected-pill" title={data.detected_devin.path}>
                        <ShieldCheck size={13} />
                        已检测到本机 Devin (v{data.detected_devin.ideVersion} / 核心 v{data.detected_devin.extensionVersion})
                      </span>
                    ) : (
                      <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                        未检测到本机 Devin 安装目录（仍支持手动填入 Token）
                      </span>
                    )}
                  </div>

                  <button
                    type="button"
                    className="search-sync-token-btn"
                    onClick={handleFetchDevinToken}
                    disabled={syncingToken}
                    title="按需从本机 Devin SQLite 读取当前已登录的 sessionToken"
                  >
                    <RefreshCw size={12} className={syncingToken ? 'spin' : ''} />
                    <span>{syncingToken ? '读取中…' : '获取本机登录态'}</span>
                  </button>
                </div>

                {/* 同步结果提示 */}
                {syncMsg && (
                  <div className={`search-sync-msg ${syncMsg.type}`}>
                    {syncMsg.type === 'ok' ? <CheckCircle2 size={13} /> : <AlertCircle size={13} />}
                    <span>{syncMsg.text}</span>
                  </div>
                )}

                {/* Token 输入行 */}
                <div className="fc-token-input-row">
                  <input
                    type={showToken ? 'text' : 'password'}
                    className="voice-input search-key-input"
                    value={devinToken}
                    onChange={(e) => setDevinToken(e.target.value)}
                    placeholder="devin-session-token$xxxxxxxxxxxxxxxxxxxx"
                    autoComplete="off"
                    spellCheck={false}
                    style={{ flex: 1, height: 36 }}
                  />
                  <button
                    type="button"
                    className="toolbar-action-btn"
                    onClick={() => setShowToken((prev) => !prev)}
                    title={showToken ? '隐藏' : '显示'}
                    style={{ flexShrink: 0, height: 36, width: 36 }}
                  >
                    {showToken ? <Eye size={14} /> : <EyeOff size={14} />}
                  </button>
                </div>

                <p style={{ margin: 0, fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.4 }}>
                  点击<strong>「获取本机登录态」</strong>可自动读取本机已登录凭据；留空则自动回退至环境变量或搜索引擎已配置凭据。
                </p>
              </div>
            )}
          </div>
        </div>
      </section>

      {/* ── 模块 2：多轮推演上限偏好 ── */}
      <section className="voice-settings-section" style={{ marginTop: 22 }}>
        <div className="voice-settings-section-title">
          <Sliders size={16} />
          <h3>多轮推演上限 (Max Turns)</h3>
        </div>

        <div className="fc-card" style={{ padding: '14px 18px' }}>
          <div className="fc-turns-row">
            <span className="fc-turns-label">单次探索最大轮数：</span>
            <div className="fc-segmented-control">
              {[1, 2, 3, 4, 5].map((turns) => (
                <button
                  key={turns}
                  type="button"
                  className={`fc-segment-item ${maxTurns === turns ? 'active' : ''}`}
                  onClick={() => setMaxTurns(turns)}
                >
                  <span>{turns} 轮</span>
                  {turns === 3 && <span className="fc-segment-badge">推荐</span>}
                </button>
              ))}
            </div>
          </div>
          <p className="fc-turns-hint" style={{ marginTop: 2 }}>
            设定 Fast Context 智能体的多轮推演循环上限。通常 2~3 轮已能精准定位全部关键符号与代码区间；若项目规模庞大或代码层级较深，可适度调高至 4~5 轮。
          </p>
        </div>
      </section>

      {/* ── 底部保存栏（稳重内联布局：保存后按钮直接置灰不可点，右侧常显说明） ── */}
      <div className="fc-save-bar">
        <button
          type="button"
          className="fc-save-btn"
          onClick={handleSave}
          disabled={saving || !isDirty}
        >
          {saving ? (
            <>
              <Loader2 size={14} className="spinning" />
              <span>保存中…</span>
            </>
          ) : (
            <span>保存配置</span>
          )}
        </button>

        {/* 状态与提示（水平居右并列，文字稳重提示） */}
        <div className="fc-save-hint-wrap">
          {isDirty && !saving && !saveSuccess && (
            <span className="fc-save-dirty-hint">
              <span className="fc-dirty-dot" />
              <span>有未保存的配置更改</span>
            </span>
          )}
          {saveSuccess && (
            <span className="fc-save-success-hint">
              <CheckCircle2 size={14} />
              <span>配置已保存并即时生效</span>
            </span>
          )}
          {saveErr && (
            <span className="fc-save-error-hint">
              <AlertCircle size={14} />
              <span>保存失败：{saveErr}</span>
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
