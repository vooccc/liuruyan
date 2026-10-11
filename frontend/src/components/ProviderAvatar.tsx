/**
 * ProviderAvatar —— 服务商头像的统一渲染入口。
 *
 * 四层决策链（优先级从高到低）：
 *   1. 用户上传的自定义图片（IndexedDB 里按 provider.id 取）
 *   2. 用户从内置图库挑的系统 logo（provider.icon 字段存的是 BrandId，如 "openai"）
 *   3. 按 name / base_url 智能识别到的真品牌（resolveBrandId 命中）
 *   4. 首字母头像（generateColorFromChar(id) 按 id 哈希配色，保证不撞车）
 *
 * 为什么不按 transport 兜底：
 *   所有 anthropic_messages 自定义 provider 都长一样（用 custom-anthropic 图标），
 *   用户添加多个 Claude 中转站就撞车了，失去辨识度。改走首字母头像。
 *
 * 设计参考：
 *   references/provider-models
 *   references/provider-models
 */
import { memo, useEffect, useState, type CSSProperties } from 'react'

import { BrandIcon, resolveBrandId, type BrandId } from './BrandIcon'
import { providerLogoStorage } from '../services/providerLogoStorage'
import {
  generateColorFromChar,
  getAvatarInitials,
} from '../utils/avatarColor'

/* ---------------------------------------------------------------------------
 * 最小 provider-like 接口
 *
 * ProviderSettings / ManageModelsPopup 传完整的 ProviderView；
 * ChatModelPicker 只有 Model 的几个字段（id/name 用作 provider_id/provider_name，
 * 没 base_url）。所以组件只依赖最小字段，两边通用。
 * ------------------------------------------------------------------------- */

export interface ProviderLike {
  id: string
  name: string
  /** 用户从内置图库选的 BrandId，或者为空字符串表示未设置。 */
  icon?: string
  base_url?: string
  transport?: string
}

/* ---------------------------------------------------------------------------
 * 品牌识别：扩展 BrandIcon.resolveBrandId 的识别维度
 *
 * BrandIcon 只认一个字符串；provider 有 id/name/icon/base_url 多个线索，
 * 逐个尝试，返回第一个非 generic 命中。
 * ------------------------------------------------------------------------- */

function resolveBuiltinLogo(icon: string | undefined): BrandId | null {
  if (!icon) return null
  const b = resolveBrandId(icon)
  return b === 'generic' ? null : b
}

function resolveSmartBrand(provider: ProviderLike): BrandId | null {
  // 1. provider.id 本身（内置如 "openai" 直接命中）
  const byId = resolveBrandId(provider.id)
  if (byId !== 'generic' && !provider.id.startsWith('custom-')) return byId
  // 2. 用户起的 name
  const byName = resolveBrandId(provider.name)
  if (byName !== 'generic') return byName
  // 3. base_url 里的 host 关键词
  if (provider.base_url) {
    try {
      const host = new URL(provider.base_url).host
      const byHost = resolveBrandId(host)
      if (byHost !== 'generic') return byHost
    } catch {
      // base_url 不是合法 URL，跳过
    }
  }
  return null
}

/* ---------------------------------------------------------------------------
 * GeneratedAvatar —— 兜底的首字母头像
 * ------------------------------------------------------------------------- */

interface GeneratedAvatarProps {
  seed: string
  name: string
  size: number
  badge?: boolean
  className?: string
  style?: CSSProperties
}

const GeneratedAvatar = memo(function GeneratedAvatar({
  seed,
  name,
  size,
  badge = true,
  className,
  style,
}: GeneratedAvatarProps) {
  // badge=true：外框 size+10（对齐 BrandIcon badge）；
  // badge=false：外框就是 size 本身（用于分组 header / 下拉按钮这类紧凑位置）
  const boxSize = badge ? size + 10 : size
  const bg = generateColorFromChar(seed || name || 'x')
  // 永远白字 —— 调色板已保证所有色对白字 WCAG 对比度足够
  // 不再用 getForegroundColor 自动选黑/白（黄绿色判黑会破坏整体视觉一致性）
  const fg = '#ffffff'
  const initials = getAvatarInitials(name)

  // 字号 = boxSize × 0.5（Linear / Stripe 标准比例，留呼吸空间）
  const fontSize = Math.round(boxSize * 0.5)

  return (
    <span
      className={className}
      aria-label={name || 'provider'}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: boxSize,
        height: boxSize,
        // 24% 圆角：保持方块感（不至于像圆形头像），同时柔和不锐利
        borderRadius: Math.round(boxSize * 0.24),
        // 纯色背景 —— 比渐变更高级、更冷静（参考 Linear / Vercel）
        background: bg,
        // 柔和品牌色 drop shadow，让方块"漂浮"在背景上；不是 inset 假高光
        boxShadow: `0 1px 2px rgba(0, 0, 0, 0.08), 0 2px 8px color-mix(in srgb, ${bg} 28%, transparent)`,
        color: fg,
        fontSize,
        fontWeight: 500,
        lineHeight: 1,
        flexShrink: 0,
        userSelect: 'none',
        // 字体回退栈让中文用系统中文 + 英文用 Inter（已在项目里）
        fontFamily:
          'Inter, "Inter Variable", -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif',
        ...style,
      }}
    >
      {initials}
    </span>
  )
})

/* ---------------------------------------------------------------------------
 * 图片头像（用户上传）
 * ------------------------------------------------------------------------- */

