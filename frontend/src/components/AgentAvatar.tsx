/**
 * AgentAvatar — 角色头像统一组件（Notion 风首字兜底）
 *
 * 显示优先级：
 *   1. src（图片 URL）→ 圆形图片
 *   2. name（角色名）→ 取首字 + 基于 name hash 的彩色背景
 *   3. avatar（emoji 字符串）→ 仅在 name 缺失时回退（向后兼容）
 *   4. 都没有 → 灰色圆 + User 剪影 SVG（"未填写"占位，比 '?' 友好）
 *
 * 设计取舍：
 *   - emoji 不再是默认兜底（项目历史包袱）。新调用点应只传 name。
 *   - avatar prop 保留是为了不一次性 break 所有旧调用；逐步迁移即可。
 *   - 首字提取规则：英文取大写首字母；中文取第一个字符；其它语言保持原样首字符。
 *   - 空状态用 User 剪影 SVG（对齐 Notion / Slack / Linear），不依赖 lucide。
 */
import { useEffect, useMemo, useState } from 'react'

interface AgentAvatarProps {
  /** 角色名字，用于生成首字头像 + 自动配色（推荐） */
  name?: string
  /** 图片 URL（最高优先级） */
  src?: string | null
  /** 弃用：旧调用点的 emoji 字符串。新代码请用 name */
  avatar?: string
  /** 三层都没有时的兜底文本，默认 '?' */
  fallbackAvatar?: string
  wrapperClassName?: string
  imageClassName?: string
  plainClassName?: string
}

// Notion / Linear 风的柔和调色板。基于 name hash 稳定取色，重命名前不变。
const INITIAL_PALETTE = [
  { bg: '#E8744D', fg: '#FFF' }, // 珊瑚
  { bg: '#D4A574', fg: '#FFF' }, // 沙金（项目主色调）
  { bg: '#7FB8FF', fg: '#FFF' }, // 天蓝
  { bg: '#9B7EDC', fg: '#FFF' }, // 紫
  { bg: '#5BB39A', fg: '#FFF' }, // 青绿
  { bg: '#E8669C', fg: '#FFF' }, // 玫红
  { bg: '#F0A93B', fg: '#FFF' }, // 琥珀
  { bg: '#6E7681', fg: '#FFF' }, // 中灰（保底）
]

function hashCode(str: string): number {
  let h = 0
  for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) | 0
  return Math.abs(h)
}

function getInitialChar(name: string): string {
  const trimmed = name.trim()
  if (!trimmed) return ''
  // 英文：取首字母大写
  if (/^[a-zA-Z]/.test(trimmed)) return trimmed[0].toUpperCase()
  // 中文/其它：取第一个字符（兼容 emoji 这种代理对，用 Array.from 安全切）
  return Array.from(trimmed)[0] || ''
}

/** Notion / Slack 风的"未填写"占位剪影。inline SVG，不依赖 lucide。 */
function PlaceholderUserIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
      focusable="false"
      className="agent-avatar-placeholder-svg"
    >
      <circle cx="12" cy="9" r="3.4" fill="currentColor" />
      <path
        d="M5.2 19c0-3.4 3-5.6 6.8-5.6s6.8 2.2 6.8 5.6"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  )
}

export default function AgentAvatar({
  name,
  src,
  avatar,
  fallbackAvatar = '?',
  wrapperClassName = 'avatar-with-fallback',
  imageClassName,
  plainClassName,
}: AgentAvatarProps) {
  const [imageFailed, setImageFailed] = useState(false)
  const showImage = Boolean(src) && !imageFailed

  useEffect(() => {
    setImageFailed(false)
  }, [src])

  // 决定渲染通道：
  //   'initial'     — name 有值 → 首字 + 彩色背景
  //   'emoji'       — 仅 avatar 有值（向后兼容旧调用）
  //   'placeholder' — 都没有 → 灰色圆 + User SVG（Notion 风"未填写"占位）
  //   'text'        — 走 fallbackAvatar 文本（极少出现）
  const channel = useMemo<'initial' | 'emoji' | 'placeholder' | 'text'>(() => {
    if (name && name.trim()) return 'initial'
    if (avatar) return 'emoji'
    if (!fallbackAvatar || fallbackAvatar === '?') return 'placeholder'
    return 'text'
  }, [name, avatar, fallbackAvatar])

  // 首字配色：基于 name 稳定 hash → 调色板。其它通道不上色。
  const colorStyle = useMemo(() => {
    if (channel !== 'initial' || !name) return undefined
    const palette = INITIAL_PALETTE[hashCode(name) % INITIAL_PALETTE.length]
    return { background: palette.bg, color: palette.fg } as React.CSSProperties
  }, [name, channel])

  if (!showImage) {
    if (plainClassName) {
      const modifier = channel === 'initial'
        ? ' is-initial'
        : channel === 'placeholder'
          ? ' is-placeholder'
          : ''
      return (
        <span className={`${plainClassName}${modifier}`} style={colorStyle}>
          {channel === 'placeholder' ? <PlaceholderUserIcon /> : null}
          {channel === 'initial' && getInitialChar(name!)}
          {channel === 'emoji' && avatar}
          {channel === 'text' && fallbackAvatar}
        </span>
      )
    }
    // 无 plainClassName 时退化为最朴素的输出
    if (channel === 'placeholder') return <PlaceholderUserIcon />
    if (channel === 'initial') return <>{getInitialChar(name!)}</>
    if (channel === 'emoji') return <>{avatar}</>
    return <>{fallbackAvatar}</>
  }

  return (
    <span className={wrapperClassName}>
      <img
        src={src || undefined}
        alt=""
        className={imageClassName}
        onError={() => setImageFailed(true)}
      />
    </span>
  )
}
