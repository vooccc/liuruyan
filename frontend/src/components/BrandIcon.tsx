/**
 * 统一品牌图标组件，走 `@lobehub/icons` 的 Mono 子模块。
 *
 * 为什么只用 Mono：
 * - lobehub 每个 provider 的顶层 index.js 会静态 import Avatar 子组件；
 *   Avatar 内部又依赖 `@lobehub/ui` 的 Center 组件——那是一整套 antd-based
 *   UI 库，装进来会把 bundle 膨到 MB 级。我们不需要它。
 * - 只 import `<Provider>/components/Mono`，Vite 的静态分析链就只进那一个
 *   文件（Mono 只依赖 React + 本地 style 常量），完全绕开 @lobehub/ui。
 * - Mono 自己是单色 SVG（用 currentColor），我们在外层包一个 <span>，通过
 *   CSS `color: brand-primary` 给它上品牌色——视觉上等价于官方彩色 logo
 *   的主色版本，但零重型依赖。
 *
 * API 向下兼容：
 *   <BrandIcon id="openrouter" size={22} badge />   // 圆形品牌底 + 主色 logo
 *   <BrandIcon id="deepseek" size={16} monochrome /> // 跟随 currentColor（主题色）
 *   <BrandIcon id="openai" size={20} />              // 仅 logo（带品牌主色）
 */
import { memo, type CSSProperties } from 'react'
import { Bot } from 'lucide-react'

// ── 按需 import：Mono（单色） + Color（真彩，部分品牌有）─────────────────
// Mono：所有品牌都有，是 currentColor 单色 SVG，给紧凑/单色场景用
// Color：部分品牌有，是带渐变/多色的官方真彩 SVG（如 Gemini 四色星、DeepSeek 蓝鲸等）
//   只依赖 useFillIds hook + 各自 style 常量，不引入 @lobehub/ui，bundle 安全
// Anthropic 公司 logo 是字母 A 几何，但用户提到 Anthropic 大都期待 Claude 的橙花瓣
// 所以 anthropic brandId 实际渲染走 Claude 资源
import AnthropicMono from '@lobehub/icons/es/Claude/components/Mono'
import AnthropicColor from '@lobehub/icons/es/Claude/components/Color'
import DeepSeekMono from '@lobehub/icons/es/DeepSeek/components/Mono'
import GeminiMono from '@lobehub/icons/es/Gemini/components/Mono'
import KimiMono from '@lobehub/icons/es/Kimi/components/Mono'
import LmStudioMono from '@lobehub/icons/es/LmStudio/components/Mono'
import MinimaxMono from '@lobehub/icons/es/Minimax/components/Mono'
import ModelScopeMono from '@lobehub/icons/es/ModelScope/components/Mono'
import OllamaMono from '@lobehub/icons/es/Ollama/components/Mono'
import OpenAIMono from '@lobehub/icons/es/OpenAI/components/Mono'
import OpenRouterMono from '@lobehub/icons/es/OpenRouter/components/Mono'
import QwenMono from '@lobehub/icons/es/Qwen/components/Mono'
import SiliconCloudMono from '@lobehub/icons/es/SiliconCloud/components/Mono'
import ZhipuMono from '@lobehub/icons/es/Zhipu/components/Mono'
// 历史兼容（老 preset id 引用）
import MistralMono from '@lobehub/icons/es/Mistral/components/Mono'
import CohereMono from '@lobehub/icons/es/Cohere/components/Mono'
import GroqMono from '@lobehub/icons/es/Groq/components/Mono'
import BaiduMono from '@lobehub/icons/es/Baidu/components/Mono'
import GoogleMono from '@lobehub/icons/es/Google/components/Mono'
import TogetherMono from '@lobehub/icons/es/Together/components/Mono'
import DoubaoMono from '@lobehub/icons/es/Doubao/components/Mono'
import StepfunMono from '@lobehub/icons/es/Stepfun/components/Mono'
import YiMono from '@lobehub/icons/es/Yi/components/Mono'
import SparkMono from '@lobehub/icons/es/Spark/components/Mono'
import IFlyTekCloudMono from '@lobehub/icons/es/IFlyTekCloud/components/Mono'

