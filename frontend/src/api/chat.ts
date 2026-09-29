import { subagentLiveBus } from './subagentLiveBus'
import { API_BASE } from './base'
import { useAppStore } from '../stores/useAppStore'
import { handleIdeToolRequest } from '../runtime/ideToolHandler'
import {
  asRuntimeNoticePayload,
  getRuntimeNoticeRefs,
  normalizeRouteDiagnosticsPayload,
  normalizeRouteBudgetState,
  normalizeRouteRuntimeSnapshot,
} from './runtimeNormalizers'
import {
  isRuntimeStreamEventType,
  type StreamEventType,
  type ConsultStreamEventType,
} from './streamEventTypes'

function noStoreFetch(input: RequestInfo | URL, init?: RequestInit) {
  return fetch(input, {
    ...init,
    cache: 'no-store',
    headers: {
      ...(init?.headers || {}),
      'Cache-Control': 'no-cache',
      Pragma: 'no-cache',
    },
  })
}

// ====== 类型定义 ======

export interface ConfigResponse {
  identity: { name: string; identity: string; style: string }
  user: { title: string; role: string }
}

export interface Message {
  id: number
  role: 'user' | 'assistant'
  content: string
  timestamp?: string
  isStreaming?: boolean
  ephemeral?: boolean
  message_seq?: number
  history_revision?: number
  // 端到端追踪：前端为每条 message 生成 UUID，backend 持久化 + 响应带回，
  // buildSessionMessages 用它一对一匹配 in-flight cache 跟 backend 持久化条目，
  // 不依赖 segments 字段对齐。老 session 没有此字段，走 signature/content 兜底匹配。
  client_id?: string
  // 产出该消息的角色 ID。流式占位阶段（segments 尚空）用于头像兜底，避免回退到
  // currentAgent 显示成默认头像；首个 segment 到达后改用 segment.agent_id。
  agent_id?: string
  // 向后兼容字段（旧消息可能只有这些）
  thinkingContent?: string
  toolCalls?: ToolCallItem[]
  // 新的有序内容片段
  segments?: ContentSegment[]
}

export const RUNTIME_SEGMENT_TYPES = ['thinking', 'text', 'tool_call', 'tool_result', 'system_notice'] as const
export type RuntimeSegmentType = typeof RUNTIME_SEGMENT_TYPES[number]

export interface ContentSegment {
  id: number                // 序号，保证渲染顺序
  type: 'thinking' | 'text' | 'tool_call' | 'tool_result' | 'system_notice'
  content?: string          // thinking/text/tool_result 的内容文本
  kind?: string             // system_notice 的通知类型
  payload?: RuntimeNoticePayload
  agent_id?: string         // 产出该片段的角色 ID（多人/征求意见场景）
  // tool_call 专用字段（多来源兼容）
  tool_name?: string         // 工具名称（渲染层优先读取）
  name?: string              // 兼容旧字段
  arguments?: any            // 工具参数（JSON 对象）
  input?: any                // 兼容旧字段
  tool_use_id?: string       // 用于匹配对应的 tool_result
  result?: any               // 工具执行结果
  toolCall?: ToolCallItem    // 向后兼容的旧格式
  // tool_call / thinking 段首次到达前端的时间戳（Date.now() 毫秒）。
  // 仅由 SSE live 事件路径注入；缓存/历史回放路径不设，确保历史消息不触发展示锁/耗时。
  // tool_call：用于 runtimeBlocks 的"calling 最小展示时长锁"。
  // thinking：用于完成后展示本轮思考耗时。
  firstSeenAt?: number
  completedAt?: number
  /** v4 (2026-05-22) L2 edit：该 segment 在原 assistant content[] 中的下标。
   *  仅由 GET /sessions/{id} 在 assistant message segments 上注入；live SSE 事件
   *  不带此字段。前端 InplaceMessageEditor 在构造 block_patches 时读此字段，
   *  作为 PATCH /sessions/{id}/messages/{idx} 的 block_patches[].block_index。 */
  block_index?: number
}

export interface ToolCallItem {
  name: string
  input?: any
  result?: string
  status: 'calling' | 'done'
}

export interface RouteSessionLoad {
  session_id?: string
  workers?: Record<string, number>
  plans?: Record<string, number>
  limits?: Record<string, number>
  capacity?: Record<string, number | boolean>
}

export interface RouteRecentOutcomes {
  [key: string]: number | undefined
  task_routed?: number
  worker_success_total?: number
  worker_failure_total?: number
  worker_plan_failed?: number
  budget_warning_total?: number
  budget_exhausted_total?: number
  fallback_to_inline?: number
}

export interface RouteBudgetPressure {
  tight_route_count?: number
  warning_routes?: number
  exhausted_routes?: number
  warning_events?: number
  exhausted_events?: number
  pressure_axes?: Record<string, number>
  limit_type_counts?: Record<string, number>
  dominant_axis?: string
  last_kind?: string
  last_recorded_at?: string
  last_snapshot?: Record<string, unknown>
  peak_highest_usage_ratio?: number
}

export interface RouteRuntimeSnapshot {
  session_load?: RouteSessionLoad
  recent_outcomes?: RouteRecentOutcomes
  budget_pressure?: RouteBudgetPressure
}

export interface RouteBudgetState {
  warning_count: number
  exhausted_count: number
  limit_type: string
  pressure_axes: string[]
  last_snapshot: Record<string, unknown>
  last_kind?: string
  last_recorded_at?: string
}

export interface RuntimeNoticeRefs {
  route_id?: string
  worker_id?: string
  plan_id?: string
  plan_step_id?: string
  plan_stage?: number
}

export interface RuntimeNoticePayload {
  contract_version?: number
  refs?: RuntimeNoticeRefs
  strategy?: 'inline' | 'worker' | 'worker_plan' | string
  confidence?: string
  source?: string
  task_class?: string
  priority?: number
  timeout?: number
  execution_status?: string
  task?: string
  title?: string
  max_steps?: number
  runtime_snapshot?: RouteRuntimeSnapshot
  route_diagnostics?: RouteDiagnostics
  budget_state?: RouteBudgetState
  depends_on?: string[]
  // Runtime notice/card rendering fields
  workerId?: string
  started_at?: string
  completed?: boolean
  status?: string
  description?: string
  provider?: string
  model?: string
  subagent_id?: string
  [key: string]: unknown
}

// P6 拆分：上述 5 个纯 payload 规整器已移到 ./runtimeNormalizers（顶部 import 供
// 本文件内部使用）。此处 facade 再导出，所有现有 `from '../api/chat'` 引用不变。
export {
  asRuntimeNoticePayload,
  getRuntimeNoticeRefs,
  normalizeRouteDiagnosticsPayload,
  normalizeRouteBudgetState,
  normalizeRouteRuntimeSnapshot,
}

export interface Session {
  id: string
  title: string
  createdAt: string
  updatedAt: string
  messageCount: number
  historyRevision?: number
  nextMessageSeq?: number
  nextEventSeq?: number
  messages: Message[]
  agent_id?: string
  status?: string  // "active" | "idle" | "terminated"
  metadata?: Record<string, unknown>  // 协作会话类型标识
  /** per-session model override：用户在该会话顶栏切过的模型 id；空表示
   *  跟随 agent.active_model / global current_model（resolve_call_model_config
   *  优先级：override > session.model > agent > global）。 */
  model?: string | null
  /** 与 model 配对的 provider id（provider.id，稳定 ID，不是人读 name）。2026-05-12 引入。
   *  同一 model id 可能存在于多个 provider 下（典型：多家 anthropic 反代都有
   *  claude-opus-4-7），仅靠 model id 反查会命中列表里第一家。hydrate picker
   *  时优先读该字段，仅当老 session 缺该字段时 fallback 到 customModels.find 。 */
  provider?: string | null
  /** Token 累计消耗（后端 session.json 持久化 → listSessions 返回 → hydrate store），
   *  与 SSE `session_token_usage` 事件同源 — payload 是累加后的总值。 */
  token_usage?: {
    input?: number
    output?: number
    cache_read?: number
    cache_create?: number
    total?: number
    calls?: number
    last_call_at?: string | null
  } | null
}

export interface InvalidSessionSummaryItem {
  session_id: string
  reason: string
  agent_id?: string
  title?: string
  metadata?: Record<string, unknown>
}

export interface SessionWarningSummary {
  invalid_session_count: number
  invalid_sessions: InvalidSessionSummaryItem[]
}

export interface ListSessionsResponse {
  sessions: Session[]
  session_warnings?: SessionWarningSummary
}

/** 跨会话感知信息 */
export interface PeerAwareness {
  session_id: string
  status: string
  title: string
  last_active_at: string
  emotion_label: string
}

/** Runtime route task */
export interface RuntimeTask {
  task_id: string
  parent_session_id: string
  agent_id: string
  description: string
  model?: string | null
  provider?: string | null
  priority: number
  timeout: number
  depends_on: string[]
  route_id?: string | null
  plan_id?: string | null
  plan_step_id?: string | null
  plan_stage?: number | null
  status: 'pending' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  result: string | null
  error: string | null
  retry_count: number
  started_at: string | null
  completed_at: string | null
  updated_at?: string | null
  worker_session_id?: string | null
  trace_event_count?: number
  notice_event_count?: number
}

