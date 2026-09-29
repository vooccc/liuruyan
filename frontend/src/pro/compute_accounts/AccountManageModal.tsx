import { useEffect, useState } from 'react'
import {
  AlertCircle,
  Check,
  CheckCircle2,
  Clock,
  Loader2,
  Sparkles,
  Trash2,
  X,
  Zap,
} from 'lucide-react'
import {
  deleteAccount,
  getEnabledModels,
  listAccounts,
  listChannelModels,
  setEnabledModels,
  testConnection,
} from './api'
import AccountQuotaCard from './AccountQuotaCard'
import { MODELS_CATALOG_CHANGED_EVENT } from '../../api/chat'
import type { ComputeAccountItem, ModelCatalogItem } from './types'
import './compute-accounts.css'

interface Props {
  providerId: string
  providerName: string
  onClose: () => void
  onAccountsChanged: () => void
}

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

export default function AccountManageModal({
  providerId,
  providerName,
  onClose,
  onAccountsChanged,
}: Props) {
  const [accounts, setAccounts] = useState<ComputeAccountItem[]>([])
  const [models, setModels] = useState<ModelCatalogItem[]>([])
  const [enabledModelKeys, setEnabledModelKeys] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(true)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string; latency_ms?: number } | null>(null)
  const [activeTab, setActiveTab] = useState<'accounts' | 'models'>('accounts')
  const [deletingId, setDeletingId] = useState<string | null>(null)
  const [searchModel, setSearchModel] = useState('')
  const [saveStatus, setSaveStatus] = useState<string | null>(null)
  const [avatarErrors, setAvatarErrors] = useState<Record<string, boolean>>({})

  const loadData = async () => {
    setLoading(true)
    try {
      const [accs, mdlList, enabledList] = await Promise.all([
        listAccounts(providerId),
        listChannelModels(providerId),
        getEnabledModels(providerId),
      ])
      setAccounts(accs)
      setModels(mdlList)
      if (enabledList && Array.isArray(enabledList)) {
        setEnabledModelKeys(new Set(enabledList))
      } else {
        // 未配置过时默认开启全部模型
        setEnabledModelKeys(new Set(mdlList.map((m) => m.id)))
      }
    } catch (e: any) {
      console.error('[AccountManageModal] 加载数据失败', e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadData()
  }, [providerId])

  const handleDelete = async (id: string, email: string) => {
    if (!window.confirm(`确定要移除账号 ${email} 吗？`)) return
    setDeletingId(id)
    try {
      await deleteAccount(providerId, id)
      await loadData()
      onAccountsChanged()
    } catch (e: any) {
      alert(`删除账号失败: ${e.message}`)
    } finally {
      setDeletingId(null)
    }
  }

  const handleTestConnection = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const res = await testConnection(providerId)
      if (res.ok) {
        setTestResult({
          ok: true,
          msg: `连接通畅！已连接账号: ${res.email || '正常'}，生效模型: ${res.model_count || models.length} 款`,
          latency_ms: res.latency_ms,
        })
      } else {
        setTestResult({ ok: false, msg: res.error || '测试连接失败' })
      }
    } catch (e: any) {
      setTestResult({ ok: false, msg: e.message || '测试连接异常' })
    } finally {
      setTesting(false)
    }
  }

  const saveEnabledModels = async (nextSet: Set<string>) => {
    setEnabledModelKeys(nextSet)
    try {
      await setEnabledModels(providerId, Array.from(nextSet))
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
    const next = new Set(models.map((m) => m.id))
    saveEnabledModels(next)
  }

  const handleSelectNone = () => {
    saveEnabledModels(new Set())
  }

  const filteredModels = models.filter(
    (m) =>
      m.id.toLowerCase().includes(searchModel.toLowerCase()) ||
      m.name.toLowerCase().includes(searchModel.toLowerCase())
  )

  return (
    <div className="ca-modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="ca-modal-container">
        {/* Modal Header */}
        <div className="ca-modal-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{ fontSize: '18px' }}>⚡</span>
            <div>
              <h2 className="ca-modal-title">{providerName} 插件管理</h2>
              <p className="ca-modal-subtitle">
                官方 OAuth2 账号连接池、测速诊断与模型展示配置（Cursor-byok 风格）
              </p>
            </div>
          </div>
          <button type="button" onClick={onClose} className="ca-modal-close-btn">
            <X size={16} />
          </button>
        </div>

        {/* Tab Switcher */}
        <div className="ca-modal-tabs">
          <button
            type="button"
            onClick={() => setActiveTab('accounts')}
            className={`ca-modal-tab-btn ${activeTab === 'accounts' ? 'active' : ''}`}
          >
            <Clock size={14} /> 已配置账号 ({accounts.length})
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('models')}
            className={`ca-modal-tab-btn ${activeTab === 'models' ? 'active' : ''}`}
          >
            <Sparkles size={14} /> 模型展示配置 ({enabledModelKeys.size}/{models.length})
          </button>
        </div>

        {/* Modal Body */}
        <div className="ca-modal-body">
          {loading ? (
            <div className="ca-empty-box">
              <Loader2 size={24} className="animate-spin" style={{ color: 'var(--primary-color)' }} />
              <span style={{ fontSize: '13px' }}>正在加载账号与模型配置...</span>
            </div>
          ) : activeTab === 'accounts' ? (
            <>
              {/* 连通性测速栏 */}
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 16px', background: 'var(--bg-secondary)', borderRadius: '10px', border: '1px solid var(--border-color)' }}>
                <div>
                  <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-color)' }}>
                    网关连通性测试
                  </div>
                  <div style={{ fontSize: '11px', color: 'var(--text-secondary)', marginTop: '2px' }}>
                    向 Google Antigravity 服务端发起轻量探测，检测 Token 与网络延迟
                  </div>
                </div>

                <button
                  type="button"
                  onClick={handleTestConnection}
                  disabled={testing || accounts.length === 0}
                  className="ca-btn ca-btn-secondary ca-btn-sm"
                >
                  <Zap size={13} style={{ color: 'var(--primary-color)' }} />
                  {testing ? '正在测试...' : '开始测速'}
                </button>
              </div>

              {testResult && (
                <div
                  style={{
                    padding: '10px 14px',
                    borderRadius: '8px',
                    fontSize: '12px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    background: testResult.ok ? 'rgba(63, 185, 80, 0.12)' : 'rgba(248, 81, 73, 0.12)',
                    border: `1px solid ${testResult.ok ? 'rgba(63, 185, 80, 0.3)' : 'rgba(248, 81, 73, 0.3)'}`,
                    color: testResult.ok ? '#4cd964' : '#f85149',
                  }}
                >
                  {testResult.ok ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
                  <span>{testResult.msg}</span>
                  {testResult.latency_ms !== undefined && (
                    <span style={{ marginLeft: 'auto', fontWeight: 600 }}>{testResult.latency_ms}ms</span>
                  )}
                </div>
              )}

              {/* 账号列表 */}
              <div className="ca-accounts-list">
                {accounts.length === 0 ? (
                  <div className="ca-empty-box">
                    <p className="ca-empty-title">暂无已登录账号</p>
                    <p className="ca-empty-desc">
                      请点击外层的「+ 添加账号」按钮，在 Google 官方页面授权登录后即可自动接入。
                    </p>
                  </div>
                ) : (
                  accounts.map((acc) => {
                    const isCooling = acc.status === 'cooling'
                    const isExpired = acc.status === 'expired'

                    return (
                      <div key={acc.id} className="ca-account-card-box">
                        <div className="ca-account-item">
                          {acc.picture && !avatarErrors[acc.id] ? (
                            <img
                              src={acc.picture}
                              alt={acc.name || acc.email}
                              className="ca-account-avatar"
                              onError={() => setAvatarErrors((prev) => ({ ...prev, [acc.id]: true }))}
                            />
                          ) : (
                            <div className="ca-account-placeholder-avatar">
                              {(acc.name || acc.email || 'G').charAt(0).toUpperCase()}
                            </div>
                          )}

                          <div className="ca-account-details">
                            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                              <span className="ca-account-name">{acc.name || 'Google 账号'}</span>
                              <span
                                className={`ca-status-badge ${
                                  isCooling
                                    ? 'unconfigured'
                                    : isExpired
                                    ? 'unconfigured'
                                    : 'configured'
                                }`}
                                style={{ fontSize: '10px', padding: '1px 6px' }}
                              >
                                {isCooling ? '冷却降温中' : isExpired ? '凭据过期' : '健康活跃'}
                              </span>
                            </div>
                            <span className="ca-account-email">{acc.email}</span>
                          </div>

                          <div className="ca-account-actions">
                            <button
                              type="button"
                              onClick={() => handleDelete(acc.id, acc.email)}
                              disabled={deletingId === acc.id}
                              className="ca-btn ca-btn-danger ca-btn-sm"
                              title="移除此账号"
                            >
                              <Trash2 size={13} />
                              {deletingId === acc.id ? '正在移除...' : '移除'}
                            </button>
                          </div>
                        </div>

                        {/* 实时配额余量展示卡片（与 Antigravity IDE 及 Cockpit Tools 严格对齐） */}
                        {providerId === 'google' && (
                          <div style={{ marginTop: '10px' }}>
                            <AccountQuotaCard
                              providerId={providerId}
                              accountId={acc.id}
                              accountEmail={acc.email}
                            />
                          </div>
                        )}
                      </div>
                    )
                  })
                )}
              </div>
            </>
          ) : (
            <>
              {/* 模型搜索栏 */}
              <input
                type="text"
                value={searchModel}
                onChange={(e) => setSearchModel(e.target.value)}
                placeholder="搜索模型（如 claude, gemini-2.5-pro, flash, imagen...）"
                className="ca-model-search"
                style={{ marginBottom: '10px' }}
              />

              {/* 模型展示配置控制条（Cursor-byok 风格） */}
              <div style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                padding: '10px 14px',
                background: 'var(--bg-secondary)',
                borderRadius: '8px',
                border: '1px solid var(--border-color)',
                marginBottom: '14px',
                fontSize: '12px',
                flexWrap: 'wrap',
                gap: '8px',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)' }}>
                  <span>已启用展示：<strong style={{ color: 'var(--primary-color)' }}>{enabledModelKeys.size}</strong> / {models.length} 款模型</span>
                  <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>(仅勾选的本渠道模型会出现在聊天选择器中，置顶优先展示)</span>
                  {saveStatus && (
                    <span style={{
                      fontSize: '11px',
                      color: '#10b981',
                      background: 'rgba(16, 185, 129, 0.1)',
                      border: '1px solid rgba(16, 185, 129, 0.3)',
                      padding: '1px 6px',
                      borderRadius: '4px',
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '4px',
                    }}>
                      <Check size={11} /> {saveStatus}
                    </span>
                  )}
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <button
                    type="button"
                    onClick={handleSelectRecommendedOnly}
                    className="ca-btn ca-btn-secondary ca-btn-sm"
                    style={{ fontSize: '11px', padding: '4px 10px' }}
                    title="仅勾选当前与 Antigravity IDE 官方对齐的核心旗舰模型（Gemini 3.8/3.7/3.6、Gemini 3.1 Pro、Claude 4.6、GPT-OSS 等）"
                  >
                    ⭐ 仅选推荐 ({RECOMMENDED_PRO_MODELS.length}款)
                  </button>
                  <button
                    type="button"
                    onClick={handleSelectAll}
                    className="ca-btn ca-btn-secondary ca-btn-sm"
                    style={{ fontSize: '11px', padding: '4px 10px' }}
                    title="勾选展示全部模型"
                  >
                    全选
                  </button>
                  <button
                    type="button"
                    onClick={handleSelectNone}
                    className="ca-btn ca-btn-secondary ca-btn-sm"
                    style={{ fontSize: '11px', padding: '4px 10px' }}
                    title="取消勾选所有模型"
                  >
                    全不选
                  </button>
                </div>
              </div>

              {/* 模型卡片网格 */}
              <div className="ca-models-grid">
                {filteredModels.map((m) => {
                  const isEnabled = enabledModelKeys.has(m.id)
                  return (
                    <div
                      key={m.id}
                      className={`ca-model-card ${isEnabled ? 'enabled' : 'disabled'}`}
                      onClick={() => toggleModel(m.id)}
                      style={{
                        cursor: 'pointer',
                        position: 'relative',
                        borderColor: isEnabled ? 'color-mix(in srgb, var(--primary-color) 60%, var(--border-color))' : undefined,
                        background: isEnabled ? 'color-mix(in srgb, var(--primary-color) 4%, var(--bg-card))' : undefined,
                        opacity: isEnabled ? 1 : 0.65,
                        transition: 'all 0.15s ease',
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '8px' }}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div className="ca-model-id">{m.name || m.id}</div>
                          <div className="ca-model-name" style={{ fontFamily: 'var(--font-mono, monospace)', fontSize: '11px', opacity: 0.85 }}>
                            {m.id}
                          </div>
                        </div>

                        {/* 复选框开关 */}
                        <div
                          style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '5px',
                            padding: '3px 8px',
                            borderRadius: '6px',
                            background: isEnabled ? 'color-mix(in srgb, var(--primary-color) 16%, transparent)' : 'var(--bg-secondary)',
                            border: `1px solid ${isEnabled ? 'color-mix(in srgb, var(--primary-color) 40%, transparent)' : 'var(--border-color)'}`,
                            color: isEnabled ? 'var(--primary-color)' : 'var(--text-muted)',
                            fontSize: '11px',
                            fontWeight: 600,
                            flexShrink: 0,
                          }}
                        >
                          <input
                            type="checkbox"
                            checked={isEnabled}
                            onChange={() => {}} // 由卡片 onClick 处理
                            style={{ cursor: 'pointer', accentColor: 'var(--primary-color)' }}
                          />
                          <span>{isEnabled ? '显示' : '隐藏'}</span>
                        </div>
                      </div>

                      <div className="ca-model-tags">
                        {m.context_window && (
                          <span className="ca-tag-pill highlight">
                            {Math.round(m.context_window / 1024)}k 上下文
                          </span>
                        )}
                        {m.vision && <span className="ca-tag-pill">视觉/多模态</span>}
                        {m.tools && <span className="ca-tag-pill">工具调用</span>}
                      </div>
                    </div>
                  )
                })}
              </div>
            </>
          )}
        </div>

        {/* Modal Footer */}
        <div className="ca-modal-footer">
          <span>支持多账号并发轮询与 429 故障转移 · 模型配置即刻生效</span>
          <button
            type="button"
            onClick={onClose}
            className="ca-btn ca-btn-secondary ca-btn-sm"
          >
            完成
          </button>
        </div>
      </div>
    </div>
  )
}
