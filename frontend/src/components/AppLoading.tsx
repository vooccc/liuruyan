import { useEffect, useState } from 'react'

/**
 * 官方品牌几何智核（Vesica Piscis 双环流光）
 *
 * 与 public/favicon.svg 品牌规范一脉相承：
 * - 双圆相交神圣几何（Vesica Piscis）：象征双脑协同、多模型协作与辩证统一
 * - 金属暖金渐变双环：交错微动光轨
 * - 冰蓝智核（AI Core）：中心能量核呼吸绽放与微光晕
 */
export function VesicaLoadingLogo({ size = 76 }: { size?: number }) {
  return (
    <div className="vesica-wrapper" style={{ width: size, height: size }}>
      <div className="vesica-bloom" />
      <svg
        className="vesica-svg"
        viewBox="0 0 100 100"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        aria-label="柳如烟品牌智核"
      >
        <defs>
          {/* 金属暖金渐变 */}
          <linearGradient id="appLoadingGoldStroke" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="#f6e2bb" />
            <stop offset="45%" stopColor="#c4a77d" />
            <stop offset="100%" stopColor="#805d39" />
          </linearGradient>

          {/* 冰蓝智核能量渐变 */}
          <radialGradient id="appLoadingCoreGlowGrad" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#ffffff" />
            <stop offset="35%" stopColor="#c9e4ff" />
            <stop offset="70%" stopColor="#5aa8e6" />
            <stop offset="100%" stopColor="#2a6ea8" stopOpacity="0" />
          </radialGradient>
        </defs>

        {/* 左环：逆时针流转 */}
        <circle
          className="vesica-ring-left"
          cx="40"
          cy="50"
          r="22"
          stroke="url(#appLoadingGoldStroke)"
          strokeWidth="4.5"
        />

        {/* 右环：顺时针流转 */}
        <circle
          className="vesica-ring-right"
          cx="60"
          cy="50"
          r="22"
          stroke="url(#appLoadingGoldStroke)"
          strokeWidth="4.5"
        />

        {/* 中心相交高亮智核 */}
        <circle
          className="vesica-core-dot"
          cx="50"
          cy="50"
          r="6.5"
          fill="url(#appLoadingCoreGlowGrad)"
        />
        <circle cx="50" cy="50" r="2.5" fill="#ffffff" />

        {/* 四向辅助微锚点（科技感节点） */}
        <circle cx="50" cy="18" r="1.8" fill="#c4a77d" opacity="0.85" />
        <circle cx="50" cy="82" r="1.8" fill="#c4a77d" opacity="0.85" />
        <circle cx="18" cy="50" r="1.5" fill="#c4a77d" opacity="0.6" />
        <circle cx="82" cy="50" r="1.5" fill="#c4a77d" opacity="0.6" />
      </svg>
    </div>
  )
}

export interface AppLoadingProps {
  /** 品牌标题，默认「柳 如 烟」；传空串则隐藏 */
  title?: string
  /** 状态副标题；若不传，则自动按启动阶段流转 */
  subtitle?: string
  /** 是否展示底部的细微 Shimmer 流光进度条 */
  showProgress?: boolean
  /** 图标尺寸，默认 76px */
  logoSize?: number
}

const DEFAULT_SUBTITLES = [
  '正在初始化智能协同中枢...',
  '加载本地会话与模型管道...',
  '工作空间即将就绪...',
]

export function AppLoading({
  title = '柳 如 烟',
  subtitle,
  showProgress = true,
  logoSize = 76,
}: AppLoadingProps) {
  const [phase, setPhase] = useState(0)

  useEffect(() => {
    // 若外部指定了固定文本（如组件懒加载时的提示），则不自动流转
    if (subtitle) return

    const t1 = setTimeout(() => setPhase(1), 1200)
    const t2 = setTimeout(() => setPhase(2), 2800)
    return () => {
      clearTimeout(t1)
      clearTimeout(t2)
    }
  }, [subtitle])

  const currentSub = subtitle ?? DEFAULT_SUBTITLES[phase]

  return (
    <div className="app-loading" role="status" aria-live="polite">
      <div className="app-loading-ambient" />
      <VesicaLoadingLogo size={logoSize} />

      {title ? <h2 className="loading-brand-title">{title}</h2> : null}

      <p className="loading-status-text">
        <span className="loading-status-indicator" />
        <span>{currentSub}</span>
      </p>

      {showProgress && <div className="loading-progress-bar" />}
    </div>
  )
}

export default AppLoading