export interface RuntimeTraceEvent {
  seq: number
  type: string
  timestamp: string
  text?: string
  kind?: string
  content?: string
  name?: string
  id?: string
  tool_use_id?: string
  input?: any
  result?: any
  status?: string
  message?: string
  worker_session_id?: string
  parent_session_id?: string
  payload?: Record<string, unknown>
}

export interface RuntimeTaskDetail extends RuntimeTask {
  trace_events: RuntimeTraceEvent[]
  notice_events: Record<string, any>[]
}

export interface RuntimePlanStep {
  step_id: string
  worker_id: string
  description: string
  priority: number
  timeout: number
  depends_on: string[]
  stage: number
  status?: RuntimeTask['status']
  error?: string | null
  result?: string | null
  updated_at?: string | null
}

export interface RuntimePlan {
  plan_id: string
  parent_session_id: string
  agent_id: string
  title?: string | null
  route_id?: string | null
  status: 'pending' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  created_at: string
  updated_at: string
  worker_count: number
  stage_count: number
  steps?: RuntimePlanStep[]
}

export type WorkerTask = RuntimeTask
export type WorkerTraceEvent = RuntimeTraceEvent
export type WorkerDetail = RuntimeTaskDetail
export type WorkerPlanStep = RuntimePlanStep
export type WorkerPlan = RuntimePlan

export interface RouteDiagnostics {
  kind: 'policy' | 'heuristic' | 'model' | 'fallback'
  rule_id: string
  title: string
  summary: string
  matched_conditions: string[]
  observed?: Record<string, unknown>
  suggested_action?: string
}

export interface Skill {
  name: string
  display_name?: string
  description: string
  disabled: boolean
  path: string
  category?: string
  tags?: string[]
}

export interface DiaryEntry {
  date: string
  path: string
}


// ====== 智能体（Agent）数据模型 ======
// 前端 localStorage + 后端 agents/ 目录双重存储
// 后端是 source of truth，前端 localStorage 作为缓存

export interface Agent {
  id: string
  name: string
  description: string
  avatar: string        // emoji 或图片 URL
  avatar_url?: string   // 模板头像 API 路径
  avatar_template?: string
  systemPrompt?: string // 可选的系统提示词
  emotion?: string      // 当前情感状态
  emotionIntensity?: number // 情感强度
  template?: string     // 创建模板
  createdAt: string
  order: number
}

export interface Template {
  template_id: string
  name: string
  description: string
  avatar: string        // emoji 备选
  avatar_url?: string   // 头像图片 URL
  prototype?: string    // 角色类型（万界型、冷傲型等）
  order?: number
  core?: string         // 核心底色
}

export interface EmotionState {
  current: string
  intensity: number
  history: Array<{ emotion: string; intensity: number; timestamp: string }>
  updated_at: string | null
}

export interface EmotionInfo {
  emotion: EmotionState
  states: Record<string, { color: string; icon: string }>
}

const AGENTS_STORAGE_KEY = 'liuryan_agents'

function parseBackendAgent(a: any): Agent {
  return {
    id: a.id,
    name: a.name,
    description: a.description || '',
    avatar: a.avatar || '🤖',
    avatar_url: a.avatar_url || undefined,
    avatar_template: a.avatar_template || undefined,
    emotion: a.emotion || '平静',
    emotionIntensity: a.emotion_intensity || 0.5,
    template: a.template || 'default',
    createdAt: a.created_at || new Date().toISOString(),
    order: a.order ?? 0,
  }
}

/** 获取所有智能体（先从后端同步，缓存到 localStorage） */
export async function syncAgentsFromBackend(): Promise<Agent[]> {
  try {
    const res = await fetch(`${API_BASE}/agents`)
    if (res.ok) {
      const data = await res.json()
      const backendAgents: Agent[] = (data.agents || []).map(parseBackendAgent)

      // 后端是 source of truth：直接以后端列表覆盖本地缓存。
      // 不再保留"后端没有的本地条目"——那是僵尸保留逻辑，会让磁盘上已删除的 agent
      // 永远赖在 sidebar 上（详见 _docs/handover_chat_dedup_client_id_root_fix_2026_04_26.md §2）。
      // 仅做同名 ID 迁移所需的旧条目读取。
      const localAgents = getAgentsLocal()
      const backendIds = new Set(backendAgents.map(a => a.id))
      const backendNames = new Set(backendAgents.map(a => a.name))

      // 同名但 ID 变更的迁移（重建场景：name 还在，id 已换）
      const idMapping: Record<string, string> = {}
      for (const local of localAgents) {
        if (!backendIds.has(local.id) && backendNames.has(local.name)) {
          const newAgent = backendAgents.find(b => b.name === local.name)
          if (newAgent) idMapping[local.id] = newAgent.id
        }
      }
      if (Object.keys(idMapping).length > 0) {
        migrateSessionAgentIds(idMapping)
      }

      localStorage.setItem(AGENTS_STORAGE_KEY, JSON.stringify(backendAgents))
      return backendAgents
    }
  } catch { /* 后端不可用，使用本地缓存 */ }
  return getAgentsLocal()
}

/** @deprecated 不再使用 localStorage session-agent 映射，迁移函数已废弃 */
function migrateSessionAgentIds(_idMapping: Record<string, string>) {
  // noop - session-agent 映射已统一用后端 session.agent_id
}

/** 从 localStorage 获取智能体列表（纯本地，不请求后端） */
export function getAgentsLocal(): Agent[] {
  try {
    const raw = localStorage.getItem(AGENTS_STORAGE_KEY)
    return raw ? JSON.parse(raw) : []
  } catch { return [] }
}

/** 获取所有智能体（兼容旧版，本地读取） */
export function getAgents(): Agent[] {
  return getAgentsLocal()
}

/** 保存所有智能体到本地 */
function saveAgents(agents: Agent[]): void {
  localStorage.setItem(AGENTS_STORAGE_KEY, JSON.stringify(agents))
}

// ====== Session ↔ Agent 映射 ======
// 已废弃：不再使用 localStorage 映射，统一用后端 session.agent_id
// 保留空函数以避免编译错误，后续可完全删除

/** @deprecated 不再使用，保留空实现 */
export function bindSessionToAgent(_sessionId: string, _agentId: string): void {}

/** @deprecated 不再使用，保留空实现 */
export function getSessionAgentId(_sessionId: string): string | null { return null }

/** @deprecated 不再使用，保留空实现 */
export function getAgentSessionIds(_agentId: string): Set<string> { return new Set() }

/** @deprecated 不再使用，保留空实现 */
export function getUnboundSessionIds(): Set<string> { return new Set() }

/** 确保默认智能体存在（每次都从后端同步以更新情感等动态字段） */
export async function ensureDefaultAgent(): Promise<Agent[]> {
  let agents = await syncAgentsFromBackend()
  if (agents.length === 0) {
    // 后端也没有，从模板创建柳如烟
    try {
      const templates = await fetchTemplates()
      const liuruyan = templates.find(t => t.template_id === 'liuruyan')
      if (liuruyan) {
        await createAgentBackend(liuruyan.name, liuruyan.description, liuruyan.avatar, 'liuruyan')
      } else {
        await createAgentBackend('柳如烟', '温柔灵性，愿为老板赴汤蹈火…', '🌸', 'default')
      }
      agents = await syncAgentsFromBackend()
    } catch {
      // 后端不可用，本地创建
      const defaultAgent: Agent = {
        id: 'default',
        name: '柳如烟',
        description: '温柔灵性，愿为老板赴汤蹈火…',
        avatar: '🌸',
        template: 'liuruyan',
        createdAt: new Date().toISOString(),
        order: 0,
      }
      saveAgents([defaultAgent])
      agents = [defaultAgent]
    }
  }
  return agents
}


// ====== 模板 API ======

/** 获取所有角色模板 */
export async function fetchTemplates(): Promise<Template[]> {
  const res = await fetch(`${API_BASE}/agents/templates`)
  if (!res.ok) throw new Error('Failed to fetch templates')
  const data = await res.json()
  return data.templates || []
}


// ====== 后端 Agent CRUD API ======

/** 在后端创建智能体 */
export async function createAgentBackend(name: string, description: string = '', avatar: string = '🤖', template: string = 'default'): Promise<Agent> {
  const res = await fetch(`${API_BASE}/agents`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description, avatar, template }),
  })
  if (!res.ok) throw new Error('Failed to create agent')
  const data = await res.json()
  const agent: Agent = parseBackendAgent(data.agent)
  // 同步到本地
  const agents = getAgents()
  agents.push(agent)
  saveAgents(agents)
  return agent
}

