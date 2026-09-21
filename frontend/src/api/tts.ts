/**
 * TTS 语音输出 API 客户端（对应 backend/routes/tts_routes.py）。
 *
 * 端点：
 *   POST /tts/synthesize  正式合成（先过 should_speak 过滤）
 *   GET  /tts/config      返当前配置 + system prompt 片段 + provider 列表
 *   PUT  /tts/config      partial-update 配置
 *   POST /tts/test        试听（不过 should_speak，支持临时 override）
 *
 * 零污染契约：本文件独立，不被 ChatBubble / agent 主流引用（M1 阶段）。
 * 撤除 TTS 时直接 rm 即可。
 *
 * 设计文档：_docs/handover_tts_design_2026-05-29.md
 */
import { API_BASE } from './base'
import type { Message, ContentSegment } from './chat'

/* ============================================================================
   类型
   ============================================================================ */

export type TTSSpeakMode = 'off' | 'key' | 'all'
export type TTSProviderType = 'openai_compat' | 'gptsovits_compat' | 'tencent_compat' | 'baidu_compat' | 'aliyun_compat'
export type TTSResponseFormat = 'mp3' | 'opus' | 'wav' | 'pcm'

/**
 * 后端返的"前端可见"配置。注意：api_key 字段被脱敏为 has_api_key 布尔，
 * 真正的 key 字面永不下发。
 */
export interface TTSConfigView {
  enabled: boolean
  mode: TTSSpeakMode
  provider_type: TTSProviderType
  /**
   * 指向 providers store 的 OpenAI 兼容 provider id。
   * 推荐：跟「模型服务」共用凭证。空串=未选（会回退到旧 base_url/api_key 字段）。
   */
  provider_id: string
  /** [兼容] 旧 M1.0 直填字段。新 UI 不展示，但响应仍会带回。 */
  base_url: string
  has_api_key: boolean
  model: string
  voice: string
  response_format: TTSResponseFormat
  speed: number
  // gptsovits_compat（M3+）
  gptsovits_base_url: string
  gptsovits_refer_wav_path: string
  gptsovits_prompt_text: string
  gptsovits_text_lang: string
  // 腾讯云
  tencent_secret_id: string
  has_tencent_secret_key: boolean
  tencent_voice_type: number
  tencent_region: string
  // 百度云
  baidu_api_key: string
  has_baidu_secret_key: boolean
  baidu_voice: number
  baidu_speed: number
  baidu_pitch: number
  baidu_volume: number
  baidu_cuid: string
  // 阿里云
  aliyun_access_key_id: string
  has_aliyun_access_key_secret: boolean
  aliyun_appkey: string
  aliyun_voice: string
  aliyun_emotion: string
  aliyun_region: string
  aliyun_sample_rate: number
  aliyun_volume: number
  aliyun_speech_rate: number
  aliyun_pitch_rate: number
  // 流式配置
  streaming_enabled: boolean
  streaming_min_segment_chars: number
  streaming_sentence_enders: string
  streaming_protect_say_tags: boolean
  streaming_flush_tail_on_complete: boolean
  // 网络
  timeout_sec: number
}

/**
 * /tts/config 返的 provider 候选。结构对齐 voice_routes 的 eligible_providers。
 * 用户在 TTSPane 下拉里选一个，凭证（base_url + api_key + extra_headers）后端从
 * providers store 借，免得在 TTS 这边重复填 key。
 */
export interface TTSEligibleProvider {
  id: string
  name: string
  has_api_key: boolean
  is_aggregator: boolean
  base_url: string
  models: { id: string; name: string }[]
}

export interface TTSBaiduVoiceOption {
  per: number
  name: string
  style: string
  tags: string[]
}

export interface TTSAliyunVoiceOption {
  voice: string
  name: string
  style: string
  tags: string[]
}

export interface TTSAliyunEmotionOption {
  value: string
  label: string
}

