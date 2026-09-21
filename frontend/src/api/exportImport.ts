/**
 * 角色 / 全局记忆 导入导出 API。
 *
 * 设计：后端先把 zip 写到 ~/.liuruyan/exports/（稳定路径）+ 返回路径。
 * 前端拿到路径后两条路：
 *   - Tauri 环境：弹 native save dialog → fs.copyFile 把 zip 拷到用户选的位置
 *   - 浏览器 dev：调 /export/open-dir 用 explorer 打开目录给用户拿
 *
 * 这样后端完全不感知前端是 Tauri 还是浏览器，逻辑分支限定在前端。
 */

import { API_BASE } from './base'

export interface AgentSummary {
  agent_id: string
  name: string
  session_count: number
  last_active: string | null
}

export interface ExportResult {
  path: string                    // 后端写盘的源路径
  filename: string                // 默认文件名（save dialog 用）
  size_bytes: number
  agents?: string[]
  skipped?: string[]
  total_files?: number
  dirs?: string[]                 // memory 包：实际包含的系统目录列表
}

export interface ImportReport {
  kind: string
  new_agents: string[]
  merged_agents: string[]
  added_sessions: string[]
  skipped_sessions: string[]
  added_files_count: number
  skipped_files_count: number
  errors: string[]
}

/** 是否运行在 Tauri webview。withGlobalTauri=true 时挂在 window.__TAURI__。 */
export function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI__' in window
}

export async function listAgentsForExport(): Promise<AgentSummary[]> {
  const res = await fetch(`${API_BASE}/export/agents/list`)
  if (!res.ok) throw new Error(`列表加载失败: ${res.status}`)
  const data = await res.json()
  return (data.agents || []) as AgentSummary[]
}

export async function exportAgentsToDisk(agentIds: string[]): Promise<ExportResult> {
  const res = await fetch(`${API_BASE}/export/agents`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent_ids: agentIds }),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`导出失败: ${res.status} ${text}`)
  }
  return (await res.json()) as ExportResult
}

export async function exportMemoryToDisk(): Promise<ExportResult> {
  const res = await fetch(`${API_BASE}/export/memory`, { method: 'POST' })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`导出失败: ${res.status} ${text}`)
  }
  return (await res.json()) as ExportResult
}

export async function openExportsDir(): Promise<{ success: boolean; path: string; error?: string }> {
  const res = await fetch(`${API_BASE}/export/open-dir`, { method: 'POST' })
  if (!res.ok) throw new Error(`打开目录失败: ${res.status}`)
  return await res.json()
}

/**
 * Tauri 环境：弹 native save dialog 让用户选目标路径，然后从 sourcePath 拷贝过去。
 * 返回最终保存路径，用户取消则返回 null。
 *
 * 失败抛异常（IO 错误、权限拒绝等）。
 */
export async function saveAsWithTauri(
  sourcePath: string,
  defaultFilename: string,
): Promise<string | null> {
  if (!isTauri()) throw new Error('saveAsWithTauri 只能在 Tauri 环境调用')

  const { save } = await import('@tauri-apps/plugin-dialog')
  const fs = await import('@tauri-apps/plugin-fs')

  const target = await save({
    defaultPath: defaultFilename,
    filters: [{ name: 'ZIP 压缩包', extensions: ['zip'] }],
  })
  if (!target) return null  // 用户取消

  // copyFile 是 Tauri fs plugin 的标准 API（v2）。源 / 目标都是绝对路径。
  await fs.copyFile(sourcePath, target)
  return target
}

export async function importArchive(file: File): Promise<ImportReport> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${API_BASE}/import/archive`, {
    method: 'POST',
    body: form,
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`导入失败: ${res.status} ${text}`)
  }
  return (await res.json()) as ImportReport
}