/** 在后端更新智能体 */
export async function updateAgentBackend(agentId: string, updates: Partial<{ name: string; description: string; avatar: string }>): Promise<Agent | null> {
  const res = await fetch(`${API_BASE}/agents/${agentId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
  if (!res.ok) throw new Error('Failed to update agent')
  const data = await res.json()
  const updated: Agent = parseBackendAgent(data.agent)
  // 同步到本地
  const agents = getAgents()
  const idx = agents.findIndex(a => a.id === agentId)
  if (idx >= 0) agents[idx] = updated
  saveAgents(agents)
  return updated
}

export async function applyTemplateAvatarBackend(agentId: string, templateId: string): Promise<Agent> {
  const res = await fetch(`${API_BASE}/agents/${agentId}/avatar/template`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ template_id: templateId }),
  })
  if (!res.ok) throw new Error('Failed to apply template avatar')
  const data = await res.json()
  const updated = parseBackendAgent(data.agent)
  const agents = getAgents()
  const idx = agents.findIndex((agent) => agent.id === agentId)
  if (idx >= 0) agents[idx] = updated
  saveAgents(agents)
  return updated
}

/** 把当前角色实例保存为新模板（反向冻结）。
 *
 * 错误语义：
 * - 400 + detail.code === 'TEMPLATE_VALIDATION_FAILED'：必填字段缺失，detail.errors 是清单
 * - 409：模板 ID 已存在 / 撞内置模板
 */
export interface SaveAsTemplateOptions {
  template_id: string
  overwrite?: boolean
  include_memory?: boolean
  include_user?: boolean
}

export interface SaveAsTemplateError extends Error {
  status: number
  code?: string
  errors?: string[]
}

export async function saveAgentAsTemplate(
  agentId: string,
  options: SaveAsTemplateOptions,
): Promise<Template> {
  const res = await fetch(`${API_BASE}/agents/${agentId}/save-as-template`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(options),
  })
  if (!res.ok) {
    let detail: any = null
    try { detail = (await res.json())?.detail } catch {}
    const err: SaveAsTemplateError = Object.assign(
      new Error(typeof detail === 'string' ? detail : (detail?.message || `保存模板失败 (HTTP ${res.status})`)),
      {
        status: res.status,
        code: typeof detail === 'object' ? detail?.code : undefined,
        errors: typeof detail === 'object' && Array.isArray(detail?.errors) ? detail.errors : undefined,
      },
    )
    throw err
  }
  const data = await res.json()
  return data.template as Template
}

/** 在后端删除智能体 */
export async function deleteAgentBackend(agentId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/agents/${agentId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('Failed to delete agent')
  // 同步到本地
  const agents = getAgents().filter(a => a.id !== agentId)
  saveAgents(agents)
}

/** 获取智能体情感状态 */
export async function getAgentEmotion(agentId: string): Promise<EmotionInfo> {
  const res = await fetch(`${API_BASE}/agents/${agentId}/emotion`)
  if (!res.ok) throw new Error('Failed to get emotion')
  return res.json()
}

// ====== 兼容旧版的本地 Agent 操作 ======

/** 创建新智能体（本地 + 后端） */
export async function createAgent(name: string, description: string = '', avatar: string = '🤖'): Promise<Agent> {
  try {
    // 优先创建后端版本，拿到真正的 agent ID
    const backendAgent = await createAgentBackend(name, description, avatar)
    // 保存到本地
    const agents = getAgents()
    agents.push(backendAgent)
    saveAgents(agents)
    return backendAgent
  } catch (err) {
    // 后端不可用，创建本地版本（使用临时 ID）
    console.warn('Backend agent creation failed, using local only:', err)
    const agents = getAgents()
    const newAgent: Agent = {
      id: 'agent_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
      name,
      description: description || `${name}的专属助手`,
      avatar,
      createdAt: new Date().toISOString(),
      order: agents.length,
    }
    agents.push(newAgent)
    saveAgents(agents)
    return newAgent
  }
}

/** 删除智能体（本地 + 后端） */
export function deleteAgent(agentId: string): void {
  const agents = getAgents().filter(a => a.id !== agentId)
  saveAgents(agents)
  deleteAgentBackend(agentId).catch(() => {})
}

/** 更新智能体信息（本地 + 后端） */
export function updateAgent(agentId: string, updates: Partial<Pick<Agent, 'name' | 'description' | 'avatar'>>): Agent | null {
  const agents = getAgents()
  const idx = agents.findIndex(a => a.id === agentId)
  if (idx < 0) return null
  agents[idx] = { ...agents[idx], ...updates }
  saveAgents(agents)
  updateAgentBackend(agentId, updates).catch(() => {})
  return agents[idx]
}


// ====== 配置 ======
export async function getConfig(): Promise<ConfigResponse> {
  const res = await fetch(`${API_BASE}/config`)
  if (!res.ok) throw new Error('Failed to fetch config')
  return res.json()
}

// ====== 流式事件类型 ======
// P6 拆分：流式/协作事件类型常量 + 两个类型守卫已移到 ./streamEventTypes
//（顶部 value-import 守卫供本文件内部使用）。此处 facade 再导出，所有现有
// `from '../api/chat'` 引用不变；StreamEvent / ConsultStreamEvent 接口仍留本文件。
export {
  STREAM_EVENT_TYPES,
  CONSULT_STREAM_EVENT_TYPES,
  COLLAB_RUNTIME_EVENT_TYPES,
  isRuntimeStreamEventType,
  isCollabRuntimeEventType,
} from './streamEventTypes'
export type { StreamEventType, ConsultStreamEventType } from './streamEventTypes'

export interface StreamEvent {
  type: StreamEventType
  content?: string
  kind?: string
  payload?: Record<string, any>
  name?: string
  input?: any
  result?: any
  error?: string
  id?: string
  session_id?: string
  tool_use_id?: string
  refs?: {
    worker_id?: string
    route_id?: string
    plan_id?: string
    plan_step_id?: string
    plan_stage?: number
  }
  // runtime transport 兼容：部分 worker/route 事件会把 refs 标量同时放在顶层。
  route_id?: string
  plan_id?: string
  worker_id?: string
  worker_ids?: string[]
}

export interface ConsultStreamEvent {
  type: ConsultStreamEventType
  session_id?: string
  agent_id?: string
  agent_name?: string
  question?: string
  display_prefix?: string
  timestamp?: string
  content?: string
  kind?: string
  name?: string
  input?: any
  result?: any
  response?: string
  message?: Message
  error?: string
  id?: string
  tool_use_id?: string
}

// ====== 聊天附件上传 ======
export interface ChatUploadResult {
  /** 后端落盘后的绝对路径（LLM 通过 read_file 工具读取） */
  path: string
  /** 用户上传时的原始文件名（带扩展） */
  original_name: string
  /** 字节数 */
  size: number
  /** mime type（来自浏览器 / 默认 application/octet-stream） */
  mime: string
}

/** 上传聊天附件到后端 data/uploads/，返绝对路径。50 MB 上限，文件名做 sanitize。 */
export async function uploadChatFile(file: File, signal?: AbortSignal): Promise<ChatUploadResult> {
  const fd = new FormData()
  fd.append('file', file, file.name)
  const res = await fetch(`${API_BASE}/chat/upload`, {
    method: 'POST',
    body: fd,
    signal,
  })
  if (!res.ok) {
    let detail = ''
    try {
      const j = await res.json()
      detail = j?.detail || ''
    } catch { /* 非 JSON */ }
    throw new Error(detail || `Upload failed: ${res.status}`)
  }
  return res.json()
}

// ====== 聊天 ======
export async function sendMessage(
  message: string,
  onChunk: (event: StreamEvent) => void,
  onComplete: () => void,
  sessionId?: string,
  modelId?: string,
  // providerId 与 modelId 配对，让后端按 (model, provider) 严格匹配 provider。
  // 同一个 model id 可能在多个 provider 下都存在（典型：多家 Anthropic 反代都有
  // claude-opus-4-7），仅靠 modelId 反查会永远命中第一家——必须把 provider 也传过去。
  providerId?: string,
  agentId?: string,
  signal?: AbortSignal,
  executionMode?: 'auto' | 'inline',
  routeMaxSteps?: number,
  clientIds?: { userClientId?: string; assistantClientId?: string },
  /** 用户从 📎 按钮上传的文件绝对路径数组（uploadChatFile 返回的 path）。
   *  后端 chat() 顶部会拼一个 "[附件]" footer 让 LLM 自动调 read_file 读。 */
  attachments?: string[],
): Promise<void> {
  const body: any = { message, stream: true }
  if (sessionId) body.session_id = sessionId
  if (modelId) body.model = modelId
  if (providerId) body.provider = providerId
  if (agentId) body.agent_id = agentId
  if (executionMode) body.execution_mode = executionMode
  if (typeof routeMaxSteps === 'number') body.route_max_steps = routeMaxSteps
  if (clientIds?.userClientId) body.user_client_id = clientIds.userClientId
  if (clientIds?.assistantClientId) body.client_id = clientIds.assistantClientId
  if (attachments && attachments.length > 0) body.attachments = attachments

  const res = await fetch(`${API_BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok) throw new Error('Failed to send message')

  const reader = res.body?.getReader()
  if (!reader) throw new Error('No response body')
  const decoder = new TextDecoder()

  let buffer = ''  // 缓冲区，处理跨 chunk 的不完整行
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // 按双换行分割完整的 SSE 事件
    const lines = buffer.split('\n\n')
    // 最后一个可能是不完整的，保留在缓冲区
    buffer = lines.pop() || ''

    for (const eventBlock of lines) {
      // 标准 SSE 事件块可能含多行：id: / event: / data:
      // 只提取 data: 行（多条 data: 按换行拼接），忽略其它元数据行
      const dataLines = eventBlock
        .split('\n')
        .filter(l => l.startsWith('data: '))
        .map(l => l.slice(6))
      if (dataLines.length > 0) {
        const data = dataLines.join('\n').trim()
        if (data === '[DONE]') { onComplete(); return }
        try {
          const parsed: StreamEvent = JSON.parse(data)
          if (parsed.error) { onChunk({ type: 'error', error: parsed.error }); onComplete(); return }

          // C-2: ide_tool_request 是 backend tool handler 反向调请求，本地代执行后 POST 回填，
          // 不进入用户聊天流（对用户透明）。详见 frontend/src/runtime/ideToolHandler.ts。
          if ((parsed as unknown as { type?: string }).type === 'ide_tool_request') {
            void handleIdeToolRequest(parsed)
            continue
          }

          // Goal 模式状态更新事件透传
          if ((parsed as unknown as { type?: string }).type === 'goal_state_update') {
            onChunk(parsed)
            continue
          }

          if (isRuntimeStreamEventType(parsed.type) && parsed.type !== 'error') {
            // ── 子代理事件处理（透传给主流 + 派发到 liveBus） ──
            if (parsed.type === 'system_notice' && typeof parsed.kind === 'string' && parsed.kind.startsWith('subagent')) {
              // batch_started：展开成 N 个 subagent_started segment，让 runtimeBlocks 渲染卡片
              if (parsed.kind === 'subagents_batch_started') {
                const metas: any[] = parsed.payload?.subagents || []
                for (const meta of metas) {
                  onChunk({
                    type: 'system_notice',
                    kind: 'subagent_started',
                    content: meta.name || '子代理',
                    payload: meta,
                  } as StreamEvent)
                }
                continue
              }
              // 其它 subagent_* 事件派发到 bus，不作为 segment 渲染（避免对话历史污染）
              subagentLiveBus.emit(parsed as any)
              continue
            }
            // ── 前端防御：解包 JSON 格式的 content ──
            if (parsed.type === 'content' && parsed.content) {
              const c = parsed.content
              if (c.length > 1 && c.trimStart().startsWith('{')) {
                try {
                  const _p = JSON.parse(c)
                  if (_p && typeof _p === 'object' && !Array.isArray(_p)) {
                    if (_p.segments && Array.isArray(_p.segments)) {
                      const _texts = _p.segments
                        .filter((s: any) => s && s.type === 'text' && s.content)
                        .map((s: any) => s.content)
                      if (_texts.length > 0) parsed.content = _texts.join('')
                      else if (typeof _p.content === 'string') parsed.content = _p.content
                    } else if (typeof _p.content === 'string') {
                      parsed.content = _p.content
                    }
                  }
                } catch { /* 不是有效 JSON，保持原样 */ }
              }
            }
            onChunk(parsed)
          } else if (parsed.content && !parsed.type) {
            onChunk({ type: 'content', content: parsed.content })
          } else if ((parsed as { type?: string }).type === 'session_token_usage') {
            // chat 流里也会镜像该事件，但 store 写入由独立 sse_manager 长连接
            // (useDispatchEventConsumer) 负责，这里静默丢弃避免重复 reduce 和噪声 warn。
          } else {
            console.warn('[SSE] Unknown event format:', parsed)
          }
        } catch (e) {
          console.warn('[SSE] JSON parse error:', data.substring(0, 100))
        }
      }
    }
  }
  onComplete()
}