export interface TTSConfigResponse {
  config: TTSConfigView
  /** 用户复制粘贴到角色 system prompt 末尾的 <say> 用法说明。 */
  system_prompt_snippet: string
  /** 当前可选 provider_type 列表。M1 只有 openai_compat。 */
  provider_types: TTSProviderType[]
  /** providers store 里能跑 TTS 的 provider 列表（openai_chat transport + enabled）。 */
  eligible_providers: TTSEligibleProvider[]
  /** 按 provider id 给的推荐 model 名（前端切 provider 时一键填好）。 */
  recommended_models: Record<string, string>
  /** 按 provider id 给的推荐 voice 列表（前端 voice 输入下拉提示用）。 */
  recommended_voices: Record<string, string[]>
  /** 百度云常见音色候选。百度没有稳定公开的账号级枚举接口，先提供本地候选 + 允许手填 per。 */
  baidu_voices: TTSBaiduVoiceOption[]
  /** 阿里云常见音色候选。控制台新增音色时仍允许手填 voice。 */
  aliyun_voices: TTSAliyunVoiceOption[]
  /** 阿里云多情感候选。空值表示不指定 emotion。 */
  aliyun_emotions: TTSAliyunEmotionOption[]
}

/**
 * partial-update body。所有字段可选；传 undefined 表示不动，传 ''/0/false 才是清空。
 * 特别注意 api_key：传 undefined → 不改；传 '' → 后端清空。
 */
export interface TTSConfigUpdate {
  enabled?: boolean
  mode?: TTSSpeakMode
  provider_type?: TTSProviderType
  /** 选 OpenAI 兼容 provider 借凭证；空串=清除回退到旧字段。 */
  provider_id?: string
  base_url?: string
  api_key?: string
  model?: string
  voice?: string
  response_format?: TTSResponseFormat
  speed?: number
  // 腾讯云
  tencent_secret_id?: string
  tencent_secret_key?: string
  tencent_voice_type?: number
  tencent_region?: string
  // 百度云
  baidu_api_key?: string
  baidu_secret_key?: string
  baidu_voice?: number
  baidu_speed?: number
  baidu_pitch?: number
  baidu_volume?: number
  baidu_cuid?: string
  // 阿里云
  aliyun_access_key_id?: string
  aliyun_access_key_secret?: string
  aliyun_appkey?: string
  aliyun_voice?: string
  aliyun_emotion?: string
  aliyun_region?: string
  aliyun_sample_rate?: number
  aliyun_volume?: number
  aliyun_speech_rate?: number
  aliyun_pitch_rate?: number
  gptsovits_base_url?: string
  gptsovits_refer_wav_path?: string
  gptsovits_prompt_text?: string
  gptsovits_text_lang?: string
  streaming_enabled?: boolean
  streaming_min_segment_chars?: number
  streaming_sentence_enders?: string
  streaming_protect_say_tags?: boolean
  streaming_flush_tail_on_complete?: boolean
  timeout_sec?: number
}

/* ============================================================================
   错误
   ============================================================================ */

export class TTSError extends Error {
  constructor(
    message: string,
    /** HTTP status：400 / 502 / 500，前端可据此分别 toast。 */
    public status: number,
    /** 后端 detail 字段，给用户友好提示。 */
    public detail: string,
  ) {
    super(message)
    this.name = 'TTSError'
  }
}

async function readErrorDetail(res: Response): Promise<string> {
  try {
    const body = await res.clone().json()
    if (typeof body.detail === 'string') return body.detail
    return JSON.stringify(body)
  } catch {
    try {
      return (await res.text()) || `HTTP ${res.status}`
    } catch {
      return `HTTP ${res.status}`
    }
  }
}

/* ============================================================================
   API：配置
   ============================================================================ */

export async function getTTSConfig(): Promise<TTSConfigResponse> {
  const res = await fetch(`${API_BASE}/tts/config`)
  if (!res.ok) {
    throw new Error(`[${res.status}] failed to load TTS config`)
  }
  return res.json()
}

