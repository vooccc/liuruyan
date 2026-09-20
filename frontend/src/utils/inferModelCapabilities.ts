/**
 * 从 model id 启发式推断能力标签。纯前端展示用，后端不依赖。
 *
 * 规则按优先级匹配，命中即加入；多类型可叠加（如 gpt-4o = vision+tools）。
 */

export type Capability = 'tools' | 'vision' | 'reasoning' | 'code' | 'embedding' | 'audio' | 'image-gen'

interface Rule {
  cap: Capability
  /** 命中其中任一 pattern 即加该能力 */
  patterns: RegExp[]
}

const RULES: Rule[] = [
  {
    cap: 'embedding',
    patterns: [/embedding/i, /^text-embedding/i, /bge-/i, /^embedding-/i, /m3e-/i],
  },
  {
    cap: 'image-gen',
    patterns: [/dall-?e/i, /^image/i, /sd-?xl/i, /flux/i, /^stable-diffusion/i],
  },
  {
    cap: 'audio',
    patterns: [/whisper/i, /tts-/i, /^audio-/i, /voice/i],
  },
  {
    cap: 'reasoning',
    patterns: [/^o1\b/i, /^o3\b/i, /reasoner/i, /\br1\b/i, /\bqwq\b/i, /thinking/i, /reason/i],
  },
  {
    cap: 'vision',
    // ⚠️ 本规则必须与 backend/providers/capabilities.py::_VISION_PATTERNS 保持同步。
    // 用户报 bug（2026-05-12）：gpt-5 / gpt-5.4 / claude-opus-4.5 在上传图片时被
    // validateAttachment 误拒，根因是前端规则漏了后端有的 model id。
    // 改动时后端同改一遍，两边测试都要加（test_providers_capabilities.py /
    // inferModelCapabilities.test.ts 如有）。
    patterns: [
      /\bvl\b/i,                    // generic -vl 后缀（qwen-vl / intern-vl 之类）
      /-?vlm\b/i,
      /vision/i,
      /gpt-?4o/i,
      /gpt-?4-?turbo/i,
      /gpt-?5/i,                    // gpt-5 / gpt-5.1 / gpt-5.4-mini / gpt-5-chat 等全家
      /claude-3/i,
      /claude.*opus/i,              // 通配 opus-3 / opus-4 / opus-4.5...
      /claude.*sonnet/i,            // 通配 sonnet-3.5 / sonnet-4 / sonnet-4.5...
      /claude.*haiku/i,             // 通配所有 haiku 变体
      /gemini.*pro/i,
      /gemini.*flash/i,
      /gemini-1\.5/i,
      /gemini-2/i,
      /glm-4v/i,
      /qwen.*vl/i,                  // qwen-vl / qwen2-vl / qwen2.5-vl
      /qwen2.*vl/i,
      /llava/i,
      /pixtral/i,
      /intern.*vl/i,
    ],
  },
  {
    cap: 'code',
    patterns: [/coder?\b/i, /code-/i, /\bcursor\b/i, /-?starcoder/i, /codestral/i, /qwen.*-coder/i],
  },
  {
    cap: 'tools',
    // tools 是默认开关——大部分主流 chat 模型都支持。
    // 只对明显不支持的（embedding/whisper/image-gen）做排除即可。
    patterns: [/.*/],
  },
]

/** 推断能力。order 保留输入规则顺序。 */
export function inferModelCapabilities(modelId: string): Capability[] {
  if (!modelId) return []
  const caps = new Set<Capability>()
  for (const rule of RULES) {
    if (rule.cap === 'tools') continue // 单独处理
    if (rule.patterns.some((re) => re.test(modelId))) caps.add(rule.cap)
  }
  // tools 默认有，除非命中 embedding/audio/image-gen 这些"非对话"类
  const nonChat: Capability[] = ['embedding', 'audio', 'image-gen']
  if (!nonChat.some((c) => caps.has(c))) caps.add('tools')
  return Array.from(caps)
}

/**
 * 后端 tags（backend/providers/capabilities.py 输出格式）→ 前端 Capability 枚举。
 *
 * 命名差异要 adapter：
 * - 后端 `function_calling` ↔ 前端 `tools`
 * - 后端 `image_gen` (snake_case) ↔ 前端 `image-gen` (kebab-case)
 * - 后端独有 `free / rerank / websearch` 暂不在前端 Capability 枚举里展示，丢弃
 *
 * 后端 tags 已按 infer_capabilities 顺序输出，前端保留同顺序。
 */
const _BACKEND_TAG_TO_CAP: Record<string, Capability> = {
  vision: 'vision',
  reasoning: 'reasoning',
  embedding: 'embedding',
  audio: 'audio',
  image_gen: 'image-gen',
  function_calling: 'tools',
}