export async function clearHistory(sessionId?: string): Promise<void> {
  const params = new URLSearchParams()
  if (sessionId) params.set('session_id', sessionId)
  const qs = params.toString()
  const url = qs ? `${API_BASE}/history?${qs}` : `${API_BASE}/history`
  const res = await fetch(url, { method: 'DELETE' })
  if (!res.ok) throw new Error('Failed to clear history')
}

// ====== 会话管理 ======
export async function listSessions(): Promise<ListSessionsResponse> {
  const res = await noStoreFetch(`${API_BASE}/sessions`)
  if (!res.ok) throw new Error('Failed to list sessions')
  const data = await res.json()
  const sessions: Session[] = data.sessions || []

  // Hydrate per-session tokenUsage 进 store。
  // 后端 session.json 持久化 token_usage，重启后 _recover_session 能恢复进内存，
  // 但前端 store 只在收到 SSE `session_token_usage` 时才填。这里补上初始拉取
  // 路径，避免重启后用户还没发新消息时 SessionTokenUsageBadge 一直显示 "—"。
  // setSessionTokenUsage 在 session 不存在时会自动创建最小 SessionState，所以
  // 不需要先 ensureSession。
  try {
    const setUsage = useAppStore.getState().setSessionTokenUsage
    for (const s of sessions) {
      const tu = s.token_usage
      if (!tu || typeof tu !== 'object') continue
      // context_breakdown：随 token_usage 持久化的上下文构成估算（可缺——
      // 旧会话/旧后端没有）。缺失时 undefined，徽章只渲染账单区。
      const cb = (tu as Record<string, unknown>).context_breakdown as
        | Record<string, unknown>
        | undefined
      setUsage(s.id, {
        input: Number(tu.input) || 0,
        output: Number(tu.output) || 0,
        cacheRead: Number(tu.cache_read) || 0,
        cacheCreate: Number(tu.cache_create) || 0,
        total: Number(tu.total) || 0,
        calls: Number(tu.calls) || 0,
        lastCallAt: typeof tu.last_call_at === 'string' ? tu.last_call_at : undefined,
        contextBreakdown: cb && typeof cb === 'object'
          ? {
              system: Number(cb.system) || 0,
              extra: Number(cb.extra) || 0,
              messages: Number(cb.messages) || 0,
              tools: Number(cb.tools) || 0,
              total: Number(cb.total) || 0,
              model: typeof cb.model === 'string' && cb.model ? cb.model : undefined,
              contextLength: Number(cb.context_length) || undefined,
              compactThreshold: Number(cb.compact_threshold) || undefined,
            }
          : undefined,
      })
    }
  } catch {
    // store hydrate 失败不影响列表返回
  }

  return {
    sessions,
    session_warnings: data.session_warnings,
  }
}

export async function createSession(title?: string, agentId?: string): Promise<Session> {
  console.log(`[API] createSession: title="${title}", agentId="${agentId}"`)
  const res = await fetch(`${API_BASE}/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, agent_id: agentId }),
  })
  if (!res.ok) throw new Error('Failed to create session')
  const data = await res.json()
  console.log(`[API] createSession response: agent_id="${data.session?.agent_id}"`)
  return data.session
}

export async function getSession(sessionId: string): Promise<Session> {
  const res = await noStoreFetch(`${API_BASE}/sessions/${sessionId}`)
  if (!res.ok) throw new Error('Session not found')
  const data = await res.json()
  return data.session
}

/** 列出指定 agent 名下的所有会话（轻量版，不 hydrate token_usage 到 store）。
 *  v4 (2026-05-22) 新增：替代旧 listDiaries 入口，给 MemoryPanel "会话浏览器" tab 用。
 *  后端 GET /sessions?agent_id=X 早已支持过滤；此 helper 仅做透薄包装并按
 *  updatedAt 倒序排列（最近活跃的在前）。
 *
 *  与 listSessions() 的区别：
 *  - 不 hydrate token_usage 到 store（避免污染当前活跃会话的 badge）
 *  - 按 updatedAt 倒序，便于 UI 直接渲染
 *  - 不抛 session_warnings（浏览用途，不参与决策）
 */
export async function listSessionsByAgent(agentId: string): Promise<Session[]> {
  const res = await noStoreFetch(`${API_BASE}/sessions?agent_id=${encodeURIComponent(agentId)}`)
  if (!res.ok) throw new Error('Failed to list sessions for agent')
  const data = await res.json()
  const sessions: Session[] = Array.isArray(data.sessions) ? data.sessions : []
  // 按 updatedAt 倒序。空字符串/undefined 排到最后。
  return sessions.slice().sort((a, b) => {
    const ta = a.updatedAt || ''
    const tb = b.updatedAt || ''
    if (!ta && !tb) return 0
    if (!ta) return 1
    if (!tb) return -1
    return ta < tb ? 1 : ta > tb ? -1 : 0
  })
}

export async function deleteSession(sessionId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('Failed to delete session')
}

/** 清除该角色下所有 main 会话的 per-session model override（session.model = null）。
 *  不清消息/token_usage/metadata。worker 子会话不动。 */
export async function clearAgentSessionModelOverrides(agentId: string): Promise<{ cleared: number; agent_id: string }> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/sessions/clear-model-override`, {
    method: 'POST',
  })
  if (!res.ok) throw new Error(`Failed to clear session model overrides: ${res.status}`)
  return res.json()
}

/** UI 层信号：清后通知 useChatModelManager 把内存 perSessionOverride 也清掉。
 *  detail.agentId 仅作日志，hook 直接清整个 map（其它 agent 的 session 切回去会
 *  从 session.model 重新种子，不影响）。 */
export const CLEAR_SESSION_MODEL_OVERRIDES_EVENT = 'liuruyan:clear-session-model-overrides'

/** UI 层信号：providers / models 目录有改动（新增/编辑/导入/删除 provider 或 model）。
 *  ProviderSettings.refresh() 成功后 dispatch；useChatModelManager 监听后重拉
 *  listModels()，让聊天界面下拉列表立即包含新加的模型，无需重启应用。 */
export const MODELS_CATALOG_CHANGED_EVENT = 'liuruyan:models-catalog-changed'

/** 截断会话消息：保留前 keep 条用户视角 message。
 *
 * 用于"编辑 user 消息后重发"和"回滚到某条消息"两个场景共用。后端会自动连带
 * 保留某 assistant turn 后跟随的 tool_result / image attachment 行（避免下次
 * LLM 调用 tool_use 没有 tool_result 配对而 400）。
 *
 * @param sessionId 会话 id
 * @param keep 保留的用户视角消息条数。0 = 清空。等价于"前端 messages 数组里
 *             被点击消息的 index"——messages[0..keep) 留下，messages[keep..) 删。
 * @returns ``{removed_lines, kept_visible, message_count}``
 */