export async function updateTTSConfig(
  patch: TTSConfigUpdate,
): Promise<{ status: string; config: TTSConfigView }> {
  const res = await fetch(`${API_BASE}/tts/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) {
    const detail = await readErrorDetail(res)
    throw new TTSError(`update tts config failed: ${detail}`, res.status, detail)
  }
  return res.json()
}

/* ============================================================================
   API：角色级 override（meta.json 的 tts_override 字段）
   ============================================================================ */

/**
 * 角色级 TTS 覆盖。所有字段都是可选；仅展示已设字段。
 *
 * 跟 TTSConfigUpdate 的差异：
 * - 不含 enabled / mode（朗读策略全局统一）
 * - 不含 api_key / base_url / timeout_sec（兼容/网络字段，不暴露给角色级）
 *
 * 设计文档：_docs/handover_tts_design_2026-05-29.md §M5
 */
export interface TTSAgentOverride {
  provider_type?: TTSProviderType
  provider_id?: string
  model?: string
  voice?: string
  speed?: number
  response_format?: TTSResponseFormat
  gptsovits_base_url?: string
  gptsovits_refer_wav_path?: string
  gptsovits_prompt_text?: string
  gptsovits_text_lang?: string
}

/**
 * partial-update body。
 *
 * - undefined：不动该字段
 * - ''（空字符串）：从 override 移除该字段（回退到全局）
 * - 其他值：覆盖
 */
export type TTSAgentOverrideUpdate = TTSAgentOverride

export interface TTSAgentOverrideResponse {
  agent_id: string
  override: TTSAgentOverride | null
}

export async function getAgentTTSOverride(
  agentId: string,
): Promise<TTSAgentOverrideResponse> {
  const res = await fetch(`${API_BASE}/tts/agents/${encodeURIComponent(agentId)}/override`)
  if (!res.ok) {
    throw new Error(`[${res.status}] failed to load agent TTS override`)
  }
  return res.json()
}

export async function updateAgentTTSOverride(
  agentId: string,
  patch: TTSAgentOverrideUpdate,
): Promise<{ status: string; agent_id: string; override: TTSAgentOverride }> {
  const res = await fetch(
    `${API_BASE}/tts/agents/${encodeURIComponent(agentId)}/override`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    },
  )
  if (!res.ok) {
    const detail = await readErrorDetail(res)
    throw new TTSError(`update agent override failed: ${detail}`, res.status, detail)
  }
  return res.json()
}

export async function deleteAgentTTSOverride(
  agentId: string,
): Promise<{ status: string; cleared: boolean; agent_id: string }> {
  const res = await fetch(
    `${API_BASE}/tts/agents/${encodeURIComponent(agentId)}/override`,
    { method: 'DELETE' },
  )
  if (!res.ok) {
    const detail = await readErrorDetail(res)
    throw new TTSError(`delete agent override failed: ${detail}`, res.status, detail)
  }
  return res.json()
}

/* ============================================================================
   API：角色参考音频文件管理（M6）
   ============================================================================ */

export interface TTSAgentReference {
  filename: string
  /** 用于写到 override.gptsovits_refer_wav_path（后端展开成绝对路径） */
  rel_path: string
  size: number
}

export interface UploadReferenceResponse {
  status: string
  agent_id: string
  filename: string
  rel_path: string
  size: number
}

/**
 * 上传角色参考音频。后端落到 ``data_dir/agents/<id>/tts/<filename>``。
 *
 * @param filename 自定义保存名（含扩展名）。不传用上传文件的原名。
 */
export async function uploadAgentReference(
  agentId: string,
  file: File,
  filename?: string,
): Promise<UploadReferenceResponse> {
  const form = new FormData()
  form.append('file', file)
  let url = `${API_BASE}/tts/agents/${encodeURIComponent(agentId)}/reference`
  if (filename) {
    url += `?filename=${encodeURIComponent(filename)}`
  }
  const res = await fetch(url, { method: 'POST', body: form })
  if (!res.ok) {
    const detail = await readErrorDetail(res)
    throw new TTSError(`upload reference failed: ${detail}`, res.status, detail)
  }
  return res.json()
}

/** 列出该角色的所有参考音频。 */
export async function listAgentReferences(
  agentId: string,
): Promise<{ agent_id: string; files: TTSAgentReference[] }> {
  const res = await fetch(
    `${API_BASE}/tts/agents/${encodeURIComponent(agentId)}/reference`,
  )
  if (!res.ok) {
    throw new Error(`[${res.status}] list references failed`)
  }
  return res.json()
}

/** 拼装某个参考音频的播放 URL（前端 ``<audio src=...>`` 用）。 */
export function getAgentReferenceUrl(agentId: string, filename: string): string {
  return `${API_BASE}/tts/agents/${encodeURIComponent(agentId)}/reference/${encodeURIComponent(filename)}`
}

/** 删除某个参考音频。 */
export async function deleteAgentReference(
  agentId: string,
  filename: string,
): Promise<{ status: string; deleted: boolean; filename: string }> {
  const res = await fetch(
    `${API_BASE}/tts/agents/${encodeURIComponent(agentId)}/reference/${encodeURIComponent(filename)}`,
    { method: 'DELETE' },
  )
  if (!res.ok) {
    const detail = await readErrorDetail(res)
    throw new TTSError(`delete reference failed: ${detail}`, res.status, detail)
  }
  return res.json()
}

/* ============================================================================
   M2：朗读链路辅助函数
   ============================================================================ */

/**
 * 从 assistant 消息中拼出"可朗读文本"——只保留 ``type === 'text'`` 的 content。
 *
 * 跳过：
 * - ``thinking``：内心独白，用户看不见的"思考过程"，不该朗读
 * - ``tool_call`` / ``tool_result``：工具调用细节，朗读出来很怪
 * - ``system_notice``：route progress / runtime notice 等系统提示
 *
 * 兼容旧消息：没 segments 字段时回退到 ``message.content``。
 *
 * 后端会进一步用 ``should_speak`` 过滤 ``<say>`` 标签或截短长文本——本函数只做
 * "结构化拼接"，不做"该不该念"的判断。
 */
export function extractSpeakableText(message: Message): string {
  const segments: ContentSegment[] | undefined = message.segments
  if (Array.isArray(segments) && segments.length > 0) {
    const parts: string[] = []
    for (const seg of segments) {
      if (seg.type === 'text' && typeof seg.content === 'string' && seg.content.trim()) {
        parts.push(seg.content)
      }
    }
    if (parts.length > 0) {
      return parts.join('\n').trim()
    }
  }
  // 旧消息 fallback
  return (message.content || '').trim()
}

/* ============================================================================
   API：合成（试听 / 正式）
   ============================================================================ */

export interface SynthesizeOptions {
  /** 临时覆盖 mode（off/key/all），不写盘。仅 /tts/synthesize 用。 */
  mode?: TTSSpeakMode
  /** 当前生效的角色 id；非空时后端合并该角色 meta.json 的 tts_override。 */
  agentId?: string
  signal?: AbortSignal
}

/**
 * 正式合成。先在后端走 should_speak 过滤，可能返 204（应静默）。
 *
 * @returns Blob (audio/*) 或 null（204 / 文本被过滤掉）
 */
export async function synthesizeText(
  text: string,
  opts: SynthesizeOptions = {},
): Promise<Blob | null> {
  const res = await fetch(`${API_BASE}/tts/synthesize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, mode: opts.mode, agent_id: opts.agentId }),
    signal: opts.signal,
  })

  if (res.status === 204) return null
  if (!res.ok) {
    const detail = await readErrorDetail(res)
    throw new TTSError(`tts synthesize failed: ${detail}`, res.status, detail)
  }
  return res.blob()
}

