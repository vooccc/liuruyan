import { useCallback, useEffect, useState } from 'react'
import {
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Code2,
  ExternalLink,
  Eye,
  EyeOff,
  Globe,
  Languages,
  Loader2,
  Mic,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Volume2,
} from 'lucide-react'
import {
  getVoiceConfig,
  updateVoiceConfig,
  type LocalWhisperSize,
  type VoiceConfig,
  type VoiceConfigResponse,
} from '../../api/voice'
import {
  getSearchSettings,
  updateSearchSettings,
  fetchLocalDevinToken,
  type SearchSettingsResponse,
} from '../../api/search'
import AuxModelsFooter from './AuxModelsFooter'
import TTSPane from './TTSPane'
import FastContextPane from './FastContextPane'

const LOCAL_MODEL_OPTIONS: Array<{ value: LocalWhisperSize; label: string; size: string; note: string }> = [
  { value: 'tiny', label: 'tiny', size: '~75MB', note: '速度最快，准度低，适合英文' },
  { value: 'base', label: 'base', size: '~145MB', note: '中文够用，准度中等' },
  { value: 'small', label: 'small', size: '~245MB', note: 'CPU 推荐，中文准度良好' },
  { value: 'medium', label: 'medium', size: '~770MB', note: '建议 8GB+ 内存或 GPU' },
  { value: 'large-v3', label: 'large-v3', size: '~1.5GB', note: '需 GPU 或 16GB+ 内存，最准' },
]

/**
 * 设置 →「辅助功能」tab（原「语音输入」，2026-05-27 改名）。
 *
 * 二级侧栏结构：
 *   - 翻译（思考块翻译模型，未来扩展话题命名 / 记忆压缩等辅助 LLM 任务）
 *   - 语音转写（disabled / whisper_api / local_whisper 三引擎切换）
 *   - 语音输出（2026-05-29 加：TTS，对应 backend/tts/，零污染挂载）
 *
 * 子 tab 选择持久化到 localStorage（key=`lr:settings-aux-subtab`）。
 * 三个 pane 各自独立加载数据，懒加载——点开"辅助功能"不会立即拉语音配置。
 *
 * 语音保存策略：每次修改立即 PUT 到后端（无 dirty/save 双态）。后端写完后 dispatch
 * 'voice-config-updated' 自定义事件，让 Chat.tsx 重拉配置 → 麦克风按钮即时显隐。
 *
 * 历史：
 *   - 辅助模型原本挂在「模型服务」tab 底部，2026-05-27 跟语音并到这里，
 *     主因「模型服务」tab 已经够长，且辅助模型属于「跨场景小工具配置」语义上跟
 *     语音转写更接近。改造为二级侧栏（同日）方便后续扩展更多辅助功能。
 *   - 2026-05-29 加 TTS 子标签：与语音转写形成完整"耳朵 + 嘴"闭环。
 *     设计文档：_docs/handover_tts_design_2026-05-29.md
 */

type AuxSubTab = 'translate' | 'voice' | 'tts' | 'search' | 'fast_context'
const AUX_SUBTAB_KEY = 'lr:settings-aux-subtab'

interface SubTabDef {
  id: AuxSubTab
  label: string
  icon: React.ReactNode
  desc: string
}

const SUB_TABS: SubTabDef[] = [
  {
    id: 'translate',
    label: '翻译',
    icon: <Languages size={16} />,
    desc: '思考块翻译模型（未来还会有话题命名 / 记忆压缩等）',
  },
  {
    id: 'voice',
    label: '语音转写',
    icon: <Mic size={16} />,
    desc: '麦克风录音 + Whisper 转文字（云端 / 本地）',
  },
  {
    id: 'tts',
    label: '语音输出',
    icon: <Volume2 size={16} />,
    desc: 'TTS：把回复念出来。基于 <say> 标签 + 三档策略，避免长篇朗读',
  },
  {
    id: 'search',
    label: '搜索引擎',
    icon: <Search size={16} />,
    desc: '联网搜索 key（Tavily）—— 柳如烟全局搜索与内容模块「联网搜资料」共用',
  },
  {
    id: 'fast_context',
    label: '代码探索',
    icon: <Code2 size={16} />,
    desc: 'Fast Context：基于多轮推演的语义代码检索，支持自选模型与 Devin 凭据',
  },
]