export async function truncateSession(
  sessionId: string,
  keep: number,
): Promise<{ removed_lines: number; kept_visible: number; message_count: number }> {
  const res = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/truncate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keep }),
  })
  if (!res.ok) {
    let detail = `Failed to truncate session (HTTP ${res.status})`
    try {
      const data = await res.json()
      if (data?.detail) detail = String(data.detail)
    } catch { /* swallow */ }
    throw new Error(detail)
  }
  return res.json()
}

/** Phase 3 (v4 2026-05-22) L2 聊天编辑：编辑 messages[visibleIndex] 的文本内容。
 *
 *  payload 两种合法形态（对齐后端 `PATCH /sessions/{sid}/messages/{idx}`）：
 *  - user 消息：`{ content: "新文本" }`
 *  - assistant 消息：`{ block_patches: [{ block_index, type: 'text'|'thinking', content }] }`
 *    block_index 是该 assistant 消息 content 块数组中的下标；只能改 text/thinking，
 *    tool_use 块禁改（patch 指向 tool_use 块或 type 不匹配后端会 400）。
 *
 *  返回值的 `edited` 字段：
 *  - `true`：磁盘已更新，前端应重拉该 session 或基于返回数据更新 store
 *  - `false`（status='noop'）：内容相同 / 越界等情况，不算错
 */
export type EditMessagePayload =
  | { content: string }
  | { block_patches: Array<{ block_index: number; type: 'text' | 'thinking'; content: string }> }

export type EditMessageResponse = {
  status: 'success' | 'noop'
  session_id: string
  visible_index: number
  edited?: boolean
  role?: string
  fields_changed?: number
  reason?: string
}

export async function editSessionMessage(
  sessionId: string,
  visibleIndex: number,
  payload: EditMessagePayload,
): Promise<EditMessageResponse> {
  const url = `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/messages/${visibleIndex}`
  const res = await fetch(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    let detail = `Failed to edit message (HTTP ${res.status})`
    try {
      const data = await res.json()
      if (data?.detail) detail = String(data.detail)
    } catch { /* swallow */ }
    throw new Error(detail)
  }
  return res.json()
}

/** v5 (2026-05-26) 删除单条消息：对齐 Cherry "选哪条删哪条"。
 *
 *  - 删 user 普通消息：仅删该行
 *  - 删 assistant turn：连带删紧随其后的 tool_result / image artifact 行
 *    （永远不留孤儿 tool_use/tool_result 配对，下次 LLM 调用零 400 风险）
 *  - emotion/mask/worker 等非 message 行保留
 *
 *  返回值的 `status`：
 *  - `'success'`：磁盘已更新，前端应重拉该 session
 *  - `'noop'`：visible_index 越界等情况，不算错
 */
export type DeleteMessageResponse = {
  status: 'success' | 'noop'
  session_id: string
  visible_index: number
  deleted?: boolean
  role?: string
  removed_lines?: number
  message_count?: number
  reason?: string
}

export async function deleteSessionMessage(
  sessionId: string,
  visibleIndex: number,
): Promise<DeleteMessageResponse> {
  const url = `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/messages/${visibleIndex}`
  const res = await fetch(url, { method: 'DELETE' })
  if (!res.ok) {
    let detail = `Failed to delete message (HTTP ${res.status})`
    try {
      const data = await res.json()
      if (data?.detail) detail = String(data.detail)
    } catch { /* swallow */ }
    throw new Error(detail)
  }
  return res.json()
}

export async function updateSessionTitle(sessionId: string, title: string): Promise<Session> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/title`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw new Error('Failed to update title')
  const data = await res.json()
  return data.session
}

/** 获取同角色其他会话的感知信息 */
export async function getSessionAwareness(sessionId: string): Promise<{ session_id: string; peers: PeerAwareness[] }> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/awareness`)
  if (!res.ok) throw new Error('Failed to get awareness')
  return res.json()
}

/** 重新激活空闲会话 */
export async function reactivateSession(sessionId: string): Promise<Session> {
  const res = await fetch(`${API_BASE}/sessions/${sessionId}/reactivate`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed to reactivate session')
  const data = await res.json()
  return data.session
}

// ====== 记忆系统 ======
export interface MemoryRequest {
  content: string
  memory_type?: string
}

export async function addMemory(req: MemoryRequest, agentId?: string): Promise<void> {
  const res = await fetch(`${API_BASE}/memory`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: req.content, memory_type: req.memory_type || 'diary', agent_id: agentId }),
  })
  if (!res.ok) throw new Error('Failed to add memory')
}

export async function getTodayMemory(agentId?: string): Promise<{ date: string; content: string }> {
  const url = agentId ? `${API_BASE}/memory/today?agent_id=${agentId}` : `${API_BASE}/memory/today`
  const res = await fetch(url)
  if (!res.ok) throw new Error('Failed')
  return res.json()
}

export async function getLongTermMemory(agentId?: string): Promise<{ content: string }> {
  const url = agentId ? `${API_BASE}/memory/long-term?agent_id=${agentId}` : `${API_BASE}/memory/long-term`
  const res = await fetch(url)
  if (!res.ok) throw new Error('Failed')
  return res.json()
}

export async function listDiaries(agentId?: string): Promise<{ diaries: DiaryEntry[] }> {
  const url = agentId ? `${API_BASE}/memory/diaries?agent_id=${agentId}` : `${API_BASE}/memory/diaries`
  const res = await fetch(url)
  if (!res.ok) throw new Error('Failed')
  return res.json()
}

export async function getDiary(date: string, agentId?: string): Promise<{ date: string; content: string }> {
  const url = agentId ? `${API_BASE}/memory/diary/${date}?agent_id=${agentId}` : `${API_BASE}/memory/diary/${date}`
  const res = await fetch(url)
  if (!res.ok) throw new Error('Diary not found')
  return res.json()
}

export async function updateDiary(date: string, content: string, agentId?: string): Promise<void> {
  const url = agentId ? `${API_BASE}/memory/diary/${date}?agent_id=${agentId}` : `${API_BASE}/memory/diary/${date}`
  const res = await fetch(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
  if (!res.ok) throw new Error('Failed to update diary')
}

export async function updateLongTermMemory(content: string, agentId?: string): Promise<void> {
  const url = agentId ? `${API_BASE}/memory/long-term?agent_id=${agentId}` : `${API_BASE}/memory/long-term`
  const res = await fetch(url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
  if (!res.ok) throw new Error('Failed to update long-term memory')
}

export async function getReflections(agentId?: string): Promise<{ content: string }> {
  const url = agentId ? `${API_BASE}/memory/reflections?agent_id=${agentId}` : `${API_BASE}/memory/reflections`
  const res = await fetch(url)
  if (!res.ok) throw new Error('Failed to get reflections')
  return res.json()
}

export async function getTempStats(agentId?: string): Promise<{ total: number; burning: number; warm: number; cold: number; unlabeled: number }> {
  const url = agentId ? `${API_BASE}/memory/temp-stats?agent_id=${agentId}` : `${API_BASE}/memory/temp-stats`
  const res = await fetch(url)
  if (!res.ok) throw new Error('Failed to get temp stats')
  return res.json()
}

export async function getMemoryReport(agentId?: string): Promise<any> {
  const url = agentId ? `${API_BASE}/memory/report?agent_id=${agentId}` : `${API_BASE}/memory/report`
  const res = await fetch(url)
  if (!res.ok) throw new Error('Failed to get memory report')
  return res.json()
}

/** 保存 REFLECTIONS.md 内容 */
export async function saveReflections(content: string, agentId?: string): Promise<{ ok: boolean; message: string }> {
  const res = await fetch(`${API_BASE}/memory/reflections`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, agent_id: agentId || 'default' }),
  })
  if (!res.ok) throw new Error('Failed to save reflections')
  return res.json()
}