/**
 * 试听。**不过 should_speak**，字面合成。支持临时 override（不写盘）。
 * 用于配置面板"试听"按钮：用户改了字段还没保存也能听效果。
 *
 * 角色试听：传 agentId，后端会先合并该角色 meta.json 的 tts_override
 * 再叠加 override，所以面板里改的字段优先级 > 角色配置 > 全局配置。
 */
export interface TestTTSOptions {
  override?: Partial<TTSConfigUpdate>
  /** 角色试听时传入；后端先合并该角色 tts_override 再叠加 override。 */
  agentId?: string
  signal?: AbortSignal
}

export async function testTTS(
  text: string,
  opts: TestTTSOptions | Partial<TTSConfigUpdate> = {},
  signalLegacy?: AbortSignal,
): Promise<Blob> {
  // 兼容旧调用 testTTS(text, override, signal)（M1 签名）
  let override: Partial<TTSConfigUpdate> | undefined
  let agentId: string | undefined
  let signal: AbortSignal | undefined
  if (opts && typeof opts === 'object' && ('override' in opts || 'agentId' in opts || 'signal' in opts)) {
    const o = opts as TestTTSOptions
    override = o.override
    agentId = o.agentId
    signal = o.signal
  } else {
    override = opts as Partial<TTSConfigUpdate>
    signal = signalLegacy
  }
  const res = await fetch(`${API_BASE}/tts/test`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, override, agent_id: agentId }),
    signal,
  })
  if (!res.ok) {
    const detail = await readErrorDetail(res)
    throw new TTSError(`tts test failed: ${detail}`, res.status, detail)
  }
  return res.blob()
}

/**
 * Blob → 可播放的 ObjectURL。调用方播完记得 URL.revokeObjectURL 释放。
 */
export function blobToAudioURL(blob: Blob): string {
  return URL.createObjectURL(blob)
}