export default function VoiceSettings() {
  const [subTab, setSubTab] = useState<AuxSubTab>(() => {
    try {
      const saved = localStorage.getItem(AUX_SUBTAB_KEY)
      if (
        saved === 'voice' ||
        saved === 'translate' ||
        saved === 'tts' ||
        saved === 'search' ||
        saved === 'fast_context'
      ) {
        return saved
      }
    } catch { /* localStorage disabled */ }
    return 'translate'
  })

  useEffect(() => {
    try { localStorage.setItem(AUX_SUBTAB_KEY, subTab) } catch { /* ignore */ }
  }, [subTab])

  return (
    <div className="aux-settings">
      <aside className="aux-settings-subnav" aria-label="辅助功能分类">
        <nav>
          {SUB_TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`aux-settings-subnav-item ${subTab === item.id ? 'active' : ''}`}
              onClick={() => setSubTab(item.id)}
              title={item.desc}
            >
              <span className="aux-settings-subnav-icon">{item.icon}</span>
              <span className="aux-settings-subnav-label">{item.label}</span>
            </button>
          ))}
        </nav>
      </aside>
      <section className="aux-settings-pane" aria-label="辅助功能内容">
        {subTab === 'translate' && <TranslatePane />}
        {subTab === 'voice' && <VoiceTranscriptionPane />}
        {subTab === 'tts' && <TTSPane />}
        {subTab === 'search' && <SearchEnginePane />}
        {subTab === 'fast_context' && <FastContextPane />}
      </section>
    </div>
  )
}

/* ── 翻译 pane ───────────────────────────────────────────────── */

function TranslatePane() {
  return (
    <div className="voice-settings">
      <header className="voice-settings-hero">
        <div className="voice-settings-hero-icon">
          <Languages size={20} />
        </div>
        <div className="voice-settings-hero-text">
          <h2>翻译</h2>
          <p>
            指定思考块「翻译」按钮使用的模型。<strong>建议挑一个非 reasoning 的 fast model</strong>，
            避免主对话是 reasoning model 时翻译跑题；留空则跟随当前会话 model，后端会自动关 reasoning 兜底。
          </p>
        </div>
      </header>
      <AuxModelsFooter />
    </div>
  )
}

/* ── 搜索引擎 pane ───────────────────────────────────────────── */

interface EngineMeta {
  key: string
  title: string
  tag: string
  subtitle: string
  hint: string
  urlText?: string
  url?: string
  placeholder: string
  icon: 'devin' | 'exa' | 'tavily'
}

const ENGINE_METAS: Record<string, EngineMeta> = {
  devin: {
    key: 'devin',
    title: 'Devin',
    tag: '代码 / 技术高精搜索',
    subtitle: '基于 Devin 官方云端服务的高精技术与文档搜索（已对齐本机客户端）',
    placeholder: 'devin-session-token$xxxxxxxxxxxxxxxxxxxx',
    hint: '支持个人 sessionToken（形如 devin-session-token$...）。若已在电脑上登录 Devin，可通过会话提取并粘贴在此。',
    icon: 'devin',
  },
  exa: {
    key: 'exa',
    title: 'Exa',
    tag: '每月 $10 免费额度',
    subtitle: '专为 AI 打造的神经语义搜索引擎，开源技术与研发排错命中极准',
    placeholder: 'exa-xxxxxxxxxxxxxxxxxxxx',
    hint: '去 exa.ai 免费注册（新用户送 $20，每月赠送 $10 额度约 1400 次搜索，免绑信用卡）。',
    urlText: 'exa.ai',
    url: 'https://exa.ai',
    icon: 'exa',
  },
  tavily: {
    key: 'tavily',
    title: 'Tavily',
    tag: '每月 1000 次免费',
    subtitle: '通用网页搜索与中文增强，结果纯净、无广告骚扰',
    placeholder: 'tvly-xxxxxxxxxxxxxxxxxxxx',
    hint: '去 app.tavily.com 免费注册（每月免费 1000 次调用额度，免绑信用卡）。',
    urlText: 'app.tavily.com',
    url: 'https://app.tavily.com',
    icon: 'tavily',
  },
}

