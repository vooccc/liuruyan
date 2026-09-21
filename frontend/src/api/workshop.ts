/**
 * 角色工坊（Character Workshop）专用 API 封装
 * 
 * 复用后端已有端点：
 *   GET/PUT  /agents/{id}/files/{filename}  → 读写任意 .md 文件
 *   PUT      /agents/{id}                   → 更新 meta.json
 *   GET      /agents/templates               → 获取模板列表
 *   POST     /agents/create-from-template    → 从模板创建角色
 */

import { API_BASE } from './base'
import { compressImageToBase64 } from '../services/providerLogoStorage'

// ====== 类型定义 ======

export interface PersonaData {
  /** meta.json 字段 */
  name: string
  description: string
  avatar: string
  /** 自定义头像图片路径（后端 /agents/{id}/avatar），若未上传图片则为空。
   *  和 avatar（emoji）共存：有图片时优先显示图片，否则显示 emoji。
   *  saveAgentPersona 不写这个字段——它由 uploadAgentAvatar / removeAgentAvatar / 模板应用专门管理。*/
  avatar_url?: string | null
  prototype?: string
  /** 角色核心字：一个汉字 + 破折号 + 一句解读（概括本质，只读展示，后续可考虑开放编辑）。
   *  e.g. "归——万界漂泊后的唯一锚点" */
  core?: string
  tags?: string[]

  /** 光谱 (7维, 每维 -1.0 ~ 1.0) */
  spectrum?: Record<string, number>

  /** 魅力层 (4层) */
  charm_layers?: Record<string, string[]>

  /** Markdown 文件内容 */
  identity_md?: string       // IDENTITY.md
  soul_md?: string            // SOUL.md
  personality_md?: string     // PERSONALITY.md
  genome_md?: string          // GENOME.md
}

/** 从后端加载的完整角色元数据（含 spectrum 等结构化字段） */
export interface AgentFullMeta extends Record<string, any> {
  id: string
  name: string
  description: string
  avatar: string
  avatar_url?: string
  prototype?: string
  core?: string
  tags?: string[]
  spectrum?: Record<string, number>
  charm_layers?: Record<string, string[]>
  template?: string
  emotion?: string
  [key: string]: any
}

// ====== 开发功能门禁 ======

export interface GeneEditorFeatureStatus {
  dev_runtime: boolean
  enabled: boolean
}

export async function getGeneEditorFeatureStatus(): Promise<GeneEditorFeatureStatus> {
  const res = await fetch(`${API_BASE}/debug/features/gene-editor`)
  if (!res.ok) return { dev_runtime: false, enabled: false }
  return res.json()
}

export async function unlockGeneEditorFeature(): Promise<GeneEditorFeatureStatus> {
  const res = await fetch(`${API_BASE}/debug/features/gene-editor/unlock`, { method: 'POST' })
  if (!res.ok) return { dev_runtime: false, enabled: false }
  return res.json()
}

// ====== 文件读写 ======

/** 读取角色的某个 .md 文件 */
export async function getAgentFile(agentId: string, filename: string): Promise<string> {
  const res = await fetch(`${API_BASE}/agents/${agentId}/files/${encodeURIComponent(filename)}`)
  if (!res.ok) throw new Error(`无法读取 ${filename}: ${res.statusText}`)
  const data = await res.json()
  return data.content || ''
}

