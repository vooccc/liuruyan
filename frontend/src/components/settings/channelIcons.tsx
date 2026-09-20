/**
 * 频道品牌图标 —— 手写简化版 SVG。
 *
 * 使用各家品牌色 + 几何简化几何元素，足以让用户一眼识别，
 * 同时避免直接拷贝官方 PNG/SVG（trademark fair use within software UI）。
 *
 * 全部 24x24，圆角矩形/圆形底色 + 内部白色简笔。
 */

interface IconProps {
  size?: number
}

/** 钉钉：蓝色圆角方 + 简化"叮"的曲线笔画 */
export function DingTalkIcon({ size = 24 }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
      <rect width="24" height="24" rx="6" fill="#007FFF" />
      <path
        d="M7 8.2 C 9.5 7.2, 14.5 7.2, 17 8.2 L 16 11.5 L 11 11 L 13.5 13.5 L 9.5 18 L 11 13.5 L 7.5 13 Z"
        fill="#fff"
      />
    </svg>
  )
}

/** 飞书：青蓝渐变方 + 飞翔翼形 */
export function FeishuIcon({ size = 24 }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
      <defs>
        <linearGradient id="feishu-grad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#3370ff" />
          <stop offset="100%" stopColor="#00d6b9" />
        </linearGradient>
      </defs>
      <rect width="24" height="24" rx="6" fill="url(#feishu-grad)" />
      {/* 双翼相交：左翼 */}
      <path d="M5 14 C 8 8, 13 6, 18 8 C 14 12, 10 14, 5 14 Z" fill="#fff" opacity="0.95" />
      {/* 右翼 */}
      <path d="M19 17 C 16 13, 11 11, 6 13 C 10 17, 14 18, 19 17 Z" fill="#fff" opacity="0.7" />
    </svg>
  )
}

/** Telegram：蓝色圆 + 纸飞机 */
export function TelegramIcon({ size = 24 }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
      <circle cx="12" cy="12" r="12" fill="#229ED9" />
      {/* 纸飞机：三角主体 + 折翼 */}
      <path
        d="M5 11.5 L 18 6.5 L 16 17 L 11.5 14 L 9 16.5 L 9 13 L 16 8 L 8 12.5 Z"
        fill="#fff"
      />
    </svg>
  )
}

/** QQ：青色圆 + 简化企鹅头 */
export function QQIcon({ size = 24 }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
      <circle cx="12" cy="12" r="12" fill="#12B7F5" />
      {/* 企鹅身体（白色椭圆） */}
      <ellipse cx="12" cy="13.5" rx="5.5" ry="6" fill="#fff" />
      {/* 头部 */}
      <ellipse cx="12" cy="9.5" rx="4.5" ry="4" fill="#1a1a1a" />
      {/* 白肚子 */}
      <ellipse cx="12" cy="14" rx="3.2" ry="4" fill="#fff" />
      {/* 眼白 */}
      <ellipse cx="10.3" cy="9" rx="1.1" ry="1.4" fill="#fff" />
      <ellipse cx="13.7" cy="9" rx="1.1" ry="1.4" fill="#fff" />
      {/* 瞳 */}
      <ellipse cx="10.5" cy="9.2" rx="0.4" ry="0.7" fill="#1a1a1a" />
      <ellipse cx="13.5" cy="9.2" rx="0.4" ry="0.7" fill="#1a1a1a" />
      {/* 嘴 */}
      <path d="M10.5 11 Q 12 12.2, 13.5 11 L 12 12.2 Z" fill="#ff8000" />
    </svg>
  )
}

/** Matrix：青绿色圆角方 + 简化的 [m] 字母 / 双向箭头几何 */
export function MatrixIcon({ size = 24 }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
      <rect width="24" height="24" rx="6" fill="#0DBD8B" />
      {/* 左竖线 */}
      <rect x="4" y="5" width="1.6" height="14" fill="#fff" />
      <rect x="3" y="5" width="3.6" height="1.4" fill="#fff" />
      <rect x="3" y="17.6" width="3.6" height="1.4" fill="#fff" />
      {/* 右竖线 */}
      <rect x="18.4" y="5" width="1.6" height="14" fill="#fff" />
      <rect x="17.4" y="5" width="3.6" height="1.4" fill="#fff" />
      <rect x="17.4" y="17.6" width="3.6" height="1.4" fill="#fff" />
      {/* 中间简化 [m] 字母：三道竖+两道连接 */}
      <path
        d="M8 17 L 8 9 L 10.5 9 L 12 12 L 13.5 9 L 16 9 L 16 17 L 14.4 17 L 14.4 11.5 L 12.6 14.5 L 11.4 14.5 L 9.6 11.5 L 9.6 17 Z"
        fill="#fff"
      />
    </svg>
  )
}

/** 微信：绿色圆角方 + 双气泡 */
export function WeChatIcon({ size = 24 }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
      <rect width="24" height="24" rx="6" fill="#07C160" />
      {/* 大气泡（左） */}
      <path
        d="M9 5 C 5 5, 2.5 7.6, 2.5 11 C 2.5 12.7, 3.4 14.2, 4.7 15.2 L 4 17 L 6 16 C 6.7 16.2, 7.4 16.4, 8.2 16.4 C 8.4 13.4, 11 11, 14.2 11 C 14.4 11, 14.6 11, 14.8 11 C 14.4 7.5, 12 5, 9 5 Z"
        fill="#fff"
      />
      <circle cx="6.5" cy="9.5" r="0.7" fill="#07C160" />
      <circle cx="11.5" cy="9.5" r="0.7" fill="#07C160" />
      {/* 小气泡（右） */}
      <path
        d="M21 16 C 21 13.5, 18.6 11.5, 15.7 11.5 C 12.8 11.5, 10.4 13.5, 10.4 16 C 10.4 18.5, 12.8 20.5, 15.7 20.5 C 16.4 20.5, 17 20.4, 17.6 20.2 L 19.4 21 L 18.8 19.6 C 20.1 18.8, 21 17.5, 21 16 Z"
        fill="#fff"
      />
      <circle cx="14" cy="15.6" r="0.55" fill="#07C160" />
      <circle cx="17.4" cy="15.6" r="0.55" fill="#07C160" />
    </svg>
  )
}