interface ImageAvatarProps {
  src: string
  alt: string
  size: number
  badge?: boolean
  className?: string
  style?: CSSProperties
}

const ImageAvatar = memo(function ImageAvatar({
  src,
  alt,
  size,
  badge = true,
  className,
  style,
}: ImageAvatarProps) {
  const boxSize = badge ? size + 10 : size
  return (
    <span
      className={className}
      aria-label={alt}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: boxSize,
        height: boxSize,
        borderRadius: Math.round(boxSize * 0.28),
        overflow: 'hidden',
        background: 'var(--bg-color, transparent)',
        boxShadow: 'inset 0 0 0 1px color-mix(in srgb, currentColor 10%, transparent)',
        flexShrink: 0,
        ...style,
      }}
    >
      <img
        src={src}
        alt={alt}
        draggable={false}
        style={{
          width: '100%',
          height: '100%',
          objectFit: 'cover',
          userSelect: 'none',
          pointerEvents: 'none',
        }}
      />
    </span>
  )
})

/* ---------------------------------------------------------------------------
 * useProviderCustomLogo —— 查 IndexedDB 拿用户上传图片
 * ------------------------------------------------------------------------- */

function useProviderCustomLogo(providerId: string | undefined): {
  logo: string | null
  loading: boolean
} {
  const [logo, setLogo] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!providerId) {
      setLogo(null)
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    providerLogoStorage
      .get(providerId)
      .then((value) => {
        if (!cancelled) {
          setLogo(value)
          setLoading(false)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setLogo(null)
          setLoading(false)
        }
      })
    return () => {
      cancelled = true
    }
  }, [providerId])

  return { logo, loading }
}

/**
 * 广播 provider logo 变化（上传/删除后立刻刷新所有 avatar），
 * 免去各组件逐个订阅 IndexedDB 的繁琐。
 */
const LOGO_UPDATE_EVENT = 'liuruyan:provider-logo-changed'

export function notifyProviderLogoChanged(providerId: string): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(
    new CustomEvent(LOGO_UPDATE_EVENT, { detail: { providerId } }),
  )
}

function useLogoUpdateTick(providerId: string | undefined): number {
  const [tick, setTick] = useState(0)
  useEffect(() => {
    if (!providerId) return
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { providerId?: string } | undefined
      if (!detail?.providerId || detail.providerId === providerId) {
        setTick((t) => t + 1)
      }
    }
    window.addEventListener(LOGO_UPDATE_EVENT, handler)
    return () => window.removeEventListener(LOGO_UPDATE_EVENT, handler)
  }, [providerId])
  return tick
}

/* ---------------------------------------------------------------------------
 * 主组件
 * ------------------------------------------------------------------------- */

export interface ProviderAvatarProps {
  provider: ProviderLike
  size?: number
  /** 包外框的圆角方块（默认 true）；
   *  下拉按钮 / group header 等紧凑位置传 false。 */
  badge?: boolean
  className?: string
  style?: CSSProperties
}

function ProviderAvatarImpl({
  provider,
  size = 22,
  badge = true,
  className,
  style,
}: ProviderAvatarProps) {
  const tick = useLogoUpdateTick(provider.id)
  const { logo: customLogo } = useProviderCustomLogoWithTick(provider.id, tick)

  // 第 1 层：用户上传图片
  if (customLogo) {
    return (
      <ImageAvatar
        src={customLogo}
        alt={provider.name || provider.id}
        size={size}
        badge={badge}
        className={className}
        style={style}
      />
    )
  }

  // 第 2 层：用户从内置图库选的 BrandId
  const builtinBrand = resolveBuiltinLogo(provider.icon)
  if (builtinBrand) {
    return badge
      ? <BrandIcon id={builtinBrand} size={size} badge className={className} style={style} />
      : <BrandIcon id={builtinBrand} size={size} className={className} style={style} />
  }

  // 第 3 层：智能识别 name / base_url / id 命中真品牌
  const smartBrand = resolveSmartBrand(provider)
  if (smartBrand) {
    return badge
      ? <BrandIcon id={smartBrand} size={size} badge className={className} style={style} />
      : <BrandIcon id={smartBrand} size={size} className={className} style={style} />
  }

  // 第 4 层：首字母头像（按 id 哈希，永远不撞）
  return (
    <GeneratedAvatar
      seed={provider.id || provider.name}
      name={provider.name || provider.id}
      size={size}
      badge={badge}
      className={className}
      style={style}
    />
  )
}

export const ProviderAvatar = memo(ProviderAvatarImpl)

// hook 的 tick 变量变化时强制重查 IndexedDB
function useProviderCustomLogoWithTick(providerId: string | undefined, tick: number) {
  const [logo, setLogo] = useState<string | null>(null)
  useEffect(() => {
    if (!providerId) {
      setLogo(null)
      return
    }
    let cancelled = false
    providerLogoStorage
      .get(providerId)
      .then((value) => {
        if (!cancelled) setLogo(value)
      })
      .catch(() => {
        if (!cancelled) setLogo(null)
      })
    return () => {
      cancelled = true
    }
  }, [providerId, tick])
  return { logo }
}

/* ---------------------------------------------------------------------------
 * 辅助导出（给 ProviderLogoPicker / Editor 用）
 * ------------------------------------------------------------------------- */

export {
  GeneratedAvatar,
  ImageAvatar,
  resolveSmartBrand,
  resolveBuiltinLogo,
  useProviderCustomLogo,
}