/** 保存角色的某个 .md 文件 */
export async function saveAgentFile(agentId: string, filename: string, content: string): Promise<void> {
  const res = await fetch(`${API_BASE}/agents/${agentId}/files/${encodeURIComponent(filename)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content })
  })
  if (!res.ok) throw new Error(`无法保存 ${filename}: ${res.statusText}`)
}

// ====== 批量操作 ======

/** 加载角色的全部人设数据（用于编辑模式） */
export async function loadAgentPersona(agentId: string, options?: { includeGenome?: boolean }): Promise<PersonaData> {
  // 并行加载所有文件
  const [identity, soul, personality, genome] = await Promise.all([
    getAgentFile(agentId, 'IDENTITY.md').catch(() => ''),
    getAgentFile(agentId, 'SOUL.md').catch(() => ''),
    getAgentFile(agentId, 'PERSONALITY.md').catch(() => ''),
    options?.includeGenome ? getAgentFile(agentId, 'GENOME.md').catch(() => '') : Promise.resolve('')
  ])

  // 加载实例 meta
  const metaRes = await fetch(`${API_BASE}/agents/${agentId}`)
  let fullMeta: Partial<AgentFullMeta> = {}
  if (metaRes.ok) {
    const metaJson = await metaRes.json()
    // 后端返回 {"agent": {...}} 包裹格式，需要解包
    fullMeta = (metaJson.agent && typeof metaJson.agent === 'object') ? metaJson.agent : metaJson
  }

  // 如果实例缺少新字段（旧数据），从源模板补充
  let mergedMeta = { ...fullMeta }
  const needsTemplateMerge =
    !fullMeta.prototype ||
    !fullMeta.spectrum || Object.keys(fullMeta.spectrum || {}).length === 0 ||
    !fullMeta.charm_layers || Object.keys(fullMeta.charm_layers || {}).length === 0

  if (needsTemplateMerge) {
    try {
      const tplKey = fullMeta.source_template || fullMeta.template || ''
      if (tplKey && tplKey !== 'default') {
        const templatesRes = await fetch(`${API_BASE}/agents/templates`)
        const { templates } = await templatesRes.json()
        const tpl = templates.find((t: any) => t.template_id === tplKey)
        if (tpl) {
          console.log(`[Workshop] 从模板 ${tplKey} 补充缺失字段`)
          mergedMeta = {
            ...mergedMeta,
            prototype: mergedMeta.prototype || tpl.prototype || '',
            tags: mergedMeta.tags || [],
            spectrum: (mergedMeta.spectrum && Object.keys(mergedMeta.spectrum).length > 0)
              ? mergedMeta.spectrum : (tpl.spectrum || {}),
            charm_layers: (mergedMeta.charm_layers && Object.keys(mergedMeta.charm_layers).length > 0)
              ? mergedMeta.charm_layers : (tpl.charm_layers || []),
          }
        }
      }
    } catch (e) {
      console.warn('[Workshop] 模板字段补充失败，使用空值', e)
    }
  }

  return {
    name: mergedMeta.name || '',
    description: mergedMeta.description || '',
    avatar: mergedMeta.avatar || '🤖',
    avatar_url: mergedMeta.avatar_url || null,
    prototype: mergedMeta.prototype || '',
    core: mergedMeta.core || '',
    tags: mergedMeta.tags || [],
    spectrum: mergedMeta.spectrum || {},
    // 兼容：模板可能存为数组（旧格式），统一转为对象
    charm_layers: _normalizeCharmLayers(mergedMeta.charm_layers),
    identity_md: identity,
    soul_md: soul,
    personality_md: personality,
    genome_md: genome,
  }
}

/**
 * 将 charm_layers 统一为 Record<string, string[]> 格式
 * 模板 meta.json 可能存为 string[]（旧格式），需要兼容转换
 */
function _normalizeCharmLayers(val: any): Record<string, string[]> {
  if (!val) return {}
  if (Array.isArray(val)) {
    // 旧格式：字符串数组 → 转为默认4层结构
    console.warn('[Workshop] charm_layers 为数组格式，已自动转换为对象')
    const layers: Record<string, string[]> = {}
    val.forEach((item: string, i: number) => {
      layers[`layer_${i + 1}`] = [item]
    })
    return layers
  }
  if (typeof val === 'object' && !Array.isArray(val)) return val
  return {}
}

/** 批量保存人设数据 */
export async function saveAgentPersona(
  agentId: string, 
  data: Partial<PersonaData>,
  options?: { skipFiles?: string[] }
): Promise<void> {
  const skipFiles = new Set(options?.skipFiles || [])

  // 保存 meta 字段
  const metaUpdates: Record<string, any> = {}
  if (data.name !== undefined) metaUpdates.name = data.name
  if (data.description !== undefined) metaUpdates.description = data.description
  if (data.avatar !== undefined) metaUpdates.avatar = data.avatar
  if (data.prototype !== undefined) metaUpdates.prototype = data.prototype
  if (data.tags !== undefined) metaUpdates.tags = data.tags
  if (data.spectrum !== undefined) metaUpdates.spectrum = data.spectrum
  if (data.charm_layers !== undefined) metaUpdates.charm_layers = data.charm_layers

  if (Object.keys(metaUpdates).length > 0) {
    const res = await fetch(`${API_BASE}/agents/${agentId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(metaUpdates)
    })
    if (!res.ok) {
      const errBody = await res.text().catch(() => '')
      throw new Error(`无法更新 meta (${res.status}): ${errBody || res.statusText}`)
    }
  }

  // 保存各 .md 文件
  const fileSaves: Array<Promise<void>> = []

  if (data.identity_md !== undefined && !skipFiles.has('IDENTITY.md')) {
    fileSaves.push(saveAgentFile(agentId, 'IDENTITY.md', data.identity_md))
  }
  if (data.soul_md !== undefined && !skipFiles.has('SOUL.md')) {
    fileSaves.push(saveAgentFile(agentId, 'SOUL.md', data.soul_md))
  }
  if (data.personality_md !== undefined && !skipFiles.has('PERSONALITY.md')) {
    fileSaves.push(saveAgentFile(agentId, 'PERSONALITY.md', data.personality_md))
  }
  if (data.genome_md !== undefined && !skipFiles.has('GENOME.md')) {
    fileSaves.push(saveAgentFile(agentId, 'GENOME.md', data.genome_md))
  }

  await Promise.all(fileSaves)
}