/** 删除 REFLECTIONS 中的某条洞察（按索引） */
export async function deleteReflectionItem(index: number, agentId?: string): Promise<{ ok: boolean; message: string }> {
  const res = await fetch(`${API_BASE}/memory/reflections/${agentId || 'default'}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index }),
  })
  if (!res.ok) throw new Error('Failed to delete reflection item')
  return res.json()
}


// ====== 模型管理 ======
export interface Model {
  id: string
  name: string
  icon?: string
  description?: string
  is_custom?: boolean
  provider?: string
  /** 自定义服务商用户填的名称（优先展示，fallback 到 getBrandName(provider)）。 */
  provider_name?: string
  /** 协议类型（openai_chat / anthropic_messages），custom provider 靠它选图标。 */
  transport?: string
  /**
   * 后端权威能力标签（来自 backend/providers/capabilities.py::infer_capabilities）。
   * 元素如 'vision' / 'reasoning' / 'function_calling' / 'embedding' / 'audio' / 'image_gen' /
   * 'rerank' / 'websearch' / 'free'。
   *
   * 来源：/models endpoint 的 custom_models_display[i].tags（chat_model_catalog_view
   * 优先读 ModelEntry.tags，缺失时用 infer_capabilities 兜底填充）。
   *
   * 前端任何能力判断（vision 拦截、Picker 胶囊）都应优先用这个，而不是再独立推断。
   * `inferModelCapabilities(modelId)` 仅在拿不到 Model 对象时（边界场景）作为 fallback。
   */
  tags?: string[]
  /** 上下文长度（token 数）。后端 infer_context_length 兜底，可空。 */
  context_length?: number | null
  /** configured=用户/服务商显式配置；inferred=按官方模型目录识别。 */
  context_length_source?: 'configured' | 'inferred'
}

export interface ModelsResponse {
  /** 主聊天页用的模型目录：按 provider 分组（每条带 provider id）。 */
  custom_models: Array<{
    id: string
    name: string
    base_url: string
    api_key: string
    provider: string
  }>
  /** 用于 ChatModelPicker 渲染的精简版（is_custom=true 标记）。 */
  custom_models_display?: Model[]
  current_model: string
  current_provider?: string
  prompt_caching_enabled?: boolean
}

export async function listModels(): Promise<ModelsResponse> {
  const res = await fetch(`${API_BASE}/models`)
  if (!res.ok) throw new Error('Failed to list models')
  return res.json()
}

export async function setCurrentModel(modelId: string, provider?: string): Promise<{ status: string; current_model: string; provider: string }> {
  const body: any = { model_id: modelId }
  if (provider) body.provider = provider
  const res = await fetch(`${API_BASE}/models/current`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error('Failed to switch model')
  return res.json()
}

export async function setPromptCachingEnabled(enabled: boolean): Promise<{ status: string; prompt_caching_enabled: boolean }> {
  const res = await fetch(`${API_BASE}/models/prompt-caching`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })
  if (!res.ok) throw new Error('Failed to update prompt caching setting')
  return res.json()
}

export async function getAnthropicPresets(): Promise<{ models: Array<{ id: string; name: string; icon: string }> }> {
  const res = await fetch(`${API_BASE}/models/anthropic-presets`)
  if (!res.ok) throw new Error('Failed to fetch Anthropic presets')
  return res.json()
}

/** 单条中继探测信号。level 决定 UI 颜色（critical=红 / warning=橙 / info=蓝）。 */
export interface RelaySignal {
  level: 'info' | 'warning' | 'critical'
  name: string
  detail: string
}

/** 中继转接探测综合结果（轻检测 A+B + 深度检测 C 共用）。 */
export interface RelayDiagnostics {
  /** native=未发现可疑 / likely_relay=有 warning 或 critical / unknown=仅 info 提示 */
  verdict: 'native' | 'likely_relay' | 'unknown'
  confidence: 'high' | 'medium' | 'low'
  signals: RelaySignal[]
}

export async function testModelConnection(
  provider: string,
  apiKey: string,
  model: string,
  baseUrl?: string,
  extraHeaders?: Record<string, string>,
  transport?: string,
): Promise<{
  success: boolean
  message: string
  identified_model?: string
  /** 中继转接探测结果（仅 success=true 时有值），verdict≠native 时前端浮警告框。 */
  relay_diagnostics?: RelayDiagnostics
}> {
  const body: Record<string, unknown> = {
    provider,
    api_key: apiKey,
    model,
    base_url: baseUrl,
  }
  if (extraHeaders && Object.keys(extraHeaders).length > 0) {
    // anthropic_messages 协议 + Cherry 风格 UA/Referer/X-Title：前端把
    // provider 字段写成 'anthropic' 字面量时后端按 id 反查不到 custom-xxx，
    // 必须显式带上 headers，否则 getrouter 等反代会返 "request was blocked"。
    body.extra_headers = extraHeaders
  }
  // transport 透传：让后端 relay_detection 知道用哪一套响应形态规则。
  // 不传时后端按 provider 字段二分推断（anthropic / 非 anthropic）。
  if (transport) body.transport = transport
  const res = await fetch(`${API_BASE}/models/test-connection`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error('Failed to test connection')
  return res.json()
}

/**
 * 深度连接检测：发两次相同长 prompt + cache_control 测 prompt cache 命中。
 * 仅 transport='anthropic_messages' 适用——OpenAI 兼容协议没有 prompt cache 标准。
 *
 * 真 Anthropic 后端：cache_read_input_tokens > 0 = 决定性证据
 * OpenAI 反代伪装 anthropic：cache 字段缺失或永远为 0 = 决定性证据相反
 *
 * 成本：两次 max_tokens=8 请求，~5 秒延迟。
 */
export async function testModelConnectionDeep(
  provider: string,
  apiKey: string,
  model: string,
  baseUrl?: string,
  extraHeaders?: Record<string, string>,
  transport?: string,
): Promise<{
  success: boolean
  /** false 时表示协议不适用（applicable=false）或检测异常（applicable=true 但 success=false） */
  applicable?: boolean
  verdict?: 'native' | 'likely_relay' | 'unknown'
  confidence?: 'high' | 'medium' | 'low'
  cache_creation_input_tokens?: number | null
  cache_read_input_tokens?: number | null
  signals?: RelaySignal[]
  message: string
}> {
  const body: Record<string, unknown> = {
    provider,
    api_key: apiKey,
    model,
    base_url: baseUrl,
  }
  if (extraHeaders && Object.keys(extraHeaders).length > 0) body.extra_headers = extraHeaders
  if (transport) body.transport = transport
  const res = await fetch(`${API_BASE}/models/test-connection-deep`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error('Failed to run deep connection test')
  return res.json()
}

export async function fetchRemoteModels(provider: string, baseUrl: string, apiKey: string): Promise<{ provider: string; models: Array<{ id: string; name: string; owned_by?: string }>; source: string }> {
  const res = await fetch(`${API_BASE}/models/fetch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, base_url: baseUrl, api_key: apiKey }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || '获取模型列表失败')
  }
  return res.json()
}


// ====== 技能管理 ======
export async function listSkills(): Promise<Skill[]> {
  const res = await fetch(`${API_BASE}/skills`)
  if (!res.ok) throw new Error('Failed')
  const data = await res.json()
  return data.skills || []
}

export async function toggleSkill(skillName: string, enabled: boolean): Promise<any> {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(skillName)}/toggle`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })
  if (!res.ok) throw new Error('Failed to toggle skill')
  return res.json()
}

export async function reloadSkills(): Promise<any> {
  const res = await fetch(`${API_BASE}/skills/reload`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed')
  return res.json()
}


// ====== 开发者模式（与 ToolsInventory 共用同一个全局开关） ======
// toggleDevMode 在 ./toolsInventory.ts 已有，复用即可，不要重复定义。

export interface DevModeStatus {
  enabled: boolean
  via_env: boolean
  via_runtime_toggle: boolean
  can_toggle_off: boolean
}

export async function getDevStatus(): Promise<DevModeStatus> {
  const res = await fetch(`${API_BASE}/dev/status`)
  if (!res.ok) throw new Error(`GET /dev/status failed: ${res.status}`)
  return res.json()
}

/**
 * 拉取技能 SKILL.md body（用于 dev mode 编辑器初始化，不含 frontmatter）。
 */
export async function getSkillContent(skillName: string): Promise<{
  name: string
  content: string
  category: string
  path: string
}> {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(skillName)}/content`)
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`GET /skills/${skillName}/content failed: ${res.status} ${text}`)
  }
  return res.json()
}

/**
 * 编辑技能 SKILL.md body（仅 dev mode 开启时后端才放行）。
 * 后端会保留原 frontmatter，只覆盖 body 部分；写入后自动 reload skill loader。
 */
export async function updateSkillContent(skillName: string, content: string): Promise<any> {
  const res = await fetch(`${API_BASE}/skills/${encodeURIComponent(skillName)}/content`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`PUT /skills/${skillName}/content failed: ${res.status} ${text}`)
  }
  return res.json()
}


// ====== 角色技能装备 ======

export interface AgentSkillInfo {
  name: string
  display_name?: string
  description: string
  category: string
  tags: string[]
  equipped: boolean
  level: number
  level_label: string
  xp: number
  xp_to_next: number
  next_level_xp: number
  progress_percent: number
  total_uses: number
  successful_uses: number
  failed_uses: number
  promotable: boolean
  last_used_at?: string | null
  last_summary?: string
}

export type SkillPromotionStatus = 'pending' | 'approved' | 'rejected' | 'applied'

export interface SkillPromotionCandidate {
  id: string
  signature: string
  status: SkillPromotionStatus
  agent_id: string
  agent_name: string
  skill_name: string
  skill_path: string
  created_at: string
  updated_at: string
  source_level: number
  source_level_label: string
  source_xp: number
  total_uses: number
  successful_uses: number
  failed_uses: number
  heuristics: string[]
  pitfalls: string[]
  last_summary: string
  review_notes?: string
  reviewed_at?: string | null
  approved_at?: string | null
  applied_at?: string | null
  applied_excerpt?: string
}

export async function listSkillPromotions(params: {
  agentId?: string
  status?: SkillPromotionStatus
  skillName?: string
} = {}): Promise<{ candidates: SkillPromotionCandidate[]; count: number }> {
  const query = new URLSearchParams()
  if (params.agentId) query.set('agent_id', params.agentId)
  if (params.status) query.set('status', params.status)
  if (params.skillName) query.set('skill_name', params.skillName)
  const suffix = query.toString() ? `?${query.toString()}` : ''
  const res = await fetch(`${API_BASE}/skills/promotions${suffix}`)
  if (!res.ok) throw new Error('Failed to load skill promotions')
  return res.json()
}

