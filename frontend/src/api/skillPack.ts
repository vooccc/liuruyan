/**
 * Skill Pack 管理 REST 客户端。
 *
 * 后端实现：backend/main.py 的 /skill-packs 系列路由
 * 后端管理逻辑：backend/skill_pack_manager.py
 * 设计文档：_docs/skill_marketplace_research.md
 *
 * 跟 mcp.ts 一样独立成文件，避免污染已经 2900+ 行的 chat.ts。
 */

import { API_BASE } from './base'

// ────────────────────────────────────────────────────────────────────────────
// 类型
// ────────────────────────────────────────────────────────────────────────────

/** 后端 normalized source dict（也接受 string 简写形态） */
export type SkillPackSource =
  | string
  | {
      source: 'github'
      repo: string
      ref?: string
      path?: string
    }
  | {
      source: 'url'
      url: string
      ref?: string
      path?: string
    }
  | {
      source: 'git-subdir'
      url: string
      path: string
      ref?: string
    }

export interface SkillPackInstalled {
  pack_name: string
  pack_dir: string
  managed_by_liuruyan: boolean
  installed_version?: string | null
  installed_at?: number | null
  last_updated_at?: number | null
  source?: Record<string, unknown>
  has_venv?: boolean
  plugin_name?: string | null
  plugin_version?: string | null
  plugin_description?: string | null
}

export interface SkillPackInstallResponse {
  success: boolean
  pack_name: string
  pack_dir: string
  plugin_root: string
  plugin_root_relative: string
  plugin_name: string
  plugin_version: string
  skills_count: number
  venv_created: boolean
  deps_installed: boolean
  messages: string[]
}

export interface SkillPackUpdateResponse {
  success: boolean
  pack_name: string
  before_version?: string | null
  after_version?: string | null
  changed: boolean
  deps_installed: boolean
  messages: string[]
}

export interface SkillPackUninstallResponse {
  success: boolean
  pack_name: string
  removed?: string
  reason?: string
}

export interface SkillPackStatusSkill {
  name: string
  fully_qualified: string
  path: string
}

export interface SkillPackStatus {
  pack_name: string
  pack_dir: string
  is_git_repo: boolean
  venv_python: string | null
  manifest: Record<string, unknown>
  meta: Record<string, unknown> | null
  skills: SkillPackStatusSkill[]
}

// ────────────────────────────────────────────────────────────────────────────
// 通用错误处理
// ────────────────────────────────────────────────────────────────────────────

async function readJsonOrThrow<T>(res: Response, label: string): Promise<T> {
  if (!res.ok) {
    let detail = ''
    try {
      const data = (await res.json()) as { detail?: string }
      if (data && typeof data.detail === 'string') detail = data.detail
    } catch {
      try {
        detail = await res.text()
      } catch {
        // 忽略，下面用默认错误
      }
    }
    throw new Error(`${label} failed: ${res.status}${detail ? ` ${detail}` : ''}`)
  }
  return (await res.json()) as T
}

// ────────────────────────────────────────────────────────────────────────────
// API
// ────────────────────────────────────────────────────────────────────────────

export async function listSkillPacks(): Promise<SkillPackInstalled[]> {
  const res = await fetch(`${API_BASE}/skill-packs`)
  const data = await readJsonOrThrow<{ packs: SkillPackInstalled[] }>(res, 'GET /skill-packs')
  return data.packs ?? []
}

export async function getSkillPackStatus(packName: string): Promise<SkillPackStatus> {
  const res = await fetch(`${API_BASE}/skill-packs/${encodeURIComponent(packName)}`)
  return readJsonOrThrow<SkillPackStatus>(res, `GET /skill-packs/${packName}`)
}

export interface InstallSkillPackArgs {
  source: SkillPackSource
  pack_name?: string
  create_venv?: boolean
  install_deps?: boolean
  overwrite?: boolean
}

export async function installSkillPack(
  args: InstallSkillPackArgs,
): Promise<SkillPackInstallResponse> {
  const res = await fetch(`${API_BASE}/skill-packs/install`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(args),
  })
  return readJsonOrThrow<SkillPackInstallResponse>(res, 'POST /skill-packs/install')
}

export async function updateSkillPack(
  packName: string,
  options?: { install_deps?: boolean },
): Promise<SkillPackUpdateResponse> {
  const res = await fetch(`${API_BASE}/skill-packs/${encodeURIComponent(packName)}/update`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(options ?? {}),
  })
  return readJsonOrThrow<SkillPackUpdateResponse>(res, `POST /skill-packs/${packName}/update`)
}

export async function uninstallSkillPack(packName: string): Promise<SkillPackUninstallResponse> {
  const res = await fetch(`${API_BASE}/skill-packs/${encodeURIComponent(packName)}`, {
    method: 'DELETE',
  })
  return readJsonOrThrow<SkillPackUninstallResponse>(res, `DELETE /skill-packs/${packName}`)
}
