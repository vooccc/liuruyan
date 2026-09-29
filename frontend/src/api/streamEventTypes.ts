/**
 * 流式 / 协作事件类型常量 + 运行时事件类型守卫（纯类型 + 纯函数层）
 *
 * 从 `chat.ts` 抽出的「流式事件类型枚举 + 类型守卫」，无网络 / 无 store 副作用，
 * 便于单测与复用（P6 chat.ts 拆分）。本模块自包含、零外部依赖；
 * `chat.ts` 顶部 value-import 守卫供内部使用、并 facade 再导出全部公开符号，
 * 所有现有 `from '../api/chat'` 引用不变。
 */

// tool_call_started：P1 体感优化（2026-05-16）。解析层在 tool name 一就位时
// 立即发，args 还在累积；前端按 tool_use_id 配对，先渲染 calling 卡占位，
// 收到完整 tool_call（args 就位）时覆盖更新。详见 chatStreamMessage.ts 分支。
export const STREAM_EVENT_TYPES = ['content', 'thinking', 'tool_call_started', 'tool_call', 'tool_result', 'system_notice', 'error'] as const
export type StreamEventType = typeof STREAM_EVENT_TYPES[number]

export const CONSULT_STREAM_EVENT_TYPES = [
  'consult_start',
  'content',
  'thinking',
  'tool_call',
  'tool_result',
  'system_notice',
  'consult_end',
  'error',
] as const
export type ConsultStreamEventType = typeof CONSULT_STREAM_EVENT_TYPES[number]

export const COLLAB_RUNTIME_EVENT_TYPES = ['agent_content', 'thinking', 'tool_call', 'tool_result', 'system_notice'] as const

export function isRuntimeStreamEventType(value: unknown): value is StreamEventType {
  return typeof value === 'string' && STREAM_EVENT_TYPES.includes(value as StreamEventType)
}

export function isCollabRuntimeEventType(value: unknown): value is typeof COLLAB_RUNTIME_EVENT_TYPES[number] {
  return typeof value === 'string' && COLLAB_RUNTIME_EVENT_TYPES.includes(value as typeof COLLAB_RUNTIME_EVENT_TYPES[number])
}
