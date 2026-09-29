import { useEffect, useState } from 'react'

/**
 * 主视图类型
 * - companions: 伙伴（单聊）—— 日常陪伴、一对一对话
 * - roundtable: 议事厅（协作）—— 多角色协作、场景化
 */
export type PrimaryView = 'companions' | 'roundtable' | 'settings'


interface PrimaryRailProps {
  view: PrimaryView
  onChange: (next: PrimaryView) => void
  /** 进行中的协作数量（小徽标） */
  roundtableRunningCount?: number
  onTopMouseDown?: (e: React.MouseEvent) => void
}

/**
 * 左侧极简 Rail。
 *
 * 视觉语言：
 *   · 顶部 Logo（线稿 Vesica Piscis + 交集斜线）= 呼应 App Icon，但克制线性
 *   · 伙伴图标 = 圆环 + 内部柔波（独处呼吸、思绪流动）
 *   · 议事厅图标 = 双圆相交轮廓（协作、多方）
 *   · 激活态 = 3px 左强调条 + 金色微光 + 图标全彩
 *   · 非激活 = 中性灰，hover 暖金柔光
 *   · 顶部 Logo 槽高度对齐全局顶栏（38px），可拖拽窗口
 */
export default function PrimaryRail({
  view,
  onChange,
  roundtableRunningCount = 0,
  onTopMouseDown,
}: PrimaryRailProps) {
  // 悬浮提示气泡（自实现、轻盈，不依赖 title 默认延迟）
  const [hovered, setHovered] = useState<PrimaryView | null>(null)

  // 键盘快捷键：Cmd/Ctrl + 1 / 2
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return
      if (e.key === '1') {
        e.preventDefault()
        onChange('companions')
      } else if (e.key === '2') {
        e.preventDefault()
        onChange('roundtable')
      } else if (e.key === ',') {
        e.preventDefault()
        onChange('settings')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onChange])

  return (
    <aside className="primary-rail" aria-label="主视图切换">
      {/* ===== 顶部 Logo 槽（高度对齐全局顶栏，整块可拖拽窗口） ===== */}
      <div
        className="rail-top"
        onMouseDown={onTopMouseDown}
      >
        <div className="rail-logo" aria-hidden="true">
          <RailLogoMark />
        </div>
      </div>

      {/* ===== 视图按钮区 ===== */}
      <nav className="rail-nav" aria-label="视图导航">
        <RailButton
          active={view === 'companions'}
          onClick={() => onChange('companions')}
          onHover={(h) => setHovered(h ? 'companions' : null)}
          showTip={hovered === 'companions'}
          label="伙伴"
          shortcut="Ctrl+1"
          icon={<CompanionsIcon active={view === 'companions'} />}
        />
        <RailButton
          active={view === 'roundtable'}
          onClick={() => onChange('roundtable')}
          onHover={(h) => setHovered(h ? 'roundtable' : null)}
          showTip={hovered === 'roundtable'}
          label="议事厅"
          shortcut="Ctrl+2"
          badge={roundtableRunningCount > 0 ? roundtableRunningCount : undefined}
          icon={<RoundtableIcon active={view === 'roundtable'} />}
        />
        
      </nav>

      <div className="rail-footer" aria-hidden="true" />
    </aside>
  )
}

/* ========== 单个 Rail 按钮 ========== */

interface RailButtonProps {
  active: boolean
  onClick: () => void
  onHover: (hovered: boolean) => void
  showTip: boolean
  label: string
  shortcut?: string
  icon: React.ReactNode
  badge?: number
}

function RailButton({
  active,
  onClick,
  onHover,
  showTip,
  label,
  shortcut,
  icon,
  badge,
}: RailButtonProps) {
  return (
    <div className="rail-slot">
      <button
        type="button"
        className={`rail-btn ${active ? 'active' : ''}`}
        onClick={onClick}
        onMouseEnter={() => onHover(true)}
        onMouseLeave={() => onHover(false)}
        onFocus={() => onHover(true)}
        onBlur={() => onHover(false)}
        aria-label={label}
        aria-current={active ? 'page' : undefined}
      >
        <span className="rail-btn-accent" aria-hidden="true" />
        <span className="rail-btn-icon">{icon}</span>
        {typeof badge === 'number' && badge > 0 && (
          <span className="rail-btn-badge" aria-label={`${badge} 项进行中`}>
            {badge > 99 ? '99+' : badge}
          </span>
        )}
      </button>
      {showTip && (
        <span className="rail-tip" role="tooltip">
          {label}
          {shortcut && <span className="rail-tip-shortcut">{shortcut}</span>}
        </span>
      )}
    </div>
  )
}

/* ========== Logo 图标：双金环相交 + 冷色核（呼应 App Icon） ========== */
function RailLogoMark() {
  return (
    <svg viewBox="0 0 40 40" width="26" height="26" aria-hidden="true" className="rail-logo-svg">
      <defs>
        <linearGradient id="rail-logo-gold" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#f4dfb8" />
          <stop offset="50%" stopColor="#c4a77d" />
          <stop offset="100%" stopColor="#8a6c49" />
        </linearGradient>
        <radialGradient id="rail-logo-core" cx="0.5" cy="0.45" r="0.6">
          <stop offset="0%" stopColor="#ffffff" />
          <stop offset="55%" stopColor="#bdddff" />
          <stop offset="100%" stopColor="#4d9fe6" />
        </radialGradient>
        <clipPath id="rail-logo-clip-left">
          <circle cx="16" cy="20" r="9" />
        </clipPath>
      </defs>
      {/* 冷色交集核 */}
      <g clipPath="url(#rail-logo-clip-left)">
        <circle cx="24" cy="20" r="9" fill="url(#rail-logo-core)" opacity="0.9" />
      </g>
      {/* 双金环 */}
      <g fill="none" stroke="url(#rail-logo-gold)" strokeWidth="2.2">
        <circle cx="16" cy="20" r="9" />
        <circle cx="24" cy="20" r="9" />
      </g>
      {/* 中心亮点 */}
      <circle cx="20" cy="20" r="1.8" fill="#ffffff" />
    </svg>
  )
}

/* ========== 图标：伙伴（圆 + 内嵌柔波） ========== */
function CompanionsIcon({ active }: { active: boolean }) {
  const stroke = active ? 2.1 : 1.9
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
      {/* 圆环 */}
      <circle
        cx="12"
        cy="12"
        r="8.4"
        fill="none"
        stroke="currentColor"
        strokeWidth={active ? 1.9 : 1.6}
      />
      {/* 内部正弦波：独处时的呼吸 / 思绪流动。做得更显眼：振幅加大、线宽更粗 */}
      <path
        d="M 6 12 Q 8.5 7.8, 12 12 T 18 12"
        fill="none"
        stroke="currentColor"
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

/* ========== 图标：议事厅（双圆相交·呼应 App Icon） ========== */
function RoundtableIcon({ active }: { active: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true">
      {active && (
        <>
          <defs>
            <clipPath id="roundtable-clip-left">
              <circle cx="9.5" cy="12" r="6.4" />
            </clipPath>
          </defs>
          <g clipPath="url(#roundtable-clip-left)">
            <circle cx="14.5" cy="12" r="6.4" fill="currentColor" opacity="0.22" />
          </g>
        </>
      )}
      <g fill="none" stroke="currentColor" strokeWidth={active ? 1.9 : 1.6}>
        <circle cx="9.5" cy="12" r="6.4" />
        <circle cx="14.5" cy="12" r="6.4" />
      </g>
    </svg>
  )
}
