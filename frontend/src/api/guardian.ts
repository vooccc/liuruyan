/**
 * Guardian 权限配置 API
 *
 * 后端：backend/guardian_config.py 落盘到 data_dir() / guardian_config.json
 * 优先级：guardian_config.json → 硬编码 defaults
 *
 * 保存后调 Guardian.reload_config() 让 in-memory 单例立刻生效（不用重启）。
 */

import { API_BASE } from './base'

export type GuardianAction = 'ALLOW' | 'DENY'

/** 5 个开关 + 2 个文件名列表，与 backend/guardian_config.py 的 HARDCODED_DEFAULTS 对齐 */
export interface GuardianRuleSet {
  private_files: string[]
  public_files: string[]
  other_private: GuardianAction
  write_other_agent: GuardianAction
  delete: GuardianAction
  execute_command: GuardianAction
  write_backend: GuardianAction
}

/** 5 个开关字段（overrides 里只能用这些 keys） */
export type GuardianSwitchKey =
  | 'other_private'
  | 'write_other_agent'
  | 'delete'
  | 'execute_command'
  | 'write_backend'

export const GUARDIAN_SWITCH_KEYS: GuardianSwitchKey[] = [
  'other_private',
  'write_other_agent',
  'delete',
  'execute_command',
  'write_backend',
]

export interface GuardianConfig {
  version?: string
  defaults?: Partial<GuardianRuleSet>
  overrides?: Record<string, Partial<Pick<GuardianRuleSet, GuardianSwitchKey>>>
}

export interface GuardianConfigResponse {
  /** 用户落盘的子集 */
  config: GuardianConfig
  /** 硬编码默认（来自后端 HARDCODED_DEFAULTS） */
  hardcoded_defaults: GuardianRuleSet
  /** HARDCODED_DEFAULTS ← config.defaults 合并后的最终 defaults */
  effective_defaults: GuardianRuleSet
  /** 可配的 agent 列表（来自 AgentConfigManager.list_agents()） */
  agents: Array<{ id: string; name: string }>
}

export async function getGuardianConfig(): Promise<GuardianConfigResponse> {
  const res = await fetch(`${API_BASE}/guardian/config`)
  if (!res.ok) throw new Error('读取权限配置失败')
  return res.json()
}

export async function setGuardianConfig(
  cfg: GuardianConfig,
): Promise<GuardianConfigResponse & { status: string }> {
  const res = await fetch(`${API_BASE}/guardian/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error(data?.detail || '保存权限配置失败')
  }
  return res.json()
}

export const GUARDIAN_SWITCH_LABELS: Record<GuardianSwitchKey, { label: string; hint: string; risky?: boolean }> = {
  other_private: {
    label: '读其他角色隐私文件',
    hint: 'SOUL.md / REFLECTIONS.md / meta.json 等。默认 DENY',
  },
  write_other_agent: {
    label: '写其他角色目录',
    hint: '修改隔壁角色的文件。默认 DENY',
  },
  delete: {
    label: '执行删除',
    hint: '默认 ALLOW（M2 上线 CONFIRM 后默认改为待用户确认）',
  },
  execute_command: {
    label: '执行命令',
    hint: 'execute_command / execute_powershell。默认 ALLOW（M2 上线 CONFIRM 后默认改为待用户确认）',
  },
  write_backend: {
    label: '写后端代码',
    hint: '允许角色改 backend/* 业务代码。Guardian 自身的 6 个文件 + 配置 json 在红区，无视此开关都拒写——所以勾上是"敢用"的。每次写会自动备份到 data_dir/backups/，落 backend_writes.jsonl 审计，并在右下角弹 toast。',
    risky: true,
  },
}
