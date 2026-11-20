/**
 * 语音转写 API 客户端（对应 backend/voice_transcription/routes.py）。
 *
 * 孤岛模块：删除本文件 + VoiceTranscriptionPane.tsx + 还原 VoiceSettings.tsx 接线即可完整撤除。
 */
import { API_BASE } from './base'

export type VoiceTranscriptionProviderType = 'local_sensevoice' | 'openai_compat'
export type VoiceTranscriptionResponseFormat = 'json' | 'text' | 'srt' | 'vtt' | 'verbose_json'

export interface VoiceTranscriptionConfigView {
  enabled: boolean
  provider_type: VoiceTranscriptionProviderType
  provider_id: string
  base_url: string
  has_api_key: boolean
  model: string
  response_format: VoiceTranscriptionResponseFormat
  language: string
  prompt: string
  temperature: number
  timeout_sec: number
  local_model_dir: string
  local_use_vad: boolean
  local_max_segment_sec: number
  local_num_threads: number
  auto_polish: boolean
}

export interface VoiceTranscriptionConfigUpdate {
  enabled?: boolean
  provider_type?: VoiceTranscriptionProviderType
  provider_id?: string
  base_url?: string
  api_key?: string
  model?: string
  response_format?: VoiceTranscriptionResponseFormat
  language?: string
  prompt?: string
  temperature?: number
  timeout_sec?: number
  local_model_dir?: string
  local_use_vad?: boolean
  local_max_segment_sec?: number
  local_num_threads?: number
  auto_polish?: boolean
}

export interface TranscriptionEligibleProvider {
  id: string
  name: string
  has_api_key: boolean
  is_aggregator: boolean
  base_url: string
  models: { id: string; name: string }[]
}

export interface TranscriptionDownloadStatus {
  phase: 'idle' | 'downloading' | 'done' | 'error'
  file: string
  label: string
  downloaded: number
  total: number
  percent: number
  files_done: number
  files_total: number
  error: string
  started_at: number
}

export interface TranscriptionLocalStatus {
  dir: string
  ready: boolean
  vad_ready: boolean
  model: string
  download?: TranscriptionDownloadStatus
}

export interface TranscriptionCapabilities {
  ffmpeg: boolean
  local_winrt?: boolean
  local_native_formats: string[]
  local_all_formats: boolean
  api_formats: string[]
}

export interface VoiceTranscriptionConfigResponse {
  config: VoiceTranscriptionConfigView
  enabled: boolean
  available: boolean
  reason: string
  eligible_providers: TranscriptionEligibleProvider[]
  recommended_models: Record<string, string[]>
  local: TranscriptionLocalStatus
  capabilities: TranscriptionCapabilities
  env_fallback: {
    has_openai_api_key: boolean
    base_url: string
  }
}

export interface VoiceTranscriptionSegment {
  start?: number
  end?: number
  text: string
  speaker?: string
}

export interface VoiceTranscriptionResult {
  status: string
  text: string
  segments?: VoiceTranscriptionSegment[]
  backend: string
  model: string
  duration_sec?: number
  language?: string
  chunks_count?: number
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

export async function getVoiceTranscriptionConfig(): Promise<VoiceTranscriptionConfigResponse> {
  const res = await fetch(`${API_BASE}/voice/transcription/config`)
  if (!res.ok) {
    throw new Error(`[${res.status}] 加载语音转写配置失败`)
  }
  return res.json()
}

export async function updateVoiceTranscriptionConfig(
  patch: VoiceTranscriptionConfigUpdate,
): Promise<VoiceTranscriptionConfigResponse & { status: string }> {
  const res = await fetch(`${API_BASE}/voice/transcription/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) {
    const detail = await readErrorDetail(res)
    throw new Error(detail)
  }
  return res.json()
}

export async function startTranscriptionModelDownload(): Promise<
  VoiceTranscriptionConfigResponse & { status: string; started: boolean }
> {
  const res = await fetch(`${API_BASE}/voice/transcription/local/download`, {
    method: 'POST',
  })
  if (!res.ok) {
    const detail = await readErrorDetail(res)
    throw new Error(detail)
  }
  return res.json()
}

export async function transcribeAudioFile(
  file: File,
  opts: {
    language?: string
    prompt?: string
    includeTimestamps?: boolean
    test?: boolean
    override?: Partial<VoiceTranscriptionConfigUpdate>
    signal?: AbortSignal
  } = {},
): Promise<VoiceTranscriptionResult> {
  const form = new FormData()
  form.append('file', file)
  if (opts.language) form.append('language', opts.language)
  if (opts.prompt) form.append('prompt', opts.prompt)
  if (opts.includeTimestamps) form.append('include_timestamps', 'true')
  if (opts.test) form.append('test', 'true')
  if (opts.override) form.append('override_json', JSON.stringify(opts.override))

  const res = await fetch(`${API_BASE}/voice/transcription/transcribe`, {
    method: 'POST',
    body: form,
    signal: opts.signal,
  })
  if (!res.ok) {
    const detail = await readErrorDetail(res)
    throw new Error(detail)
  }
  return res.json()
}
