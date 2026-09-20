/**
 * Remote Access 子系统的前端 API 封装。
 *
 * 后端实现：backend/remote/routes.py
 * 设计文档：_docs/REMOTE_M8A.md
 *
 * 注意：/remote/admin/* 端点强制 loopback，所以这里调用方必须从同机访问后端
 * （柳如烟桌面客户端 + Tauri sidecar 默认就是这样）。远程客户端访问 admin 端点
 * 一律 403——这是故意的，远程访问的总开关绝不能被远程关掉/篡改。
 */
import { API_BASE } from './base'

export interface RemoteStatus {
  enabled: boolean
  host: string
  pwa_enabled: boolean
  updated_at: string
  /** 运行时信息：前端 dist 是否已被 FastAPI StaticFiles 挂载 */
  dist_mounted: boolean
  dist_path: string | null
  /** 未挂载时的原因（dist_not_found / mount_error: ... / null） */
  mount_skip_reason: string | null
}

export interface RemoteConfig {
  version: number
  enabled: boolean
  host: string
  pwa_enabled: boolean
  updated_at: string
}

export interface RemoteConfigPayload {
  enabled?: boolean
  /** 只允许 127.0.0.1 / 0.0.0.0（后端校验） */
  host?: '127.0.0.1' | '0.0.0.0'
  pwa_enabled?: boolean
}

export interface RemoteUpdateResult {
  config: RemoteConfig
  needs_restart: boolean
}

async function http<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init?.headers as Record<string, string> | undefined),
  }
  const body = init?.json !== undefined ? JSON.stringify(init.json) : init?.body
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
    body: body as BodyInit | null | undefined,
  })
  if (!res.ok) {
    let detail: string
    try {
      const err = await res.json()
      detail = typeof err.detail === 'string'
        ? err.detail
        : err.error?.message ?? err.error?.code ?? JSON.stringify(err)
    } catch {
      detail = await res.text()
    }
    throw new Error(`[${res.status}] ${detail || res.statusText}`)
  }
  if (res.status === 204) return undefined as unknown as T
  return res.json()
}

export async function getRemoteStatus(): Promise<RemoteStatus> {
  return http<RemoteStatus>('/remote/status')
}

export async function getRemoteAdminConfig(): Promise<RemoteConfig> {
  return http<RemoteConfig>('/remote/admin/config')
}

export async function updateRemoteConfig(
  payload: RemoteConfigPayload
): Promise<RemoteUpdateResult> {
  return http<RemoteUpdateResult>('/remote/admin/config', {
    method: 'PUT',
    json: payload,
  })
}