export function capsFromBackendTags(tags: string[]): Capability[] {
  const out = new Set<Capability>()
  for (const t of tags) {
    const mapped = _BACKEND_TAG_TO_CAP[t]
    if (mapped) out.add(mapped)
  }
  return Array.from(out)
}

/**
 * 优先用后端权威 `model.tags`，缺失时 fallback 到前端正则推断（边界场景：
 * 老会话 / 用户手动写 models.json 漏 tags / 测试桩 model）。
 *
 * 这是 vision 拦截 / Picker 能力胶囊的统一入口——任何"判定 model 能力"的前端
 * 代码都应该走这个 helper，避免规则漂移。
 */
export function resolveCapabilities(model: { id: string; tags?: string[] }): Capability[] {
  if (model.tags && model.tags.length > 0) {
    return capsFromBackendTags(model.tags)
  }
  return inferModelCapabilities(model.id || '')
}

/** 便捷判定：当前 model 是否支持视觉。等价 `resolveCapabilities(m).includes('vision')`。 */
export function modelSupportsVision(model: { id: string; tags?: string[] }): boolean {
  return resolveCapabilities(model).includes('vision')
}

/** 启发式推断上下文窗口（用于胶囊里的 32K / 128K 标）。命中失败返 undefined。 */
export function inferContextWindow(modelId: string): string | undefined {
  const id = modelId.toLowerCase()
  if (/(^|[-_])128k/.test(id)) return '128K'
  if (/(^|[-_])64k/.test(id)) return '64K'
  if (/(^|[-_])32k/.test(id)) return '32K'
  if (/(^|[-_])16k/.test(id)) return '16K'
  if (/(^|[-_])8k/.test(id)) return '8K'
  if (/gpt-?4o/.test(id)) return '128K'
  if (/gpt-?4-?turbo/.test(id)) return '128K'
  if (/gpt-?4/.test(id)) return '8K'
  if (/gpt-?3\.5/.test(id)) return '16K'
  if (/claude-3/.test(id)) return '200K'
  if (/claude-sonnet-4|claude-opus-4/.test(id)) return '200K'
  if (/deepseek-(chat|reasoner)/.test(id)) return '64K'
  if (/deepseek-coder/.test(id)) return '16K'
  if (/qwen3?-/.test(id)) return '32K'
  return undefined
}

/** 标签颜色（CSS class 后缀） */
export const CAPABILITY_COLORS: Record<Capability, string> = {
  tools: 'cap-blue',
  vision: 'cap-purple',
  reasoning: 'cap-gold',
  code: 'cap-cyan',
  embedding: 'cap-green',
  audio: 'cap-pink',
  'image-gen': 'cap-orange',
}

export const CAPABILITY_LABELS: Record<Capability, string> = {
  tools: 'tools',
  vision: 'vision',
  reasoning: 'reasoning',
  code: 'code',
  embedding: 'embedding',
  audio: 'audio',
  'image-gen': 'image',
}

/**
 * 后端原生 tag 名（backend/providers/capabilities.py 输出）→ 中文 label / CSS 色板。
 *
 * 用于直接显示 `model.tags`（不经 Capability 枚举映射，保留 backend tag 的完整集合：
 * function_calling / websearch / rerank / free 等 Capability 枚举里没有的也能显示）。
 *
 * 调用方：ProviderSettings 已启用模型列表 / ManageModelsPopup 发现模型列表 ——
 * 两处都按"后端 tag 名"为 key 取色，统一视觉。新加 tag 类目只要后端补，前端
 * 这俩 map 加一行就行。
 *
 * 与 CAPABILITY_COLORS / CAPABILITY_LABELS 的关系：
 * - `CAPABILITY_*`：前端 Capability 枚举（已映射的精简集），ChatModelPicker 用
 * - `BACKEND_TAG_*`：后端原生 tag 名（全集），Settings 页直接显示 m.tags 用
 */
export const BACKEND_TAG_LABELS: Record<string, string> = {
  free: '免费',
  reasoning: '推理',
  vision: '视觉',
  function_calling: 'tools',
  websearch: '联网',
  embedding: 'embed',
  rerank: 'rerank',
  audio: 'audio',
  image_gen: 'image',
}

export const BACKEND_TAG_COLORS: Record<string, string> = {
  free: 'cap-green',
  reasoning: 'cap-gold',
  vision: 'cap-purple',
  function_calling: 'cap-blue',
  websearch: 'cap-green',
  embedding: 'cap-pink',
  rerank: 'cap-red',
  audio: 'cap-gray',
  image_gen: 'cap-orange',
}
