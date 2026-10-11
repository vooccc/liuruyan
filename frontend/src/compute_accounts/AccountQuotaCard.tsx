import { useEffect, useState } from 'react'
import {
  Gauge,
  Info,
  RefreshCw,
  ShieldCheck,
  Zap,
} from 'lucide-react'
import { getAccountQuota } from './api'
import type { AccountQuotaResult, QuotaBucketInfo } from './types'

interface Props {
  providerId: string
  accountId?: string
  accountEmail?: string
  compact?: boolean
}

/**
 * 专属自定义 Hook：管理账号配额查询、持久化缓存与 10 秒防频刷冷却
 */
export function useAccountQuota(providerId: string, accountId?: string) {
  const storageKey = `lry_quota_${providerId}_${accountId || 'default'}`

  // 初始化时优先读取持久化缓存快照，实现秒级即时展示、拒绝空白
  const [quota, setQuota] = useState<AccountQuotaResult | null>(() => {
    try {
      const saved = localStorage.getItem(storageKey)
      return saved ? JSON.parse(saved) : null
    } catch {
      return null
    }
  })

  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cooldownSec, setCooldownSec] = useState(0)
  const [lastUpdatedText, setLastUpdatedText] = useState<string>('')

  // 更新上次查询时间的展示
  const updateTimeText = (timestamp?: number) => {
    if (!timestamp) return
    const d = new Date(timestamp * 1000)
    const pad = (n: number) => String(n).padStart(2, '0')
    setLastUpdatedText(`${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`)
  }

  // 账号切换或初始化时，自动读取本地持久缓存并静默刷新一次
  useEffect(() => {
    try {
      const saved = localStorage.getItem(storageKey)
      if (saved) {
        const parsed = JSON.parse(saved)
        setQuota(parsed)
        updateTimeText(parsed.fetched_at)
        setError(null)
      }
    } catch {
      /* ignore */
    }
    // 默认自动静默读取最新数据（利用后端内存缓存，无风控风险，保证每次打开即读即显）
    loadQuota(false)
  }, [storageKey])

  useEffect(() => {
    if (quota?.fetched_at) {
      updateTimeText(quota.fetched_at)
    }
  }, [quota])

  const loadQuota = async (force = false) => {
    if (force) {
      if (cooldownSec > 0) return
      setRefreshing(true)
    } else {
      setLoading(true)
    }
    setError(null)

    try {
      const data = await getAccountQuota(providerId, accountId, force)
      if (data.ok) {
        setQuota(data)
        updateTimeText(data.fetched_at || Date.now() / 1000)
        try {
          localStorage.setItem(storageKey, JSON.stringify(data))
        } catch {
          // ignore storage quota errors
        }
        if (force) {
          // 强刷后启用 10 秒防抖倒计时，防止频繁向 Google 发送请求造成账号风控
          setCooldownSec(10)
        }
      } else {
        // 如果拉取失败但本地有缓存，不冲掉本地已有配额显示
        setError(data.error || '获取配额数据异常')
      }
    } catch (e: any) {
      setError(e.message || '网络连接超时')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  // 冷却倒计时逻辑
  useEffect(() => {
    if (cooldownSec <= 0) return
    const timer = setInterval(() => {
      setCooldownSec((prev) => (prev > 0 ? prev - 1 : 0))
    }, 1000)
    return () => clearInterval(timer)
  }, [cooldownSec])

  return {
    quota,
    loading,
    refreshing,
    error,
    cooldownSec,
    lastUpdatedText,
    loadQuota,
  }
}

const getQuotaLevel = (pct: number): 'ok' | 'warn' | 'danger' => {
  if (pct >= 50) return 'ok'
  if (pct >= 25) return 'warn'
  return 'danger'
}

function renderCockpitBucket(
  bucket?: QuotaBucketInfo,
  label: string = '5h',
  loading?: boolean,
) {
  const hasData = Boolean(bucket)
  const pct = bucket ? (bucket.percentage ?? 100) : 100
  const level = getQuotaLevel(pct)

  const resetText = bucket?.reset_in
    ? `${bucket.reset_in}${bucket.reset_formatted ? ` (${bucket.reset_formatted})` : ''}`
    : hasData
    ? '额度充裕'
    : loading
    ? '读取中...'
    : '—'

  return (
    <div className="ca-cp-bucket">
      <div className="ca-cp-bucket-header">
        <span className="ca-cp-bucket-label">{label}</span>
        <span
          className={`ca-cp-bucket-pct ${hasData ? level : 'idle'}`}
        >
          {hasData ? `${pct}%` : '—'}
        </span>
      </div>

      <div className="ca-cp-bar-track">
        <div
          className={`ca-cp-bar-fill ${level}`}
          style={{
            width: hasData ? `${Math.min(100, Math.max(0, pct))}%` : '0%',
          }}
        />
      </div>

      <div className="ca-cp-bucket-sub">
        <span className="ca-cp-reset-text">{resetText}</span>
      </div>
    </div>
  )
}

/**
 * Cockpit 风格一体化紧凑配额卡片（Claude 与 Gemini 合并为单个卡片，严丝合缝并排对齐）
 */
export function AccountQuotaBars({
  quota,
  loading,
}: {
  quota: AccountQuotaResult | null
  loading?: boolean
}) {
  const claude5h = quota?.claude?.five_hour
  const claudeWeekly = quota?.claude?.weekly
  const gemini5h = quota?.gemini?.five_hour
  const geminiWeekly = quota?.gemini?.weekly

  return (
    <div className="ca-cockpit-quota-card">
      {/* 顶部标题栏：Claude 与 Gemini 两列 */}
      <div className="ca-cp-header-row">
        <div className="ca-cp-col-head">
          <span className="ca-cp-col-title">Claude</span>
          <span className="ca-cp-vendor-tag tag-claude">Anthropic</span>
        </div>
        <div className="ca-cp-col-head">
          <span className="ca-cp-col-title">Gemini</span>
          <span className="ca-cp-vendor-tag tag-google">Google</span>
        </div>
      </div>

      {/* 5h 配额行（并排严格水平对齐） */}
      <div className="ca-cp-data-row">
        <div className="ca-cp-cell">
          {renderCockpitBucket(claude5h, '5 小时窗口', loading)}
        </div>
        <div className="ca-cp-cell">
          {renderCockpitBucket(gemini5h, '5 小时窗口', loading)}
        </div>
      </div>

      {/* 周配额行（并排严格水平对齐） */}
      <div className="ca-cp-data-row">
        <div className="ca-cp-cell">
          {renderCockpitBucket(claudeWeekly, '周配额', loading)}
        </div>
        <div className="ca-cp-cell">
          {renderCockpitBucket(geminiWeekly, '周配额', loading)}
        </div>
      </div>
    </div>
  )
}

/**
 * 兼容独立展示的配额卡片组件（保留以向下兼容弹窗等独立场景）
 */
export default function AccountQuotaCard({
  providerId,
  accountId,
  accountEmail,
  compact = false,
}: Props) {
  const {
    quota,
    loading,
    refreshing,
    error,
    cooldownSec,
    lastUpdatedText,
    loadQuota,
  } = useAccountQuota(providerId, accountId)

  // 若尚未手动查询过，渲染低侵入、100% 零网络请求的“按需查询”占位栏
  if (!quota && !loading) {
    return (
      <div className={`ca-quota-card-container ca-quota-idle-card ${compact ? 'is-compact' : ''}`}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <div className="ca-quota-icon-wrap" style={{ background: 'rgba(63, 185, 80, 0.1)', color: '#3fb950' }}>
              <ShieldCheck size={15} />
            </div>
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-color)' }}>
                  配额监控（安全防风控模式）
                </span>
                <span className="ca-quota-safe-badge">零后台轮询</span>
              </div>
              <div style={{ fontSize: '11px', color: 'var(--text-secondary)', marginTop: '2px' }}>
                不打开不查、不点不刷，杜绝后台频繁打请求给账号造成风控风险。
              </div>
            </div>
          </div>

          <button
            type="button"
            onClick={() => loadQuota(false)}
            className="ca-btn ca-btn-primary ca-btn-sm"
            style={{ flexShrink: 0, fontSize: '11px', padding: '5px 12px', gap: '5px' }}
          >
            <Zap size={12} />
            查询配额
          </button>
        </div>

        {error && (
          <div className="ca-quota-error" style={{ marginTop: '8px' }}>
            <Info size={13} />
            <span>{error}</span>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className={`ca-quota-card-container ${compact ? 'is-compact' : ''}`}>
      {/* 头部状态与防风控提示 */}
      <div className="ca-quota-header">
        <div className="ca-quota-title-box">
          <div className="ca-quota-icon-wrap">
            <Gauge size={14} />
          </div>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span className="ca-quota-title">实时配额余量</span>
              <span className="ca-quota-safe-badge" title="服务端已内置 5 分钟安全防风控静默缓存，防止频繁请求触发 Google 风险拦截">
                <ShieldCheck size={11} />
                安全防风控
              </span>
            </div>
            {accountEmail && (
              <span className="ca-quota-email-sub">{accountEmail}</span>
            )}
          </div>
        </div>

        <div className="ca-quota-actions">
          {lastUpdatedText && (
            <span className="ca-quota-cached-tag" title="本次会话中的更新时间">
              更新于 {lastUpdatedText}
            </span>
          )}
          <button
            type="button"
            onClick={() => loadQuota(true)}
            disabled={refreshing || loading || cooldownSec > 0}
            className="ca-quota-refresh-btn"
            title={cooldownSec > 0 ? `防频繁刷新冷却中 (${cooldownSec}s)` : '点击手动拉取最新官方配额'}
          >
            <RefreshCw
              size={12}
              className={refreshing || loading ? 'animate-spin' : ''}
            />
            {cooldownSec > 0 ? `${cooldownSec}s` : '刷新'}
          </button>
        </div>
      </div>

      {/* 错误提示 */}
      {error && (
        <div className="ca-quota-error">
          <Info size={13} />
          <span>{error}</span>
        </div>
      )}

      {/* 核心双列配额条 */}
      <AccountQuotaBars quota={quota} loading={loading} />
    </div>
  )
}