export async function submitSkillPromotion(
  agentId: string,
  skillName: string,
): Promise<{ status: string; created: boolean; candidate: SkillPromotionCandidate }> {
  const res = await fetch(
    `${API_BASE}/agents/${encodeURIComponent(agentId)}/skills/${encodeURIComponent(skillName)}/promote`,
    { method: 'POST' },
  )
  if (!res.ok) throw new Error('Failed to submit skill promotion')
  return res.json()
}

async function reviewSkillPromotion(
  candidateId: string,
  action: 'approve' | 'reject' | 'apply',
  reviewNotes: string = '',
): Promise<{ status: string; candidate: SkillPromotionCandidate }> {
  const res = await fetch(`${API_BASE}/skills/promotions/${encodeURIComponent(candidateId)}/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ review_notes: reviewNotes }),
  })
  if (!res.ok) throw new Error(`Failed to ${action} skill promotion`)
  return res.json()
}

export async function approveSkillPromotion(candidateId: string, reviewNotes: string = '') {
  return reviewSkillPromotion(candidateId, 'approve', reviewNotes)
}

export async function rejectSkillPromotion(candidateId: string, reviewNotes: string = '') {
  return reviewSkillPromotion(candidateId, 'reject', reviewNotes)
}

export async function applySkillPromotion(candidateId: string, reviewNotes: string = '') {
  return reviewSkillPromotion(candidateId, 'apply', reviewNotes)
}

export interface AgentSkillsResponse {
  agent_id: string
  equipped: string[]
  equipped_details: AgentSkillInfo[]
  recommended: AgentSkillInfo[]
  all_skills: AgentSkillInfo[]
  by_category: Record<string, AgentSkillInfo[]>
  total_available: number
}

export async function getAgentSkills(agentId: string): Promise<AgentSkillsResponse> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/skills`)
  if (!res.ok) throw new Error('Failed to get agent skills')
  return res.json()
}

export async function equipSkill(agentId: string, skillName: string): Promise<any> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/skills/equip`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill_name: skillName }),
  })
  if (!res.ok) throw new Error('Failed to equip skill')
  return res.json()
}

export async function unequipSkill(agentId: string, skillName: string): Promise<any> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/skills/unequip`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill_name: skillName }),
  })
  if (!res.ok) throw new Error('Failed to unequip skill')
  return res.json()
}

export async function setAgentSkills(agentId: string, skillNames: string[]): Promise<any> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/skills`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill_names: skillNames }),
  })
  if (!res.ok) throw new Error('Failed to set agent skills')
  return res.json()
}

// ====== Phase 3: 跨角色协作 API ======

/** 征求意见返回 */
export interface ConsultResponse {
  status: string
  session_id: string
  question: string
  agent_id: string
  agent_name: string
  response: string
  message: Message
}

/** 跨角色消息 */
export interface CrossMessage {
  id: string
  from_agent: string
  to_agent: string
  msg_type: string
  content: string
  emotion_tone: string
  created_at: string
  read: boolean
}

/** 征求另一角色的一次性意见，并回流主聊天 */
export async function consultAgent(fromAgentId: string, sessionId: string, toAgentId: string, question?: string): Promise<ConsultResponse> {
  const res = await fetch(`${API_BASE}/social/consult`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      from_agent: fromAgentId,
      from_session: sessionId,
      to_agent: toAgentId,
      question: question || '',
      reason: question || '',
    }),
  })
  if (!res.ok) throw new Error('征求意见失败')
  return res.json()
}

export async function* consultAgentStream(
  fromAgentId: string,
  sessionId: string,
  toAgentId: string,
  question?: string,
): AsyncGenerator<ConsultStreamEvent> {
  const res = await fetch(`${API_BASE}/social/consult`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      from_agent: fromAgentId,
      from_session: sessionId,
      to_agent: toAgentId,
      question: question || '',
      reason: question || '',
      stream: true,
    }),
  })
  if (!res.ok) throw new Error(`征求意见失败 (${res.status})`)
  if (!res.body) return

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    const lines = buffer.split('\n\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      const data = line.slice(6).trim()
      if (data === '[DONE]') return
      try {
        yield JSON.parse(data) as ConsultStreamEvent
      } catch {
        // skip malformed event
      }
    }
  }
}

/** 走廊模式：角色用自己的话转述给另一角色（走收件箱） */
export async function sendCorridorMessage(params: {
  from_agent: string; to_agent: string; content: string;
  msg_type?: string; emotion_tone?: string;
}): Promise<any> {
  const res = await fetch(`${API_BASE}/social/corridor`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      from_agent: params.from_agent,
      to_agent: params.to_agent,
      content: params.content,
      msg_type: params.msg_type || 'inform',
      emotion_tone: params.emotion_tone || '平淡',
    }),
  })
  if (!res.ok) throw new Error('走廊消息发送失败')
  return res.json()
}

/** 获取角色的跨角色消息（收件箱） */
export async function getCrossMessages(agentId: string, unreadOnly: boolean = false): Promise<{ messages: CrossMessage[] }> {
  const res = await fetch(`${API_BASE}/social/messages/${encodeURIComponent(agentId)}?unread_only=${unreadOnly}`)
  if (!res.ok) throw new Error('获取消息失败')
  return res.json()
}

/** 标记单条消息已读 */
export async function markCrossMessageRead(messageId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/social/messages/${encodeURIComponent(messageId)}/read`, { method: 'PUT' })
  if (!res.ok) throw new Error('标记已读失败')
}

/** 标记全部消息已读 */
export async function markAllCrossMessagesRead(agentId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/social/messages/${encodeURIComponent(agentId)}/read-all`, { method: 'PUT' })
  if (!res.ok) throw new Error('标记全部已读失败')
}

/** 获取角色关系摘要 */
export async function getRelationSummary(agentId: string): Promise<any> {
  const res = await fetch(`${API_BASE}/social/graph/summary/${encodeURIComponent(agentId)}`)
  if (!res.ok) throw new Error('获取关系失败')
  return res.json()
}

/** 获取用户与角色的关系 */
export async function getUserRelation(agentId: string): Promise<any> {
  const res = await fetch(`${API_BASE}/social/graph/user-relations/${encodeURIComponent(agentId)}`)
  if (!res.ok) throw new Error('获取用户关系失败')
  return res.json()
}

// ====== Phase 3: 场景 API ======

/** 场景信息 */
export interface SceneInfo {
  id: string
  scene_type: string
  scene_type_label?: string
  icon?: string
  title?: string
  participants: string[]
  initiator?: string
  trigger?: string
  narrative_arc?: string
  phase: string
  phase_label?: string
  current_turn: number
  current_speaker?: string | null
  status?: string
  is_active?: boolean
  ttl?: number
  context?: string
  shared_context?: Record<string, unknown>
  linked_task_id?: string | null
  created_at: string
  updated_at: string
  resolved_at?: string | null
  metadata?: Record<string, unknown>
}

/** 获取活跃场景 */
export async function getActiveScenes(agentId?: string): Promise<{ scenes: SceneInfo[] }> {
  const url = agentId
    ? `${API_BASE}/social/scenes/active?agent_id=${encodeURIComponent(agentId)}`
    : `${API_BASE}/social/scenes/active`
  const res = await fetch(url)
  if (!res.ok) throw new Error('获取场景失败')
  return res.json()
}

/** 创建场景 */
export async function createScene(params: {
  scene_type: string; participants: string[]; context?: string;
  trigger?: string; force?: boolean; ttl?: number;
}): Promise<{ scene: SceneInfo; session_id?: string }> {
  const res = await fetch(`${API_BASE}/social/scenes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  if (!res.ok) throw new Error('创建场景失败')
  return res.json()
}

/** 解散场景 */
export async function resolveScene(sceneId: string, endCondition: string = 'resolved'): Promise<{ scene: SceneInfo }> {
  const res = await fetch(`${API_BASE}/social/scenes/${encodeURIComponent(sceneId)}/resolve`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ end_condition: endCondition }),
  })
  if (!res.ok) throw new Error('解散场景失败')
  return res.json()
}

/** 取消场景 */
export async function cancelScene(sceneId: string): Promise<{ scene: SceneInfo }> {
  const res = await fetch(`${API_BASE}/social/scenes/${encodeURIComponent(sceneId)}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error('取消场景失败')
  return res.json()
}

/** 召唤角色加入场景 */
export async function summonToScene(sceneId: string, agentId: string): Promise<{ scene: SceneInfo }> {
  const res = await fetch(`${API_BASE}/social/scenes/${encodeURIComponent(sceneId)}/summon`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent_id: agentId }),
  })
  if (!res.ok) throw new Error('召唤角色失败')
  return res.json()
}

// ====== Phase 3: 协作对话 API ======

/** 协作对话 SSE 事件 */
export interface CollabChatEvent {
  type: string
  agent_id?: string
  content?: string
  emotion_tone?: string
  timestamp?: string
  session_id?: string
  agent_name?: string
  error?: string
  kind?: string
  name?: string
  input?: any
  result?: any
  id?: string
  tool_use_id?: string
  turn?: number
  total_turns?: number
  phase?: string
  is_active?: boolean
}

/**
 * 协作对话（SSE 流式）
 *
 * @param speakers 指定发言序列（多选+排序，允许重复）。
 *   - undefined / 空数组：按 scene.turn_order 依次发言（默认行为）
 *   - 非空数组：只让列表里的人按这个顺序发言（覆盖 turn_order）
 *   非参与者会被后端静默过滤；全被过滤光时回退默认顺序。
 *   与 fromAgent 互斥；同时传时 speakers 优先（后端逻辑）。
 */
export async function* collabChat(
  sceneId: string,
  message: string,
  fromAgent?: string,
  model?: string,
  sessionId?: string,
  signal?: AbortSignal,
  speakers?: string[],
): AsyncGenerator<CollabChatEvent> {
  const body: Record<string, unknown> = { scene_id: sceneId, message, from_agent: fromAgent, model }
  if (sessionId) body.session_id = sessionId
  if (speakers && speakers.length > 0) body.speakers = speakers
  const res = await fetch(`${API_BASE}/social/collab-chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok) throw new Error(`协作对话失败 (${res.status})`)
  if (!res.body) return

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const data = line.slice(6).trim()
        if (data === '[DONE]') return
        try {
          yield JSON.parse(data) as CollabChatEvent
        } catch { /* skip malformed */ }
      }
    }
  }
}