// Color 真彩 SVG（badge 模式优先用，让 Gemini 是渐变四色星、DeepSeek 是蓝鲸等）
import DeepSeekColor from '@lobehub/icons/es/DeepSeek/components/Color'
import GeminiColor from '@lobehub/icons/es/Gemini/components/Color'
// 这几个 Color 在小尺寸下不清晰（path 复杂或白色细节在白底消失），故意走 Mono：
//   KimiColor（白色细节在白底消失）/ MinimaxColor / SiliconCloudColor / ZhipuColor
import ModelScopeColor from '@lobehub/icons/es/ModelScope/components/Color'
import QwenColor from '@lobehub/icons/es/Qwen/components/Color'
import DoubaoColor from '@lobehub/icons/es/Doubao/components/Color'
import SparkColor from '@lobehub/icons/es/Spark/components/Color'
import IFlyTekCloudColor from '@lobehub/icons/es/IFlyTekCloud/components/Color'
import MistralColor from '@lobehub/icons/es/Mistral/components/Color'
import CohereColor from '@lobehub/icons/es/Cohere/components/Color'
import BaiduColor from '@lobehub/icons/es/Baidu/components/Color'
import GoogleColor from '@lobehub/icons/es/Google/components/Color'
import TogetherColor from '@lobehub/icons/es/Together/components/Color'
import StepfunColor from '@lobehub/icons/es/Stepfun/components/Color'
import YiColor from '@lobehub/icons/es/Yi/components/Color'

export type BrandId =
  | 'openrouter' | 'openai' | 'anthropic' | 'deepseek' | 'gemini'
  | 'kimi' | 'zhipu' | 'siliconflow' | 'dashscope' | 'modelscope'
  | 'minimax' | 'ollama' | 'lmstudio'
  | 'spark' | 'iflytek'
  // 老 preset id / 历史兼容
  | 'moonshot' | 'qwen' | 'astron' | 'custom-openai' | 'custom-anthropic'
  | 'google' | 'alibaba' | 'baidu' | 'cohere' | 'mistral' | 'stepfun'
  | 'yi' | 'groq' | 'together' | 'zero-one' | 'doubao'
  | 'generic'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type LobeMono = any

/** brand id → Mono 组件映射 */
const BRANDS: Record<BrandId, LobeMono> = {
  openrouter: OpenRouterMono,
  openai: OpenAIMono,
  anthropic: AnthropicMono,
  deepseek: DeepSeekMono,
  gemini: GeminiMono,
  kimi: KimiMono,
  zhipu: ZhipuMono,
  siliconflow: SiliconCloudMono,
  dashscope: QwenMono,
  modelscope: ModelScopeMono,
  minimax: MinimaxMono,
  ollama: OllamaMono,
  lmstudio: LmStudioMono,
  spark: SparkMono,
  iflytek: IFlyTekCloudMono,
  // 老 preset id
  moonshot: KimiMono,
  qwen: QwenMono,
  // astron 是历史 preset id（讯飞星辰系列），借用 Spark 图标；以前是 OpenAIMono 占位，会误导用户
  astron: SparkMono,
  'custom-openai': OpenAIMono,
  'custom-anthropic': AnthropicMono,
  google: GoogleMono,
  alibaba: QwenMono,
  baidu: BaiduMono,
  cohere: CohereMono,
  mistral: MistralMono,
  stepfun: StepfunMono,
  yi: YiMono,
  groq: GroqMono,
  together: TogetherMono,
  'zero-one': YiMono,
  doubao: DoubaoMono,
  // generic 之前用 OpenAIMono 占位，会让未知 provider（custom-xxx + 没填名字）
  // 显示 OpenAI 图标，严重误导用户。换成 lucide Bot 作为中性 AI 图标。
  generic: Bot,
}

