import { useEffect } from 'react'
import {
  Download,
  ExternalLink,
  Sparkles,
  X,
  ArrowRight,
  CheckCircle2,
  Loader2,
} from 'lucide-react'
import { useUpdaterContext } from '../context/UpdaterContext'

function renderFormattedText(text: string) {
  const parts = text.split(/(\*\*.*?\*\*)/g)
  return parts.map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return (
        <strong key={i} style={{ color: 'var(--text-color, #222)', fontWeight: 600 }}>
          {part.slice(2, -2)}
        </strong>
      )
    }
    return part
  })
}

export function UpdateModal() {
  const {
    showModal,
    closeModal,
    updateInfo,
    currentInstalledVersion,
    downloading,
    startUpdate,
    openManualDownload,
  } = useUpdaterContext()

  // 监听 ESC 键关闭弹窗
  useEffect(() => {
    if (!showModal) return
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        closeModal()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [showModal, closeModal])

  if (!showModal || !updateInfo) {
    return null
  }

  const notes = updateInfo.body || '本次更新包含多项稳定性改进与体验优化。'

  return (
    <div
      className="update-modal-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeModal()
      }}
    >
      <div className="update-modal-container" onClick={(e) => e.stopPropagation()}>
        {/* 弹窗头部 */}
        <div className="update-modal-header">
          <div className="update-modal-title-group">
            <div className="update-modal-icon-badge">
              <Sparkles size={20} />
            </div>
            <div>
              <h2 className="update-modal-title">发现新版本 v{updateInfo.version}</h2>
              <p className="update-modal-subtitle">
                当前版本: v{currentInstalledVersion}
                {updateInfo.date ? ` · 发布于 ${updateInfo.date}` : ''}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={closeModal}
            className="update-modal-close-btn"
            title="关闭"
          >
            <X size={16} />
          </button>
        </div>

        {/* 版本跃迁对比条 */}
        <div className="update-modal-version-bar">
          <div className="version-pill current">
            <span className="version-label">当前运行</span>
            <span className="version-val">v{currentInstalledVersion}</span>
          </div>

          <div className="version-arrow">
            <ArrowRight size={18} />
          </div>

          <div className="version-pill target">
            <span className="version-label">最新发布</span>
            <span className="version-val">v{updateInfo.version}</span>
          </div>
        </div>

        {/* 弹窗内容区：更新日志 */}
        <div className="update-modal-body">
          <div className="update-modal-section-title">
            <CheckCircle2 size={15} style={{ color: 'var(--accent-green, #16a34a)' }} />
            <span>更新日志与特性</span>
          </div>

          <div className="update-modal-notes-scroll">
            {notes.split('\n').map((line, idx) => {
              const trimmed = line.trim()
              if (!trimmed) return <div key={idx} style={{ height: '8px' }} />
              if (trimmed.startsWith('###') || trimmed.startsWith('##') || trimmed.startsWith('#')) {
                return (
                  <h4 key={idx} className="update-note-heading">
                    {renderFormattedText(trimmed.replace(/^#+\s*/, ''))}
                  </h4>
                )
              }
              if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
                return (
                  <div key={idx} className="update-note-bullet">
                    <span className="bullet-dot">•</span>
                    <span>{renderFormattedText(trimmed.replace(/^[-*]\s*/, ''))}</span>
                  </div>
                )
              }
              return (
                <p key={idx} className="update-note-text">
                  {renderFormattedText(trimmed)}
                </p>
              )
            })}
          </div>
        </div>

        {/* 弹窗底部操作栏 */}
        <div className="update-modal-footer">
          <button
            type="button"
            onClick={closeModal}
            className="update-btn-ghost"
          >
            稍后再说
          </button>

          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <button
              type="button"
              onClick={openManualDownload}
              className="update-btn-outline"
              title="前往 GitHub Releases 官方发布页"
            >
              <ExternalLink size={13} />
              GitHub Releases
            </button>

            <button
              type="button"
              onClick={startUpdate}
              disabled={downloading}
              className="update-btn-primary"
            >
              {downloading ? (
                <>
                  <Loader2 size={14} className="animate-spin" />
                  准备下载...
                </>
              ) : (
                <>
                  <Download size={14} />
                  立即下载升级
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

export default UpdateModal
