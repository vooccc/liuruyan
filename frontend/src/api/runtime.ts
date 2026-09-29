/**
 * 运行预算（AggregateRuntimeBudget）配置 API
 *
 * 后端：backend/runtime_budget_config.py 落盘到 data_dir() / runtime_budget.json
 * 优先级：runtime_budget.json → env → 默认值
 *
 * 下次 chat 自动生效（_chat_with_tools 入口每次新建 budget 实例）。
 */

import { API_BASE } from './base'

export interface RuntimeBudgetFields {
  /** AggregateRuntimeBudget 总调用次数上限 */
  max_tool_calls: number
  /** ToolLoopGuard 工具循环轮数上限——LLM 一轮可能多 calls */
  max_tool_rounds: number
  max_tool_output_chars: number
  max_wall_clock_seconds: number
  /** 单轮 LLM 输出 token 上限（对应 SDK max_tokens 参数）。默认 32768，写大文件 / 长代码场景可按模型上限再调。 */
  max_output_tokens: number
  /** 流式调用“多久收不到下一个 chunk 算挂起”（毫秒）。默认 600000=600s。
   *  thinking 模型 + 大 max_tokens 下 provider 可能更久不发 chunk，可再调高避免误报。 */
  stream_idle_timeout_ms: number
  warn_threshold_ratio: number
}

export interface RuntimeBudgetResponse {
  /** 用户在前端调过的字段（缺则未调过 → env / 默认兜底） */
  config: Partial<RuntimeBudgetFields>
  /** 经过 file → env → default 三层 fallback 后实际生效的值 */
  effective: RuntimeBudgetFields
}

export async function getRuntimeBudget(): Promise<RuntimeBudgetResponse> {
  const res = await fetch(`${API_BASE}/runtime/budget`)
  if (!res.ok) throw new Error('读取运行预算失败')
  return res.json()
}

export async function setRuntimeBudget(
  cfg: Partial<RuntimeBudgetFields>,
): Promise<RuntimeBudgetResponse & { status: string }> {
  const res = await fetch(`${API_BASE}/runtime/budget`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error(data?.detail || '保存运行预算失败')
  }
  return res.json()
}

/** 三档预设（默认档 = 后端出厂默认，改后端默认值时同步这里） */
export const RUNTIME_BUDGET_PRESETS: Record<
  'conservative' | 'balanced' | 'deep',
  { label: string; description: string; values: RuntimeBudgetFields }
> = {
  conservative: {
    label: '节省',
    description: '闲聊、单点查询，省 token',
    values: {
      max_tool_calls: 48,
      max_tool_rounds: 24,
      max_tool_output_chars: 400000,
      max_wall_clock_seconds: 600,
      max_output_tokens: 16384,
      stream_idle_timeout_ms: 300000,
      warn_threshold_ratio: 0.8,
    },
  },
  balanced: {
    label: '默认',
    description: '常规场景（与后端默认值一致）',
    values: {
      max_tool_calls: 240,
      max_tool_rounds: 120,
      max_tool_output_chars: 1800000,
      max_wall_clock_seconds: 1800,
      max_output_tokens: 32768,
      stream_idle_timeout_ms: 600000,
      warn_threshold_ratio: 0.9,
    },
  },
  deep: {
    label: '深入',
    description: '分析、长任务，warning 推迟',
    values: {
      max_tool_calls: 480,
      max_tool_rounds: 240,
      max_tool_output_chars: 3600000,
      max_wall_clock_seconds: 3600,
      max_output_tokens: 32768,
      stream_idle_timeout_ms: 600000,
      warn_threshold_ratio: 0.95,
    },
  },
}