/** Color 真彩组件映射：仅部分品牌有；其它落回 Mono + 染色。 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const BRAND_COLOR_ICONS: Partial<Record<BrandId, any>> = {
  // 实测在 22-32px 小尺寸下清晰的：DeepSeek 蓝鲸、Gemini 四色星、Doubao、Spark 等
  deepseek: DeepSeekColor,
  gemini: GeminiColor,
  modelscope: ModelScopeColor,
  qwen: QwenColor,
  dashscope: QwenColor,
  alibaba: QwenColor,
  doubao: DoubaoColor,
  spark: SparkColor,
  astron: SparkColor,
  iflytek: IFlyTekCloudColor,
  mistral: MistralColor,
  cohere: CohereColor,
  baidu: BaiduColor,
  google: GoogleColor,
  together: TogetherColor,
  stepfun: StepfunColor,
  yi: YiColor,
  'zero-one': YiColor,
  // Claude 橙色花瓣（Anthropic 品牌下用户期待的 logo）
  anthropic: AnthropicColor,
  'custom-anthropic': AnthropicColor,
  // 以下在小尺寸下股为一团糊帜，回退到 Mono + 品牌色底：
  // siliconflow 、zhipu 、minimax —— Mono 几何线条更锐利
}

/**
 * lobehub 官方 Avatar 配置（来自 `@lobehub/icons/es/<Brand>/style.js`）。
 *
 * 这是品牌官方推荐的"头像应该长什么样"配色：
 *   - background: 头像背景（纯色 or 渐变 string）
 *   - color:      Mono logo 染色（用 Color 子组件时被忽略）
 *   - iconMultiple: logo 占头像的比例（留 padding 用，0.6-0.8）
 *
 * 反向举例：
 *   - Anthropic 是米白底 + 深色 logo（不是橙底白 logo）—— 这是 Anthropic 真正的品牌识别
 *   - Gemini 是白底 + 真彩四色渐变星（不能染色）
 *   - Minimax 是粉红橙渐变底 + 白 logo（gradient 字符串）
 */
interface LobeAvatar {
  background: string
  color: string
  iconMultiple: number
}
const LOBE_AVATAR: Partial<Record<BrandId, LobeAvatar>> = {
  openrouter:  { background: '#6566F1', color: '#fff',     iconMultiple: 0.75 },
  openai:      { background: '#000',    color: '#fff',     iconMultiple: 0.75 },
  // Claude 官方品牌色：橙色 #D97757 + 白花瓣（跳出“Anthropic公司的字母 A”诡异）
  anthropic:   { background: '#D97757', color: '#fff',     iconMultiple: 0.7  },
  deepseek:    { background: '#4D6BFE', color: '#fff',     iconMultiple: 0.75 },
  gemini:      { background: '#fff',    color: '#fff',     iconMultiple: 0.8  },
  kimi:        { background: '#000',    color: '#fff',     iconMultiple: 0.7  },
  zhipu:       { background: '#3859FF', color: '#fff',     iconMultiple: 0.75 },
  siliconflow: { background: '#7c3aed', color: '#fff',     iconMultiple: 0.7  },
  dashscope:   { background: '#615ced', color: '#fff',     iconMultiple: 0.75 },
  modelscope:  { background: '#624aff', color: '#fff',     iconMultiple: 0.75 },
  minimax:     { background: 'linear-gradient(to right, #E2167E, #FE603C)', color: '#fff', iconMultiple: 0.75 },
  ollama:      { background: '#fff',    color: '#000',     iconMultiple: 0.75 },
  lmstudio:    { background: 'linear-gradient(135deg, #6C78EF, #4F14BE)', color: '#fff', iconMultiple: 0.7 },
  spark:       { background: '#0066cc', color: '#fff',     iconMultiple: 0.75 },
  iflytek:     { background: '#0066cc', color: '#fff',     iconMultiple: 0.75 },
  // 老 preset id / 历史兼容
  moonshot:    { background: '#000',    color: '#fff',     iconMultiple: 0.7  },
  qwen:        { background: '#615ced', color: '#fff',     iconMultiple: 0.75 },
  astron:      { background: '#0066cc', color: '#fff',     iconMultiple: 0.75 },
  google:      { background: '#fff',    color: '#fff',     iconMultiple: 0.75 },
  alibaba:     { background: '#615ced', color: '#fff',     iconMultiple: 0.75 },
  baidu:       { background: '#fff',    color: '#fff',     iconMultiple: 0.75 },
  cohere:      { background: '#39594D', color: '#fff',     iconMultiple: 0.6  },
  mistral:     { background: '#000',    color: '#fff',     iconMultiple: 0.75 },
  stepfun:     { background: '#005aff', color: '#fff',     iconMultiple: 0.6  },
  yi:          { background: '#003425', color: '#fff',     iconMultiple: 0.75 },
  groq:        { background: '#f55036', color: '#fff',     iconMultiple: 0.75 },
  together:    { background: '#fff',    color: '#000',     iconMultiple: 0.75 },
  'zero-one':  { background: '#003425', color: '#fff',     iconMultiple: 0.75 },
  doubao:      { background: '#fff',    color: '#fff',     iconMultiple: 0.75 },
  'custom-openai':    { background: '#000',    color: '#fff',    iconMultiple: 0.75 },
  'custom-anthropic': { background: '#D97757', color: '#fff', iconMultiple: 0.7 },
  generic:     { background: '#6b7280', color: '#fff',     iconMultiple: 0.75 },
}

