/**
 * Pro版「算力账号 / 插件配置」双列导航工作区面板。
 *
 * 采用高密度成熟桌面端设计范式（紧凑标题栏、独立滚动区、直观账号池、按需配额监控与模型网格）。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronDown,
  Code2,
  Copy,
  Cpu,
  ExternalLink,
  Flame,
  Globe,
  Info,
  KeyRound,
  Loader2,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Star,
  Terminal,
  Trash2,
  UserPlus,
  Zap,
} from 'lucide-react'
import { getRuntime } from '../../runtime'
import {
  deleteAccount,
  getAuthUrl,
  getComputeOverview,
  getEnabledModels,
  listAccounts,
  listChannelModels,
  manualExchangeToken,
  setEnabledModels,
  setPrimaryAccount,
  testConnection,
  toggleAccount,
} from './api'
import type {
  ComputeAccountItem,
  ComputeProviderOverview,
  ConnectionTestResult,
  ModelCatalogItem,
} from './types'
import { useAccountQuota, AccountQuotaBars } from './AccountQuotaCard'
import { MODELS_CATALOG_CHANGED_EVENT } from '../../api/chat'
import './compute-accounts.css'

type ComputeSubTab = 'google' | 'grok' | 'codex'
const COMPUTE_SUBTAB_KEY = 'lr:settings-compute-subtab'

const RECOMMENDED_PRO_MODELS = [
  'gemini-3.8-flash-medium',
  'gemini-3.8-flash-high',
  'gemini-3.7-flash-medium',
  'gemini-3.6-flash-medium',
  'gemini-3.1-pro-low',
  'gemini-3.1-pro-high',
  'claude-sonnet-4-6',
  'claude-opus-4-6-thinking',
  'gpt-oss-120b-medium',
]

interface ChannelMeta {
  id: ComputeSubTab
  name: string
  fullName: string
  subtitle: string
  tag: string
}

const CHANNELS_META: Record<ComputeSubTab, ChannelMeta> = {
  google: {
    id: 'google',
    name: 'Google',
    fullName: 'Google Antigravity',
    subtitle: '官方 OAuth2 直连 · 14 款核心思考分级模型',
    tag: '官方直连 · 14 款模型',
  },
  grok: {
    id: 'grok',
    name: 'Grok',
    fullName: 'xAI Grok',
    subtitle: 'xAI 官方账号直连 · Grok 2 / 3 前沿推演',
    tag: 'xAI 官方直连',
  },
  codex: {
    id: 'codex',
    name: 'Codex',
    fullName: 'OpenAI Codex',
    subtitle: 'OpenAI 代码生成 · 终端智能体推演引擎',
    tag: '代码智能体引擎',
  },
}

interface AccountMasterCardItemProps {
  acc: ComputeAccountItem
  accIdx: number
  subTab: ComputeSubTab
  activeMeta: ChannelMeta
  avatarError?: boolean
  onAvatarError: () => void
  settingPrimaryId: string | null
  deletingAccountId: string | null
  togglingPoolId: string | null
  handleTogglePool: (accountId: string, enabled: boolean) => Promise<void>
  handleSetPrimary: (channelId: string, accountId: string) => Promise<void>
  handleDeleteAccount: (channelId: string, accountId: string, email?: string) => Promise<void>
  expandedCardModels: Record<string, boolean>
  toggleCardModelsExpanded: (accountId: string) => void
  enabledModelKeys: Set<string>
  googleModels: ModelCatalogItem[]
  modelsLoading: boolean
  saveStatus: string | null
  modelSearch: string
  setModelSearch: (v: string) => void
  handleSelectRecommendedOnly: () => void
  handleSelectAll: () => void
  handleSelectNone: () => void
  filteredGoogleModels: ModelCatalogItem[]
  toggleModel: (modelId: string) => void
}

function AccountMasterCardItem({
  acc,
  accIdx,
  subTab,
  activeMeta,
  avatarError,
  onAvatarError,
  settingPrimaryId,
  deletingAccountId,
  togglingPoolId,
  handleTogglePool,
  handleSetPrimary,
  handleDeleteAccount,
  expandedCardModels,
  toggleCardModelsExpanded,
  enabledModelKeys,
  googleModels,
  modelsLoading,
  saveStatus,
  modelSearch,
  setModelSearch,
  handleSelectRecommendedOnly,
  handleSelectAll,
  handleSelectNone,
  filteredGoogleModels,
  toggleModel,
}: AccountMasterCardItemProps) {
  const isPrimary = accIdx === 0
  const isCooling = acc.status === 'cooling'
  const isInPool = acc.enabled !== false

  const {
    quota,
    loading: quotaLoading,
    refreshing: quotaRefreshing,
    error: quotaError,
    cooldownSec,
    lastUpdatedText,
    loadQuota,
  } = useAccountQuota(subTab === 'google' ? 'google' : subTab, acc.id)

  return (
    <article
      key={acc.id}
      className={`ca-account-master-card ${!isInPool ? 'is-disabled' : ''} ${isPrimary && isInPool ? 'is-primary' : ''}`}
    >
      {/* 1. 唯一合并顶栏：左侧身份与防风控徽标 + 右侧全部操作按钮流 */}
      <div className="ca-account-card-header">
        <div className="ca-account-card-left">
          {acc.picture && !avatarError ? (
            <img
              src={acc.picture}
              alt=""
              className="ca-account-avatar-img"
              onError={onAvatarError}
            />
          ) : (
            <div className="ca-account-avatar-char">
              {(acc.name || acc.email || 'A').charAt(0).toUpperCase()}
            </div>
          )}
          <div className="ca-account-identity-info">
            <div className="ca-account-title-row">
              <span className="ca-account-display-name">
                {acc.name || `${activeMeta.name} 账号`}
              </span>
              {isInPool ? (
                isPrimary ? (
                  <span className="ca-role-badge primary" title="当前主力首选调度节点">
                    <Star size={10} fill="currentColor" />
                    主力节点
                  </span>
                ) : (
                  <button
                    type="button"
                    className="ca-role-badge standby is-clickable"
                    title="备用容灾节点 · 点击设为主力"
                    onClick={() => handleSetPrimary(subTab, acc.id)}
                    disabled={settingPrimaryId === acc.id}
                  >
                    {settingPrimaryId === acc.id ? (
                      <Loader2 size={10} className="animate-spin" />
                    ) : (
                      <Star size={10} />
                    )}
                    {settingPrimaryId === acc.id ? '设置中' : '备用节点'}
                  </button>
                )
              ) : (
                <span className="ca-role-badge paused" title="已从调度池暂停">
                  已暂停调度
                </span>
              )}
              {isCooling && (
                <span className="ca-role-badge cooling" title="由于限流避让中">
                  ⏳ 冷却避让中
                </span>
              )}
            </div>
            <span className="ca-account-email-sub">{acc.email}</span>
          </div>
        </div>

        {/* 顶部右侧整合动作按钮区（从内层卡片剥离提升至顶栏，同一水平线对齐） */}
        <div className="ca-account-card-right">
          {/* 配额更新时间 */}
          {lastUpdatedText && (
            <span className="ca-quota-time-tag" title={`配额快照时间: ${lastUpdatedText}`}>
              {lastUpdatedText}
            </span>
          )}

          {/* 配额查询/刷新按钮 */}
          {subTab === 'google' && (
            <button
              type="button"
              onClick={() => loadQuota(Boolean(quota))}
              disabled={quotaRefreshing || quotaLoading || cooldownSec > 0}
              className="ca-btn ca-btn-secondary ca-btn-xs"
              title={
                cooldownSec > 0
                  ? `防频刷冷却中 (${cooldownSec}s)`
                  : quota
                  ? '点击手动拉取最新官方配额'
                  : '拉取官方配额监控'
              }
            >
              {quotaRefreshing || quotaLoading ? (
                <Loader2 size={11} className="animate-spin" />
              ) : quota ? (
                <RefreshCw size={11} />
              ) : (
                <Zap size={11} style={{ color: 'var(--primary-color)' }} />
              )}
              {cooldownSec > 0
                ? `${cooldownSec}s`
                : quota
                ? '刷新'
                : '查询配额'}
            </button>
          )}

          {/* 启用状态切换开关 */}
          <label
            className="ca-pool-switch-wrap"
            title={isInPool ? '已启用：参与对话分流与容灾调度' : '未启用：该账号暂不参与对话调度'}
          >
            <input
              type="checkbox"
              checked={isInPool}
              onChange={(e) => handleTogglePool(acc.id, e.target.checked)}
              disabled={togglingPoolId === acc.id}
              className="ca-pool-switch-input"
            />
            <span className="ca-pool-switch-slider">
              <span className="ca-pool-switch-knob" />
            </span>
            <span className={`ca-pool-switch-label ${isInPool ? 'on' : 'off'}`}>
              {isInPool ? '启用' : '未启用'}
            </span>
          </label>

          {/* 移除按钮（纯图标） */}
          <button
            type="button"
            onClick={() => handleDeleteAccount(subTab, acc.id, acc.email)}
            disabled={deletingAccountId === acc.id}
            className="ca-icon-btn-danger"
            title="解除此账号与算力池的绑定"
          >
            {deletingAccountId === acc.id ? (
              <Loader2 size={13} className="animate-spin" />
            ) : (
              <Trash2 size={13} />
            )}
          </button>
        </div>
      </div>

      {/* 2. 卡片中间：直接内嵌展示配额进度条（剥离内层卡片套嵌，零多余外边框与重复邮箱） */}
      {subTab === 'google' && (
        <div className="ca-account-card-quota-wrapper">
          {quotaError && (
            <div className="ca-quota-inline-error">
              <Info size={12} />
              <span>{quotaError}</span>
            </div>
          )}

          <AccountQuotaBars quota={quota} loading={quotaLoading} />
        </div>
      )}

      {/* 3. 卡片底部：思考分级模型配置抽屉（收起时为精简标签，展开后为完整勾选） */}
      {subTab === 'google' && (
        <div className="ca-account-card-models-drawer">
          <div
            className="ca-card-models-header"
            onClick={() => toggleCardModelsExpanded(acc.id)}
          >
            <div className="ca-card-models-title">
              <Sparkles size={13} style={{ color: 'var(--primary-color)' }} />
              <span>官方思考分级模型配置</span>
              <span className="ca-card-models-count-tag">
                已启用 {enabledModelKeys.size} / {googleModels.length || 14} 款
              </span>
              {modelsLoading && (
                <Loader2 size={11} className="animate-spin" style={{ color: 'var(--primary-color)' }} />
              )}
              {saveStatus && (
                <span className="ca-save-pill"><Check size={11} /> {saveStatus}</span>
              )}
            </div>

            <div className="ca-card-models-toggle-btn">
              <span>{expandedCardModels[acc.id] ? '收起配置' : '展开微调 14 款官方模型'}</span>
              <ChevronDown
                size={14}
                className={`ca-chevron-icon ${expandedCardModels[acc.id] ? 'expanded' : ''}`}
              />
            </div>
          </div>

          {/* 折叠态：精美单行预览标签 */}
          {!expandedCardModels[acc.id] && (
            <div
              className="ca-models-chip-preview"
              onClick={() => toggleCardModelsExpanded(acc.id)}
              title="点击展开完整 14 款模型与思考分级微调"
            >
              <span className="ca-model-chip">Gemini 3.8 Flash High</span>
              <span className="ca-model-chip">3.8 Flash Med</span>
              <span className="ca-model-chip tag-pro-chip">3.1 Pro High/Low</span>
              <span className="ca-model-chip tag-claude-chip">Claude 4.6 Thinking</span>
              <span className="ca-model-chip">GPT-OSS 120B</span>
              <span className="ca-model-chip-hint">+ 点击展开微调</span>
            </div>
          )}

          {/* 展开态：完整 14 款官方核心思考分级模型目录与控制 */}
          {expandedCardModels[acc.id] && (
            <div className="ca-models-expanded-content">
              <div className="ca-model-controls-bar">
                <div className="ca-search-input-box">
                  <Search size={13} className="ca-search-icon" />
                  <input
                    type="text"
                    value={modelSearch}
                    onChange={(e) => setModelSearch(e.target.value)}
                    placeholder="按模型名过滤（3.8, flash, pro, claude, high...）"
                    className="ca-model-search-field"
                  />
                  {modelSearch && (
                    <button
                      type="button"
                      onClick={() => setModelSearch('')}
                      className="ca-clear-search-btn"
                    >
                      ×
                    </button>
                  )}
                </div>
                <div className="ca-quick-filter-group">
                  <button
                    type="button"
                    onClick={handleSelectRecommendedOnly}
                    className="ca-btn ca-btn-ghost ca-btn-xs"
                  >
                    ⭐ 推荐 9 款
                  </button>
                  <button
                    type="button"
                    onClick={handleSelectAll}
                    className="ca-btn ca-btn-ghost ca-btn-xs"
                  >
                    全选
                  </button>
                  <button
                    type="button"
                    onClick={handleSelectNone}
                    className="ca-btn ca-btn-ghost ca-btn-xs"
                  >
                    全不选
                  </button>
                </div>
              </div>

              <div className="ca-models-grid-compact">
                {filteredGoogleModels.map((m) => {
                  const isChecked = enabledModelKeys.has(m.id)
                  const isRecommended = RECOMMENDED_PRO_MODELS.includes(m.id)
                  const isHigh = m.id.includes('high')
                  const isMed = m.id.includes('medium')
                  const isLow = m.id.includes('low')
                  const isClaudeThinking = m.id.includes('thinking')

                  return (
                    <div
                      key={m.id}
                      className={`ca-model-row-card ${isChecked ? 'selected' : ''}`}
                      onClick={() => toggleModel(m.id)}
                    >
                      <div className="ca-model-row-left">
                        <label
                          className="ca-custom-checkbox"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <input
                            type="checkbox"
                            checked={isChecked}
                            onChange={() => toggleModel(m.id)}
                          />
                          <span className="ca-checkmark-box" />
                        </label>
                        <div className="ca-model-text-wrap">
                          <div className="ca-model-name-line">
                            <span className="ca-model-main-name">{m.name}</span>
                            {isRecommended && (
                              <span className="ca-tier-tag tag-pro">
                                <Sparkles size={9} /> 推荐
                              </span>
                            )}
                            {isHigh && (
                              <span className="ca-tier-tag tag-high">
                                <Flame size={9} /> High 深度思考
                              </span>
                            )}
                            {isMed && (
                              <span className="ca-tier-tag tag-med">
                                <Cpu size={9} /> Med 均衡思考
                              </span>
                            )}
                            {isLow && (
                              <span className="ca-tier-tag tag-low">
                                <Zap size={9} /> Low 轻量思考
                              </span>
                            )}
                            {isClaudeThinking && (
                              <span className="ca-tier-tag tag-claude">
                                <Sparkles size={9} /> Thinking 深度推演
                              </span>
                            )}
                          </div>
                          <span className="ca-model-id-code">{m.id}</span>
                        </div>
                      </div>
                      <div className="ca-model-row-right">
                        <span className="ca-spec-pill">
                          {m.context_window ? `${Math.round(m.context_window / 1024)}k` : '官方'}
                        </span>
                        {m.vision && <span className="ca-spec-pill">多模态</span>}
                        {m.tools && <span className="ca-spec-pill">工具</span>}
                      </div>
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </article>
  )
}

export function ComputeAccountsPanel() {
  const [subTab, setSubTab] = useState<ComputeSubTab>(() => {
    try {
      const saved = localStorage.getItem(COMPUTE_SUBTAB_KEY)
      if (saved === 'google' || saved === 'grok' || saved === 'codex') {
        return saved
      }
    } catch {
      /* ignore */
    }
    return 'google'
  })

  // 渠道总体概览（用于左栏状态指示）
  const [overviewChannels, setOverviewChannels] = useState<ComputeProviderOverview[]>([])
  const [overviewLoading, setOverviewLoading] = useState(false)

  // 当前激活渠道的具体账号
  const [currentAccounts, setCurrentAccounts] = useState<ComputeAccountItem[]>([])
  const [accountsLoading, setAccountsLoading] = useState(false)
  const [deletingAccountId, setDeletingAccountId] = useState<string | null>(null)
  const [settingPrimaryId, setSettingPrimaryId] = useState<string | null>(null)

  // Google 专属模型目录与勾选状态
  const [googleModels, setGoogleModels] = useState<ModelCatalogItem[]>([])
  const [enabledModelKeys, setEnabledModelKeys] = useState<Set<string>>(new Set())
  const [modelsLoading, setModelsLoading] = useState(false)
  const [modelSearch, setModelSearch] = useState('')
  const [saveStatus, setSaveStatus] = useState<string | null>(null)

  // 连通性测速结果缓存：channelId -> testResult
  const [testResults, setTestResults] = useState<Record<string, ConnectionTestResult>>({})
  const [testingChannel, setTestingChannel] = useState<string | null>(null)

  // 头像加载错误记录
  const [avatarErrors, setAvatarErrors] = useState<Record<string, boolean>>({})

  // 授权辅助引导弹窗状态
  const [openingAuthId, setOpeningAuthId] = useState<string | null>(null)
  const [authGuidance, setAuthGuidance] = useState<{
    open: boolean
    providerId: string
    providerName: string
    authUrl: string
  } | null>(null)
  const [copied, setCopied] = useState(false)
  const [manualInput, setManualInput] = useState('')
  const [manualSubmitting, setManualSubmitting] = useState(false)
  const [manualError, setManualError] = useState<string | null>(null)
  const [manualSuccess, setManualSuccess] = useState(false)

  // 持久化当前子 tab
  useEffect(() => {
    try {
      localStorage.setItem(COMPUTE_SUBTAB_KEY, subTab)
    } catch {
      /* ignore */
    }
  }, [subTab])

  // 获取概览数据
  const fetchOverview = useCallback(async (quiet = false) => {
    if (!quiet) setOverviewLoading(true)
    try {
      const data = await getComputeOverview()
      setOverviewChannels(data)
      return data
    } catch (e) {
      console.error('[ComputeAccounts] 获取概览失败', e)
      return []
    } finally {
      if (!quiet) setOverviewLoading(false)
    }
  }, [])

  // 获取当前子 tab 的详细账号
  const fetchCurrentTabAccounts = useCallback(async (channelId: string) => {
    setAccountsLoading(true)
    try {
      const accs = await listAccounts(channelId)
      setCurrentAccounts(accs)
    } catch (e) {
      console.error(`[ComputeAccounts] 获取 ${channelId} 账号失败`, e)
      setCurrentAccounts([])
    } finally {
      setAccountsLoading(false)
    }
  }, [])

  // 获取 Google 专属模型目录
  const fetchGoogleModels = useCallback(async () => {
    setModelsLoading(true)
    try {
      const [mdlList, enabledList] = await Promise.all([
        listChannelModels('google'),
        getEnabledModels('google'),
      ])
      setGoogleModels(mdlList)
      if (enabledList && Array.isArray(enabledList)) {
        setEnabledModelKeys(new Set(enabledList))
      } else {
        setEnabledModelKeys(new Set(mdlList.map((m) => m.id)))
      }
    } catch (e) {
      console.error('[ComputeAccounts] 获取 Google 模型目录失败', e)
    } finally {
      setModelsLoading(false)
    }
  }, [])

  // 初始化与 Tab 切换数据联动
  useEffect(() => {
    void fetchOverview()
  }, [fetchOverview])

  useEffect(() => {
    void fetchCurrentTabAccounts(subTab)
    if (subTab === 'google') {
      void fetchGoogleModels()
    }
  }, [subTab, fetchCurrentTabAccounts, fetchGoogleModels])

  // 授权状态轮询（当用户在系统浏览器中授权时自动检测并刷新）
  useEffect(() => {
    const timer = setInterval(async () => {
      const latest = await fetchOverview(true)
      if (authGuidance) {
        const currentChannel = latest.find((c: any) => c.id === authGuidance.providerId)
        if (currentChannel && currentChannel.status === 'configured') {
          setManualSuccess(true)
          window.dispatchEvent(new CustomEvent(MODELS_CATALOG_CHANGED_EVENT))
          await fetchCurrentTabAccounts(authGuidance.providerId)
          setTimeout(() => {
            setAuthGuidance(null)
            setManualSuccess(false)
            setManualInput('')
          }, 1200)
        }
      }
    }, 3000)

    return () => clearInterval(timer)
  }, [authGuidance, fetchOverview, fetchCurrentTabAccounts])

  const openUrlInExternalBrowser = async (url: string) => {
    try {
      const runtime = await getRuntime()
      await runtime.shell.openExternal(url)
    } catch (err) {
      console.warn('[ComputeAccounts] openExternal 失败，降级为 window.open', err)
      window.open(url, '_blank', 'noopener,noreferrer')
    }
  }

  // 发起 OAuth 授权
  const handleAddAccount = async (providerId: string, providerName: string) => {
    setOpeningAuthId(providerId)
    try {
      const authData = await getAuthUrl(providerId)
      if (authData.auth_url) {
        await openUrlInExternalBrowser(authData.auth_url)
        setAuthGuidance({
          open: true,
          providerId,
          providerName,
          authUrl: authData.auth_url,
        })
        setCopied(false)
        setManualError(null)
      }
    } catch (e: any) {
      console.error('[ComputeAccounts] 获取授权地址失败', e)
      alert(`获取授权地址失败: ${e.message}`)
    } finally {
      setOpeningAuthId(null)
    }
  }

  // 复制授权链接
  const handleCopyLink = async () => {
    if (!authGuidance?.authUrl) return
    try {
      await navigator.clipboard.writeText(authGuidance.authUrl)
      setCopied(true)
      setTimeout(() => setCopied(false), 2500)
    } catch {
      const el = document.createElement('textarea')
      el.value = authGuidance.authUrl
      document.body.appendChild(el)
      el.select()
      document.execCommand('copy')
      document.body.removeChild(el)
      setCopied(true)
      setTimeout(() => setCopied(false), 2500)
    }
  }

  // 手动输入回调 code 兑换
  const handleManualSubmit = async () => {
    if (!manualInput.trim() || !authGuidance) return
    setManualSubmitting(true)
    setManualError(null)
    try {
      await manualExchangeToken(authGuidance.providerId, {
        redirect_url: manualInput.trim(),
        code: manualInput.trim(),
      })
      setManualSuccess(true)
      window.dispatchEvent(new CustomEvent(MODELS_CATALOG_CHANGED_EVENT))
      await fetchOverview(true)
      await fetchCurrentTabAccounts(authGuidance.providerId)
      setTimeout(() => {
        setAuthGuidance(null)
        setManualSuccess(false)
        setManualInput('')
      }, 1000)
    } catch (e: any) {
      setManualError(e.message || '兑换 Token 失败')
    } finally {
      setManualSubmitting(false)
    }
  }

  // 记录每个账号卡片内模型微调抽屉的展开状态
  const [expandedCardModels, setExpandedCardModels] = useState<Record<string, boolean>>({})
  const [togglingPoolId, setTogglingPoolId] = useState<string | null>(null)

  const toggleCardModelsExpanded = (accId: string) => {
    setExpandedCardModels((prev) => ({
      ...prev,
      [accId]: !prev[accId],
    }))
  }

  // 切换账号入池调度开关
  const handleTogglePool = async (accountId: string, nextEnabled: boolean) => {
    setTogglingPoolId(accountId)
    // 乐观立即刷新 UI
    setCurrentAccounts((prev) =>
      prev.map((a) => (a.id === accountId ? { ...a, enabled: nextEnabled } : a))
    )
    try {
      const res = await toggleAccount(subTab, accountId, nextEnabled)
      setCurrentAccounts((prev) =>
        prev.map((a) => (a.id === accountId ? { ...a, enabled: res.enabled } : a))
      )
      window.dispatchEvent(new CustomEvent(MODELS_CATALOG_CHANGED_EVENT))
      await fetchOverview(true)
    } catch (err: any) {
      alert(err?.message || '切换账号入池状态失败')
      await fetchCurrentTabAccounts(subTab)
    } finally {
      setTogglingPoolId(null)
    }
  }

  // 设为主力调度账号
  const handleSetPrimary = async (channelId: string, accountId: string) => {
    setSettingPrimaryId(accountId)
    try {
      const ok = await setPrimaryAccount(channelId, accountId)
      if (ok) {
        await fetchCurrentTabAccounts(channelId)
        await fetchOverview(true)
        window.dispatchEvent(new CustomEvent(MODELS_CATALOG_CHANGED_EVENT))
      }
    } catch (e: any) {
      alert(`设为主力账号失败: ${e.message}`)
    } finally {
      setSettingPrimaryId(null)
    }
  }

  // 移除指定账号
  const handleDeleteAccount = async (channelId: string, accountId: string, email?: string) => {
    if (!window.confirm(`确定要解除账号【${email || accountId}】的关联吗？`)) return
    setDeletingAccountId(accountId)
    try {
      await deleteAccount(channelId, accountId)
      await fetchCurrentTabAccounts(channelId)
      await fetchOverview(true)
      window.dispatchEvent(new CustomEvent(MODELS_CATALOG_CHANGED_EVENT))
    } catch (e: any) {
      alert(`移除账号失败: ${e.message}`)
    } finally {
      setDeletingAccountId(null)
    }
  }

  // 测试网关连通性
  const handleTestConnection = async (channelId: string) => {
    setTestingChannel(channelId)
    try {
      const res = await testConnection(channelId)
      setTestResults((prev) => ({ ...prev, [channelId]: res }))
    } catch (e: any) {
      setTestResults((prev) => ({
        ...prev,
        [channelId]: { ok: false, error: e.message || '测试连接异常' },
      }))
    } finally {
      setTestingChannel(null)
    }
  }

  // 保存模型勾选配置
  const saveEnabledModels = async (nextSet: Set<string>) => {
    setEnabledModelKeys(nextSet)
    try {
      await setEnabledModels('google', Array.from(nextSet))
      window.dispatchEvent(new CustomEvent(MODELS_CATALOG_CHANGED_EVENT))
      setSaveStatus('已自动同步生效')
      setTimeout(() => setSaveStatus(null), 2500)
    } catch (err: any) {
      console.error('保存模型配置失败', err)
      setSaveStatus('保存失败')
    }
  }

  const toggleModel = (modelId: string) => {
    const next = new Set(enabledModelKeys)
    if (next.has(modelId)) {
      next.delete(modelId)
    } else {
      next.add(modelId)
    }
    saveEnabledModels(next)
  }

  const handleSelectRecommendedOnly = () => {
    const next = new Set(RECOMMENDED_PRO_MODELS)
    saveEnabledModels(next)
  }

  const handleSelectAll = () => {
    const next = new Set(googleModels.map((m) => m.id))
    saveEnabledModels(next)
  }

  const handleSelectNone = () => {
    saveEnabledModels(new Set())
  }

  // 搜索过滤模型
  const filteredGoogleModels = useMemo(() => {
    if (!modelSearch.trim()) return googleModels
    const q = modelSearch.toLowerCase().trim()
    return googleModels.filter(
      (m) => m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q)
    )
  }, [googleModels, modelSearch])

  const currentOverview = overviewChannels.find((c) => c.id === subTab)
  const isConfigured = currentOverview?.status === 'configured' || currentAccounts.length > 0
  const activeMeta = CHANNELS_META[subTab]
  const currentTestResult = testResults[subTab]

  return (
    <div className="ca-split-layout">
      {/* ── 左侧：二级侧栏导航列表 ─────────────────────────────────── */}
      <aside className="ca-subnav-sidebar" aria-label="算力平台分类导航">
        <div className="ca-subnav-top">
          <div className="ca-subnav-header">
            <div className="ca-subnav-title-group">
              <span className="ca-subnav-title">算力账号</span>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                {overviewLoading && (
                  <Loader2 size={11} className="animate-spin" style={{ color: 'var(--primary-color)' }} />
                )}
                <span className="ca-pro-tag">
                  <Sparkles size={9} />
                  PRO
                </span>
              </div>
            </div>
            <p className="ca-subnav-desc">免 API Key 官方直连池</p>
          </div>

          <nav className="ca-subnav-nav">
            {(['google', 'grok', 'codex'] as ComputeSubTab[]).map((tabKey) => {
              const meta = CHANNELS_META[tabKey]
              const ov = overviewChannels.find((c) => c.id === tabKey)
              const hasAccounts = (ov?.account_count ?? 0) > 0 || (subTab === tabKey && currentAccounts.length > 0)
              const active = subTab === tabKey

              return (
                <button
                  key={tabKey}
                  type="button"
                  className={`ca-subnav-item ${active ? 'active' : ''}`}
                  onClick={() => setSubTab(tabKey)}
                >
                  <div className={`ca-subnav-item-icon ca-logo-${tabKey}`}>
                    {tabKey === 'google' ? (
                      <svg width="17" height="17" viewBox="0 0 24 24">
                        <path fill="#4285F4" d="M23.745 12.27c0-.7-.06-1.4-.19-2.07H12v4.51h6.6c-.29 1.52-1.14 2.8-2.4 3.66v3.05h3.88c2.27-2.09 3.66-5.17 3.66-9.15z"/>
                        <path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.88-3.05c-1.08.72-2.45 1.16-4.05 1.16-3.12 0-5.77-2.1-6.72-4.93H1.27v3.15C3.25 21.31 7.33 24 12 24z"/>
                        <path fill="#FBBC05" d="M5.28 14.27c-.25-.72-.38-1.49-.38-2.27s.13-1.55.38-2.27V6.58H1.27C.46 8.2 0 10.04 0 12s.46 3.8 1.27 5.42l4.01-3.15z"/>
                        <path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.95 1.19 15.24 0 12 0 7.33 0 3.25 2.69 1.27 6.58l4.01 3.15c.95-2.83 3.6-4.98 6.72-4.98z"/>
                      </svg>
                    ) : tabKey === 'grok' ? (
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/>
                      </svg>
                    ) : (
                      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="16 18 22 12 16 6"></polyline>
                        <polyline points="8 6 2 12 8 18"></polyline>
                      </svg>
                    )}
                  </div>

                  <div className="ca-subnav-item-content">
                    <div className="ca-subnav-item-row">
                      <span className="ca-subnav-item-name">{meta.name}</span>
                      <span
                        className={`ca-subnav-status-dot ${hasAccounts ? 'configured' : ''}`}
                        title={hasAccounts ? '已配置活跃' : '未配置'}
                      />
                    </div>
                    <span className="ca-subnav-item-sub">
                      {hasAccounts
                        ? `${ov?.account_count || 1} 个账号`
                        : '未配置'}
                    </span>
                  </div>
                </button>
              )
            })}
          </nav>
        </div>
      </aside>

      {/* ── 右侧：平台子页面（高度紧凑、独立流畅滚动区） ───────────── */}
      <main className="ca-subnav-pane" aria-label="算力平台详情配置">
        {/* 1. 紧凑型顶部操作栏（替代过去巨大的 Hero 卡片，节省超 250px 纵向高度） */}
        <header className="ca-compact-header">
          <div className="ca-compact-header-left">
            <div className={`ca-header-icon-badge ca-logo-${subTab}`}>
              {subTab === 'google' ? (
                <svg width="22" height="22" viewBox="0 0 24 24">
                  <path fill="#4285F4" d="M23.745 12.27c0-.7-.06-1.4-.19-2.07H12v4.51h6.6c-.29 1.52-1.14 2.8-2.4 3.66v3.05h3.88c2.27-2.09 3.66-5.17 3.66-9.15z"/>
                  <path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.88-3.05c-1.08.72-2.45 1.16-4.05 1.16-3.12 0-5.77-2.1-6.72-4.93H1.27v3.15C3.25 21.31 7.33 24 12 24z"/>
                  <path fill="#FBBC05" d="M5.28 14.27c-.25-.72-.38-1.49-.38-2.27s.13-1.55.38-2.27V6.58H1.27C.46 8.2 0 10.04 0 12s.46 3.8 1.27 5.42l4.01-3.15z"/>
                  <path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.95 1.19 15.24 0 12 0 7.33 0 3.25 2.69 1.27 6.58l4.01 3.15c.95-2.83 3.6-4.98 6.72-4.98z"/>
                </svg>
              ) : subTab === 'grok' ? (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/>
                </svg>
              ) : (
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="16 18 22 12 16 6"></polyline>
                  <polyline points="8 6 2 12 8 18"></polyline>
                </svg>
              )}
            </div>

            <div className="ca-compact-header-info">
              <div className="ca-compact-title-row">
                <h1 className="ca-compact-title">{activeMeta.fullName}</h1>
                <span className={`ca-status-pill ${isConfigured ? 'active' : 'unconfigured'}`}>
                  <span className="ca-pulse-dot" />
                  {isConfigured ? '已就绪 · 运行中' : '未配置'}
                </span>
                {currentTestResult?.ok && (
                  <span className="ca-latency-pill">
                    <Zap size={11} /> {currentTestResult.latency_ms}ms 极速
                  </span>
                )}
              </div>
              <span className="ca-compact-subtitle">{activeMeta.subtitle}</span>
            </div>
          </div>

          <div className="ca-compact-header-actions">
            <button
              type="button"
              onClick={() => handleAddAccount(subTab, activeMeta.name)}
              disabled={openingAuthId === subTab}
              className="ca-btn ca-btn-primary ca-btn-sm"
            >
              {openingAuthId === subTab ? (
                <>
                  <Loader2 size={13} className="animate-spin" />
                  正在拉起授权...
                </>
              ) : (
                <>
                  <UserPlus size={13} />
                  关联 {activeMeta.name} 账号
                </>
              )}
            </button>

            {isConfigured && (
              <button
                type="button"
                onClick={() => handleTestConnection(subTab)}
                disabled={testingChannel === subTab}
                className="ca-btn ca-btn-secondary ca-btn-sm"
                title="向官方网关发起轻量测速探测"
              >
                <Zap size={13} style={{ color: 'var(--primary-color)' }} />
                {testingChannel === subTab ? '测速中...' : '网关测速'}
              </button>
            )}

            <button
              type="button"
              onClick={async () => {
                await fetchOverview(false)
                await fetchCurrentTabAccounts(subTab)
                if (subTab === 'google') await fetchGoogleModels()
              }}
              disabled={accountsLoading}
              className="ca-btn ca-btn-secondary ca-btn-sm ca-icon-only-btn"
              title="刷新当前算力平台状态"
            >
              <RefreshCw size={13} className={accountsLoading ? 'animate-spin' : ''} />
            </button>
          </div>
        </header>

        {/* 测速结果提示 */}
        {currentTestResult && (
          <div className={`ca-test-alert ${currentTestResult.ok ? 'ok' : 'err'}`}>
            {currentTestResult.ok ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
            <span className="ca-test-alert-text">
              {currentTestResult.ok ? (
                <>
                  <strong>网关连通性测试通过！</strong>往返延迟{' '}
                  <strong>{currentTestResult.latency_ms}ms</strong>，生效可用模型:{' '}
                  <strong>{currentTestResult.model_count || 14}</strong> 款。
                </>
              ) : (
                <>
                  <strong>测试连接异常：</strong>
                  {currentTestResult.error}。请检查本地科学上网客户端分流规则是否包含{' '}
                  <code>*.googleapis.com</code>。
                </>
              )}
            </span>
          </div>
        )}

        {/* ── 2. 账号卡片流 (每账号独立卡片：配额直接内嵌 + 模型微调抽屉 + 顶部全操作合并) ── */}
        {!accountsLoading && currentAccounts.length > 0 && (
          <div className="ca-section-head">
            <span className="ca-section-head-title">
              账号节点
              <span className="ca-section-head-count">{currentAccounts.length}</span>
            </span>
          </div>
        )}
        <div className="ca-account-cards-stack">
          {accountsLoading ? (
            <div className="ca-state-loading ca-card-section" style={{ padding: '28px' }}>
              <Loader2 size={20} className="animate-spin" style={{ color: 'var(--primary-color)' }} />
              <span>正在加载 {activeMeta.name} 算力账号池...</span>
            </div>
          ) : currentAccounts.length === 0 ? (
            <div className="ca-card-section ca-empty-account-card">
              <div className="ca-empty-card-inner">
                <div className="ca-empty-avatar-icon">
                  <UserPlus size={24} />
                </div>
                <div className="ca-empty-card-text">
                  <h3>尚未关联 {activeMeta.name} 官方平台账号</h3>
                  <p>一键授权免 API Key 账号直连池，获得高稳定性思考分级模型与独立配额监控保障。</p>
                </div>
                <button
                  type="button"
                  onClick={() => handleAddAccount(subTab, activeMeta.name)}
                  disabled={openingAuthId === subTab}
                  className="ca-btn ca-btn-primary ca-btn-md"
                >
                  <UserPlus size={14} />
                  立即去浏览器绑定账号
                </button>
              </div>
            </div>
          ) : (
            currentAccounts.map((acc, accIdx) => (
              <AccountMasterCardItem
                key={acc.id}
                acc={acc}
                accIdx={accIdx}
                subTab={subTab}
                activeMeta={activeMeta}
                avatarError={avatarErrors[acc.id]}
                onAvatarError={() => setAvatarErrors((p) => ({ ...p, [acc.id]: true }))}
                settingPrimaryId={settingPrimaryId}
                deletingAccountId={deletingAccountId}
                togglingPoolId={togglingPoolId}
                handleTogglePool={handleTogglePool}
                handleSetPrimary={handleSetPrimary}
                handleDeleteAccount={handleDeleteAccount}
                expandedCardModels={expandedCardModels}
                toggleCardModelsExpanded={toggleCardModelsExpanded}
                enabledModelKeys={enabledModelKeys}
                googleModels={googleModels}
                modelsLoading={modelsLoading}
                saveStatus={saveStatus}
                modelSearch={modelSearch}
                setModelSearch={setModelSearch}
                handleSelectRecommendedOnly={handleSelectRecommendedOnly}
                handleSelectAll={handleSelectAll}
                handleSelectNone={handleSelectNone}
                filteredGoogleModels={filteredGoogleModels}
                toggleModel={toggleModel}
              />
            ))
          )}

          {/* 底部：添加更多账号入口与调度机制说明 */}
          {currentAccounts.length > 0 && (
            <div className="ca-cards-stack-footer">
              <span className="ca-stack-footer-hint">
                💡 多账号调度机制：开启「启用」的账号自动参与会话负载，遇 429 配额耗尽时秒级自动熔断切换至下一就绪账号。
              </span>
              <button
                type="button"
                onClick={() => handleAddAccount(subTab, activeMeta.name)}
                disabled={openingAuthId === subTab}
                className="ca-btn ca-btn-secondary ca-btn-sm"
              >
                <UserPlus size={13} />
                <span>+ 关联更多 {activeMeta.name} 账号</span>
              </button>
            </div>
          )}
        </div>

        {/* 5. Grok / Codex 专属：平台能力特性矩阵卡片 */}
        {(subTab === 'grok' || subTab === 'codex') && (
          <section className="ca-card-section">
            <div className="ca-card-section-header">
              <div className="ca-card-title-group">
                <span className="ca-card-section-title">{activeMeta.name} 平台架构能力</span>
              </div>
              <span className="ca-card-section-hint">官方协议直连与调度</span>
            </div>

            <div className="ca-card-section-content">
              <div className="ca-feature-triad-grid">
                {subTab === 'grok' ? (
                  <>
                    <div className="ca-feature-mini-card">
                      <div className="ca-feature-icon-badge" style={{ color: '#e0c59e' }}>
                        <Flame size={18} />
                      </div>
                      <div className="ca-feature-meta">
                        <span className="ca-feature-item-title">Grok 3 深度推演</span>
                        <p className="ca-feature-item-desc">
                          xAI 最新超大规模长上下文推理架构，在复杂前沿数学逻辑与程序推演中表现顶尖。
                        </p>
                      </div>
                    </div>

                    <div className="ca-feature-mini-card">
                      <div className="ca-feature-icon-badge" style={{ color: '#58a6ff' }}>
                        <Globe size={18} />
                      </div>
                      <div className="ca-feature-meta">
                        <span className="ca-feature-item-title">全球动态实时情报</span>
                        <p className="ca-feature-item-desc">
                          整合 xAI 独家全球动态实时情报源与联网搜索，有效弥补模型静态训练集知识截断。
                        </p>
                      </div>
                    </div>

                    <div className="ca-feature-mini-card">
                      <div className="ca-feature-icon-badge" style={{ color: '#3fb950' }}>
                        <ShieldCheck size={18} />
                      </div>
                      <div className="ca-feature-meta">
                        <span className="ca-feature-item-title">官方订阅算力池直连</span>
                        <p className="ca-feature-item-desc">
                          免 API Key 直接复用官方个人或团队订阅额度，无需部署反向代理或预充值。
                        </p>
                      </div>
                    </div>
                  </>
                ) : (
                  <>
                    <div className="ca-feature-mini-card">
                      <div className="ca-feature-icon-badge" style={{ color: '#10A37F' }}>
                        <Code2 size={18} />
                      </div>
                      <div className="ca-feature-meta">
                        <span className="ca-feature-item-title">工程级多文件代码推演</span>
                        <p className="ca-feature-item-desc">
                          深度理解多目录工程依赖，精准分析跨模块符号调用并生成高可用单元测试。
                        </p>
                      </div>
                    </div>

                    <div className="ca-feature-mini-card">
                      <div className="ca-feature-icon-badge" style={{ color: '#bc8cff' }}>
                        <Terminal size={18} />
                      </div>
                      <div className="ca-feature-meta">
                        <span className="ca-feature-item-title">终端与智能体深度协同</span>
                        <p className="ca-feature-item-desc">
                          原生对齐命令执行与工具链调用标准，赋能自主排障与自动化测试验证。
                        </p>
                      </div>
                    </div>

                    <div className="ca-feature-mini-card">
                      <div className="ca-feature-icon-badge" style={{ color: '#f0883e' }}>
                        <Zap size={18} />
                      </div>
                      <div className="ca-feature-meta">
                        <span className="ca-feature-item-title">极速高吞吐流式通道</span>
                        <p className="ca-feature-item-desc">
                          专为代码实时补全与极速多轮交互优化的低延迟高吞吐网络通道。
                        </p>
                      </div>
                    </div>
                  </>
                )}
              </div>
            </div>
          </section>
        )}
      </main>

      {/* ── 授权辅助引导弹窗 ────────────────────────────────────────── */}
      {authGuidance?.open && (
        <div className="ca-modal-overlay">
          <div className="ca-modal-container" style={{ maxWidth: '500px' }}>
            <div className="ca-modal-header">
              <div className="ca-modal-title-group">
                <div
                  className="ca-status-dot"
                  style={{ background: '#3fb950', width: '8px', height: '8px' }}
                />
                <h3 className="ca-modal-title">
                  正在进行 {authGuidance.providerName} 授权
                </h3>
              </div>
              <button
                type="button"
                className="ca-modal-close-btn"
                onClick={() => setAuthGuidance(null)}
              >
                ×
              </button>
            </div>

            <div className="ca-modal-body">
              <div className="ca-auth-step-box">
                <div className="ca-auth-step-title">
                  <Loader2 size={15} className="animate-spin" style={{ color: 'var(--primary-color)' }} />
                  已在系统默认浏览器中打开官方授权页
                </div>
                <p className="ca-auth-step-desc">
                  请在打开的官方网页中登录并同意授权。完成后，柳如烟将自动拦截本地回调并激活。
                </p>
                <div className="ca-auth-step-actions">
                  <button
                    type="button"
                    onClick={() => openUrlInExternalBrowser(authGuidance.authUrl)}
                    className="ca-btn ca-btn-secondary ca-btn-sm"
                  >
                    <ExternalLink size={12} />
                    重新在浏览器打开
                  </button>
                  <button
                    type="button"
                    onClick={handleCopyLink}
                    className="ca-btn ca-btn-secondary ca-btn-sm"
                  >
                    {copied ? (
                      <Check size={12} style={{ color: '#4cd964' }} />
                    ) : (
                      <Copy size={12} />
                    )}
                    {copied ? '已复制链接！' : '复制授权链接'}
                  </button>
                </div>
              </div>

              {manualSuccess ? (
                <div className="ca-auth-success-alert">
                  <CheckCircle2 size={16} />
                  账号授权成功！正在刷新算力池...
                </div>
              ) : (
                <div className="ca-manual-paste-box">
                  <div className="ca-manual-paste-label">
                    <KeyRound size={12} />
                    <span>若浏览器未自动跳回，请在此粘贴授权完成后的完整地址或 Code：</span>
                  </div>
                  <textarea
                    rows={2}
                    value={manualInput}
                    onChange={(e) => setManualInput(e.target.value)}
                    placeholder="http://127.0.0.1:.../callback?code=4/0A... 或直接粘贴 code"
                    className="ca-manual-textarea"
                  />

                  {manualError && (
                    <div className="ca-manual-error">
                      <AlertCircle size={13} /> {manualError}
                    </div>
                  )}

                  <div className="ca-manual-submit-row">
                    <button
                      type="button"
                      onClick={handleManualSubmit}
                      disabled={manualSubmitting || !manualInput.trim()}
                      className="ca-btn ca-btn-primary ca-btn-sm"
                    >
                      {manualSubmitting ? '正在兑换...' : '手动确认绑定'}
                    </button>
                  </div>
                </div>
              )}
            </div>

            <div className="ca-modal-footer">
              <button
                type="button"
                onClick={() => setAuthGuidance(null)}
                className="ca-btn ca-btn-ghost ca-btn-sm"
              >
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default ComputeAccountsPanel
