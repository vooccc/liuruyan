import { RefreshCw, Download, ExternalLink, CheckCircle2, AlertCircle } from 'lucide-react'
import { useAutoUpdater } from '../../hooks/useAutoUpdater'

export default function UpdateSettings() {
  const {
    checking,
    updateAvailable,
    updateInfo,
    downloading,
    downloadProgress,
    error,
    checkForUpdates,
    startUpdate,
    openManualDownload,
  } = useAutoUpdater()

  return (
    <div className="update-settings-panel" style={{ padding: '24px', maxWidth: '720px' }}>
      <div style={{ marginBottom: '24px' }}>
        <h2 style={{ fontSize: '18px', fontWeight: 600, margin: 0, marginBottom: '8px' }}>软件版本与更新</h2>
        <p style={{ color: 'var(--text-secondary, #888)', fontSize: '13px', margin: 0 }}>
          柳如烟支持自动化安全更新，基于 Ed25519 数字签名防篡改。所有安装包均通过官方 GitHub Releases 分发。
        </p>
      </div>

      {/* 当前版本与检查卡片 */}
      <div
        style={{
          background: 'var(--bg-card, rgba(255, 255, 255, 0.04))',
          border: '1px solid var(--border-color, rgba(255, 255, 255, 0.08))',
          borderRadius: '12px',
          padding: '20px',
          marginBottom: '20px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div>
          <div style={{ fontSize: '12px', color: 'var(--text-secondary, #888)', marginBottom: '4px' }}>当前安装版本</div>
          <div style={{ fontSize: '20px', fontWeight: 700, fontFamily: 'monospace' }}>
            {updateInfo?.currentVersion ? `v${updateInfo.currentVersion}` : 'v2.0.17'}
          </div>
        </div>

        <div style={{ display: 'flex', gap: '10px' }}>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => checkForUpdates(true)}
            disabled={checking || downloading}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              padding: '8px 16px',
              borderRadius: '8px',
              cursor: checking || downloading ? 'not-allowed' : 'pointer',
            }}
          >
            <RefreshCw size={15} className={checking ? 'animate-spin' : ''} />
            <span>{checking ? '正在检查…' : '检查新版本'}</span>
          </button>

          <button
            type="button"
            className="btn btn-secondary"
            onClick={openManualDownload}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              padding: '8px 14px',
              borderRadius: '8px',
              cursor: 'pointer',
              background: 'transparent',
              border: '1px solid var(--border-color, rgba(255, 255, 255, 0.15))',
              color: 'inherit',
            }}
            title="前往 GitHub Releases 查看全部历史版本"
          >
            <ExternalLink size={14} />
            <span>手动下载</span>
          </button>
        </div>
      </div>

      {/* 状态消息 */}
      {error && !updateAvailable && (
        <div
          style={{
            padding: '12px 16px',
            borderRadius: '8px',
            background: error.includes('最新版本') ? 'rgba(34, 197, 94, 0.1)' : 'rgba(239, 68, 68, 0.1)',
            border: `1px solid ${error.includes('最新版本') ? 'rgba(34, 197, 94, 0.2)' : 'rgba(239, 68, 68, 0.2)'}`,
            color: error.includes('最新版本') ? '#4ade80' : '#f87171',
            fontSize: '13px',
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            marginBottom: '20px',
          }}
        >
          {error.includes('最新版本') ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
          <span>{error}</span>
        </div>
      )}

      {/* 发现新版本卡片 */}
      {updateAvailable && updateInfo && (
        <div
          style={{
            background: 'linear-gradient(180deg, rgba(59, 130, 246, 0.08) 0%, rgba(59, 130, 246, 0.02) 100%)',
            border: '1px solid rgba(59, 130, 246, 0.3)',
            borderRadius: '12px',
            padding: '20px',
            marginBottom: '20px',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <span style={{ fontSize: '16px', fontWeight: 600 }}>发现新版本: v{updateInfo.version}</span>
              <span
                style={{
                  fontSize: '11px',
                  background: '#3b82f6',
                  color: '#fff',
                  padding: '2px 8px',
                  borderRadius: '12px',
                  fontWeight: 600,
                }}
              >
                可更新
              </span>
            </div>
            {updateInfo.date && (
              <span style={{ fontSize: '12px', color: 'var(--text-secondary, #888)' }}>
                发布于 {new Date(updateInfo.date).toLocaleDateString()}
              </span>
            )}
          </div>

          {updateInfo.body && (
            <div
              style={{
                fontSize: '13px',
                color: 'var(--text-secondary, #aaa)',
                whiteSpace: 'pre-wrap',
                maxHeight: '160px',
                overflowY: 'auto',
                background: 'rgba(0, 0, 0, 0.2)',
                padding: '12px',
                borderRadius: '8px',
                marginBottom: '16px',
                fontFamily: 'monospace',
              }}
            >
              {updateInfo.body}
            </div>
          )}

          {downloading && (
            <div style={{ marginBottom: '16px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '12px', marginBottom: '6px' }}>
                <span>正在下载更新包并安装…</span>
                <span>{downloadProgress}%</span>
              </div>
              <div
                style={{
                  width: '100%',
                  height: '6px',
                  background: 'rgba(255, 255, 255, 0.1)',
                  borderRadius: '3px',
                  overflow: 'hidden',
                }}
              >
                <div
                  style={{
                    width: `${downloadProgress}%`,
                    height: '100%',
                    background: '#3b82f6',
                    transition: 'width 0.2s ease',
                  }}
                />
              </div>
            </div>
          )}

          <div style={{ display: 'flex', gap: '10px' }}>
            <button
              type="button"
              className="btn btn-primary"
              onClick={startUpdate}
              disabled={downloading}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '8px 18px',
                borderRadius: '8px',
                cursor: downloading ? 'not-allowed' : 'pointer',
                background: '#2563eb',
                color: '#fff',
                border: 'none',
                fontWeight: 500,
              }}
            >
              <Download size={15} />
              <span>{downloading ? '正在安装…' : '立即安装并重启'}</span>
            </button>

            <button
              type="button"
              onClick={openManualDownload}
              style={{
                padding: '8px 14px',
                borderRadius: '8px',
                cursor: 'pointer',
                background: 'transparent',
                border: '1px solid rgba(255, 255, 255, 0.15)',
                color: 'inherit',
                fontSize: '13px',
              }}
            >
              浏览器直接下载
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
