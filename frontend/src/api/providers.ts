/**
 * Provider API 客户端（对应 backend/routes/providers_routes.py）。
 *
 * 后端路由：
 *   GET    /providers                         列出全部（不含 api_key）
 *   GET    /providers/{id}                    详情
 *   POST   /providers                         添加自定义
 *   PUT    /providers/{id}                    更新（partial）
 *   DELETE /providers/{id}                    删除自定义
 *   POST   /providers/{id}/models/discover    拉远端模型
 *   POST   /providers/{id}/models/import      批量导入到 models[]
 */
import { API_BASE } from './base'

/* ============================================================================
   类型
   ============================================================================ */

export interface ModelEntry {
  id: string
  name: string
  tags: string[]
  icon: string
  added_by_user: boolean
  pricing?: Record<string, string> | null
  context_length?: number | null
  context_length_source?: 'configured' | 'inferred'
}

/** GET /providers 单个响应（含完整 api_key；本地桌面应用前端持有明文）。 */
export interface ProviderView {
  id: string
  name: string
  base_url: string
  api_key: string
  api_key_prefix: string
  transport: string
  require_api_key: boolean
  freeze_url: boolean
  enabled: boolean
  is_aggregator: boolean
  is_builtin: boolean
  extra_headers: Record<string, string>
  description: string
  icon: string
  homepage: string
  api_key_url: string
  models: ModelEntry[]
  has_api_key: boolean
}

export interface ProvidersListResponse {
  providers: ProviderView[]
  current_provider: string
  current_model: string
}

export interface DiscoveredModel {
  id: string
  name: string
  owned_by: string
  description: string
  is_free: boolean
  free_reason: string   // "pricing" / "suffix" / "token" / "whitelist" / "local"
  tags: string[]
  pricing?: Record<string, string> | null
  context_length?: number | null
}

export interface DiscoverResponse {
  provider_id: string
  count: number
  free_count: number
  models: DiscoveredModel[]
}

export interface ImportResponse {
  status: string
  added: string[]
  added_count: number
  total_models: number
}

/* ============================================================================
   helpers
   ============================================================================ */

async function http<T>(
  path: string,
  init?: RequestInit & { json?: unknown },
): Promise<T> {
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
      detail = typeof err.detail === 'string' ? err.detail : JSON.stringify(err)
    } catch {
      detail = await res.text()
    }
    throw new Error(`[${res.status}] ${detail || res.statusText}`)
  }
  if (res.status === 204) return undefined as unknown as T
  return res.json()
}

/* ============================================================================
   API
   ============================================================================ */

export function listProviders(): Promise<ProvidersListResponse> {
  return http('/providers')
}

/** 拉项目级默认 LLM headers（Cherry-style UA + HTTP-Referer + X-Title）。
 *  后端 platform 动态生成，前端无法精确还原，必须靠这个 endpoint。 */
export function getDefaultHeaders(transport?: string): Promise<{ headers: Record<string, string> }> {
  const qs = transport ? `?transport=${encodeURIComponent(transport)}` : ''
  return http(`/providers/default-headers${qs}`)
}

export function getProvider(id: string): Promise<ProviderView> {
  return http(`/providers/${encodeURIComponent(id)}`)
}

export interface CreateProviderBody {
  id: string
  name: string
  base_url?: string
  api_key?: string
  transport?: string
  api_key_prefix?: string
  extra_headers?: Record<string, string>
  description?: string
  icon?: string
}

export function createProvider(body: CreateProviderBody): Promise<{ status: string; provider: ProviderView }> {
  return http('/providers', { method: 'POST', json: body })
}

/** 重排 providers 顺序。前端拖拽完成后调一次，后端按列表顺序写盘。 */
export function reorderProviders(order: string[]): Promise<{ status: string; order: string[] }> {
  return http('/providers/order', { method: 'PUT', json: { order } })
}