/** 品牌主色（lobehub colorPrimary 对应的 hex；手动维护一份，避免拉顶层 index） */
const BRAND_COLORS: Partial<Record<BrandId, string>> = {
  openrouter: '#6467f2',
  openai: '#10a37f',
  anthropic: '#d97757',
  deepseek: '#4d6bfe',
  gemini: '#1c69ff',
  kimi: '#16191e',
  zhipu: '#3859ff',
  siliconflow: '#7c3aed',
  dashscope: '#615ced',
  modelscope: '#624aff',
  minimax: '#f23f5d',
  ollama: '#6b6b6b',
  lmstudio: '#4b5563',
  moonshot: '#16191e',
  qwen: '#615ced',
  astron: '#0066cc',
  google: '#4285f4',
  alibaba: '#615ced',
  baidu: '#2932e1',
  cohere: '#ff7759',
  mistral: '#ff7000',
  stepfun: '#005aff',
  yi: '#003425',
  groq: '#f55036',
  together: '#0f6fff',
  'zero-one': '#003425',
  doubao: '#4e75ff',
  'custom-openai': '#6b7280',
  'custom-anthropic': '#d97757',
  spark: '#0066cc',
  iflytek: '#0066cc',
  generic: '#6b7280',
}

const DISPLAY_NAME: Partial<Record<BrandId, string>> = {
  openrouter: 'OpenRouter',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  deepseek: 'DeepSeek',
  gemini: 'Gemini',
  kimi: 'Kimi',
  zhipu: '智谱',
  siliconflow: '硅基流动',
  dashscope: '阿里 DashScope',
  modelscope: '魔搭 ModelScope',
  minimax: 'MiniMax',
  ollama: 'Ollama',
  lmstudio: 'LM Studio',
  moonshot: 'Moonshot',
  qwen: 'Qwen',
  spark: '讯飞星火',
  iflytek: '讯飞云',
  astron: '讯飞星辰',
  'custom-openai': '自定义 OpenAI',
  'custom-anthropic': '自定义 Anthropic',
  google: 'Google',
  baidu: '百度',
  cohere: 'Cohere',
  mistral: 'Mistral',
  stepfun: '阶跃星辰',
  yi: '零一万物',
  groq: 'Groq',
  together: 'Together',
  'zero-one': '零一万物',
  doubao: '豆包',
  alibaba: '阿里云',
  generic: 'Model',
}