function SearchEnginePane() {
  const [data, setData] = useState<SearchSettingsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)
  const [saveSuccess, setSaveSuccess] = useState(false)

  // 手动获取本地 Devin 凭据状态
  const [syncingToken, setSyncingToken] = useState(false)
  const [syncMsg, setSyncMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null)

  const handleSyncDevinToken = async () => {
    setSyncingToken(true)
    setSyncMsg(null)
    try {
      const res = await fetchLocalDevinToken()
      if (res.success && res.token) {
        setKeys((prev) => ({ ...prev, devin: res.token! }))
        setSyncMsg({ type: 'ok', text: `已成功读取本机 Devin 登录态 (${res.masked})，点击下方保存即可应用` })
      } else {
        setSyncMsg({ type: 'err', text: res.error || '未检测到有效 Token' })
      }
    } catch (e) {
      setSyncMsg({ type: 'err', text: e instanceof Error ? e.message : String(e) })
    } finally {
      setSyncingToken(false)
      setTimeout(() => setSyncMsg(null), 5000)
    }
  }

  // 临时编辑状态
  const [keys, setKeys] = useState<Record<string, string>>({
    devin: '',
    exa: '',
    tavily: '',
  })
  const [reveals, setReveals] = useState<Record<string, boolean>>({
    devin: false,
    exa: false,
    tavily: false,
  })
  const [enabled, setEnabled] = useState<Record<string, boolean>>({
    devin: true,
    exa: true,
    tavily: true,
  })
  const [priority, setPriority] = useState<string[]>(['devin', 'exa', 'tavily'])

  const refresh = useCallback(async () => {
    setLoading(true)
    setLoadErr(null)
    try {
      const r = await getSearchSettings()
      setData(r)
      const engs = r.engines || {}
      setKeys({
        devin: engs.devin?.api_key || '',
        exa: engs.exa?.api_key || '',
        tavily: engs.tavily?.api_key || r.tavily?.api_key || '',
      })
      setEnabled({
        devin: engs.devin?.enabled ?? true,
        exa: engs.exa?.enabled ?? true,
        tavily: engs.tavily?.enabled ?? true,
      })
      if (Array.isArray(r.priority_order) && r.priority_order.length > 0) {
        // 确保包含所有支持的引擎
        const base = [...r.priority_order]
        for (const k of ['devin', 'exa', 'tavily']) {
          if (!base.includes(k)) base.push(k)
        }
        setPriority(base)
      } else {
        setPriority(['devin', 'exa', 'tavily'])
      }
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const moveUp = (index: number) => {
    if (index <= 0) return
    setPriority((prev) => {
      const next = [...prev]
      const temp = next[index - 1]
      next[index - 1] = next[index]
      next[index] = temp
      return next
    })
  }

  const moveDown = (index: number) => {
    if (index >= priority.length - 1) return
    setPriority((prev) => {
      const next = [...prev]
      const temp = next[index + 1]
      next[index + 1] = next[index]
      next[index] = temp
      return next
    })
  }

  const toggleEnabled = (key: string) => {
    setEnabled((prev) => ({
      ...prev,
      [key]: !prev[key],
    }))
  }

  // 脏检查
  const isDirty = Boolean(
    data && (
      keys.devin.trim() !== (data.engines?.devin?.api_key || '') ||
      keys.exa.trim() !== (data.engines?.exa?.api_key || '') ||
      keys.tavily.trim() !== (data.engines?.tavily?.api_key || data.tavily?.api_key || '') ||
      enabled.devin !== (data.engines?.devin?.enabled ?? true) ||
      enabled.exa !== (data.engines?.exa?.enabled ?? true) ||
      enabled.tavily !== (data.engines?.tavily?.enabled ?? true) ||
      JSON.stringify(priority) !== JSON.stringify(data.priority_order || ['devin', 'exa', 'tavily'])
    )
  )

  const save = useCallback(async () => {
    setSaving(true)
    setSaveErr(null)
    setSaveSuccess(false)
    try {
      const res = await updateSearchSettings({
        tavily_api_key: keys.tavily.trim(),
        exa_api_key: keys.exa.trim(),
        devin_api_key: keys.devin.trim(),
        enabled_engines: enabled,
        priority_order: priority,
      })
      setData(res)
      setSaveSuccess(true)
      setTimeout(() => setSaveSuccess(false), 3500)
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }, [keys, enabled, priority])

  if (loading) {
    return (
      <div style={{ padding: 24, display: 'flex', alignItems: 'center', gap: 8 }}>
        <Loader2 size={16} className="spin" /> 加载搜索引擎配置…
      </div>
    )
  }

  if (loadErr || !data) {
    return (
      <div style={{ padding: 24, color: 'var(--accent-red)' }}>
        <AlertCircle size={16} /> 加载失败：{loadErr ?? '未知错误'}
        <button type="button" className="toolbar-action-btn" onClick={refresh} style={{ marginLeft: 12 }}>
          重试
        </button>
      </div>
    )
  }

  // 计算当前有效执行链
  const activeChain = priority.filter((k) => enabled[k])

  return (
    <div className="voice-settings">
      <header className="voice-settings-hero">
        <div className="voice-settings-hero-icon">
          <Search size={20} />
        </div>
        <div className="voice-settings-hero-text">
          <h2>搜索引擎</h2>
          <p>
            配置与编排 <strong>Devin</strong>、<strong>Exa</strong>、<strong>Tavily</strong> 三大 AI 搜索引擎。
            通过上下箭头自定义调用优先级；任一引擎失败或额度耗尽时，系统将<strong>自动沿流水线无缝降级</strong>，末尾恒有百度与 Bing 兜底。
          </p>
        </div>
      </header>

      {/* 实时级联流水线预览 */}
      <section className="search-pipeline-box">
        <div className="search-pipeline-title">
          <ShieldCheck size={15} style={{ color: 'var(--primary-color)' }} />
          <span>当前搜索级联流水线（按先后尝试）</span>
        </div>
        <div className="search-pipeline-flow">
          {activeChain.map((k, idx) => {
            const meta = ENGINE_METAS[k] || { title: k }
            const isFirst = idx === 0
            const cfg = data.engines?.[k]
            const isConfigured = cfg?.configured
            return (
              <div key={k} className="search-pipeline-step">
                <div className={`search-step-pill ${isFirst ? 'first' : ''} ${!isConfigured ? 'unconfigured' : ''}`}>
                  <span className="step-num">#{idx + 1}</span>
                  <span className="step-name">{meta.title}</span>
                  <span className="step-tag">{isFirst ? '首选' : '备选'}</span>
                  {!isConfigured && <span className="step-warn-dot" title="尚未配置密钥，调用时将跳过此引擎" />}
                </div>
                <ArrowRight size={13} className="search-step-arrow" />
              </div>
            )
          })}
          <div className="search-step-pill fallback">
            <span className="step-name">🛡️ 百度 / Bing</span>
            <span className="step-tag">永久安全气囊</span>
          </div>
        </div>
      </section>

      {/* 引擎列表（根据 priority 排序渲染） */}
      <div className="search-engines-list">
        {priority.map((engKey, index) => {
          const meta = ENGINE_METAS[engKey]
          if (!meta) return null
          const isEnabled = enabled[engKey] ?? true
          const cfg = data.engines?.[engKey]
          const isDevin = engKey === 'devin'
          const localDevin = data.detected_devin

          return (
            <div key={engKey} className={`search-engine-card ${!isEnabled ? 'disabled' : ''}`}>
              {/* 排序手柄 */}
              <div className="search-card-rank-col">
                <div className="search-card-rank-num">#{index + 1}</div>
                <div className="search-card-rank-btns">
                  <button
                    type="button"
                    className="search-rank-btn"
                    onClick={() => moveUp(index)}
                    disabled={index === 0}
                    title="上移（优先使用）"
                  >
                    <ChevronUp size={14} />
                  </button>
                  <button
                    type="button"
                    className="search-rank-btn"
                    onClick={() => moveDown(index)}
                    disabled={index === priority.length - 1}
                    title="下移"
                  >
                    <ChevronDown size={14} />
                  </button>
                </div>
              </div>

              {/* 卡片主体 */}
              <div className="search-card-main">
                <div className="search-card-header">
                  <div className="search-card-title-wrap">
                    {meta.icon === 'devin' && <Code2 size={16} className="search-eng-icon devin" />}
                    {meta.icon === 'exa' && <Sparkles size={16} className="search-eng-icon exa" />}
                    {meta.icon === 'tavily' && <Globe size={16} className="search-eng-icon tavily" />}
                    <h3 className="search-card-title">{meta.title}</h3>
                    <span className="search-card-badge">{meta.tag}</span>
                    {isDevin && localDevin?.installed && (
                      <span className="search-card-detected-pill" title={localDevin.path}>
                        ✓ 已对齐本机 v{localDevin.ideVersion} / 核心 v{localDevin.extensionVersion}
                      </span>
                    )}
                  </div>

                  {/* 启用开关 */}
                  <button
                    type="button"
                    className={`search-card-toggle ${isEnabled ? 'on' : 'off'}`}
                    onClick={() => toggleEnabled(engKey)}
                    title={isEnabled ? '点击停用' : '点击启用'}
                  >
                    <span className="toggle-indicator" />
                    <span className="toggle-text">{isEnabled ? '已启用' : '已停用'}</span>
                  </button>
                </div>

                <div className="search-card-sub">{meta.subtitle}</div>

                {/* 密钥输入行 */}
                <div className="search-card-input-row">
                  <input
                    className="voice-input search-key-input"
                    type={reveals[engKey] ? 'text' : 'password'}
                    value={keys[engKey]}
                    onChange={(e) => setKeys((prev) => ({ ...prev, [engKey]: e.target.value }))}
                    placeholder={meta.placeholder}
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <button
                    type="button"
                    className="toolbar-action-btn"
                    onClick={() => setReveals((prev) => ({ ...prev, [engKey]: !prev[engKey] }))}
                    title={reveals[engKey] ? '隐藏' : '显示'}
                    style={{ flexShrink: 0 }}
                  >
                    {reveals[engKey] ? <Eye size={15} /> : <EyeOff size={15} />}
                  </button>

                  {isDevin && localDevin?.installed && (
                    <button
                      type="button"
                      className="search-sync-token-btn"
                      onClick={handleSyncDevinToken}
                      disabled={syncingToken}
                      title="点击手动读取本机 Devin 当前登录的 Session Token 并填入"
                    >
                      <RefreshCw size={12} className={syncingToken ? 'spin' : ''} />
                      <span>{syncingToken ? '读取中…' : '获取本机登录态'}</span>
                    </button>
                  )}
                </div>

                {isDevin && syncMsg && (
                  <div className={`search-sync-msg ${syncMsg.type}`}>
                    {syncMsg.type === 'ok' ? <CheckCircle2 size={13} /> : <AlertCircle size={13} />}
                    <span>{syncMsg.text}</span>
                  </div>
                )}

                {/* 提示与状态 */}
                <div className="search-card-footer">
                  <div className="search-card-status">
                    {cfg?.configured ? (
                      <span className="voice-settings-status ok">
                        <CheckCircle2 size={13} /> 已配置：<code>{cfg.masked}</code>
                      </span>
                    ) : cfg?.env_fallback ? (
                      <span className="voice-settings-status">
                        <CheckCircle2 size={13} style={{ color: 'var(--accent-green)' }} /> 已通过环境变量 / .env 配置（在此填写可覆盖）
                      </span>
                    ) : (
                      <span className="voice-settings-status" style={{ opacity: 0.7 }}>
                        <AlertCircle size={13} /> 未配置密钥
                      </span>
                    )}
                  </div>

                  <div className="search-card-hint">
                    {meta.hint}
                    {meta.url && (
                      <a
                        href={meta.url}
                        target="_blank"
                        rel="noreferrer"
                        className="search-card-link"
                      >
                        {meta.urlText} <ExternalLink size={11} />
                      </a>
                    )}
                  </div>
                </div>
              </div>
            </div>
          )
        })}
      </div>

      {/* 底部统一保存栏 */}
      <div className="search-save-toolbar">
        <button
          type="button"
          className="search-save-btn"
          onClick={save}
          disabled={saving || !isDirty}
        >
          {saving ? <Loader2 size={14} className="spin" /> : '保存搜索引擎设置'}
        </button>

        {isDirty && <span className="search-dirty-hint">有未保存的调整（优先级顺序或密钥修改）</span>}
        {saveSuccess && (
          <span className="search-save-toast ok">
            <CheckCircle2 size={14} /> 搜索引擎配置与调用顺序已保存并立即生效！
          </span>
        )}
        {saveErr && (
          <span className="search-save-toast err">
            <AlertCircle size={14} /> 保存失败：{saveErr}
          </span>
        )}
      </div>
    </div>
  )
}


/* ── 语音转写 pane ───────────────────────────────────────────── */

function VoiceTranscriptionPane() {
  const [data, setData] = useState<VoiceConfigResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setLoadErr(null)
    try {
      const result = await getVoiceConfig()
      setData(result)
    } catch (e) {
      setLoadErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const updateConfig = useCallback(
    async (patch: Partial<VoiceConfig>) => {
      if (!data) return
      setSaving(true)
      setSaveErr(null)
      try {
        const result = await updateVoiceConfig(patch)
        setData((prev) => (prev ? { ...prev, config: result.config } : prev))
        // 通知 Chat.tsx 重拉配置（麦克风按钮即时显隐）
        window.dispatchEvent(new CustomEvent('voice-config-updated'))
      } catch (e) {
        setSaveErr(e instanceof Error ? e.message : String(e))
      } finally {
        setSaving(false)
      }
    },
    [data],
  )

  if (loading) {
    return (
      <div style={{ padding: 24 }}>
        <Loader2 size={16} className="spin" /> 加载语音配置…
      </div>
    )
  }

  if (loadErr || !data) {
    return (
      <div style={{ padding: 24, color: 'var(--accent-red)' }}>
        <AlertCircle size={16} /> 加载失败：{loadErr ?? '未知错误'}
        <button type="button" className="toolbar-action-btn" onClick={refresh} style={{ marginLeft: 12 }}>
          重试
        </button>
      </div>
    )
  }

  const { config, eligible_providers, local_whisper_status, recommended_models } = data
  const validProviders = eligible_providers.filter((p) => p.has_api_key)

  return (
    <div className="voice-settings">
      <header className="voice-settings-hero">
        <div className="voice-settings-hero-icon">
          <Mic size={20} />
        </div>
        <div className="voice-settings-hero-text">
          <h2>语音转写</h2>
          <p>
            按住 <kbd>Ctrl + Shift + M</kbd> 或点击输入框右下角的麦克风开始录音；松开后自动转写到输入框。
            支持云端 Whisper API（推荐国内 SiliconFlow）和完全离线的本地 Whisper。
          </p>
        </div>
      </header>

      <section className="voice-settings-section">
        <h3>转写引擎</h3>
        <div className="voice-radio-group">
          <RadioCard
            checked={config.provider_type === 'disabled'}
            onChange={() => updateConfig({ provider_type: 'disabled' })}
            title="关闭"
            desc="输入框不显示麦克风按钮，快捷键也不生效"
          />
          <RadioCard
            checked={config.provider_type === 'whisper_api'}
            onChange={() => updateConfig({ provider_type: 'whisper_api' })}
            title="Whisper API（推荐）"
            desc="走 OpenAI 兼容端点。国内推荐 SiliconFlow（FunAudioLLM/SenseVoiceSmall），中文优化"
          />
          <RadioCard
            checked={config.provider_type === 'local_whisper'}
            onChange={() => updateConfig({ provider_type: 'local_whisper' })}
            title="本地 Whisper"
            desc="完全离线，零联网，零费用。需安装 ffmpeg + faster-whisper"
          />
        </div>
      </section>

      {config.provider_type === 'whisper_api' && (
        <ApiPanel
          config={config}
          providers={validProviders}
          recommendedModels={recommended_models}
          onUpdate={updateConfig}
          eligibleCount={eligible_providers.length}
        />
      )}

      {config.provider_type === 'local_whisper' && (
        <LocalPanel config={config} status={local_whisper_status} onUpdate={updateConfig} />
      )}

      <footer className="voice-settings-footer">
        {saving ? (
          <span className="voice-settings-status">
            <Loader2 size={13} className="spin" /> 保存中…
          </span>
        ) : saveErr ? (
          <span className="voice-settings-status err">
            <AlertCircle size={13} /> 保存失败：{saveErr}
          </span>
        ) : (
          <span className="voice-settings-status ok">
            <CheckCircle2 size={13} /> 已与服务端同步（修改即时生效）
          </span>
        )}
      </footer>
    </div>
  )
}

/* ── 子组件 ──────────────────────────────────────────────────── */

function RadioCard({
  checked,
  onChange,
  title,
  desc,
}: {
  checked: boolean
  onChange: () => void
  title: string
  desc: string
}) {
  return (
    <button type="button" className={`voice-radio-card ${checked ? 'checked' : ''}`} onClick={onChange}>
      <span className="voice-radio-dot" aria-hidden>
        {checked ? <span className="voice-radio-dot-inner" /> : null}
      </span>
      <span className="voice-radio-text">
        <span className="voice-radio-title">{title}</span>
        <span className="voice-radio-desc">{desc}</span>
      </span>
    </button>
  )
}

function ApiPanel({
  config,
  providers,
  recommendedModels,
  onUpdate,
  eligibleCount,
}: {
  config: VoiceConfig
  providers: Array<{ id: string; name: string }>
  recommendedModels: Record<string, string>
  onUpdate: (patch: Partial<VoiceConfig>) => void
  eligibleCount: number
}) {
  return (
    <section className="voice-settings-section">
      <h3>Provider</h3>
      {providers.length === 0 ? (
        <div className="voice-settings-warn">
          <AlertCircle size={14} />
          {eligibleCount === 0
            ? '没有可用的 OpenAI 兼容 provider，请先到「模型服务」配置。'
            : '没有 provider 配置了 API Key，请先到「模型服务」填写 Key。'}
        </div>
      ) : (
        <select
          className="voice-select"
          value={config.provider_id}
          onChange={(e) => {
            const pid = e.target.value
            // 切 provider 时自动填推荐模型；用户已手填则不覆盖
            const recommended = recommendedModels[pid]
            const patch: Partial<VoiceConfig> = { provider_id: pid }
            if (!config.model && recommended) patch.model = recommended
            onUpdate(patch)
          }}
        >
          <option value="">请选择 provider…</option>
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      )}

      <h3 style={{ marginTop: 16 }}>模型</h3>
      <input
        className="voice-input"
        type="text"
        value={config.model}
        onChange={(e) => onUpdate({ model: e.target.value })}
        placeholder="例如：FunAudioLLM/SenseVoiceSmall"
      />
      <p className="voice-settings-hint">
        推荐值：
        <code>FunAudioLLM/SenseVoiceSmall</code>（SiliconFlow，中文优）·{' '}
        <code>whisper-large-v3-turbo</code>（Groq，最快）·{' '}
        <code>whisper-1</code>（OpenAI 官方）·{' '}
        <code>paraformer-realtime-v2</code>（DashScope，中文最强）
      </p>
    </section>
  )
}

function LocalPanel({
  config,
  status,
  onUpdate,
}: {
  config: VoiceConfig
  status: { available: boolean; ffmpeg_installed: boolean; faster_whisper_installed: boolean }
  onUpdate: (patch: Partial<VoiceConfig>) => void
}) {
  return (
    <section className="voice-settings-section">
      <h3>模型大小</h3>
      <div className="voice-radio-group">
        {LOCAL_MODEL_OPTIONS.map((opt) => (
          <RadioCard
            key={opt.value}
            checked={config.local_model_size === opt.value}
            onChange={() => onUpdate({ local_model_size: opt.value })}
            title={`${opt.label} · ${opt.size}`}
            desc={opt.note}
          />
        ))}
      </div>

      <h3 style={{ marginTop: 16 }}>本地依赖</h3>
      <ul className="voice-deps-list">
        <li>
          {status.ffmpeg_installed ? (
            <CheckCircle2 size={14} style={{ color: 'var(--accent-green)' }} />
          ) : (
            <AlertCircle size={14} style={{ color: 'var(--accent-red)' }} />
          )}
          <span>ffmpeg（系统级，需在 PATH 中）</span>
        </li>
        <li>
          {status.faster_whisper_installed ? (
            <CheckCircle2 size={14} style={{ color: 'var(--accent-green)' }} />
          ) : (
            <AlertCircle size={14} style={{ color: 'var(--accent-red)' }} />
          )}
          <span>faster-whisper（Python 包）</span>
        </li>
      </ul>

      {!status.available && (
        <pre className="voice-deps-install">
{`# Windows
winget install ffmpeg
pip install faster-whisper

# macOS
brew install ffmpeg
pip install faster-whisper

# Linux
sudo apt install ffmpeg
pip install faster-whisper

# 装好后重启后端进程使其检测到`}
        </pre>
      )}
    </section>
  )
}