export interface UpdateProviderBody {
  name?: string
  base_url?: string
  api_key?: string
  transport?: string
  enabled?: boolean
  extra_headers?: Record<string, string>
  description?: string
  icon?: string
  models?: ModelEntry[]
}

export function updateProvider(id: string, body: UpdateProviderBody): Promise<{ status: string; provider: ProviderView }> {
  const json = body.models
    ? {
        ...body,
        models: body.models.map(({ context_length_source: source, ...model }) => ({
          ...model,
          // API 展示层会补推断值；写回时必须还原为空，否则一次重排/删除
          // 就会把所有自动识别结果误存成用户显式配置。
          context_length: source === 'inferred' ? null : model.context_length,
        })),
      }
    : body
  return http(`/providers/${encodeURIComponent(id)}`, { method: 'PUT', json })
}

export function deleteProvider(id: string): Promise<{ status: string }> {
  return http(`/providers/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export interface DiscoverOverrides {
  api_key?: string
  base_url?: string
  extra_headers?: Record<string, string>
}

export function discoverModels(id: string, overrides?: DiscoverOverrides): Promise<DiscoverResponse> {
  return http(`/providers/${encodeURIComponent(id)}/models/discover`, {
    method: 'POST',
    json: overrides ?? {},
  })
}

export interface ImportModelsBody {
  model_ids?: string[]
  models?: Array<Partial<DiscoveredModel>>
  replace?: boolean
}

export function importModels(id: string, body: ImportModelsBody): Promise<ImportResponse> {
  return http(`/providers/${encodeURIComponent(id)}/models/import`, {
    method: 'POST',
    json: body,
  })
}

/* ============================================================================
   /providers/active —— per-agent model routing 阶段 2
   ============================================================================ */

export type ActiveScope = 'global' | 'agent' | 'effective'

export interface ActiveModelView {
  provider_id: string
  model: string
}

/** 读 model 配置。
 *  - scope='global'：全局 current_provider/current_model
 *  - scope='agent'：agent meta.json 的 active_model（未配置返回空字符串）
 *  - scope='effective'：跑 resolve 链得到实际生效值（含 fallback）
 *  agent_id：scope=agent 必填；scope=effective 可选
 */
export function getActiveModel(scope: ActiveScope, agentId?: string): Promise<ActiveModelView> {
  const qs = new URLSearchParams({ scope })
  if (agentId) qs.set('agent_id', agentId)
  return http(`/providers/active?${qs.toString()}`)
}

export interface SetActiveModelBody {
  scope: 'global' | 'agent'
  agent_id?: string
  provider_id?: string
  /** scope=agent 传空字符串表示清除（让 agent 回到全局默认）；scope=global 不允许空 */
  model: string
}

export function setActiveModel(body: SetActiveModelBody): Promise<ActiveModelView> {
  return http('/providers/active', { method: 'PUT', json: body })
}

/* ============================================================================
   /providers/translate-model —— 思考块翻译辅助模型（2026-05-25）

   "翻译模型"是任务级 model 配置，与 active scope=global/agent 不同维度：
   前者是"主对话用什么"，translate-model 是"思考块翻译这个任务用什么"。
   主对话用 reasoning model（Claude 4 / o-series / R1）时把翻译指给一个 fast
   non-reasoning model（gpt-4o-mini / qwen-turbo / deepseek-chat）能彻底规避
   "reasoning model 把待译文本当指令理解 → 跑题"。
   未配置（两字段都为 ""）时后端 fallback 到当前会话 model + use_disable_thinking
   兜底关 reasoning。
   ============================================================================ */

export function getTranslateModel(): Promise<ActiveModelView> {
  return http('/providers/translate-model')
}

export interface SetTranslateModelBody {
  /** 空串 + model 为空串 = 清除配置（fallback 到会话 model） */
  provider_id: string
  model: string
}

export function setTranslateModel(body: SetTranslateModelBody): Promise<ActiveModelView> {
  return http('/providers/translate-model', { method: 'PUT', json: body })
}