/** 把任意输入字符串解析到 BrandId。 */
export function resolveBrandId(raw: string | null | undefined): BrandId {
  if (!raw) return 'generic'
  const key = raw.toLowerCase().trim()
  if (key in BRANDS) return key as BrandId
  if (key.includes('openrouter') || key === 'or') return 'openrouter'
  if (key.includes('openai') || key === 'gpt') return 'openai'
  if (key.includes('anthropic') || key.includes('claude')) return 'anthropic'
  if (key.includes('deepseek')) return 'deepseek'
  if (key.includes('gemini')) return 'gemini'
  if (key.includes('google')) return 'google'
  if (key.includes('moonshot') || key.includes('kimi')) return 'kimi'
  if (key.includes('zhipu') || key.includes('glm') || key.includes('bigmodel') || key.includes('chatglm')) return 'zhipu'
  if (key.includes('siliconflow') || key.includes('silicon')) return 'siliconflow'
  if (key.includes('dashscope') || key.includes('qwen') || key.includes('alibaba') || key.includes('aliyun') || key.includes('bailian')) return 'dashscope'
  if (key.includes('modelscope')) return 'modelscope'
  if (key.includes('minimax')) return 'minimax'
  if (key.includes('ollama')) return 'ollama'
  if (key.includes('lmstudio') || key.includes('lm-studio') || key.includes('lm_studio') || key.includes('lm studio')) return 'lmstudio'
  if (key.includes('baidu')) return 'baidu'
  if (key.includes('mistral')) return 'mistral'
  if (key.includes('cohere')) return 'cohere'
  if (key.includes('groq')) return 'groq'
  if (key.includes('together')) return 'together'
  if (key.includes('yi') || key.includes('zero-one')) return 'zero-one'
  if (key.includes('stepfun') || key.includes('step-') || key.includes('阶跃')) return 'stepfun'
  if (key.includes('doubao') || key.includes('豆包')) return 'doubao'
  if (key.includes('bytedance') || key.includes('字节')) return 'doubao'
  if (key.includes('spark') || key.includes('xfyun')) return 'spark'
  if (key.includes('iflytek')) return 'iflytek'
  if (key.includes('astron')) return 'astron'
  return 'generic'
}

/** transport (openai_chat / anthropic_messages) → BrandId。
 *  用于 custom provider (用户添加服务商时显式选了协议) 切换图标。 */
export function brandIdFromTransport(transport: string | null | undefined): BrandId {
  if (!transport) return 'generic'
  if (transport === 'anthropic_messages') return 'custom-anthropic'
  if (transport === 'openai_chat') return 'custom-openai'
  return 'generic'
}

interface BrandIconProps {
  id: string | null | undefined
  size?: number
  /** monochrome=true 走 currentColor（跟随主题，不加品牌主色） */
  monochrome?: boolean
  className?: string
  style?: CSSProperties
  /** badge=true：自己包一个圆形品牌底色的方块（列表项标配） */
  badge?: boolean
}

