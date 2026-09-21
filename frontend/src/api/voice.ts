/**
 * 语音转写 API 客户端（对应 backend/routes/voice_routes.py）。
 *
 * 后端路由：
 *   POST /voice/transcribe   上传 multipart 音频，返 {text}
 *   GET  /voice/config       当前配置 + 可选 provider 列表 + 本地依赖状态
 *   PUT  /voice/config       partial-update 配置
 */
import { API_BASE } from './base'

/* ============================================================================
   类型
   ============================================================================ */

export type VoiceProviderType = 'disabled' | 'whisper_api' | 'local_whisper'
export type LocalWhisperSize = 'tiny' | 'base' | 'small' | 'medium' | 'large-v3'

export interface VoiceConfig {
  provider_type: VoiceProviderType
  provider_id: string
  model: string
  local_model_size: LocalWhisperSize
  max_duration_seconds: number
  max_audio_size_mb: number
}

export interface VoiceEligibleProvider {
  id: string
  name: string
  has_api_key: boolean
  is_aggregator: boolean
}

export interface LocalWhisperStatus {
  available: boolean
  ffmpeg_installed: boolean
  faster_whisper_installed: boolean
}

export interface VoiceConfigResponse {
  config: VoiceConfig
  eligible_providers: VoiceEligibleProvider[]
  local_whisper_status: LocalWhisperStatus
  recommended_models: Record<string, string>
}

export interface VoiceConfigUpdate {
  provider_type?: VoiceProviderType
  provider_id?: string
  model?: string
  local_model_size?: LocalWhisperSize
  max_duration_seconds?: number
  max_audio_size_mb?: number
}

export interface TranscribeResult {
  text: string
}

/* ============================================================================
   错误类型
   ============================================================================ */

export class TranscriptionError extends Error {
  constructor(
    message: string,
    /** HTTP 状态码：403=disabled / 413=too large / 500=失败 */
    public status: number,
    /** 后端 detail 字段，给调用方做精细提示 */
    public detail: string,
  ) {
    super(message)
    this.name = 'TranscriptionError'
  }
}

/* ============================================================================
   API
   ============================================================================ */

/** 读取当前语音转写配置 + provider 候选列表 + 本地 whisper 状态。 */
export async function getVoiceConfig(): Promise<VoiceConfigResponse> {
  const res = await fetch(`${API_BASE}/voice/config`)
  if (!res.ok) {
    throw new Error(`[${res.status}] failed to load voice config`)
  }
  return res.json()
}

/** 更新配置（partial）。后端返新的完整 config。 */
export async function updateVoiceConfig(
  body: VoiceConfigUpdate,
): Promise<{ status: string; config: VoiceConfig }> {
  const res = await fetch(`${API_BASE}/voice/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const errText = await res.text().catch(() => '')
    throw new Error(`[${res.status}] update voice config failed: ${errText}`)
  }
  return res.json()
}

/** 上传录音转文字。失败抛 TranscriptionError，调用方可按 status 给具体提示。 */
export async function transcribeAudio(
  blob: Blob,
  filename = 'audio.webm',
): Promise<TranscribeResult> {
  const form = new FormData()
  form.append('file', blob, filename)
  const res = await fetch(`${API_BASE}/voice/transcribe`, {
    method: 'POST',
    body: form,
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const err = await res.json()
      detail = typeof err.detail === 'string' ? err.detail : detail
    } catch {
      /* 非 JSON body，detail 保留 HTTP 状态 */
    }
    throw new TranscriptionError(detail, res.status, detail)
  }
  return res.json()
}
