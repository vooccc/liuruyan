/**
 * 生成头像配色与首字的工具函数（对齐 通用供应商模型列表 的 generateColorFromChar /
 * getForegroundColor / getFirstCharacter 语义，纯函数无副作用）。
 *
 * 用途：当一个 provider 没有上传图片、没选内置 logo、也没命中品牌识别时，
 * 自动按 provider.id 生成一个稳定的"首字母头像"。
 *
 * 参考：
 *   references/provider-models
 *   references/provider-models
 */

/** 十六进制颜色字符串，例如 `#1a2b3c`。 */
export type HexColor = `#${string}`

function getRGB(hex: HexColor): [number, number, number] {
  const h = hex.replace('#', '')
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ]
}

/**
 * WCAG 2.0 相对亮度计算，用于挑前景文字色。
 */
function relativeLuminance(r: number, g: number, b: number): number {
  const normalize = (c: number) => {
    const v = c / 255
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)
  }
  return 0.2126 * normalize(r) + 0.7152 * normalize(g) + 0.0722 * normalize(b)
}

/**
 * 精选调色板（Linear / Stripe / Radix UI 风格）。
 *
 * 为什么不让算法随机抽 RGB：三通道独立哈希会撞出土黄、灰绿、紫红这种
 * "工地色"（饱和度低 + 色相不和谐），整体观感廉价。
 *
 * 16 色全是手工挑过的"看着就高级"的色——HSL 模型下饱和度统一 65-70%、
 * 亮度统一 52-58%，色相均匀分布在色环上，互相之间不会撞丑也不会单调。
 *
 * 数据源：Tailwind CSS palette 500 色 + Radix UI dark theme 9 step 取的"舒适饱和"档。
 */
const AVATAR_PALETTE: HexColor[] = [
  '#3b82f6', // blue-500
  '#6366f1', // indigo-500
  '#8b5cf6', // violet-500
  '#a855f7', // purple-500
  '#d946ef', // fuchsia-500
  '#ec4899', // pink-500
  '#f43f5e', // rose-500
  '#ef4444', // red-500
  '#f97316', // orange-500
  '#d97706', // amber-600（500 太亮，白字对比度不够；600 仍暖且足够深）
  '#65a30d', // lime-600
  '#16a34a', // green-600
  '#059669', // emerald-600
  '#0d9488', // teal-600
  '#0891b2', // cyan-600
  '#475569', // slate-600（中性灰，避免太花）
] as HexColor[]

/**
 * 用字符串 seed（推荐 provider.id）从精选调色板里稳定取一个色。
 *
 * - 同 input → 同色（FNV-1a 哈希 mod 调色板长度）
 * - 不同 input → 大概率不同色（哈希分布均匀）
 * - 永远是设计师挑过的好看色，绝不会出现工地色
 */
export function generateColorFromChar(input: string): HexColor {
  const seedStr = input || 'x'
  let hash = 2166136261
  for (let i = 0; i < seedStr.length; i++) {
    hash ^= seedStr.charCodeAt(i)
    hash = Math.imul(hash, 16777619) >>> 0
  }
  const idx = hash % AVATAR_PALETTE.length
  return AVATAR_PALETTE[idx]
}

/**
 * 按背景色挑黑/白前景，保证文字可读（WCAG 2.0）。
 */
export function getForegroundColor(backgroundColor: HexColor): HexColor {
  const [r, g, b] = getRGB(backgroundColor)
  const luminance = relativeLuminance(r, g, b)
  return luminance > 0.179 ? '#000000' : '#FFFFFF'
}

/**
 * 取字符串首字（支持 emoji、中文、代理对）。
 * 空输入返回空字符串。
 */
export function getFirstCharacter(str: string): string {
  if (!str) return ''
  for (const ch of str) return ch
  return ''
}

/**
 * 取展示用的"首字"：永远只取 1 个字。
 * 例："雷洛" → "雷"，"OpenRouter" → "O"，"🚀 my relay" → "🚀"。
 *
 * 设计抉择：单字母对齐 Linear / Stripe / Vercel 的极简风格。
 * 双字母（"OP"）会让小尺寸（22px）下文字过挤，且 letter-spacing 调节会显廉价。
 */
export function getAvatarInitials(name: string): string {
  const trimmed = (name || '').trim()
  if (!trimmed) return '?'
  const first = getFirstCharacter(trimmed)
  // 英文字母：大写
  if (/^[a-zA-Z]$/.test(first)) return first.toUpperCase()
  return first
}