function BrandIconImpl({ id, size = 20, monochrome, className, style, badge }: BrandIconProps) {
  const brandId = resolveBrandId(id)
  const Mono = BRANDS[brandId] ?? Bot
  const primary = BRAND_COLORS[brandId]
  const title = DISPLAY_NAME[brandId] ?? String(brandId)

  // monochrome：不上色，跟随 currentColor
  if (monochrome) {
    return <Mono size={size} className={className} style={style} aria-label={title} />
  }

  // 非 badge：直接渲染 Mono，套上品牌主色
  if (!badge) {
    return (
      <Mono
        size={size}
        className={className}
        style={{ color: primary, ...style }}
        aria-label={title}
      />
    )
  }

  // badge 模式：分两条渲染路线
  //
  // A 类（BRAND_COLOR_ICONS 命中）：真彩 logo + 白底
  //   lobehub Color 子组件画的是"品牌色 logo on 透明底"（如 DeepSeek 是蓝色 #4D6BFE 鲸鱼）。
  //   必须配白底才显示——配同色背景（蓝底蓝鲸）= 隐形。
  //   阴影用 BRAND_COLORS 主色给柔光，让"品牌色身份"不丢。
  //
  // B 类（只 Mono）：白色 logo + 品牌色底
  //   Mono 是 currentColor 单色线稿，染白后在品牌色实色底上对比清晰。
  //   OpenAI / Kimi / Anthropic（用 Claude Mono）/ 智谱 / 硅基流动 走这条。
  const avatar = LOBE_AVATAR[brandId] ?? LOBE_AVATAR.generic!
  const boxSize = size + 10
  const innerSize = Math.round(size * avatar.iconMultiple)
  const ColorIcon = BRAND_COLOR_ICONS[brandId]

  // 真彩 logo → 白底；Mono → 用 LOBE_AVATAR 配的品牌底（含纯色和渐变）
  const useWhiteBg = !!ColorIcon
  const background = useWhiteBg ? '#ffffff' : avatar.background
  const isGradient = !useWhiteBg && avatar.background.includes('gradient')

  // 阴影颜色：
  //   - Color 路径（白底真彩）：用 BRAND_COLORS 主色给柔光（蓝鲸→蓝光晕呼应 logo）
  //   - Mono 路径（品牌色实底）：用底色本身（OpenAI 黑底→中性灰阴影，避免 BRAND_COLORS
  //     给 OpenAI 的绿色 #10a37f 在头像周围蒙一层"OpenAI 边缘发绿"的脏光晕）
  //   - 渐变底：fallback 到 BRAND_COLORS（color-mix 不吃 gradient string）
  const shadowColor = useWhiteBg
    ? (primary ?? '#6b7280')
    : (isGradient ? (primary ?? '#6b7280') : avatar.background)

  return (
    <span
      className={className}
      aria-label={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: boxSize,
        height: boxSize,
        borderRadius: Math.round(boxSize * 0.24),
        background,
        // 三层叠：极细 inset ring（让白底品牌在白色面板上不消失） + 贴近阴影 + 品牌色柔光
        boxShadow: isGradient
          ? 'inset 0 0 0 1px rgba(0, 0, 0, 0.06), 0 1px 2px rgba(0, 0, 0, 0.1), 0 4px 12px rgba(0, 0, 0, 0.08)'
          : `inset 0 0 0 1px rgba(0, 0, 0, 0.06), 0 1px 2px rgba(0, 0, 0, 0.08), 0 2px 8px color-mix(in srgb, ${shadowColor} 28%, transparent)`,
        // Mono 染色：白底走 currentColor=黑会冲突；ColorIcon 自带颜色不需要 color
        color: useWhiteBg ? undefined : avatar.color,
        flexShrink: 0,
        ...style,
      }}
    >
      {ColorIcon ? <ColorIcon size={innerSize} /> : <Mono size={innerSize} />}
    </span>
  )
}

export const BrandIcon = memo(BrandIconImpl)

export function getBrandName(id: string | null | undefined): string {
  // 后端 legacy_flat_models_view 对"没匹配到任何 provider 的老 custom_model"
  // 会按 base_url host 生成 virtual id `custom-<host>`。这里把 host 直接当名字
  // 展示，避免落到通用 "Model" 丢失区分度。
  if (typeof id === 'string' && id.startsWith('custom-')) {
    return id.slice(7) || '自定义'
  }
  return DISPLAY_NAME[resolveBrandId(id)] ?? 'Model'
}

/**
 * 取品牌强调色（用于选中态条带 / hover 高亮 / shadow 等）。
 *
 * 决策：跟随头像实际显示的主色，避免"选中条颜色 ≠ 头像颜色"的视觉脱节。
 *   - 头像是纯色实底（OpenAI 黑、Anthropic 橙、智谱蓝紫...）→ 用底色
 *   - 头像走 Color 真彩白底（DeepSeek 蓝鲸 on 白底...）→ fallback 到 BRAND_COLORS
 *     （品牌主色，跟 logo 内的色同源，选中条带和 logo 视觉呼应）
 *   - 头像是渐变（Minimax / LM Studio）→ fallback 到 BRAND_COLORS
 *     （选中态需要单色做 color-mix，无法用渐变 string）
 */
export function getBrandColor(id: string | null | undefined): string | null {
  const brandId = resolveBrandId(id)
  const avatar = LOBE_AVATAR[brandId]
  if (avatar) {
    const bg = avatar.background.toLowerCase()
    // 排除白底（白色选中条在白面板上消失）和渐变（color-mix 接不了 string）
    const isWhite = bg === '#fff' || bg === '#ffffff' || bg === 'white'
    const isGradient = bg.includes('gradient')
    if (!isWhite && !isGradient) return avatar.background
  }
  return BRAND_COLORS[brandId] ?? null
}