// ====== 模板操作 ======

/** 从模板加载默认人设数据（用于新建模式） */
export async function loadTemplatePersona(templateId: string, options?: { includeGenome?: boolean }): Promise<PersonaData> {
  // 获取模板列表
  const templatesRes = await fetch(`${API_BASE}/agents/templates`)
  const { templates } = await templatesRes.json()
  const tpl = templates.find((t: any) => t.template_id === templateId)
  if (!tpl) throw new Error(`模板 ${templateId} 不存在`)

  // 尝试从模板目录读取实际 .md 文件内容
  let identity_md = '', soul_md = '', personality_md = '', genome_md = ''
  if (templateId !== 'custom') {
    try {
      [identity_md, soul_md, personality_md, genome_md] = await Promise.all([
        getAgentFile(templateId, 'IDENTITY.md').catch(() => ''),
        getAgentFile(templateId, 'SOUL.md').catch(() => ''),
        getAgentFile(templateId, 'PERSONALITY.md').catch(() => ''),
        options?.includeGenome ? getAgentFile(templateId, 'GENOME.md').catch(() => '') : Promise.resolve('')
      ])
    } catch (e) {
      console.warn('[Workshop] 模板md文件读取失败，使用空值', e)
    }
  }

  return {
    name: tpl.name || '',
    description: tpl.description || '',
    avatar: tpl.avatar || '✨',
    prototype: tpl.prototype || '',
    core: tpl.core || '',
    tags: [],
    spectrum: tpl.spectrum || {},
    charm_layers: _normalizeCharmLayers(tpl.charm_layers),
    identity_md,
    soul_md,
    personality_md,
    genome_md,
  }
}

// ====== 头像图片上传 / 移除 ======

/** 上传一张图片作为该角色的自定义头像。
 *  - 自动压缩到 ≤256px / quality 0.85
 *  - 后端写到 agent_dir/custom_avatar.png 并设置 meta.avatar_url
 *  - 返回新 avatar_url（不带 cache buster，调用方按需自加 ?v=Date.now()）
 */
export async function uploadAgentAvatar(
  agentId: string,
  file: File,
): Promise<{ avatar_url: string }> {
  if (file.size > 5 * 1024 * 1024) {
    throw new Error('图片不能超过 5MB')
  }
  const base64 = await compressImageToBase64(file, { maxSize: 256, quality: 0.85 })
  const res = await fetch(`${API_BASE}/agents/${agentId}/avatar`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ image_data: base64 }),
  })
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`上传失败 (${res.status}): ${txt || res.statusText}`)
  }
  const data = await res.json()
  return { avatar_url: data.avatar_url || `/agents/${agentId}/avatar` }
}

/** 移除该角色的自定义头像，回退到 emoji 显示。 */
export async function removeAgentAvatar(agentId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/agents/${agentId}/avatar`, { method: 'DELETE' })
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`移除失败 (${res.status}): ${txt || res.statusText}`)
  }
}
