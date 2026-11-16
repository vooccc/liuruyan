/**
 * voice 模块共享类型（前后端 WS 协议镜像 backend/voice_stream/service.py 头注释）。
 */
import { API_BASE } from '../api/base'

/* ============================================================================
   服务端事件（服务端 → 浏览器，JSON）
   ============================================================================ */

export type VoiceStreamServerEvent =
  | { type: 'started' }
  | { type: 'partial'; text: string }
  | { type: 'final'; text: string }
  | { type: 'finished'; text: string }
  | { type: 'failed'; code: string; message: string }

/* ============================================================================
   浏览器控制帧（浏览器 → 服务端）
   ============================================================================ */

export type VoiceStreamClientControl =
  | { type: 'start' }
  | { type: 'finish' }
  | { type: 'cancel' }

/* ============================================================================
   按钮对外能力（Ctrl+Shift+M 快捷键 / 外部控制用）
   ============================================================================ */

export interface VoiceStreamButtonRef {
  toggleRecording: () => void
  isRecording: () => boolean
  isLoading: () => boolean
}

/* ============================================================================
   /voice/stream/config 响应（降级判断依据）
   ============================================================================ */

export interface VoiceStreamPolishStatus {
  enabled: boolean
  provider_id: string
  model: string
  has_api_key: boolean
}

export interface VoiceStreamLocalStatus {
  dir: string
  ready: boolean
  model: string
}

export interface VoiceStreamConfigResponse {
  enabled: boolean
  available: boolean
  provider: string
  model: string
  has_api_key: boolean
  sample_rate: number
  language_hints: string[]
  heartbeat: boolean
  max_session_seconds: number
  reason: string
  supported_models: string[]
  polish: VoiceStreamPolishStatus
  local: VoiceStreamLocalStatus
}

/* ============================================================================
   常量
   ============================================================================ */

/** 浏览器采集固定 16k 单声道 PCM16（与后端 run-task parameters 对齐）。 */
export const TARGET_SAMPLE_RATE = 16000
/** 每 100ms 一帧（1600 样本 = 3200 字节），与官方 demo 的发送节奏一致。 */
export const CHUNK_MS = 100

/** WS 端点：由 API_BASE 推导（http→ws）。 */
export const VOICE_STREAM_WS_URL = `${API_BASE.replace(/^http/, 'ws')}/voice/stream/ws`
export const VOICE_STREAM_CONFIG_URL = `${API_BASE}/voice/stream/config`

/* ============================================================================
   API 客户端（设置页 + VoiceInputSlot 共用）
   ============================================================================ */

export interface VoiceStreamConfigUpdate {
  enabled?: boolean
  provider_id?: string
  model?: string
  language_hints?: string[]
  heartbeat?: boolean
  max_session_seconds?: number
  local_model_dir?: string
  polish_enabled?: boolean
  polish_provider_id?: string
  polish_model?: string
}

/** 读流式语音配置。后端未起 / 路由不存在时返 null（降级语义，不抛错）。 */
export async function fetchVoiceStreamConfig(): Promise<VoiceStreamConfigResponse | null> {
  try {
    const res = await fetch(VOICE_STREAM_CONFIG_URL)
    if (!res.ok) return null
    return (await res.json()) as VoiceStreamConfigResponse
  } catch {
    return null
  }
}

/** partial 更新配置。返回刷新后的完整状态（含 stream_status）。 */
export async function updateVoiceStreamConfig(
  patch: VoiceStreamConfigUpdate,
): Promise<{ status: string; stream_status: VoiceStreamConfigResponse }> {
  const res = await fetch(VOICE_STREAM_CONFIG_URL, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`[${res.status}] update voice_stream config failed: ${text}`)
  }
  return res.json()
}
