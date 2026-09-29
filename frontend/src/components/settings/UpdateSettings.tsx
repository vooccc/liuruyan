import { RefreshCw, Download, ExternalLink, CheckCircle2, AlertCircle, Eye } from 'lucide-react'
import { useUpdaterContext } from '../../context/UpdaterContext'

export default function UpdateSettings() {
  const {
    checking,
    updateAvailable,
    updateInfo,
    downloading,
    downloadProgress,
    error,
    currentInstalledVersion,
    openModal,
    checkForUpdates,
    startUpdate,
    openManualDownload,
  } = useUpdaterContext()

  return (
    <div className="update-settings-panel" style={{ padding: '24px', maxWidth: '720px' }}>
      {/* 沉稳固定的按钮交互样式，彻底移除 hover 上浮与晃动位移 */}
      <style>{`
        .update-settings-panel .update-btn-primary {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          padding: 8px 16px;
          border-radius: 8px;
          font-size: 13px;
          font-weight: 500;
          background: var(--primary-color, #c4a77d);
          color: #fff;
          border: none;
          cursor: pointer;
          transform: none !important;
          box-shadow: none !important;
          transition: filter 0.15s ease, opacity 0.15s ease, background 0.15s ease;
        }
        .update-settings-panel .update-btn-primary:hover:not(:disabled) {
          filter: brightness(1.06);
          transform: none !important;
          box-shadow: none !important;
        }
        .update-settings-panel .update-btn-primary:active:not(:disabled) {
          filter: brightness(0.96);
          transform: none !important;
        }
        .update-settings-panel .update-btn-primary:disabled {
          opacity: 0.6;
          cursor: not-allowed;
          transform: none !important;
          box-shadow: none !important;
        }
        .update-settings-panel .update-btn-secondary {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          padding: 8px 14px;
          border-radius: 8px;
          font-size: 13px;
          font-weight: 400;
          background: transparent;
          border: 1px solid var(--border-color, rgba(125, 125, 125, 0.25));
          color: inherit;
          cursor: pointer;
          transform: none !important;
          box-shadow: none !important;
          transition: background 0.15s ease, border-color 0.15s ease;
        }
        .update-settings-panel .update-btn-secondary:hover:not(:disabled) {
          background: var(--bg-tertiary, rgba(125, 125, 125, 0.08));
          border-color: var(--text-secondary, rgba(125, 125, 125, 0.35));
          transform: none !important;
          box-shadow: none !important;
        }
        .update-settings-panel .update-btn-secondary:active:not(:disabled) {
          transform: none !important;
        }
      `}</style>

      <div style={{ marginBottom: '24px' }}>
        <h2 style={{ fontSize: '18px', fontWeight: 600, margin: 0, marginBottom: '8px' }}>软件版本与更新</h2>
        <p style={{ color: 'var(--text-secondary, #888)', fontSize: '13px', margin: 0 }}>
          柳如烟支持自动化版本检测与一键极速更新，所有正式安装包均通过官方 GitHub Releases 安全分发。
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
            v{currentInstalledVersion}
          </div>
        </div>

        <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
          <button
            type="button"
            className="update-btn-primary"
            onClick={() => checkForUpdates(true)}
            disabled={checking || downloading}
          >
            <RefreshCw size={15} className={checking ? 'animate-spin' : ''} />
            <span>{checking ? '正在检查…' : '检查新版本'}</span>
          </button>

          <button
            type="button"
            className="update-btn-secondary"
            onClick={openManualDownload}
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

          <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            <button
              type="button"
              className="update-btn-primary"
              onClick={startUpdate}
              disabled={downloading}
              style={{ background: '#16a34a' }}
            >
              <Download size={15} />
              <span>{downloading ? '正在拉取下载…' : '一键下载最新安装包 (.exe)'}</span>
            </button>

            <button
              type="button"
              className="update-btn-secondary"
              onClick={openModal}
              title="打开版本更新详情卡片"
            >
              <Eye size={14} />
              <span>查看更新弹窗</span>
            </button>

            <button
              type="button"
              className="update-btn-secondary"
              onClick={() => window.open(updateInfo.htmlUrl || 'https://github.com/vooccc/liuruyan/releases', '_blank')}
            >
              查看 GitHub Release 说明
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