// ====== Phase 3.4: 游戏化打磨 API ======

/** 解散叙事场景 */
export async function dismissCollabScene(sceneId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/social/collab-chat/${encodeURIComponent(sceneId)}`, { method: 'DELETE' })
  if (!res.ok) console.warn('[CollabChat] 解散场景失败:', res.status)
}

/** 关注值状态 */
export interface AttentionState {
  agent_id: string
  attention_score: number
  peak_attention: number
  last_interaction: string | null
}

/** 吃醋事件 */
export interface JealousyEvent {
  id: string
  jealous_agent: string
  target_agent: string
  trigger: string
  jealousy_style: string
  intensity: number
  timestamp: string
  behavior_modifier: {
    tone: string
    action: string
  }
}

/** 吃醋行为修饰 */
export interface JealousyModifier {
  active: boolean
  modifier?: {
    style: string
    intensity: number
    modifier: { tone: string; action: string }
    prompt_hint: string
  }
}

/** 成就 */
export interface Achievement {
  id: string
  title: string
  desc: string
  rarity: string
  rarity_label: string
  rarity_color: string
  rarity_glow: boolean
  category: string
  icon: string
  agents: string[]
  hidden: boolean
  unlocked: boolean
  unlocked_at: string | null
}

/** 游戏通知 */
export interface GameNotification {
  type: string
  timestamp: string
  urgency: string
  [key: string]: any
}

/** 获取角色关注值 */
export async function getAttention(agentId: string): Promise<AttentionState> {
  const res = await fetch(`${API_BASE}/game/attention/${encodeURIComponent(agentId)}`)
  if (!res.ok) throw new Error('获取关注值失败')
  return res.json()
}

/** 获取所有角色关注值 */
export async function getAllAttention(): Promise<{ attention: AttentionState[] }> {
  const res = await fetch(`${API_BASE}/game/attention`)
  if (!res.ok) throw new Error('获取关注值失败')
  return res.json()
}

/** 更新角色关注值 */
export async function updateAttention(
  agentId: string, event: string, intensity: number = 1.0, context: string = ''
): Promise<any> {
  const res = await fetch(`${API_BASE}/game/attention/${encodeURIComponent(agentId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event, intensity, context }),
  })
  if (!res.ok) throw new Error('更新关注值失败')
  return res.json()
}

/** 触发吃醋检测 */
export async function triggerJealousy(
  favoredAgent: string, context: string = ''
): Promise<{ jealousy_events: JealousyEvent[]; count: number }> {
  const res = await fetch(`${API_BASE}/game/jealousy/trigger`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ favored_agent: favoredAgent, context }),
  })
  if (!res.ok) throw new Error('触发吃醋检测失败')
  return res.json()
}

/** 获取当前吃醋状态 */
export async function getActiveJealousies(): Promise<{ jealousies: JealousyEvent[] }> {
  const res = await fetch(`${API_BASE}/game/jealousy/active`)
  if (!res.ok) throw new Error('获取吃醋状态失败')
  return res.json()
}

/** 获取角色吃醋行为修饰 */
export async function getJealousyModifier(agentId: string): Promise<JealousyModifier> {
  const res = await fetch(`${API_BASE}/game/jealousy/${encodeURIComponent(agentId)}/modifier`)
  if (!res.ok) throw new Error('获取吃醋修饰失败')
  return res.json()
}

/** 平复角色吃醋 */
export async function calmJealousy(
  agentId: string, amount: number = 0.3
): Promise<any> {
  const res = await fetch(`${API_BASE}/game/jealousy/${encodeURIComponent(agentId)}/calm`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ amount }),
  })
  if (!res.ok) throw new Error('平复吃醋失败')
  return res.json()
}

/** 获取成就列表 */
export async function getAchievements(): Promise<{ achievements: Achievement[] }> {
  const res = await fetch(`${API_BASE}/game/achievements`)
  if (!res.ok) throw new Error('获取成就失败')
  return res.json()
}

/** 获取成就统计 */
export async function getAchievementStats(): Promise<{ unlocked: number; total: number }> {
  const res = await fetch(`${API_BASE}/game/achievements/stats`)
  if (!res.ok) throw new Error('获取成就统计失败')
  return res.json()
}

/** 获取待推送游戏通知 */
export async function getGameNotifications(): Promise<{ notifications: GameNotification[]; count: number }> {
  const res = await fetch(`${API_BASE}/game/notifications`)
  if (!res.ok) throw new Error('获取通知失败')
  return res.json()
}

/** 获取通知历史 */
export async function getNotificationHistory(limit: number = 50): Promise<{ history: GameNotification[] }> {
  const res = await fetch(`${API_BASE}/game/notifications/history?limit=${limit}`)
  if (!res.ok) throw new Error('获取通知历史失败')
  return res.json()
}

/** 获取游戏化系统统计 */
export async function getGameStats(): Promise<any> {
  const res = await fetch(`${API_BASE}/game/stats`)
  if (!res.ok) throw new Error('获取游戏统计失败')
  return res.json()
}

// ====== Pinned context（参考 Windsurf SetPinnedContext / SetPinnedGuideline）======

/** 单条 pinned scope item，types 见后端 backend/scope_item.py。 */
export type ScopeItemType = 'file' | 'directory' | 'code_context' | 'rule' | 'code_map'

export interface ScopeItem {
  type: ScopeItemType
  path: string
  label?: string
  line_range?: [number, number]
  /** directory 类型：一级 entries 数（不递归）。前端用于显示"_research · 41 项" + 大目录警告。
   *  对齐 Codeium PathScopeItem.num_files；柳如烟无 indexer 故只统计一级。 */
  entries_count?: number
}

export interface SessionPinnedState {
  session_id: string
  guideline: string | null
  scope: ScopeItem[]
}

export async function getSessionPinned(sessionId: string): Promise<SessionPinnedState> {
  const res = await noStoreFetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/pinned`)
  if (!res.ok) throw new Error('获取 pinned 状态失败')
  return res.json()
}

export async function updateSessionPinnedGuideline(
  sessionId: string,
  guideline: string | null,
): Promise<{ session_id: string; guideline: string | null }> {
  const res = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/pinned/guideline`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ guideline }),
  })
  if (!res.ok) throw new Error('保存 pinned guideline 失败')
  return res.json()
}

export async function updateSessionPinnedScope(
  sessionId: string,
  scope: ScopeItem[],
): Promise<{ session_id: string; scope: ScopeItem[] }> {
  const res = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/pinned/scope`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scope }),
  })
  if (!res.ok) throw new Error('保存 pinned scope 失败')
  return res.json()
}

/** @ mention 候选检索（参考 Windsurf SearchScopeItems）。 */
export async function matchScopeItems(
  query: string,
  options: { types?: ScopeItemType[]; limit?: number; signal?: AbortSignal } = {},
): Promise<ScopeItem[]> {
  const body: Record<string, unknown> = { query }
  if (options.types) body.types = options.types
  if (typeof options.limit === 'number') body.limit = options.limit
  const res = await fetch(`${API_BASE}/context/scope/match`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: options.signal,
  })
  if (!res.ok) throw new Error('scope 检索失败')
  const data = (await res.json()) as { items: ScopeItem[] }
  return data.items || []
}

// ====== Goal 模式前端 API ======

export interface GoalIteration {
  iteration_index: number
  next_action: string
  evidence: string[]
  summary: string
  timestamp: number
}

export interface GoalState {
  goal_id: string
  session_id: string
  objective: string
  status: 'none' | 'active' | 'paused' | 'completed' | 'blocked'
  max_turns: number
  used_turns: number
  consecutive_stagnations: number
  created_at: number
  updated_at: number
  iterations: GoalIteration[]
  blocked_reason?: string | null
  final_evidence?: string[]
}

export async function getSessionGoal(sessionId: string): Promise<GoalState | null> {
  const res = await noStoreFetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/goal`)
  if (!res.ok) return null
  const data = await res.json()
  return data.goal || null
}

export async function updateSessionGoal(
  sessionId: string,
  payload: { action: 'set' | 'pause' | 'resume' | 'clear'; objective?: string; max_turns?: number }
): Promise<GoalState | null> {
  const res = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/goal`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new Error('操作目标失败')
  const data = await res.json()
  return data.goal || null
}

